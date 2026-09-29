"""Concurrency invariants exercised by overlapping async repository calls."""
import asyncio

from homeshield.core.errors import DuplicateMessage
from homeshield.core.models import Level, Mode


async def test_concurrent_user_get_or_create_returns_one_user(deps):
    users = await asyncio.gather(*(deps.repos.users.get_or_create("pg:user-race") for _ in range(2)))
    assert users[0].id == users[1].id
    assert (await deps.repos.users.get_by_openid("pg:user-race")).id == users[0].id


async def test_concurrent_duplicate_message_maps_only_msg_id_constraint(deps, user):
    async def insert():
        try:
            return await deps.repos.query.insert(user.id, "text", "同一条消息", "pg-msg-race")
        except DuplicateMessage as exc:
            return exc

    results = await asyncio.gather(insert(), insert())
    assert sum(isinstance(result, int) for result in results) == 1
    duplicate = next(result for result in results if isinstance(result, DuplicateMessage))
    assert duplicate.msg_id == "pg-msg-race"
    assert (await deps.repos.query.find_by_msg_id("pg-msg-race"))["id"] == next(
        result for result in results if isinstance(result, int)
    )


async def test_concurrent_invite_claim_returns_created_and_used(deps):
    creator = await deps.repos.users.get_or_create("pg:invite-creator")
    protectors = await asyncio.gather(*(deps.repos.users.get_or_create(f"pg:invite-protected-{i}") for i in range(2)))
    invite = await deps.repos.invite.create(creator.id, "家人", 7)
    results = await asyncio.gather(*(
        deps.repos.invite.claim(invite["code"], person.id, 10) for person in protectors
    ))
    assert sorted(result[0] for result in results) == ["created", "used"]
    assert (await deps.repos.invite.get(invite["code"]))["used_at"] is not None


async def test_concurrent_distinct_invites_respect_relation_capacity(deps):
    creators = await asyncio.gather(*(
        deps.repos.users.get_or_create(f"pg:capacity-creator-{index}") for index in range(2)
    ))
    protected = await deps.repos.users.get_or_create("pg:capacity-protected")
    invites = [await deps.repos.invite.create(user.id, "家人", 7) for user in creators]

    async with deps.pool.connection() as conn:
        await conn.execute("DROP TRIGGER IF EXISTS test_pause_relation_insert ON guard_relation")
        await conn.execute("DROP FUNCTION IF EXISTS test_pause_relation_insert()")
        await conn.execute("""
            CREATE FUNCTION test_pause_relation_insert() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                PERFORM pg_sleep(0.1);
                RETURN NEW;
            END
            $$
        """)
        await conn.execute("""
            CREATE TRIGGER test_pause_relation_insert BEFORE INSERT ON guard_relation
            FOR EACH ROW EXECUTE FUNCTION test_pause_relation_insert()
        """)

    try:
        results = await asyncio.gather(*(
            deps.repos.invite.claim(invite["code"], protected.id, 1) for invite in invites
        ))
    finally:
        async with deps.pool.connection() as conn:
            await conn.execute("DROP TRIGGER IF EXISTS test_pause_relation_insert ON guard_relation")
            await conn.execute("DROP FUNCTION IF EXISTS test_pause_relation_insert()")

    assert sorted(result[0] for result in results) == ["created", "limit"]
    count = (await (await deps.conn.execute(
        "SELECT COUNT(*) FROM guard_relation WHERE protected_user_id=%s AND ended_at IS NULL",
        (protected.id,),
    )).fetchone())[0]
    assert count == 1


async def test_concurrent_alert_recording_is_idempotent(deps, relations):
    protected, _, relation_id = relations
    query_id = await deps.repos.query.insert(protected.id, "text", "危险消息", None)
    verdict_id = await deps.repos.verdict.insert(query_id, Level.DANGEROUS, [], [], "原因", "回复", 1, Mode.MOCK)
    results = await asyncio.gather(*(
        deps.repos.alert.record_alerts_for_verdict(verdict_id, query_id) for _ in range(2)
    ))
    assert all(result["recipients"][0]["relation_id"] == relation_id for result in results)
    count = (await (await deps.conn.execute("SELECT COUNT(*) FROM alert WHERE verdict_id=%s", (verdict_id,))).fetchone())[0]
    assert count == 1


async def test_concurrent_wecom_member_reassignment_keeps_unique_mapping(deps):
    users = await asyncio.gather(*(deps.repos.users.get_or_create(f"pg:wecom-{i}") for i in range(2)))
    results = await asyncio.gather(*(deps.repos.wecom_member.link(user.id, "CorpRace") for user in users))
    assert results == [None, None]
    mapped = await asyncio.gather(*(deps.repos.wecom_member.get(user.id) for user in users))
    assert mapped.count("CorpRace") == 1
    count = (await (await deps.conn.execute("SELECT COUNT(*) FROM wecom_member WHERE corp_userid=%s", ("CorpRace",))).fetchone())[0]
    assert count == 1
