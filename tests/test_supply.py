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


async def _verify(service, user, text):
    return await service.verify(user=user, content=text)


async def test_s2_raises_after_current_only_judge(deps, relations, monkeypatch):
    user = relations[0]
    service = _service(deps)
    inputs = []
    original = service.pipeline.judge.judge

    async def spy(inp, **kwargs):
        inputs.append(inp)
        return await original(inp, **kwargs)

    monkeypatch.setattr(service.pipeline.judge, "judge", spy)
    first = await _verify(service, user, "这是机密，不能告诉任何人")
    second = await _verify(service, user, "请转账")
    assert second.result.verdict.level is Level.DANGEROUS
    assert {f.type for f in inputs[-1].features} == {"transfer"}
    assert "这是机密" not in inputs[-1].text
    verdict = await deps.repos.verdict.get(second.result.verdict_id)
    snapshot = json.loads(verdict["context_snapshot"])
    features = json.loads(verdict["features"])
    synthetic = [f for f in features if f.get("origin", "self") == "prior"]
    assert len(synthetic) == 1 and synthetic[0]["type"] == "isolation"
    assert snapshot["synthetic_ids"] == [synthetic[0]["id"]]
    assert synthetic[0]["id"] in second.result.verdict.cited_ids
    assert "跨消息" in second.result.verdict.reason
    assert "前情有保密要求" in second.result.reply
    assert "这是机密" not in second.result.reply
    assert (await deps.repos.query.get(first.query_id))["incident_id"] == (await deps.repos.query.get(second.query_id))["incident_id"]


async def test_sensitive_request_mislabel_does_not_trigger_money_floor(deps, relations):
    user = relations[0]
    on = _service(deps)
    off = _service(deps, False)
    await _verify(on, user, "这是机密，不能告诉任何人")
    candidate = await _verify(on, user, "给我验证码")
    baseline = await _verify(off, user, "给我验证码")
    assert candidate.result.verdict.level == baseline.result.verdict.level
    assert "本条又要求转账" not in candidate.result.reply
    assert json.loads((await deps.repos.verdict.get(candidate.result.verdict_id))["context_snapshot"])["synthetic_ids"] == []


async def test_prior_alone_cannot_realert_and_supply_failure_baseline(deps, relations, monkeypatch):
    user = relations[0]
    on = _service(deps)
    off = _service(deps, False)
    await _verify(on, user, "案件保密，立即转账")
    unrelated = await _verify(on, user, "今天天气不错")
    assert unrelated.result.verdict.level is Level.SAFE
    assert json.loads((await deps.repos.verdict.get(unrelated.result.verdict_id))["context_snapshot"])["prior"]
    inputs = []
    original = deps.judge.judge

    async def spy(inp, **kwargs):
        inputs.append(inp.model_dump_json())
        return await original(inp, **kwargs)

    monkeypatch.setattr(deps.judge, "judge", spy)
    monkeypatch.setattr(deps.repos.query, "supply_context", lambda *args: (_ for _ in ()).throw(RuntimeError()))
    failed = await _verify(on, user, "请转账")
    baseline = await _verify(off, user, "请转账")
    assert inputs[-2] == inputs[-1]
    assert failed.result.verdict.model_dump() == baseline.result.verdict.model_dump()
    assert failed.result.reply == baseline.result.reply
    assert (await deps.repos.verdict.get(failed.result.verdict_id))["context_snapshot"] is None


async def test_synthesis_failure_returns_current_only_result(deps, relations, monkeypatch):
    user = relations[0]
    on = _service(deps)
    off = _service(deps, False)
    await _verify(on, user, "这是机密，别告诉家人")
    monkeypatch.setattr(on.pipeline, "_cross_message_features",
                        lambda *args: (_ for _ in ()).throw(RuntimeError("injected")))
    failed = await _verify(on, user, "请转账")
    baseline = await _verify(off, user, "请转账")
    assert failed.result.verdict.model_dump() == baseline.result.verdict.model_dump()
    assert failed.result.reply == baseline.result.reply
    assert (await deps.repos.verdict.get(failed.result.verdict_id))["context_snapshot"] is None


async def test_partition_failure_disables_supply_for_current_query(deps, relations, monkeypatch):
    user = relations[0]
    on = _service(deps)
    off = _service(deps, False)
    await _verify(on, user, "这是机密，不能告诉任何人")
    monkeypatch.setattr(deps.repos.incident, "attach_query_to_incident",
                        lambda *args: (_ for _ in ()).throw(RuntimeError("injected")))
    failed = await _verify(on, user, "请转账")
    baseline = await _verify(off, user, "请转账")
    assert (await deps.repos.query.get(failed.query_id))["incident_id"] is None
    assert (await deps.repos.verdict.get(failed.result.verdict_id))["context_snapshot"] is None
    assert failed.result.verdict.model_dump() == baseline.result.verdict.model_dump()


