"""The explicit maintenance migration preserves historical rows and the KF cursor."""
import uuid
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql

from homeshield.core.config import Settings
from homeshield.core.db import init_schema, make_pool, migrate_crash_recovery


async def test_v4_migration_marks_history_legacy_and_keeps_cursor():
    base = urlsplit(Settings.load().database_url)
    name = "homeshield_migration_" + uuid.uuid4().hex[:12]
    database_url = urlunsplit(base._replace(path="/" + name))
    admin_url = urlunsplit(base._replace(path="/postgres"))
    async with await psycopg.AsyncConnection.connect(admin_url, autocommit=True) as admin:
        await admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    pool = make_pool(database_url)
    try:
        await pool.open(wait=True)
        async with pool.connection() as conn:
            await conn.execute("""
                CREATE TABLE "user" (
                    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    openid TEXT NOT NULL UNIQUE,token TEXT NOT NULL UNIQUE,
                    created_at TIMESTAMPTZ NOT NULL,session_epoch BIGINT NOT NULL DEFAULT 0);
                CREATE TABLE query (
                    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES "user"(id),
                    content_type TEXT NOT NULL,content TEXT NOT NULL,msg_id TEXT,
                    created_at TIMESTAMPTZ NOT NULL,incident_id BIGINT,
                    transcript TEXT,degraded_reply TEXT,
                    kind TEXT NOT NULL DEFAULT 'query');
                CREATE UNIQUE INDEX uq_query_msg_id ON query(msg_id) WHERE msg_id IS NOT NULL;
                CREATE TABLE verdict (
                    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    query_id BIGINT NOT NULL REFERENCES query(id),level TEXT NOT NULL,
                    cited_ids JSONB NOT NULL,features JSONB NOT NULL,reason TEXT NOT NULL,
                    reply TEXT NOT NULL,latency_ms BIGINT NOT NULL,mode TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL,context_snapshot JSONB);
                CREATE TABLE kf_cursor (kfid TEXT PRIMARY KEY,cursor TEXT NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
                INSERT INTO "user"(openid,token,created_at) VALUES('wxkf:old','old-token',now());
                INSERT INTO query(user_id,content_type,content,msg_id,created_at,kind,degraded_reply)
                  VALUES(1,'text','old verdict','old-v',now(),'query',NULL),
                        (1,'text','old degraded','old-d',now(),'query','原降级回复'),
                        (1,'text','old ack','old-a',now(),'ack',NULL),
                        (1,'text','orphan','old-o',now(),'query',NULL);
                INSERT INTO verdict(query_id,level,cited_ids,features,reason,reply,latency_ms,mode,created_at)
                  VALUES(1,'safe','[]','[]','理由','回复',1,'mock',now());
                INSERT INTO kf_cursor(kfid,cursor) VALUES('kf-old','cursor-old');
            """)
        await migrate_crash_recovery(pool)
        await init_schema(pool)
        async with pool.connection() as conn:
            rows = await (await conn.execute(
                "SELECT channel,outcome_kind,claim_token,lease_until FROM query ORDER BY id"
            )).fetchall()
            assert [row["channel"] for row in rows] == ["legacy"] * 4
            assert [row["outcome_kind"] for row in rows] == ["verdict", "degraded", "ack", None]
            assert all(row["claim_token"] is None and row["lease_until"] is None for row in rows)
            cursor = await (await conn.execute("SELECT cursor FROM kf_cursor WHERE kfid='kf-old'")).fetchone()
            assert cursor["cursor"] == "cursor-old"
            old_index = await (await conn.execute("SELECT to_regclass('uq_query_msg_id') old")).fetchone()
            assert old_index["old"] is None
            await conn.execute(
                "INSERT INTO query(user_id,content_type,content,msg_id,created_at,kind) "
                "VALUES(1,'text','new web','new-web',now(),'query')"
            )
            row = await (await conn.execute("SELECT channel FROM query WHERE msg_id='new-web'")).fetchone()
            assert row["channel"] == "web"
    finally:
        await pool.close()
        async with await psycopg.AsyncConnection.connect(admin_url, autocommit=True) as admin:
            await admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
