"""离线库 schema 与连接。独立于 core/db.py 的运行时库,二者不共享文件。

时间戳 INTEGER(unix 秒);外键开启。原始语料表 fr_case 受 .gitignore 保护
(data/raw/ 与 *.db 均不入库),只有审核后的产出物进入版本库。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

OFFLINE_DB_PATH = "data/kb_build.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS fr_case(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fr_id INTEGER NOT NULL,            -- Fraud-R1 原始 id
    split TEXT NOT NULL CHECK(split IN ('base','levelup')),
    level INTEGER NOT NULL CHECK(level BETWEEN 0 AND 3),  -- 0=base,1~3=增强轮(C/U/E)
    lang TEXT NOT NULL CHECK(lang IN ('zh','en')),
    category TEXT NOT NULL,            -- 5 大类(原文小写)
    subcategory TEXT NOT NULL DEFAULT '',
    data_type TEXT NOT NULL DEFAULT '',
    role_bg TEXT NOT NULL DEFAULT '',  -- levelup 角色背景,JSON 字符串
    text TEXT NOT NULL,                -- 判定正文:base=generated text,levelup=对应轮
    raw_seed TEXT NOT NULL DEFAULT '', -- 真实种子仅存参考,禁止进入评测导出
    provenance TEXT NOT NULL,
    license_note TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(fr_id, level, lang)
);
CREATE INDEX IF NOT EXISTS idx_fr_case_lang_level ON fr_case(lang, level);

-- 层2:战术挖掘(FraudShield Step1 工艺,fr-mine 批抽产物,当前为空表预留)
CREATE TABLE IF NOT EXISTS fr_tactic_extract(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES fr_case(id),
    tactic TEXT NOT NULL CHECK(tactic IN
        ('urgency','suspicious_info','sensitive_req','credibility','isolation','semantic')),
    keyword TEXT NOT NULL,
    confidence INTEGER NOT NULL CHECK(confidence BETWEEN 0 AND 10),
    rationale TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tactic_keyword ON fr_tactic_extract(tactic, keyword);

-- 层3:词表审核队列(产出进 taxonomy.py 的唯一通道,状态机仿 correction)
CREATE TABLE IF NOT EXISTS marker_candidate(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword TEXT NOT NULL,
    tactic TEXT NOT NULL,
    scam_type TEXT,
    agg_confidence REAL NOT NULL,
    freq INTEGER NOT NULL,
    example_case_ids TEXT NOT NULL,    -- JSON 数组,证据可溯
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','approved','rejected')),
    decided_by TEXT,
    decided_at INTEGER,
    created_at INTEGER NOT NULL,
    UNIQUE(keyword, tactic)
);

-- 导入批次元数据(provenance 可溯)
CREATE TABLE IF NOT EXISTS fr_meta(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

PROVENANCE = "Fraud-R1 (Findings of ACL 2025), arXiv:2502.12904"
LICENSE_NOTE = "HF gated (Chouoftears/Fraud-R1-LLM-Defense-Fraud-Benchmark): research/education only, no redistribution, no commercial use"


def connect(path: str | Path = OFFLINE_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
