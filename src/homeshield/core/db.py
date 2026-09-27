"""SQLite 连接与 schema。时间戳为 Unix 秒;外键开启。"""
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
CREATE TABLE IF NOT EXISTS protection_group(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    created_by_user_id INTEGER REFERENCES user(id),
    created_at INTEGER NOT NULL,
    disbanded_at INTEGER
);
CREATE TABLE IF NOT EXISTS member(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL REFERENCES protection_group(id),
    user_id INTEGER REFERENCES user(id),
    name TEXT NOT NULL,
    trusted INTEGER NOT NULL DEFAULT 0,
    mute INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    ended_at INTEGER,
    end_reason TEXT CHECK(end_reason IN ('left','removed','disbanded')),
    CHECK((ended_at IS NULL) = (end_reason IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_member
    ON member(group_id,user_id) WHERE user_id IS NOT NULL AND ended_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_member_user_active ON member(user_id,ended_at);
CREATE INDEX IF NOT EXISTS ix_member_group_active ON member(group_id,ended_at);
CREATE TABLE IF NOT EXISTS bind_code(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    member_id INTEGER NOT NULL REFERENCES member(id),
    created_by INTEGER REFERENCES member(id),
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    used_at INTEGER
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
CREATE TABLE IF NOT EXISTS query_group(
    query_id INTEGER NOT NULL REFERENCES query(id),
    group_id INTEGER NOT NULL REFERENCES protection_group(id),
    query_member_id INTEGER NOT NULL REFERENCES member(id),
    PRIMARY KEY(query_id,group_id)
);
CREATE INDEX IF NOT EXISTS ix_query_group_id ON query_group(group_id,query_id);
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
CREATE TABLE IF NOT EXISTS alert(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    verdict_id INTEGER NOT NULL REFERENCES verdict(id),
    membership_id INTEGER NOT NULL REFERENCES member(id),
    group_name_at_alert TEXT NOT NULL,
    delivered_at INTEGER NOT NULL,
    read_at INTEGER,
    UNIQUE(verdict_id,membership_id)
);
CREATE INDEX IF NOT EXISTS ix_alert_member_time ON alert(membership_id,delivered_at);
CREATE TABLE IF NOT EXISTS correction(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    verdict_id INTEGER NOT NULL REFERENCES verdict(id),
    by_user_id INTEGER NOT NULL REFERENCES user(id),
    label TEXT NOT NULL CHECK(label IN ('real','false_positive')),
    note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK(status IN ('pending','confirmed','rejected')),
    decided_by_membership_id INTEGER REFERENCES member(id),
    created_at INTEGER NOT NULL,
    decided_at INTEGER,
    UNIQUE(verdict_id,by_user_id)
);
CREATE TABLE IF NOT EXISTS correction_group(
    correction_id INTEGER NOT NULL REFERENCES correction(id),
    group_id INTEGER NOT NULL REFERENCES protection_group(id),
    by_membership_id INTEGER NOT NULL REFERENCES member(id),
    PRIMARY KEY(correction_id,group_id)
);
CREATE INDEX IF NOT EXISTS ix_correction_group_id ON correction_group(group_id,correction_id);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
