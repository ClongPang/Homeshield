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


def export_cross_message(
    conn: sqlite3.Connection,
    out: str | Path,
    *,
    benign_path: str | Path = "data/datasets/restricted/two_sided_v0.jsonl",
    lang: str = "zh",
    seed: int = 42,
) -> dict[str, int]:
    """导出分层跨消息弧;人工撰写的恐吓/误并补例标记待复核。"""
    pool, _ = _build_mapped_case_pool(conn, lang)
    picked = _sample_cases_by_scam_type(pool, 4, seed)
    arcs: list[dict] = []

    def add(arc_id: str, stratum: str, texts: list[str], delays: list[int],
            label: str, source: str, scam_type: str | None = None) -> None:
        row = {"arc_id": arc_id, "stratum": stratum,
               "messages": [{"text": t, "delay_seconds": d} for t, d in zip(texts, delays)],
               "final_label": label, "scam_type": scam_type, "source": source}
        arcs.append(row)

    for p in picked:
        levels = conn.execute(
            "SELECT text FROM fr_case WHERE fr_id=? AND lang=? AND level<=3 ORDER BY level",
            (p["fr_id"], lang),
        ).fetchall()
        turns = [r["text"].strip() for r in levels]
        if len(turns) >= 3:
            add(f"conv#{p['fr_id']:04d}", "trust_same_incident", turns,
                [0] + [3600] * (len(turns) - 1), "scam",
                "adapted:fraud-r1:unreviewed", p["scam_type"])

    for index, p in enumerate(picked[:12], 1):
        account = f"622202020202{index:04d}"
        add(f"linked#{index:02d}", "trust_linked",
            [p["text"] + f"\n账号 {account}", f"请转账到账号 {account}"],
            [0, 86400 + index * 3600], "scam", "adapted:fraud-r1:unreviewed", p["scam_type"])
        add(f"unlinked#{index:02d}", "trust_unlinked",
            [p["text"], "他又让我转账了"], [0, 86400 + index * 3600],
            "scam", "adapted:fraud-r1:unreviewed", p["scam_type"])

    fear_priors = [
        "自称公安局的人说这是机密，不能告诉任何人", "他说是检察院调查，案件保密",
        "对方说法院正在查我，不许告诉任何人", "有人冒充客服，说退款流程绝对保密",
        "对方说安全账户核查期间别声张", "他说银行账户涉案，不要告诉任何人",
        "自称公安局的人说不能声张", "对方说要配合清查，这是机密",
        "客服说退款涉及内部审核，案件保密", "有人说是法院通知，绝对保密",
        "对方称检察院在查，不能告诉任何人", "自称公安局的人让我别声张",
    ]
    for index, prior in enumerate(fear_priors, 1):
        add(f"fear#{index:02d}", "fear", [prior, "对方接着要求我转账"],
            [0, index * 300], "scam", "author_draft:unreviewed", "impersonate_police")
    for index in range(8):
        add(f"overwindow#{index:02d}", "overwindow",
            [fear_priors[index], "对方接着要求我转账"], [0, 604801 + index],
            "scam", "author_draft:unreviewed", "impersonate_police")

    benign: list[dict] = []
    for path in [Path(benign_path), Path("data/datasets/core/benign_hard.jsonl")]:
        if path.exists():
            benign += [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                       if line.strip() and json.loads(line).get("label") in {"benign", "edge"}]
    by_text = {row["text"]: row for row in benign}
    authored = [
        "今天给孩子转账生活费，已经到账", "我把房租转账给房东了", "给同事转账还了午饭钱",
        "买菜付款后收到电子小票", "今天下午去公园散步", "家里水电费已经缴清",
        "明天接孩子放学", "快递已由物业代收",
    ]
    for text in authored:
        by_text.setdefault(text, {"text": text, "source": "author_draft:unreviewed"})
    benign_rows = list(by_text.values())[:30]
    if len(benign_rows) < 30:
        raise ValueError("need at least 30 distinct benign current messages")
    for index, row in enumerate(benign_rows, 1):
        add(f"mismerge_benign#{index:02d}", "mismerge_benign",
            ["明天一起吃饭吧", row["text"]], [0, 300], "benign",
            row.get("source", "adapted:benign"))
        prior = "案件保密，请立即转账" if index <= 15 else "这是机密，别告诉家人"
        add(f"mismerge_risky#{index:02d}", "mismerge_risky",
            [prior, row["text"]], [0, 300], "benign", row.get("source", "adapted:benign"))

    _write_jsonl(out, arcs)
    return dict(Counter(row["stratum"] for row in arcs))


def _write_jsonl(path: str | Path, rows: list[dict]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
