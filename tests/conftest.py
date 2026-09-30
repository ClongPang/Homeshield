"""Postgres-backed async fixtures isolated with TRUNCATE between tests."""
from dataclasses import replace
import os
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql
from psycopg.errors import DuplicateDatabase
import pytest
import pytest_asyncio

from homeshield.core.config import Settings
from homeshield.core.db import init_schema, make_pool, migrate_crash_recovery, utc_epoch_dict_row
from homeshield.core.deps import build_deps

def _test_url(production_url: str) -> str:
    configured = os.environ.get("TEST_DATABASE_URL")
    parts = urlsplit(configured or production_url)
    db_name = parts.path.rsplit("/", 1)[-1]
    if not configured:
        db_name += "_test"
        parts = parts._replace(path="/" + db_name)
    production_name = urlsplit(production_url).path.rsplit("/", 1)[-1]
    if db_name == production_name:
        raise RuntimeError("TEST_DATABASE_URL must use a separate database from DATABASE_URL")
    admin_url = urlunsplit((parts.scheme, parts.netloc, "/postgres", parts.query, parts.fragment))
    with psycopg.connect(admin_url, autocommit=True) as admin:
        try:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name)))
        except DuplicateDatabase:
            pass
    resolved = urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, parts.fragment))
    os.environ["DATABASE_URL"] = resolved
    return resolved


_TEST_DATABASE_URL = _test_url(Settings.load().database_url)


class HybridRow(dict):
    """Support assertions by column name and positional index."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)


def _test_row_factory(cursor):
    make_row = utc_epoch_dict_row(cursor)

    def hybrid(values):
        return HybridRow(make_row(values))

    return hybrid


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def pg_pool():
    pool = make_pool(_TEST_DATABASE_URL)
    await pool.open(wait=True)
    await migrate_crash_recovery(pool)
    await init_schema(pool)
    yield pool
    await pool.close()


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def clean_postgres(pg_pool):
    async with pg_pool.connection() as conn:
        await conn.execute(
            'TRUNCATE correction_vote, correction_case, outbound, alert, query_relation, verdict, query, '
            'invite_code, guard_relation, incident, session_reset_msg, wecom_member, kf_cursor, "user" '
            'RESTART IDENTITY CASCADE'
        )


@pytest.fixture
def settings() -> Settings:
    return replace(Settings.load(), mode="mock")


@pytest_asyncio.fixture(loop_scope="session")
async def deps(settings, pg_pool):
    result = build_deps(settings, pool=pg_pool)
    conn = await pg_pool.getconn()
    conn.row_factory = _test_row_factory
    result.conn = conn  # Raw SQL is test-only; production repositories expose only the pool.
    yield result
    await result.recovery.close()
    await pg_pool.putconn(conn)


@pytest_asyncio.fixture(loop_scope="session")
async def user(deps):
    return await deps.repos.users.get_or_create("test:queryer")


@pytest_asyncio.fixture(loop_scope="session")
async def relations(deps):
    protector = await deps.repos.users.get_or_create("test:protector")
    protected = await deps.repos.users.get_or_create("test:protected")
    invite = await deps.relations.issue_invite(protector.id, "妈妈")
    _, relation_id, _ = await deps.relations.join(protected.openid, invite["code"])
    await deps.repos.relation.update(relation_id, protected.id, inverse_name="儿子")
    return protected, protector, relation_id


async def ingest_user(repos, user, **kwargs):
    from homeshield.core.intake import ingest

    return await ingest(repos, user_id=user.id, **kwargs)
