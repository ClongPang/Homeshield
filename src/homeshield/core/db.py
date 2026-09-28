"""SQLite schema. Timestamps are Unix seconds and foreign keys stay enabled."""
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS user(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    openid TEXT NOT NULL UNIQUE,
    token TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL,
    session_epoch INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS session_reset_msg(
    msg_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES user(id),
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS incident(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES user(id),
    opened_at INTEGER NOT NULL,
    last_query_at INTEGER NOT NULL,
    closed_at INTEGER,
    close_reason TEXT CHECK(close_reason IN ('timeout','explicit','manual'))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_open_incident ON incident(user_id) WHERE closed_at IS NULL;
CREATE TABLE IF NOT EXISTS query(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES user(id),
    content_type TEXT NOT NULL CHECK(content_type IN ('text','url','image')),
    content TEXT NOT NULL,
    msg_id TEXT,
    created_at INTEGER NOT NULL,
    incident_id INTEGER REFERENCES incident(id),
    transcript TEXT,
    kind TEXT NOT NULL DEFAULT 'query' CHECK(kind IN ('query','ack'))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_query_msg_id ON query(msg_id) WHERE msg_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_query_user_time ON query(user_id,created_at);
CREATE INDEX IF NOT EXISTS ix_query_incident ON query(incident_id);
CREATE TABLE IF NOT EXISTS guard_relation(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    protector_user_id INTEGER NOT NULL REFERENCES user(id),
    protected_user_id INTEGER NOT NULL REFERENCES user(id),
    name TEXT NOT NULL,
    inverse_name TEXT,
    mute INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    ended_at INTEGER,
    end_reason TEXT CHECK(end_reason IN ('by_protector','by_protected')),
    CHECK(protector_user_id <> protected_user_id),
    CHECK((ended_at IS NULL) = (end_reason IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_relation
    ON guard_relation(protector_user_id,protected_user_id) WHERE ended_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_relation_protected_active ON guard_relation(protected_user_id,ended_at);
CREATE INDEX IF NOT EXISTS ix_relation_protector_active ON guard_relation(protector_user_id,ended_at);
CREATE TABLE IF NOT EXISTS invite_code(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    creator_user_id INTEGER NOT NULL REFERENCES user(id),
    name TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    used_at INTEGER,
    used_by_user_id INTEGER REFERENCES user(id),
    revoked_at INTEGER,
    CHECK(NOT (used_at IS NOT NULL AND revoked_at IS NOT NULL)),
    CHECK((used_at IS NULL) = (used_by_user_id IS NULL))
);
CREATE INDEX IF NOT EXISTS ix_invite_creator ON invite_code(creator_user_id,id);
CREATE TABLE IF NOT EXISTS verdict(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query_id INTEGER NOT NULL REFERENCES query(id),
    level TEXT NOT NULL CHECK(level IN ('safe','suspicious','dangerous')),
    cited_ids TEXT NOT NULL,
    features TEXT NOT NULL,
    reason TEXT NOT NULL,
    reply TEXT NOT NULL,
    latency_ms INTEGER NOT NULL,
    mode TEXT NOT NULL CHECK(mode IN ('mock','llm')),
    created_at INTEGER NOT NULL,
    context_snapshot TEXT
);
CREATE INDEX IF NOT EXISTS ix_verdict_query ON verdict(query_id,id);
CREATE TABLE IF NOT EXISTS query_relation(
    query_id INTEGER NOT NULL REFERENCES query(id),
    relation_id INTEGER NOT NULL REFERENCES guard_relation(id),
    PRIMARY KEY(query_id,relation_id)
);
CREATE TABLE IF NOT EXISTS alert(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    verdict_id INTEGER NOT NULL REFERENCES verdict(id),
    relation_id INTEGER NOT NULL REFERENCES guard_relation(id),
    name_at_alert TEXT NOT NULL,
    delivered_at INTEGER NOT NULL,
    read_at INTEGER,
    UNIQUE(verdict_id,relation_id)
);
CREATE INDEX IF NOT EXISTS ix_alert_relation_time ON alert(relation_id,delivered_at);
CREATE TABLE IF NOT EXISTS correction_case(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    verdict_id INTEGER NOT NULL UNIQUE REFERENCES verdict(id),
    queryer_label TEXT CHECK(queryer_label IN ('real','false_positive')),
    queryer_note TEXT NOT NULL DEFAULT '',
    queryer_feedback_at INTEGER,
    status TEXT NOT NULL CHECK(status IN ('pending','confirmed','no_consensus')),
    resolved_label TEXT CHECK(resolved_label IN ('real','false_positive')),
    opened_at INTEGER NOT NULL,
    closes_at INTEGER NOT NULL,
    resolved_at INTEGER,
    CHECK(closes_at > opened_at),
    CHECK((queryer_label IS NULL) = (queryer_feedback_at IS NULL)),
    CHECK((status = 'confirmed') = (resolved_label IS NOT NULL)),
    CHECK((status = 'pending') = (resolved_at IS NULL))
);
CREATE INDEX IF NOT EXISTS ix_correction_case_status_closes ON correction_case(status,closes_at);
CREATE TABLE IF NOT EXISTS correction_vote(
    case_id INTEGER NOT NULL REFERENCES correction_case(id),
    relation_id INTEGER NOT NULL REFERENCES guard_relation(id),
    label TEXT CHECK(label IN ('real','false_positive')),
    voted_at INTEGER,
    PRIMARY KEY(case_id,relation_id),
    CHECK((label IS NULL) = (voted_at IS NULL))
);
CREATE INDEX IF NOT EXISTS ix_correction_vote_relation ON correction_vote(relation_id,case_id);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)          # 数据库的幂等初始化
    conn.commit()
