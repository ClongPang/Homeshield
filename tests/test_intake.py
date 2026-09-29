"""Query intake is private and snapshots zero or more incoming relations."""
import pytest
from psycopg.errors import RaiseException

from homeshield.core.intake import ingest
from homeshield.core.models import ContentType


async def test_normalize_idempotency_and_bare_query(deps, user):
    one = await ingest(deps.repos, user_id=user.id, content="别告诉家人,立即转账5万", msg_id="M1")
    assert not one.duplicate and one.message.content_type is ContentType.TEXT
    assert one.message.relation_ids == []
    two = await ingest(deps.repos, user_id=user.id, content="重复", msg_id="M1")
    assert two.duplicate and two.query_id == one.query_id


async def test_url_detection_empty_content_and_image(deps, user):
    url = await ingest(deps.repos, user_id=user.id, content="看这个 http://a.cn/x")
    assert url.message.content_type is ContentType.URL
    image = await ingest(deps.repos, user_id=user.id, content="AAAA", content_type="image")
    assert image.message.content_type is ContentType.IMAGE
    with pytest.raises(ValueError, match="empty content"):
        await ingest(deps.repos, user_id=user.id, content="   ")


async def test_snapshot_is_fixed_at_query_acceptance(deps, relations):
    protected, protector, relation_id = relations
    first = await ingest(deps.repos, user_id=protected.id, content="before link")
    assert first.message.relation_ids == [relation_id]
    await deps.repos.relation.end(relation_id, protector.id)
    second = await ingest(deps.repos, user_id=protected.id, content="after end")
    assert second.message.relation_ids == []
    assert (await deps.repos.query.list_relations_for_query(first.query_id))[0]["id"] == relation_id


async def test_query_and_relation_snapshot_roll_back_together(deps, relations):
    protected, _, _ = relations
    trigger = "test_reject_query_relation_insert"
    function = "test_reject_query_relation_insert_fn"
    async with deps.pool.connection() as conn, conn.transaction():
        await conn.execute(f"DROP TRIGGER IF EXISTS {trigger} ON query_relation")
        await conn.execute(f"DROP FUNCTION IF EXISTS {function}()")
        await conn.execute(
            f"CREATE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'forced query_relation failure'; END $$"
        )
        await conn.execute(
            f"CREATE TRIGGER {trigger} BEFORE INSERT ON query_relation "
            f"FOR EACH ROW EXECUTE FUNCTION {function}()"
        )
    try:
        with pytest.raises(RaiseException, match="forced query_relation failure"):
            await deps.repos.query.insert(
                protected.id, "text", "atomic-ingest-failure", "atomic-ingest-failure"
            )
    finally:
        async with deps.pool.connection() as conn, conn.transaction():
            await conn.execute(f"DROP TRIGGER IF EXISTS {trigger} ON query_relation")
            await conn.execute(f"DROP FUNCTION IF EXISTS {function}()")

    assert await deps.repos.query.find_by_msg_id("atomic-ingest-failure") is None
