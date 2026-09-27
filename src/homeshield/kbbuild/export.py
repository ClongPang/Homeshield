"""离线库 → 评测资产导出(§6.1 口径)。

产出两份 JSONL:
- base 评测集:level0 中文样本按类均衡抽样,label=scam,source=synthetic:llm:deepseek-r1,
  notes 标注「待人工过筛」——导出即候选,过筛后才可并入冷启动集;
- levelup 退化集:同批案例的 level0~3 配对样本,专供「AI 生成话术检测退化曲线」
  (带 level/case_key 扩展字段,eval.dataset.Sample 按 pydantic v2 默认忽略扩展字段)。

原始种子(raw_seed)与映射外样本(phishing 缺口)一律不进导出。
"""
from __future__ import annotations

import json
import random
import sqlite3
from collections import Counter
from pathlib import Path

from homeshield.kbbuild.mapping import map_scam_type

MIN_TEXT_CHARS = 30  # 过滤空壳/占位文本


def _build_mapped_case_pool(conn: sqlite3.Connection, lang: str) -> tuple[list[dict], dict[str, int]]:
    rows = conn.execute(
        "SELECT fr_id, category, subcategory, text FROM fr_case"
        " WHERE lang=? AND level=0 ORDER BY fr_id",
        (lang,),
    ).fetchall()
    pool: list[dict] = []
    unmapped: dict[str, int] = {}
    for r in rows:
        text = r["text"] or ""
        if len(text.strip()) < MIN_TEXT_CHARS:
            continue
        scam_type = map_scam_type(r["category"], r["subcategory"], text)
        if scam_type is None:
            key = f"{r['category']}|{r['subcategory']}"
            unmapped[key] = unmapped.get(key, 0) + 1
            continue
        pool.append({"fr_id": r["fr_id"], "text": text, "scam_type": scam_type})
    return pool, unmapped


def sample_cases_by_type(pool: list[dict], per_class: int, seed: int) -> list[dict]:
    """按类均衡确定性抽样(export-eval 与 export-conversations 共用,保证案例集一致)。"""
    return _sample_cases_by_scam_type(pool, per_class, seed)


def _sample_cases_by_scam_type(pool: list[dict], per_class: int, seed: int) -> list[dict]:
    by_type: dict[str, list[dict]] = {}
    for item in pool:
        by_type.setdefault(item["scam_type"], []).append(item)
    rng = random.Random(seed)
    picked: list[dict] = []
    for scam_type in sorted(by_type):
        items = sorted(by_type[scam_type], key=lambda x: x["fr_id"])
        picked.extend(items if len(items) <= per_class else rng.sample(items, per_class))
    return picked


def export_eval(
    conn: sqlite3.Connection,
    out_base: str | Path,
    out_levelup: str | Path,
    lang: str = "zh",
    per_class: int = 4,
    seed: int = 42,
) -> dict:
    pool, unmapped = _build_mapped_case_pool(conn, lang)
    picked = _sample_cases_by_scam_type(pool, per_class, seed)
    source = "synthetic:llm:deepseek-r1"

    base_rows = [
        {
            "id": f"FR1-{p['fr_id']:04d}",
            "text": p["text"],
            "label": "scam",
            "scam_type": p["scam_type"],
            "source": source,
            "notes": f"Fraud-R1#{p['fr_id']} 生成文本,待人工过筛",
        }
        for p in picked
    ]
    _write_jsonl(out_base, base_rows)

    # 退化集:同批案例 level0~3 配对(level0 与 base 集同文,配对看趋势)
    level_rows: list[dict] = []
    for p in picked:
        levels = conn.execute(
            "SELECT level, text FROM fr_case WHERE fr_id=? AND lang=? AND level<=3"
            " ORDER BY level",
            (p["fr_id"], lang),
        ).fetchall()
        for r in levels:
            level_rows.append({
                "id": f"FR1L-{p['fr_id']:04d}-L{r['level']}",
                "level": r["level"],
                "case_key": p["fr_id"],
                "text": r["text"],
                "label": "scam",
                "scam_type": p["scam_type"],
                "source": source,
                "notes": f"Fraud-R1#{p['fr_id']} 增强级 {r['level']}(C/U/E 渐进)",
            })
    _write_jsonl(out_levelup, level_rows)

    by_type: dict[str, int] = {}
    for p in picked:
        by_type[p["scam_type"]] = by_type.get(p["scam_type"], 0) + 1
    return {
        "pool_size": len(pool),
        "exported_base": len(base_rows),
        "exported_levelup": len(level_rows),
        "per_class": by_type,
        "unmapped_gap": unmapped,
    }


def export_conversations(
    conn: sqlite3.Connection,
    out: str | Path,
    lang: str = "zh",
    per_class: int = 4,
    seed: int = 42,
) -> dict:
    """导出会话体样本(重构四):每案例 level0~3 各为一轮, Fraud-R1 levelup 同构。"""
    pool, unmapped = _build_mapped_case_pool(conn, lang)
    picked = _sample_cases_by_scam_type(pool, per_class, seed)
    source = "synthetic:llm:deepseek-r1"
    conv_rows = []
    for p_ in picked:
        levels = conn.execute(
            "SELECT level, text FROM fr_case WHERE fr_id=? AND lang=? AND level<=3 ORDER BY level",
            (p_["fr_id"], lang),
        ).fetchall()
        turns = [r["text"].strip() for r in levels]
        conv_rows.append({
            "id": f"FR1C-{p_['fr_id']:04d}",
            "text": "\n".join(f"【第{i}轮】{t}" for i, t in enumerate(turns, 1)),
            "turns": turns,
            "label": "scam",
            "scam_type": p_["scam_type"],
            "source": source,
            "notes": f"Fraud-R1#{p_['fr_id']} 四轮会话(可信度→紧迫感→情绪操纵渐进)",
        })
    _write_jsonl(out, conv_rows)
    by_type = Counter(c["scam_type"] for c in conv_rows)
    return {"exported_conversations": len(conv_rows), "per_class": dict(by_type),
            "unmapped_gap": unmapped}


def _write_jsonl(path: str | Path, rows: list[dict]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