async def test_same_incident_cap_and_cross_incident_account(deps, relations):
    user = relations[0]
    service = _service(deps)
    for i in range(11):
        await _verify(service, user, f"前情消息{i}")
    latest = await _verify(service, user, "当前消息")
    row = await deps.repos.verdict.get(latest.result.verdict_id)
    prior = json.loads(row["context_snapshot"])["prior"]
    assert len(prior) == 10
    assert prior[0]["query_id"] == latest.query_id - 10
    await deps.repos.incident.close_open_incident(user.id)
    await _verify(service, user, "账号 6222020202020202，这不是新案吗")
    await deps.repos.incident.close_open_incident(user.id)
    match = await _verify(service, user, "同一个账号 6222020202020202，请核实")
    prior = json.loads((await deps.repos.verdict.get(match.result.verdict_id))["context_snapshot"])["prior"]
    assert len(prior) == 1 and prior[0]["source"] == "feature_match"
    assert prior[0]["matched_values"] == ["6222020202020202"]


async def test_s1_only_suspicious_and_unlinked_blind_spot(deps, relations):
    user = relations[0]
    service = _service(deps)
    await _verify(service, user, "我是公安局工作人员")
    result = await _verify(service, user, "给我验证码")
    assert result.result.verdict.level in {Level.SUSPICIOUS, Level.DANGEROUS}
    assert any(f["type"] == "escalation" for f in json.loads(
        (await deps.repos.verdict.get(result.result.verdict_id))["features"]))
    await deps.repos.incident.close_open_incident(user.id)
    await _verify(service, user, "这是机密，别声张")
    await deps.repos.incident.close_open_incident(user.id)
    unlinked = await _verify(service, user, "他又让我转账")
    assert (await deps.repos.verdict.get(unlinked.result.verdict_id))["context_snapshot"] is None


async def test_s1_floor_stops_at_suspicious(deps, relations):
    class SafeJudge:
        mode = Mode.MOCK

        async def judge(self, inp, *, constrained=False):
            return JudgeOutput(level=Level.SAFE, confidence=90, cited_ids=[], reason="本条单看未命中")

    user = relations[0]
    service = _service(deps)
    service.pipeline.judge = SafeJudge()
    await _verify(service, user, "我是公安局工作人员")
    result = await _verify(service, user, "请转账")
    assert result.result.verdict.level is Level.SUSPICIOUS
    assert result.result.rule_floor_level is Level.SUSPICIOUS
    assert "前情已有铺垫" in result.result.reply
    assert "跨消息" in result.result.verdict.reason


async def test_s1_prior_bait_fear_emotion_are_reachable(deps, relations):
    class SafeJudge:
        mode = Mode.MOCK

        async def judge(self, inp, *, constrained=False):
            return JudgeOutput(level=Level.SAFE, confidence=90, cited_ids=[], reason="本条单看未命中")

    user = relations[0]
    service = _service(deps)
    service.pipeline.judge = SafeJudge()
    for prior in ("有返利", "已经被通缉", "为了我们以后"):
        await deps.repos.incident.close_open_incident(user.id)
        await _verify(service, user, prior)
        result = await _verify(service, user, "请转账")
        assert result.result.verdict.level is Level.SUSPICIOUS
        assert any(f["type"] == "escalation" for f in json.loads(
            (await deps.repos.verdict.get(result.result.verdict_id))["features"]))


async def test_prior_ask_before_trust_does_not_hide_current_escalation(deps, relations):
    class SafeJudge:
        mode = Mode.MOCK

        async def judge(self, inp, *, constrained=False):
            return JudgeOutput(level=Level.SAFE, confidence=90, cited_ids=[], reason="本条单看未命中")

    user = relations[0]
    service = _service(deps)
    service.pipeline.judge = SafeJudge()
    await _verify(service, user, "请转账")
    await _verify(service, user, "我相信你，后续有返利")
    result = await _verify(service, user, "请转账")
    assert result.result.verdict.level is Level.SUSPICIOUS
    assert any(f["type"] == "escalation" for f in json.loads(
        (await deps.repos.verdict.get(result.result.verdict_id))["features"]))


