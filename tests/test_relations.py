"""Directed relation, invitation, alert lifecycle and consent boundaries."""
import asyncio

import pytest

from homeshield.core.models import Level, Mode
from homeshield.core.relations import RelationError, RelationService


async def _link(deps, protector, protected, name="家人"):
    invite = await deps.relations.issue_invite(protector.id, name)
    _, relation_id, _ = await deps.relations.join(protected.openid, invite["code"])
    return relation_id, invite


async def test_invitation_creates_one_directed_relation_and_reverse_is_independent(deps):
    a = await deps.repos.users.get_or_create("wx:a")
    b = await deps.repos.users.get_or_create("wx:b")
    ab, _ = await _link(deps, a, b, "妈妈")
    rel = await deps.repos.relation.get(ab)
    assert (rel["protector_user_id"], rel["protected_user_id"], rel["name"]) == (a.id, b.id, "妈妈")
    assert (await deps.relations.list_for_user(a.id))["guardings"][0]["name"] == "妈妈"
    assert (await deps.relations.list_for_user(b.id))["guardians"][0]["name"] == f"联防者 #{ab}"
    ba, _ = await _link(deps, b, a, "儿子")
    assert ba != ab
    assert len((await deps.relations.list_for_user(a.id))["guardings"]) == 1
    assert len((await deps.relations.list_for_user(a.id))["guardians"]) == 1
    assert await deps.relations.end(a.id, ab) == "by_protector"
    assert (await deps.repos.relation.get(ba))["ended_at"] is None


async def test_invite_invalid_states_self_duplicate_and_reissue(deps):
    a = await deps.repos.users.get_or_create("wx:creator")
    b = await deps.repos.users.get_or_create("wx:recipient")
    service = deps.relations
    invite = await service.issue_invite(a.id, "爸爸")
    with pytest.raises(RelationError) as self_error: await service.join(a.openid, invite["code"])
    assert self_error.value.reason == "self"
    _, relation_id, _ = await service.join(b.openid, invite["code"])
    with pytest.raises(RelationError) as used: await service.join((await deps.repos.users.get_or_create("wx:other")).openid, invite["code"])
    assert used.value.reason == "used"
    duplicate = await service.issue_invite(a.id, "爸爸")
    with pytest.raises(RelationError) as duplicate_error: await service.join(b.openid, duplicate["code"])
    assert duplicate_error.value.reason == "already_exists"
    assert await deps.repos.invite.get_valid(duplicate["code"]) is not None

    revoke = await service.issue_invite(a.id, "家人")
    assert await deps.repos.invite.revoke(revoke["id"], a.id) == "revoked"
    with pytest.raises(RelationError) as revoked: await service.join((await deps.repos.users.get_or_create("wx:c")).openid, revoke["code"])
    assert revoked.value.reason == "revoked"
    expired = await service.issue_invite(a.id, "家人")
    async with deps.conn.transaction():
        await deps.conn.execute('UPDATE invite_code SET expires_at=to_timestamp(0) WHERE id=%s', (expired["id"],))
    with pytest.raises(RelationError) as expired_error: await service.join((await deps.repos.users.get_or_create("wx:d")).openid, expired["code"])
    assert expired_error.value.reason == "expired"


async def test_relation_capacity_counts_both_directions_and_retries_do_not_consume(deps):
    service = RelationService(deps.repos, max_relations=1, invite_ttl_days=7)
    a = await deps.repos.users.get_or_create("cap:a")
    b = await deps.repos.users.get_or_create("cap:b")
    c = await deps.repos.users.get_or_create("cap:c")
    invite = await service.issue_invite(a.id, "B")
    await service.join(b.openid, invite["code"])
    with pytest.raises(RelationError) as full: await service.issue_invite(a.id, "C")
    assert full.value.reason == "limit"
    c_invite = await service.issue_invite(c.id, "B")
    with pytest.raises(RelationError) as receiver_full: await service.join(b.openid, c_invite["code"])
    assert receiver_full.value.reason == "limit"
    assert await deps.repos.invite.get_valid(c_invite["code"]) is not None


async def test_protector_end_revokes_all_unused_codes_but_protected_end_does_not(deps):
    a = await deps.repos.users.get_or_create("revoke:a")
    b = await deps.repos.users.get_or_create("revoke:b")
    first, invite = await _link(deps, a, b)
    spare1 = await deps.relations.issue_invite(a.id, "other")
    spare2 = await deps.relations.issue_invite(a.id, "other")
    assert await deps.relations.end(b.id, first) == "by_protected"
    assert await deps.repos.invite.get_valid(spare1["code"]) is not None
    assert await deps.relations.end(a.id, first) == "already_ended"
    new_relation, _ = await _link(deps, a, b, "new name")
    assert new_relation != first
    assert await deps.relations.end(a.id, new_relation) == "by_protector"
    assert await deps.repos.invite.get_valid(spare1["code"]) is None
    assert await deps.repos.invite.get_valid(spare2["code"]) is None
    old = await deps.repos.relation.get(first)
    assert old["end_reason"] == "by_protected" and old["ended_at"] is not None


async def test_concurrent_claim_consumes_code_once(deps):
    creator = await deps.repos.users.get_or_create("race:creator")
    a = await deps.repos.users.get_or_create("race:a")
    b = await deps.repos.users.get_or_create("race:b")
    invite = await deps.relations.issue_invite(creator.id, "家人")
    async def claim(user):
        try: return (await deps.relations.join(user.openid, invite["code"]))[2]
        except RelationError as exc: return exc.reason
    results = await asyncio.gather(claim(a), claim(b))
    assert results.count("created") == 1 and results.count("used") == 1
    assert (await (await deps.conn.execute('SELECT COUNT(*) FROM guard_relation WHERE protector_user_id=%s', (creator.id,))).fetchone())[0] == 1
    assert (await (await deps.conn.execute("SELECT COUNT(*) FROM pg_constraint WHERE contype='f'")).fetchone())[0] > 0


