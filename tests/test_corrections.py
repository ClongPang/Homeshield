"""Fixed electorate and strict majority voting on query-relative trust."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from homeshield.core.errors import ValidationError
from homeshield.core.feedback import CorrectionService
from homeshield.core.models import Level, Mode


def _case_data(deps, level=Level.DANGEROUS, protector_count=0):
    queryer = deps.repos.users.get_or_create(f"correction:q:{level.value}:{protector_count}")
    protectors = [deps.repos.users.get_or_create(f"correction:p:{level.value}:{protector_count}:{i}")
                  for i in range(protector_count)]
    for i, protector in enumerate(protectors):
        invite = deps.relations.issue_invite(protector.id, f"亲友{i}")
        _, rid, _ = deps.relations.join(queryer.openid, invite["code"])
        deps.repos.relation.update(rid, queryer.id, inverse_name=f"称呼{i}")
    query_id = deps.repos.query.insert(queryer.id, "text", "这是一条原查询内容", None)
    verdict_id = deps.repos.verdict.insert(query_id, level, [], [], "原始理由", "原始回复", 1, Mode.MOCK)
    if level is Level.DANGEROUS:
        deps.repos.alert.record_alerts_for_verdict(verdict_id, query_id)
    return queryer, protectors, query_id, verdict_id


def test_queryer_feedback_is_not_a_vote_and_majority_is_confirmed(deps):
    queryer, protectors, _, verdict_id = _case_data(deps, protector_count=3)
    svc = CorrectionService(deps.repos)
    opened = svc.submit(verdict_id, queryer.id, "real", "我认为是真骗局")
    assert opened["eligible_count"] == 3 and opened["votes"] == {}
    one = svc.submit(verdict_id, protectors[0].id, "real")
    assert one["status"] == "pending"
    two = svc.submit(verdict_id, protectors[1].id, "false_positive")
    assert two["status"] == "pending"
    final = svc.submit(verdict_id, protectors[2].id, "false_positive")
    assert final["status"] == "confirmed" and final["resolved_label"] == "false_positive"
    saved = deps.repos.verdict.get(verdict_id)
    assert saved["level"] == "dangerous" and saved["reply"] == "原始回复"
    assert deps.repos.correction.confirmed_labels() == [{"verdict_id": verdict_id, "resolved_label": "false_positive"}]


@pytest.mark.parametrize("count,labels,status,winner", [
    (0, [], "no_consensus", None),
    (1, ["real"], "confirmed", "real"),
    (2, ["real"], "pending", None),
    (2, ["real", "false_positive"], "pending", None),
    (3, ["false_positive", "false_positive"], "confirmed", "false_positive"),
])
def test_majority_thresholds_and_zero_eligible(deps, count, labels, status, winner):
    queryer, protectors, _, verdict_id = _case_data(deps, protector_count=count)
    svc = CorrectionService(deps.repos)
    summary = svc.submit(verdict_id, queryer.id, "false_positive")
    if count == 0:
        assert summary["status"] == "no_consensus" and summary["required_votes"] == 0
        return
    assert summary["required_votes"] == count // 2 + 1
    result = summary
    for protector, label in zip(protectors, labels):
        result = svc.submit(verdict_id, protector.id, label)
    assert result["status"] == status and result["resolved_label"] == winner


@pytest.mark.parametrize("level", [Level.SAFE, Level.DANGEROUS])
def test_zero_relation_feedback_is_retained_without_consensus(deps, level):
    queryer, _, _, verdict_id = _case_data(deps, level=level, protector_count=0)
    feedback = CorrectionService(deps.repos).submit(verdict_id, queryer.id, "false_positive", "个人反馈")

    assert feedback["status"] == "no_consensus" and feedback["eligible_count"] == 0
    case = deps.repos.correction.get_case_for_verdict(verdict_id)
    assert case["queryer_label"] == "false_positive" and case["queryer_note"] == "个人反馈"
    assert deps.repos.correction.confirmed_labels() == []
    assert deps.conn.execute("SELECT COUNT(*) FROM correction_vote WHERE case_id=?", (case["case_id"],)).fetchone()[0] == 0


def test_outgoing_protector_role_does_not_add_votes_to_own_query(deps):
    queryer, protectors, _, verdict_id = _case_data(deps, protector_count=1)
    other_user = deps.repos.users.get_or_create("correction:outgoing-target")
    invite = deps.relations.issue_invite(queryer.id, "我保护的人")
    deps.relations.join(other_user.openid, invite["code"])

    feedback = CorrectionService(deps.repos).submit(verdict_id, queryer.id, "real", "这只是反馈")

    relation_id = deps.repos.conn.execute(
        "SELECT id FROM guard_relation WHERE protector_user_id=?", (protectors[0].id,)
    ).fetchone()[0]
    assert feedback["eligible_count"] == 1 and feedback["votes"] == {}
    assert deps.repos.conn.execute(
        "SELECT relation_id FROM correction_vote WHERE case_id=?", (feedback["case_id"],)
    ).fetchone()[0] == relation_id


def test_duplicate_vote_is_idempotent_but_vote_cannot_change(deps):
    _, protectors, _, verdict_id = _case_data(deps, protector_count=1)
    svc = CorrectionService(deps.repos)
    first = svc.submit(verdict_id, protectors[0].id, "real")
    repeated = svc.submit(verdict_id, protectors[0].id, "real")
    assert repeated["case_id"] == first["case_id"] and repeated["votes"]["real"] == 1
    with pytest.raises(ValidationError, match="vote cannot be changed"):
        svc.submit(verdict_id, protectors[0].id, "false_positive")


def test_fixed_electorate_survives_end_until_lazy_expiry(deps):
    queryer, protectors, _, verdict_id = _case_data(deps, protector_count=2)
    svc = CorrectionService(deps.repos)
    opened = svc.submit(verdict_id, queryer.id, "real")
    assert opened["eligible_count"] == 2
    relation1 = deps.repos.conn.execute("SELECT id FROM guard_relation WHERE protector_user_id=?", (protectors[0].id,)).fetchone()[0]
    relation2 = deps.repos.conn.execute("SELECT id FROM guard_relation WHERE protector_user_id=?", (protectors[1].id,)).fetchone()[0]
    assert deps.relations.end(protectors[0].id, relation1) == "by_protector"
    with pytest.raises(ValidationError, match="no active voting relation"):
        svc.submit(verdict_id, protectors[0].id, "real")
    one_vote = svc.submit(verdict_id, protectors[1].id, "real")
    assert one_vote["status"] == "pending" and one_vote["eligible_count"] == 2
    with deps.repos.conn:
        deps.repos.conn.execute("UPDATE correction_case SET opened_at=0,closes_at=1 WHERE verdict_id=?", (verdict_id,))
    assert deps.repos.correction.list_pending_for_user(protectors[1].id) == []
    assert deps.repos.correction.get_case_for_verdict(verdict_id)["status"] == "no_consensus"
    assert deps.repos.correction.detail_for_relation(opened["case_id"], relation2, protectors[1].id)["status"] == "no_consensus"


def test_low_risk_content_is_visible_only_to_snapshot_voters(deps):
    queryer, protectors, _, verdict_id = _case_data(deps, level=Level.SAFE, protector_count=2)
    svc = CorrectionService(deps.repos)
    opened = svc.submit(verdict_id, queryer.id, "false_positive", "this is ordinary")
    assert opened["eligible_count"] == 2
    assert len(svc.pending_for_user(protectors[0].id)) == 1
    late = deps.repos.users.get_or_create("correction:late")
    invite = deps.relations.issue_invite(late.id, "后来者")
    _, late_relation, _ = deps.relations.join(queryer.openid, invite["code"])
    assert svc.pending_for_user(late.id) == []
    old_relation = deps.repos.conn.execute("SELECT id FROM guard_relation WHERE protector_user_id=?", (protectors[1].id,)).fetchone()[0]
    assert deps.relations.end(protectors[1].id, old_relation) == "by_protector"
    assert deps.repos.correction.detail_for_relation(opened["case_id"], old_relation, protectors[1].id) is None
    assert len(svc.pending_for_user(protectors[0].id)) == 1
    assert deps.repos.relation.get(late_relation)["id"] == late_relation


def test_concurrent_case_open_and_decisive_votes_are_serialized(deps):
    queryer, protectors, _, verdict_id = _case_data(deps, protector_count=2)
    gate = Barrier(2)
    def submit(user):
        gate.wait()
        return CorrectionService(deps.repos).submit(verdict_id, user.id, "real")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, protectors))
    cases = deps.repos.conn.execute("SELECT COUNT(*) n FROM correction_case WHERE verdict_id=?", (verdict_id,)).fetchone()["n"]
    votes = deps.repos.conn.execute("SELECT COUNT(*) n FROM correction_vote WHERE case_id=? AND label IS NOT NULL", (results[0]["case_id"],)).fetchone()["n"]
    assert cases == 1 and votes == 2
    assert deps.repos.correction.get_case_for_verdict(verdict_id)["status"] == "confirmed"


def test_late_queryer_feedback_does_not_open_a_second_case(deps):
    queryer, protectors, _, verdict_id = _case_data(deps, protector_count=1)
    svc = CorrectionService(deps.repos)
    first = svc.submit(verdict_id, protectors[0].id, "real")
    feedback = svc.submit(verdict_id, queryer.id, "false_positive", "my feedback")
    assert feedback["case_id"] == first["case_id"]
    repeated = svc.submit(verdict_id, queryer.id, "false_positive", "changed note")
    assert repeated["queryer_label"] == "false_positive"
    assert repeated["queryer_note"] == "my feedback"
    with pytest.raises(ValidationError, match="queryer feedback cannot be changed"):
        svc.submit(verdict_id, queryer.id, "real")
    assert deps.repos.conn.execute("SELECT COUNT(*) FROM correction_case WHERE verdict_id=?", (verdict_id,)).fetchone()[0] == 1
