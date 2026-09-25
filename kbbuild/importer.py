"""Fraud-R1 官方 JSON → fr_case 导入。

levelup 文件的 multi-rounds 共 4 轮:round1 与 base 原文相同(导入时按
UNIQUE(fr_id, level, lang) 去重),round2~4 对应增强级 level 1~3。
幂等:重复导入以 INSERT OR IGNORE 跳过,计数进 fr_meta。
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from kbbuild.db import LICENSE_NOTE, PROVENANCE


def _load(path: str | Path) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path}: 期望 JSON 数组,得到 {type(data).__name__}")
    return data


def _role_bg(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def import_official(
    conn: sqlite3.Connection,
    base_path: str | Path,
    levelup_path: str | Path,
    lang: str = "zh",
) -> dict:
    """导入 base 与 levelup 两个文件,返回统计。幂等,可重复执行。"""
    now = int(time.time())
    stats: dict = {"base": 0, "levelup": 0, "skipped": 0, "round1_duplicated": 0}
    base_items = _load(base_path)
    levelup_items = _load(levelup_path)

    by_fr_id = {item["id"]: item for item in base_items}

    def _insert(item: dict, split: str, level: int, text: str) -> None:
        cur = conn.execute(
            "INSERT OR IGNORE INTO fr_case(fr_id, split, level, lang, category,"
            " subcategory, data_type, role_bg, text, raw_seed, provenance,"
            " license_note, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                item["id"], split, level, lang,
                str(item.get("category", "")).strip(),
                str(item.get("subcategory", "")).strip(),
                str(item.get("data_type", "")).strip(),
                _role_bg(item.get("role_bg")),
                text,
                str(item.get("raw_data", "") or ""),
                PROVENANCE, LICENSE_NOTE, now,
            ),
        )
        if cur.rowcount:
            stats[split] += 1
        else:
            stats["skipped"] += 1

    for item in base_items:
        _insert(item, "base", 0, str(item.get("generated text", "") or ""))

    for item in levelup_items:
        for rnd in item.get("multi-rounds fraud", []):
            level = int(rnd["round"]) - 1  # round1→level0(与 base 重复),round2~4→level1~3
            if level == 0:
                base_text = str(by_fr_id.get(item["id"], {}).get("generated text", "") or "")
                if base_text and rnd["generated_data"].strip() == base_text.strip():
                    stats["round1_duplicated"] += 1
            _insert(item, "levelup", level, str(rnd.get("generated_data", "") or ""))

    for split, file in (("base", base_path), ("levelup", levelup_path)):
        conn.execute(
            "INSERT OR REPLACE INTO fr_meta(key, value) VALUES (?,?)",
            (f"import:{lang}:{split}", json.dumps(
                {"file": str(file), "items": len(base_items if split == "base" else levelup_items),
                 "inserted": stats[split], "skipped": stats["skipped"], "at": now},
                ensure_ascii=False)),
        )
    conn.commit()
    return stats
