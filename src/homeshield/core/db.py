"""SQLite 连接与 schema。

时间戳一律 INTEGER(unix 秒);外键开启;WAL 模式。
"""
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS family(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS member(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    family_id INTEGER NOT NULL REFERENCES family(id),
    name TEXT NOT NULL,
    trusted INTEGER NOT NULL DEFAULT 0,   -- 纠正信任位:1=纠正即时生效+可管理成员;0=纠正需信任成员确认
    openid TEXT UNIQUE,                -- 微信零注册映射
    token TEXT UNIQUE,                 -- 个人链接凭证
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS query(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    family_id INTEGER NOT NULL REFERENCES family(id),
    member_id INTEGER NOT NULL REFERENCES member(id),
    content_type TEXT NOT NULL CHECK(content_type IN ('text','url','image')),
    content TEXT NOT NULL,
    msg_id TEXT,                       -- 幂等键
    created_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_query_msg_id
    ON query(msg_id) WHERE msg_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS verdict(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query_id INTEGER NOT NULL REFERENCES query(id),
    level TEXT NOT NULL CHECK(level IN ('safe','suspicious','dangerous')),
    cited_ids TEXT NOT NULL,           -- JSON 数组
    features TEXT NOT NULL,            -- 全量特征快照(JSON)
    reason TEXT NOT NULL,
    reply TEXT NOT NULL,
    latency_ms INTEGER NOT NULL,
    mode TEXT NOT NULL CHECK(mode IN ('mock','llm')),
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS alert(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    verdict_id INTEGER NOT NULL REFERENCES verdict(id),
    member_id INTEGER NOT NULL REFERENCES member(id),
    delivered_at INTEGER NOT NULL,
    read_at INTEGER
);
CREATE TABLE IF NOT EXISTS correction(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    verdict_id INTEGER NOT NULL REFERENCES verdict(id),
    by_member_id INTEGER NOT NULL REFERENCES member(id),
    label TEXT NOT NULL CHECK(label IN ('real','false_positive','confirmed_scam')),
    note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK(status IN ('pending','confirmed','rejected')),
    decided_by INTEGER,
    created_at INTEGER NOT NULL,
    decided_at INTEGER
);
CREATE TABLE IF NOT EXISTS bind_code(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,         -- 8 位无歧义大写字母数字(公众号回复"绑定 <码>")
    member_id INTEGER NOT NULL REFERENCES member(id),
    created_by INTEGER REFERENCES member(id),
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    used_at INTEGER                    -- 一次性:领取即写时间戳
);
"""


def connect(path: str) -> sqlite3.Connection:
    # 创建一个为 FastAPI 应用定制的 SQLite 连接
    # FastAPI 的同步（def 而非 async def）端点会被丢进线程池执行，每次请求可能由不同线程处理
    # 如果整个应用共享一个全局连接，势必跨线程访问 → 必须设 check_same_thread=False
    conn = sqlite3.connect(path, check_same_thread=False) # 允许跨线程使用连接，同时不存在时创建
    conn.row_factory = sqlite3.Row # 行对象支持按列名访问，对 FastAPI 特别有用——转成 dict 后可以直接做 JSON 响应
    conn.execute("PRAGMA foreign_keys=ON") # 开启外键约束
    conn.execute("PRAGMA journal_mode=WAL") # 开启 WAL 日志模式
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
