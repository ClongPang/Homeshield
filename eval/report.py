"""评测报告渲染。"""
from core.models import utcnow


def render_report(
    matrix: dict[str, dict],
    mode: str,
    dataset: str,
    n: int,
    sweep: list[dict] | None = None,
    data_profile: dict[str, int] | None = None,
) -> str:
    lines = [
        "# Homeshield 评测报告",
        "",
        f"- 模式:{mode} | 数据集:{dataset} | 样本数:{n} | 生成时间:{utcnow()}",
    ]
    if data_profile:
        profile = " / ".join(f"{k}:{v}" for k, v in sorted(data_profile.items()))
        lines.append(f"- 数据构成:{profile}(adapted=改编,synthetic=合成)")
    lines += [
        "",
        "> **口径与纪律**:Recall=TP/(TP+FN),TP=scam→dangerous+suspicious;"
        "FPR=FP/(FP+TN),FP=benign/edge→dangerous;fpr_strict 为用户感知口径"
        "(suspicious 亦计入),工作点选择建议以它为准。",
        "> mock / 种子 / 合成口径下的数字仅验证管道正确性,**不是产品质量**;"
        "质量收敛唯一标准是真实世界使用。",
        "",
    ]
    for name, r in matrix.items():
        lines += [
            f"## {name}",
            "",
            f"- Recall={r['recall']}  FPR={r['fpr']}  FPR(strict)={r['fpr_strict']}"
            f"  延迟 P50={r['latency']['p50']}ms P95={r['latency']['p95']}ms",
            "",
            "| 真实\\判定 | dangerous | suspicious | safe |",
            "|---|---|---|---|",
        ]
        for t in ("scam", "edge", "benign"):
            row = r["confusion"][t]
            lines.append(f"| {t} | {row['dangerous']} | {row['suspicious']} | {row['safe']} |")
        lines.append("")
    if sweep:
        lines += ["## 阈值扫描(完整方案 C)", "", "| threshold | precision | recall |", "|---|---|---|"]
        for p in sweep:
            lines.append(f"| {p['threshold']} | {p['precision']} | {p['recall']} |")
        lines.append("")
    return "\n".join(lines)
