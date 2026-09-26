"""对照集机制覆盖检查:对照组是考题,不是注释(《判定模型_设计教训》§4)。

用途:验证 benign-hard 对照集是否覆盖"危险区"——索取机制与
信任替代/核实抑制机制共现的正常样本。判定模型的重构(内联标注等)
必须在该对照集上证明 FPR 不升,而覆盖不足的对照集会让 FPR 数字失真。

用法:
    uv run python -m eval.contrast --dataset data/samples/benign_hard.jsonl

行格式:eval.dataset.Sample 兼容(JSONL),扩展字段 mechanics 为人工标注
的机制 id 列表;缺省时按 mechanics.markers 子串自动推导(仅用于覆盖统计)。
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from core.knowledge.mechanics import REGISTRY, Function


def annotate(text: str) -> list[str]:
    """按机制种子词表推导文本命中的机制(子串匹配,覆盖统计用)。"""
    return [m.id for m in REGISTRY.values() if any(k in text for k in m.markers)]


def _function(mechanic_id: str) -> Function:
    return REGISTRY[mechanic_id].function


def check(path: str | Path) -> dict:
    rows = [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    for row in rows:
        if "mechanics" not in row:
            row["mechanics"] = annotate(row.get("text", ""))

    per_label = Counter(r.get("label") for r in rows)
    per_mechanic = Counter(mid for r in rows for mid in r["mechanics"])

    # 危险区覆盖:每类索取机制在 benign/edge 侧的共现情况。
    # suspicious 区 = 索取∧信任替代;dangerous 区 = 索取∧核实抑制(最有价值的对照)。
    asks = [m.id for m in REGISTRY.values() if m.function is Function.ASK]
    zones: dict[str, dict] = {}
    for ask in asks:
        hits = [r for r in rows if ask in r["mechanics"] and r.get("label") in ("benign", "edge")]
        trust = sum(
            1 for r in hits
            if any(_function(mid) is Function.TRUST_SUBSTITUTE for mid in r["mechanics"] if mid != ask)
        )
        suppression = sum(
            1 for r in hits
            if any(_function(mid) is Function.VERIFICATION_SUPPRESSION for mid in r["mechanics"] if mid != ask)
        )
        zones[ask] = {
            "benign_rows": len(hits),
            "with_trust_substitute": trust,
            "with_verification_suppression": suppression,
        }

    return {
        "rows": len(rows),
        "per_label": dict(per_label),
        "per_mechanic": dict(per_mechanic),
        "ask_zones": zones,
        # 验收口径:索取机制必须有 benign 侧共现样本,否则该机制的
        # FPR 结论不可信(对照缺口,需真实收集补齐,见设计教训 §4.1)。
        "gaps": [ask for ask, z in zones.items() if z["benign_rows"] == 0],
        "suppression_zone_gaps": [
            ask for ask, z in zones.items() if z["with_verification_suppression"] == 0
        ],
    }


def render(report: dict) -> str:
    lines = [
        f"对照集覆盖报告:共 {report['rows']} 条,标签分布 {report['per_label']}",
        f"机制命中分布:{report['per_mechanic']}",
        "索取机制危险区覆盖(benign/edge 侧):",
    ]
    for ask, z in report["ask_zones"].items():
        name = REGISTRY[ask].name
        lines.append(
            f"  {name:<6} 样本 {z['benign_rows']:>2} | 含信任替代 {z['with_trust_substitute']:>2}"
            f" | 含核实抑制 {z['with_verification_suppression']:>2}"
        )
    if report["gaps"]:
        lines.append(f"⚠ 对照缺口(零覆盖):{report['gaps']} → 需真实收集补齐(设计教训 §4.1)")
    if report["suppression_zone_gaps"]:
        lines.append(f"⚠ dangerous 区对照缺口(无核实抑制共现):{report['suppression_zone_gaps']}")
    if not report["gaps"] and not report["suppression_zone_gaps"]:
        lines.append("✓ 危险区覆盖完整")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser("contrast")
    ap.add_argument("--dataset", default="data/samples/benign_hard.jsonl")
    args = ap.parse_args()
    print(render(check(args.dataset)))


if __name__ == "__main__":
    main()
