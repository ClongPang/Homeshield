"""离线素材库 CLI(开发者工具,与运行时 cli.py 分离,不依赖 .env/LLM)。

用法:
    uv run python kbcli.py init-db
    uv run python kbcli.py import --lang zh                # data/raw/fraud-r1/*.json → 离线库
    uv run python kbcli.py stats                           # 库存与分类学映射覆盖率
    uv run python kbcli.py export-eval --per-class 4       # 导出评测集 + 退化集(JSONL)
    uv run python kbcli.py mechanics-report                # 机制卡种子词语料支撑度(评审用)
"""
import argparse
import json
from collections import Counter
from pathlib import Path

from kbbuild.db import OFFLINE_DB_PATH, connect, init_schema
from kbbuild.export import export_eval
from kbbuild.importer import import_official

_LANG_FILE = {"zh": "Chinese", "en": "English"}


def _cmd_init_db(args) -> None:
    conn = connect(args.db)
    init_schema(conn)
    print("offline db ready:", args.db)


def _cmd_import(args) -> None:
    raw = Path(args.raw_dir)
    stem = _LANG_FILE[args.lang]
    base_path = raw / f"FP-base-{stem}.json"
    levelup_path = raw / f"FP-levelup-{stem}.json"
    for p in (base_path, levelup_path):
        if not p.exists():
            raise SystemExit(f"缺少源文件 {p},请先将 Fraud-R1 JSON 放入 {raw}/")
    conn = connect(args.db)
    init_schema(conn)
    stats = import_official(conn, base_path, levelup_path, lang=args.lang)
    print(f"import[{args.lang}]:", json.dumps(stats, ensure_ascii=False))


def _cmd_stats(args) -> None:
    conn = connect(args.db)
    total = conn.execute("SELECT COUNT(*) FROM fr_case").fetchone()[0]
    if total == 0:
        print("空库,先执行 import")
        return
    print(f"fr_case 总量: {total}")
    for row in conn.execute(
        "SELECT lang, level, COUNT(*) n FROM fr_case GROUP BY lang, level ORDER BY lang, level"
    ):
        print(f"  lang={row['lang']} level={row['level']}: {row['n']}")
    print("\n类目 × 子类:")
    for row in conn.execute(
        "SELECT category, subcategory, COUNT(*) n FROM fr_case WHERE level=0"
        " GROUP BY category, subcategory ORDER BY n DESC"
    ):
        print(f"  {row['n']:5d}  {row['category']} | {row['subcategory']}")

    from kbbuild.mapping import TAXONOMY_GAP, map_scam_type

    mapped, unmapped = 0, Counter()
    for row in conn.execute(
        "SELECT category, subcategory, text FROM fr_case WHERE lang='zh' AND level=0"
    ):
        st = map_scam_type(row["category"], row["subcategory"], row["text"] or "")
        if st:
            mapped += 1
        else:
            unmapped[f"{row['category']}|{row['subcategory']}"] += 1
    print(f"\n12 类映射覆盖: {mapped} 可映射 / {sum(unmapped.values())} 映射外")
    for key, n in unmapped.most_common():
        gap = " ← 分类学缺口(建议新增类目)" if key.split("|")[0] in TAXONOMY_GAP else ""
        print(f"  {n:5d}  {key}{gap}")


def _cmd_export_eval(args) -> None:
    conn = connect(args.db)
    summary = export_eval(
        conn, args.out_base, args.out_levelup,
        lang=args.lang, per_class=args.per_class, seed=args.seed,
    )
    print("export-eval:", json.dumps(
        {k: v for k, v in summary.items() if k != "unmapped_gap"}, ensure_ascii=False, indent=2))
    if summary["unmapped_gap"]:
        print("映射外(未导出):", json.dumps(summary["unmapped_gap"], ensure_ascii=False))
    print("base  →", args.out_base)
    print("level →", args.out_levelup)
    print("提醒:导出即候选,须人工过筛后方可并入冷启动评测集(§6.1)")


def _cmd_mechanics_report(args) -> None:
    """机制卡证据报告:每个种子词在中文诈骗语料中的支撑度,供卡片评审参考。"""
    from core.knowledge.mechanics import MECHANIC_LIST

    conn = connect(args.db)
    total = conn.execute(
        "SELECT COUNT(*) FROM fr_case WHERE lang='zh' AND level=0"
    ).fetchone()[0]
    if total == 0:
        raise SystemExit("语料为空,先执行 import")
    for mech in MECHANIC_LIST:
        print(f"\n[{mech.id}] {mech.name}({mech.function.value})")
        for kw in mech.markers:
            rows = conn.execute(
                "SELECT fr_id FROM fr_case WHERE lang='zh' AND level=0 AND instr(text, ?) > 0"
                " ORDER BY fr_id LIMIT 3",
                (kw,),
            ).fetchall()
            n = conn.execute(
                "SELECT COUNT(*) FROM fr_case WHERE lang='zh' AND level=0 AND instr(text, ?)",
                (kw,),
            ).fetchone()[0]
            examples = ",".join(f"#{r['fr_id']}" for r in rows) or "-"
            flag = "  ⚠语料零支撑" if n == 0 else ""
            print(f"  {kw:<8} {n:>4}/{total}{flag}  例:{examples}")


def main() -> None:
    ap = argparse.ArgumentParser("kbbuild", description="离线素材库构建工具")
    ap.add_argument("--db", default=OFFLINE_DB_PATH, help="离线库路径")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db", help="建库建表,幂等")
    p = sub.add_parser("import", help="Fraud-R1 JSON → fr_case")
    p.add_argument("--lang", choices=["zh", "en"], default="zh")
    p.add_argument("--raw-dir", default="data/raw/fraud-r1")
    sub.add_parser("stats", help="库存统计与映射覆盖率")
    p = sub.add_parser("export-eval", help="导出评测集与退化集")
    p.add_argument("--lang", choices=["zh", "en"], default="zh")
    p.add_argument("--per-class", type=int, default=4, help="每 scam_type 抽样数")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--out-base", default="data/samples/fraud_r1_base.jsonl")
    p.add_argument("--out-levelup", default="data/samples/fraud_r1_levelup.jsonl")
    sub.add_parser("mechanics-report", help="机制卡种子词的语料支撑度报告(卡片评审用)")

    args = ap.parse_args()
    {"init-db": _cmd_init_db, "import": _cmd_import,
     "stats": _cmd_stats, "export-eval": _cmd_export_eval,
     "mechanics-report": _cmd_mechanics_report}[args.cmd](args)


if __name__ == "__main__":
    main()