async def test_query_snapshot_alert_mute_rename_and_ended_access(deps):
    queryer = await deps.repos.users.get_or_create("alert:queryer")
    p1 = await deps.repos.users.get_or_create("alert:p1")
    p2 = await deps.repos.users.get_or_create("alert:p2")
    r1, _ = await _link(deps, p1, queryer, "妈妈")
    r2, _ = await _link(deps, p2, queryer, "孩子")
    await deps.repos.relation.update(r1, queryer.id, inverse_name="儿子")
    await deps.repos.relation.update(r2, queryer.id, inverse_name="女儿")
    await deps.repos.relation.update(r2, p2.id, mute=True)
    qid = await deps.repos.query.insert(queryer.id, "text", "转账", None)
    assert {r["id"] for r in await deps.repos.query.list_relations_for_query(qid)} == {r1, r2}
    vid = await deps.repos.verdict.insert(qid, Level.DANGEROUS, [], [], "reason", "reply", 1, Mode.MOCK)
    fanout = await deps.repos.alert.record_alerts_for_verdict(vid, qid)
    assert len(fanout["recipients"]) == 2
    alert1 = next(r for r in fanout["recipients"] if r["relation_id"] == r1)
    alert2 = next(r for r in fanout["recipients"] if r["relation_id"] == r2)
    assert await deps.repos.alert.push_context(alert1["alert_id"]) is not None
    assert await deps.repos.alert.push_context(alert2["alert_id"]) is None
    assert await deps.repos.alert.event_context(alert2["alert_id"]) is not None
    await deps.repos.relation.update(r1, p1.id, name="妈妈的新称呼")
    listed = await deps.repos.alert.list_for_user(p1.id)
    assert listed[0]["name_at_alert"] == "妈妈"
    assert await deps.relations.end(p1.id, r1) == "by_protector"
    detail, denial = await deps.repos.alert.detail_for_user(p1.id, alert1["alert_id"])
    assert detail is None and denial == "relation_ended"
    assert await deps.repos.alert.event_context(alert1["alert_id"]) is None
    assert await deps.repos.alert.list_for_user(p1.id) == []
    assert (await (await deps.conn.execute("SELECT COUNT(*) FROM pg_constraint WHERE contype='f'")).fetchone())[0] > 0


async def test_relation_added_after_intake_does_not_receive_old_query(deps):
    queryer = await deps.repos.users.get_or_create("snapshot:q")
    qid = await deps.repos.query.insert(queryer.id, "text", "danger", None)
    protector = await deps.repos.users.get_or_create("snapshot:p")
    await _link(deps, protector, queryer)
    vid = await deps.repos.verdict.insert(qid, Level.DANGEROUS, [], [], "reason", "reply", 1, Mode.MOCK)
    fanout = await deps.repos.alert.record_alerts_for_verdict(vid, qid)
    assert fanout["recipients"] == []
    assert (await (await deps.conn.execute("SELECT COUNT(*) FROM alert")).fetchone())[0] == 0


async def test_ended_snapshot_relation_and_late_replacement_do_not_receive_old_query(deps):
    queryer = await deps.repos.users.get_or_create("snapshot:ended-queryer")
    protector = await deps.repos.users.get_or_create("snapshot:ended-protector")
    old_relation, _ = await _link(deps, protector, queryer, "旧称呼")
    query_id = await deps.repos.query.insert(queryer.id, "text", "危险查询", None)
    assert await deps.relations.end(protector.id, old_relation) == "by_protector"
    new_relation, _ = await _link(deps, protector, queryer, "新称呼")
    verdict_id = await deps.repos.verdict.insert(query_id, Level.DANGEROUS, [], [], "reason", "reply", 1, Mode.MOCK)

    fanout = await deps.repos.alert.record_alerts_for_verdict(verdict_id, query_id)

    assert fanout["recipients"] == []
    assert (await deps.repos.relation.get(new_relation))["ended_at"] is None
    assert (await (await deps.conn.execute('SELECT COUNT(*) FROM alert WHERE verdict_id=%s', (verdict_id,))).fetchone())[0] == 0


async def test_alert_record_racing_with_relation_end_never_leaves_sendable_alert(deps):

    queryer = await deps.repos.users.get_or_create("alert-race:queryer")
    protector = await deps.repos.users.get_or_create("alert-race:protector")
    relation_id, _ = await _link(deps, protector, queryer, "家人")
    query_id = await deps.repos.query.insert(queryer.id, "text", "危险查询", None)
    verdict_id = await deps.repos.verdict.insert(query_id, Level.DANGEROUS, [], [], "reason", "reply", 1, Mode.MOCK)
    ready = 0
    gate = asyncio.Event()

    async def rendezvous():
        nonlocal ready
        ready += 1
        if ready == 2: gate.set()
        await gate.wait()

    async def record():
        await rendezvous()
        return await deps.repos.alert.record_alerts_for_verdict(verdict_id, query_id)

    async def end():
        await rendezvous()
        return await deps.relations.end(protector.id, relation_id)

    fanout, end_status = await asyncio.gather(record(), end())

    assert end_status == "by_protector"
    assert len(fanout["recipients"]) in (0, 1)
    alerts = await (await deps.conn.execute('SELECT id FROM alert WHERE verdict_id=%s', (verdict_id,))).fetchall()
    for alert in alerts:
        assert await deps.repos.alert.event_context(alert["id"]) is None
        assert await deps.repos.alert.push_context(alert["id"]) is None
