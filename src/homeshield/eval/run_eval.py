"""一条命令复现评测数字:

    uv run python -m eval.run_eval --dataset data/samples/samples.jsonl --mode mock
"""
import argparse
import asyncio
import dataclasses
from pathlib import Path
from collections import Counter

from homeshield.core.config import Settings
from homeshield.core.deps import build_deps, make_pipeline
from homeshield.core.intake import ingest
from homeshield.eval.ablation import run_matrix
from homeshield.eval.dataset import load_dataset
from homeshield.eval.metrics import threshold_sweep
from homeshield.eval.report import render_report


def _make_runner(deps, member_id: int, config):
    pipeline = make_pipeline(deps, config)

    async def run(sample) -> tuple[str, int, int]:
        content = (
            "\n".join(f"【第{i}轮】{t}" for i, t in enumerate(sample.turns, 1))
            if sample.turns
            else sample.text
        )
        member = deps.repos.member.get(member_id)
        intake = ingest(
            deps.repos,
            user_id=member.user_id,
            memberships=deps.repos.member.list_for_user(member.user_id),
            content=content,
        )
        result = await pipeline.run(intake.message, intake.query_id)
        verdict = result.verdict
        level = verdict.level.value if verdict else "safe"
        score = verdict.confidence if (verdict and verdict.level.value != "safe") else 0
        return level, score, result.latency_ms

    return run


def main() -> None:
    ap = argparse.ArgumentParser("run_eval")
    ap.add_argument("--dataset", default="data/samples/samples.jsonl")
    ap.add_argument("--mode", default=None, help="mock|llm;缺省读 .env")
    ap.add_argument("--configs", default=None,
                    help="逗号分隔的消融名(如 C_full,D_semantics,E_semantics_inline);缺省全跑")
    ap.add_argument("--out", default="docs/reports/report.md")
    ap.add_argument("--checkpoint", default="data/eval_out/checkpoint.jsonl",
                    help="逐样本断点文件;置空字符串关闭")
    args = ap.parse_args()

    settings = Settings.load()
    if args.mode:
        settings = dataclasses.replace(settings, mode=args.mode)
    settings = dataclasses.replace(settings, db_path=":memory:")  # 评测隔离,不污染主库

    samples = load_dataset(args.dataset)
    # 各来源样本数,写入报告头部
    profile = Counter(s.source.split(":", 1)[0] for s in samples)
    deps = build_deps(settings)
    fid = deps.repos.family.create("eval")
    mid = deps.repos.member.add(fid, "evaler", openid="test:eval")

    names = [c.strip() for c in args.configs.split(",")] if args.configs else None
    if args.checkpoint:
        Path(args.checkpoint).parent.mkdir(parents=True, exist_ok=True)

    async def _evaluate():
        matrix = await run_matrix(
            lambda cfg: _make_runner(deps, mid, cfg), samples, names,
            checkpoint=args.checkpoint or None,
        )
        # 单一事件循环:AsyncOpenAI 客户端绑定创建它的循环,跨 asyncio.run 复用会炸
        full_run = _make_runner(deps, mid, None)
        scores = await _collect_scores(full_run, samples)
        return matrix, scores

    matrix, scores = asyncio.run(_evaluate())
    y_true = [s.label.value for s in samples]
    report = render_report(
        matrix,
        mode=settings.mode,
        dataset=args.dataset,
        n=len(samples),
        sweep=threshold_sweep(y_true, scores),
        data_profile=dict(profile),
    )
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(report)
    print(report)


async def _collect_scores(run, samples) -> list[int]:
    return [(await run(s))[1] for s in samples]


if __name__ == "__main__":
    main()
