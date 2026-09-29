"""Postgres schema contract checks required by the storage specification."""
import asyncio

import pytest

from homeshield.core.db import SCHEMA_VERSION, init_schema
from homeshield.core.errors import DuplicateMessage


async def _schema_fingerprint(pool):
    async with pool.connection() as conn:
        columns = await (await conn.execute(
            "SELECT table_name, column_name, data_type, is_nullable, column_default, "
            "is_identity, identity_generation FROM information_schema.columns "
            "WHERE table_schema = current_schema() ORDER BY table_name, ordinal_position"
        )).fetchall()
        constraints = await (await conn.execute(
            "SELECT rel.relname, con.conname, con.contype, pg_get_constraintdef(con.oid) AS definition "
            "FROM pg_constraint con JOIN pg_class rel ON rel.oid = con.conrelid "
            "JOIN pg_namespace ns ON ns.oid = rel.relnamespace "
            "WHERE ns.nspname = current_schema() ORDER BY rel.relname, con.conname"
        )).fetchall()
        indexes = await (await conn.execute(
            "SELECT tablename, indexname, indexdef FROM pg_indexes "
            "WHERE schemaname = current_schema() ORDER BY tablename, indexname"
        )).fetchall()
        versions = await (await conn.execute(
            "SELECT version, applied_at FROM schema_version ORDER BY version"
        )).fetchall()
    return columns, constraints, indexes, versions


async def test_schema_init_is_idempotent_and_records_version(pg_pool):
    await init_schema(pg_pool)
    before = await _schema_fingerprint(pg_pool)

    await init_schema(pg_pool)
    after = await _schema_fingerprint(pg_pool)

    assert after == before
    assert max(row["version"] for row in after[3]) == SCHEMA_VERSION


async def test_concurrent_schema_init_is_serialized(pg_pool):
    await asyncio.gather(*(init_schema(pg_pool) for _ in range(4)))


async def test_partial_message_id_index_allows_null_but_rejects_duplicate(deps, user):
    await deps.repos.query.insert(user.id, "text", "无消息 ID 一", None)
    await deps.repos.query.insert(user.id, "text", "无消息 ID 二", None)
    await deps.repos.query.insert(user.id, "text", "带消息 ID", "schema-msg-id")

    with pytest.raises(DuplicateMessage, match="schema-msg-id"):
        await deps.repos.query.insert(user.id, "text", "重复消息 ID", "schema-msg-id")

    async with deps.pool.connection() as conn:
        rows = await (await conn.execute(
            "SELECT COUNT(*) AS n FROM query WHERE msg_id IS NULL"
        )).fetchone()
    assert rows["n"] == 2
