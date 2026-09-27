import asyncio
import json

from homeshield.core.deps import make_pipeline
from homeshield.core.features import extract_strong_values
from homeshield.core.intake import ingest
from homeshield.core.models import Level
from homeshield.core.models import JudgeOutput, Mode
from homeshield.core.pipeline import PipelineConfig
from homeshield.core.verification import VerificationService


def _service(deps, enabled=True):
    return VerificationService(deps.repos, make_pipeline(deps, PipelineConfig(supply_features=enabled)))


def _verify(service, member, text):
    return asyncio.run(service.verify(member=member, content=text))


def test_s2_raises_after_current_only_judge(deps, group, monkeypatch):
    member = deps.repos.member.get(group[1])
    service = _service(deps)
    inputs = []
    original = service.pipeline.judge.judge

    async def spy(inp, **kwargs):
        inputs.append(inp)
        return await original(inp, **kwargs)

    monkeypatch.setattr(service.pipeline.judge, "judge", spy)
    first = _verify(service, member, "这是机密，不能告诉任何人")
    second = _verify(service, member, "请转账")
    assert second.result.verdict.level is Level.DANGEROUS
    assert {f.type for f in inputs[-1].features} == {"transfer"}
    assert "这是机密" not in inputs[-1].text
    verdict = deps.repos.verdict.get(second.result.verdict_id)
    snapshot = json.loads(verdict["context_snapshot"])
    features = json.loads(verdict["features"])
    synthetic = [f for f in features if f.get("origin", "self") == "prior"]
    assert len(synthetic) == 1 and synthetic[0]["type"] == "isolation"
    assert snapshot["synthetic_ids"] == [synthetic[0]["id"]]
    assert synthetic[0]["id"] in second.result.verdict.cited_ids
    assert "跨消息" in second.result.verdict.reason
    assert "前情有保密要求" in second.result.reply
    assert "这是机密" not in second.result.reply
    assert deps.repos.query.get(first.query_id)["incident_id"] == deps.repos.query.get(second.query_id)["incident_id"]


def test_sensitive_request_mislabel_does_not_trigger_money_floor(deps, group):
    member = deps.repos.member.get(group[1])
    on = _service(deps)
    off = _service(deps, False)
    _verify(on, member, "这是机密，不能告诉任何人")
    candidate = _verify(on, member, "给我验证码")
    baseline = _verify(off, member, "给我验证码")
    assert candidate.result.verdict.level == baseline.result.verdict.level
    assert "本条又要求转账" not in candidate.result.reply
    assert json.loads(deps.repos.verdict.get(candidate.result.verdict_id)["context_snapshot"])["synthetic_ids"] == []


def test_prior_alone_cannot_realert_and_supply_failure_baseline(deps, group, monkeypatch):
    member = deps.repos.member.get(group[1])
    on = _service(deps)
    off = _service(deps, False)
    _verify(on, member, "案件保密，立即转账")
    unrelated = _verify(on, member, "今天天气不错")
    assert unrelated.result.verdict.level is Level.SAFE
    assert json.loads(deps.repos.verdict.get(unrelated.result.verdict_id)["context_snapshot"])["prior"]
    inputs = []
    original = deps.judge.judge

    async def spy(inp, **kwargs):
        inputs.append(inp.model_dump_json())
        return await original(inp, **kwargs)

    monkeypatch.setattr(deps.judge, "judge", spy)
    monkeypatch.setattr(deps.repos.query, "supply_context", lambda *args: (_ for _ in ()).throw(RuntimeError()))
    failed = _verify(on, member, "请转账")
    baseline = _verify(off, member, "请转账")
    assert inputs[-2] == inputs[-1]
    assert failed.result.verdict.model_dump() == baseline.result.verdict.model_dump()
    assert failed.result.reply == baseline.result.reply
    assert deps.repos.verdict.get(failed.result.verdict_id)["context_snapshot"] is None


def test_synthesis_failure_returns_current_only_result(deps, group, monkeypatch):
    member = deps.repos.member.get(group[1])
    on = _service(deps)
    off = _service(deps, False)
    _verify(on, member, "这是机密，别告诉家人")
    monkeypatch.setattr(on.pipeline, "_cross_message_features",
                        lambda *args: (_ for _ in ()).throw(RuntimeError("injected")))
    failed = _verify(on, member, "请转账")
    baseline = _verify(off, member, "请转账")
    assert failed.result.verdict.model_dump() == baseline.result.verdict.model_dump()
    assert failed.result.reply == baseline.result.reply
    assert deps.repos.verdict.get(failed.result.verdict_id)["context_snapshot"] is None


