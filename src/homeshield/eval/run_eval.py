"""一条命令复现评测数字:

    uv run python -m eval.run_eval --dataset data/samples/samples.jsonl --mode mock
"""
import argparse
import asyncio
import dataclasses
from pathlib import Path
from collections import Counter

from homeshield.core.config import Settings
from homeshield.core.db import ensure_database
from homeshield.core.deps import build_deps, initialize_deps, make_pipeline
from homeshield.core.intake import ingest
from homeshield.eval.ablation import run_ablation_matrix
from homeshield.eval.dataset import load_dataset
from homeshield.eval.metrics import threshold_sweep
from homeshield.eval.report import render_report
from homeshield.eval.session import resolve_eval_database_url, run_session_evaluation


def _make_runner(deps, user_id: int, config):
    pipeline = make_pipeline(deps, config)

    async def run(sample) -> tuple[str, int, int]:
        content = (
            "\n".join(f"【第{i}轮】{t}" for i, t in enumerate(sample.turns, 1))
            if sample.turns
            else sample.text
        )
        intake = await ingest(
            deps.repos,
            user_id=user_id,
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
    ap.add_argument("--dataset", default=None)
    ap.add_argument("--mode", default=None, help="mock|llm;缺省读 .env")
    ap.add_argument("--configs", default=None,
                    help="逗号分隔的消融名(如 C_full,D_semantics,E_semantics_inline);缺省全跑")
    ap.add_argument("--out", default=None)
    ap.add_argument("--database-url", default=None,
                    help="评测专用隔离 Postgres 库;默认 TEST_DATABASE_URL,缺省派生 <主库名>_test")
    ap.add_argument("--supply", choices=["on", "off", "both"], default=None,
                    help="跨消息弧评测;on/both 配对对比,off 仅输出单条基线")
    ap.add_argument("--checkpoint", default="data/eval_out/checkpoint.jsonl",
                    help="逐样本断点文件;置空字符串关闭")
    asyncio.run(_run(ap.parse_args()))


async def _run(args) -> None:
    settings = Settings.load()
    if args.mode:
        settings = dataclasses.replace(settings, mode=args.mode)
    settings = dataclasses.replace(
        settings, database_url=resolve_eval_database_url(args.database_url))
    await ensure_database(settings.database_url)
    if args.supply:
        dataset = args.dataset or "data/samples/fraud_r1_cross_message.jsonl"
        out = args.out or ("docs/reports/session_report.md" if args.supply != "off"
                           else "data/eval_out/session_off.json")
        result = await run_session_evaluation(dataset, settings, args.supply, out)
        print(result)
        return

    dataset = args.dataset or "data/samples/samples.jsonl"
    out = args.out or "docs/reports/report.md"
    samples = load_dataset(dataset)
    profile = Counter(s.source.split(":", 1)[0] for s in samples)
    deps = build_deps(settings)
    try:
        await initialize_deps(deps)
        eval_user = await deps.repos.users.get_or_create("eval:main")
        names = [c.strip() for c in args.configs.split(",")] if args.configs else None
        if args.checkpoint:
            Path(args.checkpoint).parent.mkdir(parents=True, exist_ok=True)

        matrix = await run_ablation_matrix(
            lambda cfg: _make_runner(deps, eval_user.id, cfg), samples, names,
            checkpoint=args.checkpoint or None,
        )
        full_run = _make_runner(deps, eval_user.id, None)
        scores = await _collect_scores(full_run, samples)
        y_true = [s.label.value for s in samples]
        report = render_report(
            matrix,
            mode=settings.mode,
            dataset=dataset,
            n=len(samples),
            sweep=threshold_sweep(y_true, scores),
            data_profile=dict(profile),
        )
        with open(out, "w", encoding="utf-8") as f:
            f.write(report)
        print(report)
    finally:
        await deps.pool.close()


async def _collect_scores(run, samples) -> list[int]:
    return [(await run(s))[1] for s in samples]


if __name__ == "__main__":
    main()