async def test_cross_incident_url_normalizes_slash_and_case(deps, relations):
    user = relations[0]
    service = _service(deps)
    await _verify(service, user, "这个网址 https://EXAMPLE.com/path/ 是什么")
    await deps.repos.incident.close_open_incident(user.id)
    result = await _verify(service, user, "https://example.com/path 是同一个官网吗")
    prior = json.loads((await deps.repos.verdict.get(result.result.verdict_id))["context_snapshot"])["prior"]
    assert prior[0]["source"] == "feature_match"
    assert prior[0]["matched_values"] == ["https://example.com/path"]


async def test_strong_values_use_full_url_and_digit_boundaries(deps, relations):
    prefix = "https://example.com/pay?order=12345678901234567890"
    assert extract_strong_values(prefix + "AAA") != extract_strong_values(prefix + "BBB")
    assert extract_strong_values("账号1381234567890000") == {"1381234567890000"}
    assert extract_strong_values("HTTPS://EXAMPLE.COM/pay/") == {"https://example.com/pay"}
    assert extract_strong_values("https://EXAMPLE.com/pay，先看看") == {"https://example.com/pay"}
    assert extract_strong_values("https://example.com/pay，别点") == {"https://example.com/pay"}

    user = relations[0]
    service = _service(deps)
    await _verify(service, user, "案件保密，链接是 " + prefix + "AAA")
    await deps.repos.incident.close_open_incident(user.id)
    different = await _verify(service, user, "请转账，链接是 " + prefix + "BBB")
    assert (await deps.repos.verdict.get(different.result.verdict_id))["context_snapshot"] is None
    await deps.repos.incident.close_open_incident(user.id)
    matched = await _verify(service, user, "请转账，链接是 " + prefix + "AAA")
    assert matched.result.verdict.level is Level.DANGEROUS
    assert json.loads((await deps.repos.verdict.get(matched.result.verdict_id))["context_snapshot"])["prior"][0]["source"] == "feature_match"


async def test_contiguous_card_matches_but_different_card_does_not(deps, relations):
    user = relations[0]
    service = _service(deps)
    await _verify(service, user, "案件保密，账号1381234567890000")
    await deps.repos.incident.close_open_incident(user.id)
    different = await _verify(service, user, "请转账到账号1381234567899999")
    assert (await deps.repos.verdict.get(different.result.verdict_id))["context_snapshot"] is None
    await deps.repos.incident.close_open_incident(user.id)
    same = await _verify(service, user, "请转账到账号1381234567890000")
    assert same.result.verdict.level is Level.DANGEROUS
    prior = json.loads((await deps.repos.verdict.get(same.result.verdict_id))["context_snapshot"])["prior"]
    assert prior[0]["matched_values"] == ["1381234567890000"]


async def test_image_transcript_backfill(deps, relations, monkeypatch):
    user = relations[0]
    service = _service(deps)
    first = await service.verify(user=user, content="base64fake", content_type="image")
    assert (await deps.repos.query.get(first.query_id))["transcript"]
    monkeypatch.setattr(deps.repos.query, "update_transcript", lambda *args: (_ for _ in ()).throw(RuntimeError()))
    second = await service.verify(user=user, content="base64other", content_type="image")
    assert second.result.verdict is not None


async def test_untranscribed_image_never_extracts_base64(deps, relations):
    user = relations[0]
    intake = await ingest(deps.repos, user_id=user.id,
                    content="这是机密，不能告诉任何人，6222020202020202", content_type="image")
    await deps.repos.incident.attach_query_to_incident(intake.query_id, user.id, 21600)
    result = await _verify(_service(deps), user, "请转账")
    verdict = await deps.repos.verdict.get(result.result.verdict_id)
    snapshot = json.loads(verdict["context_snapshot"])
    assert snapshot["prior"][0]["query_id"] == intake.query_id
    assert snapshot["synthetic_ids"] == []
    assert "6222020202020202" not in result.result.reply


async def test_seven_day_supply_window_is_inclusive(deps, relations):
    user = relations[0]
    old = await deps.repos.query.insert(user.id, "text", "账号 6222020202020202", None)
    current = await deps.repos.query.insert(user.id, "text", "同账号 6222020202020202", None)
    await deps.conn.execute('UPDATE query SET created_at=to_timestamp(10000) WHERE id=%s', (old,))
    await deps.conn.execute('UPDATE query SET created_at=to_timestamp(%s) WHERE id=%s', (10000 + 604800, current))
    await deps.conn.commit()
    await deps.repos.incident.attach_query_to_incident(current, user.id, 21600)
    assert [r["id"] for r in await deps.repos.query.supply_context(current, user.id, 604800)] == [old]
    await deps.conn.execute('UPDATE query SET created_at=to_timestamp(9999) WHERE id=%s', (old,))
    await deps.conn.commit()
    assert await deps.repos.query.supply_context(current, user.id, 604800) == []