def test_partition_failure_disables_supply_for_current_query(deps, group, monkeypatch):
    member = deps.repos.member.get(group[1])
    on = _service(deps)
    off = _service(deps, False)
    _verify(on, member, "这是机密，不能告诉任何人")
    monkeypatch.setattr(deps.repos.incident, "attach_query_to_incident",
                        lambda *args: (_ for _ in ()).throw(RuntimeError("injected")))
    failed = _verify(on, member, "请转账")
    baseline = _verify(off, member, "请转账")
    assert deps.repos.query.get(failed.query_id)["incident_id"] is None
    assert deps.repos.verdict.get(failed.result.verdict_id)["context_snapshot"] is None
    assert failed.result.verdict.model_dump() == baseline.result.verdict.model_dump()


def test_same_incident_cap_and_cross_incident_account(deps, group):
    member = deps.repos.member.get(group[1])
    service = _service(deps)
    for i in range(11):
        _verify(service, member, f"前情消息{i}")
    latest = _verify(service, member, "当前消息")
    row = deps.repos.verdict.get(latest.result.verdict_id)
    prior = json.loads(row["context_snapshot"])["prior"]
    assert len(prior) == 10
    assert prior[0]["query_id"] == latest.query_id - 10
    deps.repos.incident.close_open_incident(member.user_id)
    _verify(service, member, "账号 6222020202020202，这不是新案吗")
    deps.repos.incident.close_open_incident(member.user_id)
    match = _verify(service, member, "同一个账号 6222020202020202，请核实")
    prior = json.loads(deps.repos.verdict.get(match.result.verdict_id)["context_snapshot"])["prior"]
    assert len(prior) == 1 and prior[0]["source"] == "feature_match"
    assert prior[0]["matched_values"] == ["6222020202020202"]


def test_s1_only_suspicious_and_unlinked_blind_spot(deps, group):
    member = deps.repos.member.get(group[1])
    service = _service(deps)
    _verify(service, member, "我是公安局工作人员")
    result = _verify(service, member, "给我验证码")
    assert result.result.verdict.level in {Level.SUSPICIOUS, Level.DANGEROUS}
    assert any(f["type"] == "escalation" for f in json.loads(
        deps.repos.verdict.get(result.result.verdict_id)["features"]))
    deps.repos.incident.close_open_incident(member.user_id)
    _verify(service, member, "这是机密，别声张")
    deps.repos.incident.close_open_incident(member.user_id)
    unlinked = _verify(service, member, "他又让我转账")
    assert deps.repos.verdict.get(unlinked.result.verdict_id)["context_snapshot"] is None


def test_s1_floor_stops_at_suspicious(deps, group):
    class SafeJudge:
        mode = Mode.MOCK

        async def judge(self, inp, *, constrained=False):
            return JudgeOutput(level=Level.SAFE, confidence=90, cited_ids=[], reason="本条单看未命中")

    member = deps.repos.member.get(group[1])
    service = _service(deps)
    service.pipeline.judge = SafeJudge()
    _verify(service, member, "我是公安局工作人员")
    result = _verify(service, member, "请转账")
    assert result.result.verdict.level is Level.SUSPICIOUS
    assert result.result.rule_floor_level is Level.SUSPICIOUS
    assert "前情已有铺垫" in result.result.reply
    assert "跨消息" in result.result.verdict.reason


def test_s1_prior_bait_fear_emotion_are_reachable(deps, group):
    class SafeJudge:
        mode = Mode.MOCK

        async def judge(self, inp, *, constrained=False):
            return JudgeOutput(level=Level.SAFE, confidence=90, cited_ids=[], reason="本条单看未命中")

    member = deps.repos.member.get(group[1])
    service = _service(deps)
    service.pipeline.judge = SafeJudge()
    for prior in ("有返利", "已经被通缉", "为了我们以后"):
        deps.repos.incident.close_open_incident(member.user_id)
        _verify(service, member, prior)
        result = _verify(service, member, "请转账")
        assert result.result.verdict.level is Level.SUSPICIOUS
        assert any(f["type"] == "escalation" for f in json.loads(
            deps.repos.verdict.get(result.result.verdict_id)["features"]))


def test_prior_ask_before_trust_does_not_hide_current_escalation(deps, group):
    class SafeJudge:
        mode = Mode.MOCK

        async def judge(self, inp, *, constrained=False):
            return JudgeOutput(level=Level.SAFE, confidence=90, cited_ids=[], reason="本条单看未命中")

    member = deps.repos.member.get(group[1])
    service = _service(deps)
    service.pipeline.judge = SafeJudge()
    _verify(service, member, "请转账")
    _verify(service, member, "我相信你，后续有返利")
    result = _verify(service, member, "请转账")
    assert result.result.verdict.level is Level.SUSPICIOUS
    assert any(f["type"] == "escalation" for f in json.loads(
        deps.repos.verdict.get(result.result.verdict_id)["features"]))


