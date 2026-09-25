"""一条命令复现评测数字:

    uv run python -m eval.run_eval --dataset data/samples/samples.jsonl --mode mock
"""
import argparse
import asyncio
import dataclasses
from collections import Counter

from core.config import Settings
from core.deps import build_deps, make_pipeline
from core.intake import ingest
from core.models import Role
from eval.ablation import run_matrix
from eval.dataset import load_dataset
from eval.metrics import threshold_sweep
from eval.report import render_report


def _make_runner(deps, member_id: int, family_id: int, config):
    pipeline = make_pipeline(deps, config)

    async def run(sample) -> tuple[str, int, int]:
        intake = ingest(
            deps.repos,
            member_id=member_id,
            family_id=family_id,
            content=sample.text,
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
    ap.add_argument("--out", default="eval/report.md")
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
    mid = deps.repos.member.add(fid, "evaler", Role.ELDER)

    matrix = asyncio.run(
        run_matrix(lambda cfg: _make_runner(deps, mid, fid, cfg), samples)
    )
    full_run = _make_runner(deps, mid, fid, None)
    y_true = [s.label.value for s in samples]
    scores = asyncio.run(
        _collect_scores(full_run, samples)
    )
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
