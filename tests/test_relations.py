"""Directed relation, invitation, alert lifecycle and consent boundaries."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from homeshield.core.errors import ValidationError
from homeshield.core.models import Level, Mode
from homeshield.core.relations import RelationError, RelationService


def _link(deps, protector, protected, name="家人"):
    invite = deps.relations.issue_invite(protector.id, name)
    _, relation_id, _ = deps.relations.join(protected.openid, invite["code"])
    return relation_id, invite


def test_invitation_creates_one_directed_relation_and_reverse_is_independent(deps):
    a = deps.repos.users.get_or_create("wx:a")
    b = deps.repos.users.get_or_create("wx:b")
    ab, _ = _link(deps, a, b, "妈妈")
    rel = deps.repos.relation.get(ab)
    assert (rel["protector_user_id"], rel["protected_user_id"], rel["name"]) == (a.id, b.id, "妈妈")
    assert deps.relations.list_for_user(a.id)["guardings"][0]["name"] == "妈妈"
    assert deps.relations.list_for_user(b.id)["guardians"][0]["name"] == f"联防者 #{ab}"
    ba, _ = _link(deps, b, a, "儿子")
    assert ba != ab
    assert len(deps.relations.list_for_user(a.id)["guardings"]) == 1
    assert len(deps.relations.list_for_user(a.id)["guardians"]) == 1
    assert deps.relations.end(a.id, ab) == "by_protector"
    assert deps.repos.relation.get(ba)["ended_at"] is None


def test_invite_invalid_states_self_duplicate_and_reissue(deps):
    a = deps.repos.users.get_or_create("wx:creator")
    b = deps.repos.users.get_or_create("wx:recipient")
    service = deps.relations
    invite = service.issue_invite(a.id, "爸爸")
    with pytest.raises(RelationError) as self_error: service.join(a.openid, invite["code"])
    assert self_error.value.reason == "self"
    _, relation_id, _ = service.join(b.openid, invite["code"])
    with pytest.raises(RelationError) as used: service.join(deps.repos.users.get_or_create("wx:other").openid, invite["code"])
    assert used.value.reason == "used"
    duplicate = service.issue_invite(a.id, "爸爸")
    with pytest.raises(RelationError) as duplicate_error: service.join(b.openid, duplicate["code"])
    assert duplicate_error.value.reason == "already_exists"
    assert deps.repos.invite.get_valid(duplicate["code"]) is not None

    revoke = service.issue_invite(a.id, "家人")
    assert deps.repos.invite.revoke(revoke["id"], a.id) == "revoked"
    with pytest.raises(RelationError) as revoked: service.join(deps.repos.users.get_or_create("wx:c").openid, revoke["code"])
    assert revoked.value.reason == "revoked"
    expired = service.issue_invite(a.id, "家人")
    with deps.repos.conn:
        deps.repos.conn.execute("UPDATE invite_code SET expires_at=0 WHERE id=?", (expired["id"],))
    with pytest.raises(RelationError) as expired_error: service.join(deps.repos.users.get_or_create("wx:d").openid, expired["code"])
    assert expired_error.value.reason == "expired"


def test_relation_capacity_counts_both_directions_and_retries_do_not_consume(deps):
    service = RelationService(deps.repos, max_relations=1, invite_ttl_days=7)
    a = deps.repos.users.get_or_create("cap:a")
    b = deps.repos.users.get_or_create("cap:b")
    c = deps.repos.users.get_or_create("cap:c")
    invite = service.issue_invite(a.id, "B")
    service.join(b.openid, invite["code"])
    with pytest.raises(RelationError) as full: service.issue_invite(a.id, "C")
    assert full.value.reason == "limit"
    c_invite = service.issue_invite(c.id, "B")
    with pytest.raises(RelationError) as receiver_full: service.join(b.openid, c_invite["code"])
    assert receiver_full.value.reason == "limit"
    assert deps.repos.invite.get_valid(c_invite["code"]) is not None


def test_protector_end_revokes_all_unused_codes_but_protected_end_does_not(deps):
    a = deps.repos.users.get_or_create("revoke:a")
    b = deps.repos.users.get_or_create("revoke:b")
    first, invite = _link(deps, a, b)
    spare1 = deps.relations.issue_invite(a.id, "other")
    spare2 = deps.relations.issue_invite(a.id, "other")
    assert deps.relations.end(b.id, first) == "by_protected"
    assert deps.repos.invite.get_valid(spare1["code"]) is not None
    assert deps.relations.end(a.id, first) == "already_ended"
    new_relation, _ = _link(deps, a, b, "new name")
    assert new_relation != first
    assert deps.relations.end(a.id, new_relation) == "by_protector"
    assert deps.repos.invite.get_valid(spare1["code"]) is None
    assert deps.repos.invite.get_valid(spare2["code"]) is None
    old = deps.repos.relation.get(first)
    assert old["end_reason"] == "by_protected" and old["ended_at"] is not None


def test_concurrent_claim_consumes_code_once(deps):
    creator = deps.repos.users.get_or_create("race:creator")
    a = deps.repos.users.get_or_create("race:a")
    b = deps.repos.users.get_or_create("race:b")
    invite = deps.relations.issue_invite(creator.id, "家人")
    def claim(user):
        try: return deps.relations.join(user.openid, invite["code"])[2]
        except RelationError as exc: return exc.reason
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, [a, b]))
    assert results.count("created") == 1 and results.count("used") == 1
    assert deps.conn.execute("SELECT COUNT(*) FROM guard_relation WHERE protector_user_id=?", (creator.id,)).fetchone()[0] == 1
    assert deps.conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_query_snapshot_alert_mute_rename_and_ended_access(deps):
    queryer = deps.repos.users.get_or_create("alert:queryer")
    p1 = deps.repos.users.get_or_create("alert:p1")
    p2 = deps.repos.users.get_or_create("alert:p2")
    r1, _ = _link(deps, p1, queryer, "妈妈")
    r2, _ = _link(deps, p2, queryer, "孩子")
    deps.repos.relation.update(r1, queryer.id, inverse_name="儿子")
    deps.repos.relation.update(r2, queryer.id, inverse_name="女儿")
    deps.repos.relation.update(r2, p2.id, mute=True)
    qid = deps.repos.query.insert(queryer.id, "text", "转账", None)
    assert {r["id"] for r in deps.repos.query.list_relations_for_query(qid)} == {r1, r2}
    vid = deps.repos.verdict.insert(qid, Level.DANGEROUS, [], [], "reason", "reply", 1, Mode.MOCK)
    fanout = deps.repos.alert.record_alerts_for_verdict(vid, qid)
    assert len(fanout["recipients"]) == 2
    alert1 = next(r for r in fanout["recipients"] if r["relation_id"] == r1)
    alert2 = next(r for r in fanout["recipients"] if r["relation_id"] == r2)
    assert deps.repos.alert.push_context(alert1["alert_id"]) is not None
    assert deps.repos.alert.push_context(alert2["alert_id"]) is None
    assert deps.repos.alert.event_context(alert2["alert_id"]) is not None
    deps.repos.relation.update(r1, p1.id, name="妈妈的新称呼")
    listed = deps.repos.alert.list_for_user(p1.id)
    assert listed[0]["name_at_alert"] == "妈妈"
    assert deps.relations.end(p1.id, r1) == "by_protector"
    detail, denial = deps.repos.alert.detail_for_user(p1.id, alert1["alert_id"])
    assert detail is None and denial == "relation_ended"
    assert deps.repos.alert.event_context(alert1["alert_id"]) is None
    assert deps.repos.alert.list_for_user(p1.id) == []
    assert deps.conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_relation_added_after_intake_does_not_receive_old_query(deps):
    queryer = deps.repos.users.get_or_create("snapshot:q")
    qid = deps.repos.query.insert(queryer.id, "text", "danger", None)
    protector = deps.repos.users.get_or_create("snapshot:p")
    _link(deps, protector, queryer)
    vid = deps.repos.verdict.insert(qid, Level.DANGEROUS, [], [], "reason", "reply", 1, Mode.MOCK)
    fanout = deps.repos.alert.record_alerts_for_verdict(vid, qid)
    assert fanout["recipients"] == []
    assert deps.conn.execute("SELECT COUNT(*) FROM alert").fetchone()[0] == 0


def test_ended_snapshot_relation_and_late_replacement_do_not_receive_old_query(deps):
    queryer = deps.repos.users.get_or_create("snapshot:ended-queryer")
    protector = deps.repos.users.get_or_create("snapshot:ended-protector")
    old_relation, _ = _link(deps, protector, queryer, "旧称呼")
    query_id = deps.repos.query.insert(queryer.id, "text", "危险查询", None)
    assert deps.relations.end(protector.id, old_relation) == "by_protector"
    new_relation, _ = _link(deps, protector, queryer, "新称呼")
    verdict_id = deps.repos.verdict.insert(query_id, Level.DANGEROUS, [], [], "reason", "reply", 1, Mode.MOCK)

    fanout = deps.repos.alert.record_alerts_for_verdict(verdict_id, query_id)

    assert fanout["recipients"] == []
    assert deps.repos.relation.get(new_relation)["ended_at"] is None
    assert deps.conn.execute("SELECT COUNT(*) FROM alert WHERE verdict_id=?", (verdict_id,)).fetchone()[0] == 0


def test_alert_record_racing_with_relation_end_never_leaves_sendable_alert(deps):
    from threading import Barrier

    queryer = deps.repos.users.get_or_create("alert-race:queryer")
    protector = deps.repos.users.get_or_create("alert-race:protector")
    relation_id, _ = _link(deps, protector, queryer, "家人")
    query_id = deps.repos.query.insert(queryer.id, "text", "危险查询", None)
    verdict_id = deps.repos.verdict.insert(query_id, Level.DANGEROUS, [], [], "reason", "reply", 1, Mode.MOCK)
    gate = Barrier(2)

    def record():
        gate.wait()
        return deps.repos.alert.record_alerts_for_verdict(verdict_id, query_id)

    def end():
        gate.wait()
        return deps.relations.end(protector.id, relation_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        record_future, end_future = pool.submit(record), pool.submit(end)
        fanout, end_status = record_future.result(), end_future.result()

    assert end_status == "by_protector"
    assert len(fanout["recipients"]) in (0, 1)
    alerts = deps.conn.execute("SELECT id FROM alert WHERE verdict_id=?", (verdict_id,)).fetchall()
    for alert in alerts:
        assert deps.repos.alert.event_context(alert["id"]) is None
        assert deps.repos.alert.push_context(alert["id"]) is None
