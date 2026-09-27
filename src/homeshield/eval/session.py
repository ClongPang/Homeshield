"""跨消息弧的逐条回放与预注册门检查。mock 数字仅验证管道。"""
import asyncio
import dataclasses
import json
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from homeshield.core.config import Settings
from homeshield.core.deps import build_deps, make_pipeline
from homeshield.core.models import LEVEL_RANK, Level
from homeshield.core.pipeline import PipelineConfig
from homeshield.eval.metrics import latency_percentiles


def load_arcs(path: str | Path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


async def run_arm(arcs: list[dict], settings: Settings, enabled: bool) -> list[dict]:
    deps = build_deps(dataclasses.replace(settings, db_path=":memory:"))
    deps.verification.pipeline = make_pipeline(
        deps, dataclasses.replace(PipelineConfig.product_default(), supply_features=enabled)
    )
    judge_calls = 0
    original_judge = deps.verification.pipeline.judge.judge

    async def counted_judge(inp, *, constrained):
        nonlocal judge_calls
        judge_calls += 1
        return await original_judge(inp, constrained=constrained)

    deps.verification.pipeline.judge.judge = counted_judge
    results = []
    for arc in arcs:
        group_id = deps.repos.group.create(f"eval:{arc['arc_id']}")
        member_id = deps.repos.member.add(group_id, "eval", openid=f"eval:{arc['arc_id']}")
        member = deps.repos.member.get(member_id)
        now = 1_800_000_000
        final = None
        calls_before = judge_calls
        for message in arc["messages"]:
            now += int(message["delay_seconds"])
            with patch("homeshield.core.repo.utc_timestamp", return_value=now):
                final = await deps.verification.verify(
                    member=member, content=message["text"], content_type="text"
                )
        result = final.result
        stored = deps.repos.verdict.get(result.verdict_id) if result.verdict_id else None
        results.append({
            "arc_id": arc["arc_id"], "stratum": arc["stratum"],
            "label": arc["final_label"],
            "level": result.verdict.level.value if result.verdict else "degraded",
            "latency_ms": result.latency_ms,
            "feature_count": len(json.loads(stored["features"])) if stored else 0,
            "synthetic_types": [f["type"] for f in json.loads(stored["features"])
                                if f.get("origin") == "prior"] if stored else [],
            "judge_calls": judge_calls - calls_before,
            "supplied": bool(stored and stored["context_snapshot"]),
            "cited_ids": result.verdict.cited_ids if result.verdict else [],
            "reason": result.verdict.reason if result.verdict else "",
            "reply": result.reply,
        })
    deps.conn.close()
    return results


def _rate(rows: list[dict], positive: bool) -> float:
    return sum(r["level"] in ({"suspicious", "dangerous"} if positive else {"dangerous"})
               for r in rows) / len(rows) if rows else 0.0


def _rank(level: str) -> int:
    try:
        return LEVEL_RANK[Level(level)]
    except ValueError:
        return -1


def evaluate_pairs(arcs: list[dict], off: list[dict], on: list[dict], mode: str,
                   fault_passed: bool = True) -> tuple[dict, str]:
    by_id = {r["arc_id"]: r for r in on}
    pairs = [(base, by_id[base["arc_id"]]) for base in off]
    covered = {"trust_same_incident", "trust_linked", "fear"}
    positive = [(a, b) for a, b in pairs if a["stratum"] in covered]
    negative = [(a, b) for a, b in pairs if a["stratum"] in {"mismerge_benign", "mismerge_risky"}]
    regressions = [a["arc_id"] for a, b in pairs
                   if _rank(b["level"]) < _rank(a["level"])]
    corrected = [a["arc_id"] for a, b in positive if a["level"] == "safe"
                 and b["level"] in {"suspicious", "dangerous"}]
    new_misses = [a["arc_id"] for a, b in positive if a["level"] != "safe" and b["level"] == "safe"]
    fpr_off = _rate([a for a, _ in negative], False)
    fpr_on = _rate([b for _, b in negative], False)
    strict_off = _rate([a for a, _ in negative], True)
    strict_on = _rate([b for _, b in negative], True)
    feature_over = [a["arc_id"] for a, b in pairs if b["feature_count"] > a["feature_count"] + 2]
    judge_over = [a["arc_id"] for a, b in pairs if b["judge_calls"] > a["judge_calls"]]
    new_false_alarms = [a["arc_id"] for a, b in negative
                        if a["level"] != "dangerous" and b["level"] == "dangerous"]
    p95_off = latency_percentiles([r["latency_ms"] for r in off])["p95"]
    p95_on = latency_percentiles([r["latency_ms"] for r in on])["p95"]
    source_reviewed = all("unreviewed" not in a["source"] for a in arcs)
    per_stratum = {}
    for stratum in sorted({a["stratum"] for a in arcs}):
        group = [(a, b) for a, b in pairs if a["stratum"] == stratum]
        per_stratum[stratum] = {
            "n": len(group), "supply_hit": sum(b["supplied"] for _, b in group),
            "detected_off": sum(a["level"] != "safe" for a, _ in group),
            "detected_on": sum(b["level"] != "safe" for _, b in group),
            "dangerous_off": sum(a["level"] == "dangerous" for a, _ in group),
            "dangerous_on": sum(b["level"] == "dangerous" for _, b in group),
            "s1": sum("escalation" in b["synthetic_types"] for _, b in group),
            "s2": sum("isolation" in b["synthetic_types"] for _, b in group),
        }
    gates = {
        "recall_gain": bool(corrected) and not new_misses,
        "fpr": fpr_on <= fpr_off and strict_on <= strict_off,
        "monotonic": not regressions,
        "feature_budget": not feature_over,
        "judge_calls": not judge_over,
        "fault_injection": fault_passed,
        "llm_latency": mode == "llm" and p95_on - p95_off <= 50,
        "sample_review": source_reviewed,
    }
    result = {
        "counts": dict(Counter(a["stratum"] for a in arcs)),
        "per_stratum": per_stratum,
        "covered_recall_off": _rate([a for a, _ in positive], True),
        "covered_recall_on": _rate([b for _, b in positive], True),
        "supply_hit_rate": sum(b["supplied"] for _, b in positive) / len(positive) if positive else 0.0,
        "corrected": corrected, "new_misses": new_misses,
        "fpr_off": fpr_off, "fpr_on": fpr_on,
        "fpr_strict_off": strict_off, "fpr_strict_on": strict_on,
        "monotonic_regressions": regressions, "feature_over": feature_over,
        "judge_over": judge_over, "new_false_alarms": new_false_alarms,
        "p95_off_ms": p95_off, "p95_on_ms": p95_on,
        "gates": gates, "default_enable": all(gates.values()),
    }
    report = ["# 家中盾会话模型评测", "",
              f"- 模式: {mode};样本: {len(arcs)} 条弧;数据含待人工复核内容。",
              "- mock 模式仅验证管道，不作为产品质量或默认开启依据。", "",
              "## 分层样本", "",
              "| 层 | 数量 |", "|---|---:|"]
    report += [f"| {key} | {n} |" for key, n in sorted(result["counts"].items())]
    report += ["", "## 各层命中与判定", "",
               "| 层 | 供给命中 | S1/S2 生成 | 非 safe off→on | 高危 off→on |",
               "|---|---:|---:|---:|---:|"]
    report += [f"| {key} | {v['supply_hit']}/{v['n']} | {v['s1']}/{v['s2']} | {v['detected_off']}→{v['detected_on']} | {v['dangerous_off']}→{v['dangerous_on']} |"
               for key, v in per_stratum.items()]
    report += ["", "## 配对结果", "",
               f"- 可覆盖层 Recall: {result['covered_recall_off']:.3f} → {result['covered_recall_on']:.3f};供给命中率 {result['supply_hit_rate']:.3f}。",
               f"- 基线漏报修正: {len(corrected)}；新增漏报: {len(new_misses)}。",
               f"- 误并 FPR: {fpr_off:.3f} → {fpr_on:.3f};FPR(strict): {strict_off:.3f} → {strict_on:.3f}。",
               f"- 新增高危误报: {', '.join(new_false_alarms) or '无'}。",
               f"- 单向等级回退: {len(regressions)}；特征超额: {len(feature_over)}；额外 judge 调用: {len(judge_over)}。",
               f"- p95 延迟: {p95_off}ms → {p95_on}ms（mock 不验收延迟）。", "",
               "## 门禁", ""]
    report += [f"- {key}: {'通过' if passed else ('未验证' if key in {'llm_latency', 'sample_review'} else '未通过')}"
               for key, passed in gates.items()]
    report += ["", f"**默认供给: {'可开启' if result['default_enable'] else '保持关闭'}。**",
               "", "故障注入使用固定 mock judge/reply 验证判定语义与 JudgeInput；真实微信通道仍需实号验收。", ""]
    return result, "\n".join(report)


async def fault_injection_check() -> bool:
    deps = build_deps(Settings(mode="mock", db_path=":memory:"))
    group_id = deps.repos.group.create("fault-check")
    member = deps.repos.member.get(deps.repos.member.add(group_id, "tester", openid="fault:test"))
    deps.verification.pipeline = make_pipeline(deps, PipelineConfig(supply_features=True))
    await deps.verification.verify(member=member, content="这是机密，别告诉家人")
    seen = []
    original = deps.judge.judge

    async def capture(inp, *, constrained):
        seen.append(inp.model_dump_json())
        return await original(inp, constrained=constrained)

    deps.judge.judge = capture
    with patch.object(deps.repos.query, "supply_context", side_effect=RuntimeError("injected")), \
            patch("homeshield.core.pipeline.logger.warning"):
        failed = await deps.verification.verify(member=member, content="请转账")
    deps.verification.pipeline = make_pipeline(deps, PipelineConfig(supply_features=False))
    baseline = await deps.verification.verify(member=member, content="请转账")
    deps.conn.close()
    return (seen[-2] == seen[-1]
            and failed.result.verdict.model_dump() == baseline.result.verdict.model_dump()
            and failed.result.reply == baseline.result.reply)


async def run_session_evaluation(dataset: str, settings: Settings, supply: str, out: str) -> dict:
    arcs = load_arcs(dataset)
    off = await run_arm(arcs, settings, False)
    if supply == "off":
        payload = {"mode": settings.mode, "arm": "off", "rows": off}
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload
    on = await run_arm(arcs, settings, True)
    fault_passed = await fault_injection_check()
    metrics, report = evaluate_pairs(arcs, off, on, settings.mode, fault_passed)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(report, encoding="utf-8")
    return metrics
