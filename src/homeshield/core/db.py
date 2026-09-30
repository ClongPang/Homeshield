"""Postgres schema and connection-pool setup for the production data layer."""
from datetime import datetime, timezone
import json
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.errors import DuplicateDatabase
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

DB_POOL_MAX_SIZE = 10
DB_STATEMENT_TIMEOUT_MS = 5_000
SCHEMA_VERSION = 5
SCHEMA_INIT_LOCK_KEY = 0x484F4D455343484D  # Dedicated transaction advisory lock: "HOMESCHM".

_TIMESTAMP_FIELDS = {
    "created_at", "opened_at", "last_query_at", "closed_at", "ended_at", "expires_at",
    "used_at", "revoked_at", "delivered_at", "read_at", "queryer_feedback_at", "closes_at",
    "resolved_at", "voted_at", "verdict_at", "query_at", "current_created_at",
    "bound_at", "verified_at", "last_fail_at", "lease_until", "next_attempt_at", "sent_at",
}
_JSON_FIELDS = {"cited_ids", "features", "context_snapshot"}


def utc_epoch_dict_row(cursor):
    """Keep the app's existing Unix-second API while using timestamptz in Postgres."""
    make_dict = dict_row(cursor)

    def make_row(values):
        row = make_dict(values)
        for key, value in row.items():
            if key in _TIMESTAMP_FIELDS and isinstance(value, datetime):
                row[key] = int(value.astimezone(timezone.utc).timestamp())
            elif key in _JSON_FIELDS and value is not None and not isinstance(value, str):
                row[key] = json.dumps(value, ensure_ascii=False)
        return row

    return make_row


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS "user" (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    openid TEXT NOT NULL UNIQUE,
    token TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL,
    session_epoch BIGINT NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS session_reset_msg (
    msg_id TEXT PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES "user"(id),
    created_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS incident (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES "user"(id),
    opened_at TIMESTAMPTZ NOT NULL,
    last_query_at TIMESTAMPTZ NOT NULL,
    closed_at TIMESTAMPTZ,
    close_reason TEXT CHECK(close_reason IN ('timeout','explicit','manual'))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_open_incident ON incident(user_id) WHERE closed_at IS NULL;
CREATE TABLE IF NOT EXISTS query (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES "user"(id),
    content_type TEXT NOT NULL CHECK(content_type IN ('text','url','image')),
    content TEXT NOT NULL,
    msg_id TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    incident_id BIGINT REFERENCES incident(id),
    transcript TEXT,
    degraded_reply TEXT,
    kind TEXT NOT NULL DEFAULT 'query' CHECK(kind IN ('query','ack')),
    channel TEXT NOT NULL DEFAULT 'web' CHECK(channel IN ('web','wecom','legacy')),
    open_kfid TEXT,
    session_epoch_at_claim BIGINT,
    claim_token BIGINT,
    lease_until TIMESTAMPTZ,
    outcome_kind TEXT CHECK(outcome_kind IN ('verdict','degraded','ack')),
    delivery_notice TEXT NOT NULL DEFAULT '',
    CONSTRAINT ck_query_channel_delivery CHECK (
        (channel IN ('web','legacy') AND open_kfid IS NULL AND claim_token IS NULL AND lease_until IS NULL)
        OR (channel='wecom' AND NULLIF(msg_id,'') IS NOT NULL AND NULLIF(open_kfid,'') IS NOT NULL
            AND claim_token IS NOT NULL AND claim_token>=1 AND lease_until IS NOT NULL)
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_query_web_msg_id ON query(msg_id)
    WHERE channel='web' AND msg_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_query_wecom_msg_id ON query(open_kfid,msg_id)
    WHERE channel='wecom';
CREATE UNIQUE INDEX IF NOT EXISTS uq_query_legacy_msg_id ON query(msg_id)
    WHERE channel='legacy' AND msg_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_query_recovery ON query(lease_until)
    WHERE channel='wecom' AND outcome_kind IS NULL;
ALTER TABLE query ADD COLUMN IF NOT EXISTS degraded_reply TEXT;
CREATE INDEX IF NOT EXISTS ix_query_user_time ON query(user_id,created_at);
CREATE INDEX IF NOT EXISTS ix_query_incident ON query(incident_id);
CREATE TABLE IF NOT EXISTS guard_relation (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    protector_user_id BIGINT NOT NULL REFERENCES "user"(id),
    protected_user_id BIGINT NOT NULL REFERENCES "user"(id),
    name TEXT NOT NULL,
    inverse_name TEXT,
    mute BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ,
    end_reason TEXT CHECK(end_reason IN ('by_protector','by_protected')),
    CHECK(protector_user_id <> protected_user_id),
    CHECK((ended_at IS NULL) = (end_reason IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_relation
    ON guard_relation(protector_user_id,protected_user_id) WHERE ended_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_relation_protected_active ON guard_relation(protected_user_id,ended_at);
CREATE INDEX IF NOT EXISTS ix_relation_protector_active ON guard_relation(protector_user_id,ended_at);
CREATE TABLE IF NOT EXISTS invite_code (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    creator_user_id BIGINT NOT NULL REFERENCES "user"(id),
    name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ,
    used_by_user_id BIGINT REFERENCES "user"(id),
    revoked_at TIMESTAMPTZ,
    CHECK(NOT (used_at IS NOT NULL AND revoked_at IS NOT NULL)),
    CHECK((used_at IS NULL) = (used_by_user_id IS NULL))
);
CREATE INDEX IF NOT EXISTS ix_invite_creator ON invite_code(creator_user_id,id);
CREATE TABLE IF NOT EXISTS verdict (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    query_id BIGINT NOT NULL REFERENCES query(id),
    level TEXT NOT NULL CHECK(level IN ('safe','suspicious','dangerous')),
    cited_ids JSONB NOT NULL,
    features JSONB NOT NULL,
    reason TEXT NOT NULL,
    reply TEXT NOT NULL,
    latency_ms BIGINT NOT NULL,
    mode TEXT NOT NULL CHECK(mode IN ('mock','llm')),
    created_at TIMESTAMPTZ NOT NULL,
    context_snapshot JSONB
);
CREATE INDEX IF NOT EXISTS ix_verdict_query ON verdict(query_id,id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_verdict_query ON verdict(query_id);
CREATE TABLE IF NOT EXISTS query_relation (
    query_id BIGINT NOT NULL REFERENCES query(id),
    relation_id BIGINT NOT NULL REFERENCES guard_relation(id),
    PRIMARY KEY(query_id,relation_id)
);
CREATE TABLE IF NOT EXISTS alert (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    verdict_id BIGINT NOT NULL REFERENCES verdict(id),
    relation_id BIGINT NOT NULL REFERENCES guard_relation(id),
    name_at_alert TEXT NOT NULL,
    delivered_at TIMESTAMPTZ NOT NULL,
    read_at TIMESTAMPTZ,
    CONSTRAINT uq_alert_verdict_relation UNIQUE(verdict_id,relation_id)
);
CREATE INDEX IF NOT EXISTS ix_alert_relation_time ON alert(relation_id,delivered_at);
CREATE TABLE IF NOT EXISTS outbound (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('reply','push')),
    query_id BIGINT NOT NULL REFERENCES query(id),
    alert_id BIGINT REFERENCES alert(id),
    to_user_id BIGINT NOT NULL REFERENCES "user"(id),
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','leased','accepted','skipped','failed')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0),
    lease_token BIGINT NOT NULL DEFAULT 0,
    lease_until TIMESTAMPTZ,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at TIMESTAMPTZ,
    last_error TEXT,
    CHECK((kind='reply') = (alert_id IS NULL)),
    CHECK((state='accepted') = (sent_at IS NOT NULL)),
    CHECK((state='leased') = (lease_until IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_outbound_reply ON outbound(query_id) WHERE kind='reply';
CREATE UNIQUE INDEX IF NOT EXISTS uq_outbound_push ON outbound(alert_id) WHERE kind='push';
CREATE INDEX IF NOT EXISTS ix_outbound_pending ON outbound(next_attempt_at,id) WHERE state='pending';
CREATE INDEX IF NOT EXISTS ix_outbound_expired ON outbound(lease_until,id) WHERE state='leased';
CREATE TABLE IF NOT EXISTS correction_case (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    verdict_id BIGINT NOT NULL UNIQUE REFERENCES verdict(id),
    queryer_label TEXT CHECK(queryer_label IN ('real','false_positive')),
    queryer_note TEXT NOT NULL DEFAULT '',
    queryer_feedback_at TIMESTAMPTZ,
    status TEXT NOT NULL CHECK(status IN ('pending','confirmed','no_consensus')),
    resolved_label TEXT CHECK(resolved_label IN ('real','false_positive')),
    opened_at TIMESTAMPTZ NOT NULL,
    closes_at TIMESTAMPTZ NOT NULL,
    resolved_at TIMESTAMPTZ,
    CHECK(closes_at > opened_at),
    CHECK((queryer_label IS NULL) = (queryer_feedback_at IS NULL)),
    CHECK((status = 'confirmed') = (resolved_label IS NOT NULL)),
    CHECK((status = 'pending') = (resolved_at IS NULL))
);
CREATE INDEX IF NOT EXISTS ix_correction_case_status_closes ON correction_case(status,closes_at);
CREATE TABLE IF NOT EXISTS correction_vote (
    case_id BIGINT NOT NULL REFERENCES correction_case(id),
    relation_id BIGINT NOT NULL REFERENCES guard_relation(id),
    label TEXT CHECK(label IN ('real','false_positive')),
    voted_at TIMESTAMPTZ,
    PRIMARY KEY(case_id,relation_id),
    CHECK((label IS NULL) = (voted_at IS NULL))
);
CREATE INDEX IF NOT EXISTS ix_correction_vote_relation ON correction_vote(relation_id,case_id);
CREATE TABLE IF NOT EXISTS wecom_member (
    user_id BIGINT PRIMARY KEY REFERENCES "user"(id),
    corp_userid TEXT NOT NULL UNIQUE,
    bound_via TEXT NOT NULL DEFAULT 'cli',
    bound_at TIMESTAMPTZ,
    verified_at TIMESTAMPTZ,
    last_fail_at TIMESTAMPTZ,
    last_fail_reason TEXT
);
ALTER TABLE wecom_member ADD COLUMN IF NOT EXISTS bound_via TEXT NOT NULL DEFAULT 'cli';
ALTER TABLE wecom_member ADD COLUMN IF NOT EXISTS bound_at TIMESTAMPTZ;
ALTER TABLE wecom_member ADD COLUMN IF NOT EXISTS verified_at TIMESTAMPTZ;
ALTER TABLE wecom_member ADD COLUMN IF NOT EXISTS last_fail_at TIMESTAMPTZ;
ALTER TABLE wecom_member ADD COLUMN IF NOT EXISTS last_fail_reason TEXT;
CREATE TABLE IF NOT EXISTS kf_cursor (
    kfid TEXT PRIMARY KEY,
    cursor TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def make_pool(database_url: str) -> AsyncConnectionPool:
    conninfo = make_conninfo(
        database_url,
        options=f"-c timezone=UTC -c statement_timeout={DB_STATEMENT_TIMEOUT_MS}",
    )
    return AsyncConnectionPool(
        conninfo,
        min_size=1,
        max_size=DB_POOL_MAX_SIZE,
        timeout=5,
        kwargs={"row_factory": utc_epoch_dict_row},
        open=False,
    )


async def ensure_database(database_url: str) -> None:
    """Create the target database on an existing server if missing (eval/scratch workflows).

    服务启动不走这里:生产库应显式创建,避免连错口令或库名时静默新建空库。
    """
    parts = urlsplit(database_url)
    dbname = parts.path.rsplit("/", 1)[-1]
    admin_url = urlunsplit((parts.scheme, parts.netloc, "/postgres", parts.query, parts.fragment))
    try:
        async with await psycopg.AsyncConnection.connect(admin_url, autocommit=True) as conn:
            await conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
    except DuplicateDatabase:
        pass


async def _apply_schema(conn) -> None:
    for statement in SCHEMA.split(";"):
        if statement.strip():
            await conn.execute(statement)


async def _query_has_channel_column(conn) -> bool:
    row = await (await conn.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() "
        "AND table_name='query' AND column_name='channel'"
    )).fetchone()
    return row is not None


async def init_schema(pool: AsyncConnectionPool) -> None:
    if pool.closed:
        await pool.open(wait=True)
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_INIT_LOCK_KEY,))
        old = await (await conn.execute(
            "SELECT to_regclass('query') IS NOT NULL AS present"
        )).fetchone()
        if old["present"] and not await _query_has_channel_column(conn):
            raise RuntimeError("schema v4 requires homeshield-cli migrate-crash-recovery before startup")
        await _apply_schema(conn)
        await conn.execute(
            "INSERT INTO schema_version(version) VALUES(%s) ON CONFLICT(version) DO NOTHING",
            (SCHEMA_VERSION,),
        )


async def migrate_crash_recovery(pool: AsyncConnectionPool) -> None:
    """Explicit v4->v5 maintenance migration; callers must stop old application workers."""
    if pool.closed:
        await pool.open(wait=True)
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_INIT_LOCK_KEY,))
        await conn.execute("SET LOCAL statement_timeout=0")
        existing = await (await conn.execute("SELECT to_regclass('query') IS NOT NULL AS present")).fetchone()
        if not existing["present"]:
            await _apply_schema(conn)
        else:
            if not await _query_has_channel_column(conn):
                bad = await (await conn.execute("""
                    SELECT q.id FROM query q LEFT JOIN verdict v ON v.query_id=q.id
                    GROUP BY q.id HAVING COUNT(v.id)>1
                       OR (COUNT(v.id)>0 AND NULLIF(MAX(q.degraded_reply),'') IS NOT NULL)
                       OR (MAX(q.kind)='ack' AND
                           (COUNT(v.id)>0 OR NULLIF(MAX(q.degraded_reply),'') IS NOT NULL))
                    LIMIT 10
                """)).fetchall()
                if bad:
                    raise RuntimeError(f"historical query outcomes conflict: {[r['id'] for r in bad]}")
                await conn.execute("ALTER TABLE query ADD COLUMN channel TEXT NOT NULL DEFAULT 'legacy'")
                await conn.execute("ALTER TABLE query ALTER COLUMN channel SET DEFAULT 'web'")
                await conn.execute("ALTER TABLE query ADD COLUMN open_kfid TEXT")
                await conn.execute("ALTER TABLE query ADD COLUMN session_epoch_at_claim BIGINT")
                await conn.execute("ALTER TABLE query ADD COLUMN claim_token BIGINT")
                await conn.execute("ALTER TABLE query ADD COLUMN lease_until TIMESTAMPTZ")
                await conn.execute("ALTER TABLE query ADD COLUMN outcome_kind TEXT")
                await conn.execute("ALTER TABLE query ADD COLUMN delivery_notice TEXT NOT NULL DEFAULT ''")
                await conn.execute("ALTER TABLE query ADD CONSTRAINT ck_query_channel CHECK(channel IN ('web','wecom','legacy'))")
                await conn.execute("ALTER TABLE query ADD CONSTRAINT ck_query_outcome CHECK(outcome_kind IN ('verdict','degraded','ack'))")
                await conn.execute("""ALTER TABLE query ADD CONSTRAINT ck_query_channel_delivery CHECK (
                    (channel IN ('web','legacy') AND open_kfid IS NULL AND claim_token IS NULL AND lease_until IS NULL)
                    OR (channel='wecom' AND NULLIF(msg_id,'') IS NOT NULL AND NULLIF(open_kfid,'') IS NOT NULL
                        AND claim_token IS NOT NULL AND claim_token>=1 AND lease_until IS NOT NULL))""")
                await conn.execute("""UPDATE query q SET outcome_kind=CASE
                    WHEN EXISTS(SELECT 1 FROM verdict v WHERE v.query_id=q.id) THEN 'verdict'
                    WHEN NULLIF(q.degraded_reply,'') IS NOT NULL THEN 'degraded'
                    WHEN q.kind='ack' THEN 'ack' ELSE NULL END""")
            await _apply_schema(conn)
            await conn.execute("DROP INDEX IF EXISTS uq_query_msg_id")
        await conn.execute("INSERT INTO schema_version(version) VALUES(%s) ON CONFLICT DO NOTHING", (SCHEMA_VERSION,))