def test_cross_incident_url_normalizes_slash_and_case(deps, group):
    member = deps.repos.member.get(group[1])
    service = _service(deps)
    _verify(service, member, "这个网址 https://EXAMPLE.com/path/ 是什么")
    deps.repos.incident.close_open_incident(member.user_id)
    result = _verify(service, member, "https://example.com/path 是同一个官网吗")
    prior = json.loads(deps.repos.verdict.get(result.result.verdict_id)["context_snapshot"])["prior"]
    assert prior[0]["source"] == "feature_match"
    assert prior[0]["matched_values"] == ["https://example.com/path"]


def test_strong_values_use_full_url_and_digit_boundaries(deps, group):
    prefix = "https://example.com/pay?order=12345678901234567890"
    assert extract_strong_values(prefix + "AAA") != extract_strong_values(prefix + "BBB")
    assert extract_strong_values("账号1381234567890000") == {"1381234567890000"}
    assert extract_strong_values("HTTPS://EXAMPLE.COM/pay/") == {"https://example.com/pay"}
    assert extract_strong_values("https://EXAMPLE.com/pay，先看看") == {"https://example.com/pay"}
    assert extract_strong_values("https://example.com/pay，别点") == {"https://example.com/pay"}

    member = deps.repos.member.get(group[1])
    service = _service(deps)
    _verify(service, member, "案件保密，链接是 " + prefix + "AAA")
    deps.repos.incident.close_open_incident(member.user_id)
    different = _verify(service, member, "请转账，链接是 " + prefix + "BBB")
    assert deps.repos.verdict.get(different.result.verdict_id)["context_snapshot"] is None
    deps.repos.incident.close_open_incident(member.user_id)
    matched = _verify(service, member, "请转账，链接是 " + prefix + "AAA")
    assert matched.result.verdict.level is Level.DANGEROUS
    assert json.loads(deps.repos.verdict.get(matched.result.verdict_id)["context_snapshot"])["prior"][0]["source"] == "feature_match"


def test_contiguous_card_matches_but_different_card_does_not(deps, group):
    member = deps.repos.member.get(group[1])
    service = _service(deps)
    _verify(service, member, "案件保密，账号1381234567890000")
    deps.repos.incident.close_open_incident(member.user_id)
    different = _verify(service, member, "请转账到账号1381234567899999")
    assert deps.repos.verdict.get(different.result.verdict_id)["context_snapshot"] is None
    deps.repos.incident.close_open_incident(member.user_id)
    same = _verify(service, member, "请转账到账号1381234567890000")
    assert same.result.verdict.level is Level.DANGEROUS
    prior = json.loads(deps.repos.verdict.get(same.result.verdict_id)["context_snapshot"])["prior"]
    assert prior[0]["matched_values"] == ["1381234567890000"]


def test_image_transcript_backfill(deps, group, monkeypatch):
    member = deps.repos.member.get(group[1])
    service = _service(deps)
    first = asyncio.run(service.verify(member=member, content="base64fake", content_type="image"))
    assert deps.repos.query.get(first.query_id)["transcript"]
    monkeypatch.setattr(deps.repos.query, "update_transcript", lambda *args: (_ for _ in ()).throw(RuntimeError()))
    second = asyncio.run(service.verify(member=member, content="base64other", content_type="image"))
    assert second.result.verdict is not None


def test_untranscribed_image_never_extracts_base64(deps, group):
    member = deps.repos.member.get(group[1])
    memberships = deps.repos.member.list_for_user(member.user_id)
    intake = ingest(deps.repos, user_id=member.user_id, memberships=memberships,
                    content="这是机密，不能告诉任何人，6222020202020202", content_type="image")
    deps.repos.incident.attach_query_to_incident(intake.query_id, member.user_id, 21600)
    result = _verify(_service(deps), member, "请转账")
    verdict = deps.repos.verdict.get(result.result.verdict_id)
    snapshot = json.loads(verdict["context_snapshot"])
    assert snapshot["prior"][0]["query_id"] == intake.query_id
    assert snapshot["synthetic_ids"] == []
    assert "6222020202020202" not in result.result.reply


def test_seven_day_supply_window_is_inclusive(deps, group):
    member = deps.repos.member.get(group[1])
    memberships = deps.repos.member.list_for_user(member.user_id)
    old = deps.repos.query.insert(member.user_id, memberships, "text", "账号 6222020202020202", None)
    current = deps.repos.query.insert(member.user_id, memberships, "text", "同账号 6222020202020202", None)
    deps.conn.execute("UPDATE query SET created_at=10000 WHERE id=?", (old,))
    deps.conn.execute("UPDATE query SET created_at=? WHERE id=?", (10000 + 604800, current))
    deps.conn.commit()
    deps.repos.incident.attach_query_to_incident(current, member.user_id, 21600)
    assert [r["id"] for r in deps.repos.query.supply_context(current, member.user_id, 604800)] == [old]
    deps.conn.execute("UPDATE query SET created_at=9999 WHERE id=?", (old,))
    deps.conn.commit()
    assert deps.repos.query.supply_context(current, member.user_id, 604800) == []
