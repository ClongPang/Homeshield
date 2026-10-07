"""One product_default end-to-end pass with explicit degradation accounting.

退出码约定(交付测试团队):本工具恒返回 2(未完成)——end_to_end_product_default
的验收要求同标签的旧/新 product_default 配对运行,该配对运行尚未捕获
(见报告 old_new_comparison 字段)。当前运行结果一律视为 candidate_only,
供开发迭代与冒烟;不得据此宣称端到端验收通过。配对运行补齐后本约定随
规格变更一并更新。
"""
import argparse
import asyncio
import dataclasses
import hashlib
import json
import sys
import time
from collections import defaultdict
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from homeshield.core.config import Settings
from homeshield.core.db import ensure_database
from homeshield.core.deps import build_deps, initialize_deps, make_pipeline
from homeshield.core.intake import ingest
from homeshield.core.pipeline import PIPELINE_VERSION, PipelineConfig
from homeshield.eval.dataset import load_dataset
from homeshield.eval.metrics import recall, user_visible_false_positive_rate
from homeshield.eval.session import resolve_eval_database_url

ROOT = Path(__file__).resolve().parents[3]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def run(datasets: list[str], out: Path, database_url: str | None,
              database_suffix: str = "retrieval_eval") -> int:
    out.mkdir(parents=True, exist_ok=True)
    base_settings = Settings.load()
    try:
        embed_provider = base_settings.get_embedding_provider() if base_settings.llm_enabled else None
    except Exception as exc:
        (out / "summary.json").write_text(json.dumps({"status": "incomplete", "error_type": type(exc).__name__}, indent=2))
        return 2
    if not base_settings.llm_enabled or embed_provider is None:
        (out / "summary.json").write_text(json.dumps({"status": "incomplete",
            "reason": "real MODE=llm and EMBED_PROVIDER are required"}, indent=2))
        return 2
    eval_url = resolve_eval_database_url(database_url)
    if database_url is None:
        parts = urlsplit(eval_url)
        db_name = parts.path.rsplit("/", 1)[-1]
        eval_url = urlunsplit((parts.scheme, parts.netloc, f"/{db_name}_{database_suffix}", parts.query, parts.fragment))
    settings = dataclasses.replace(base_settings, database_url=eval_url)
    await ensure_database(settings.database_url)
    deps = build_deps(settings)
    started = time.time()
    rows, summaries = [], []
    current_dataset = None
    current_sample_id = None
    try:
        await initialize_deps(deps)
        user = await deps.repos.users.get_or_create(f"eval:retrieval-e2e:{int(started)}")
        pipeline = make_pipeline(deps, PipelineConfig.product_default())
        modes = defaultdict(lambda: {"hybrid": 0, "keyword": 0, "keyword_fallback": 0, "empty": 0})
        original_search_cases = pipeline.retriever.search_cases

        async def observed_search(query: str):
            if hasattr(pipeline.retriever, "search"):
                result = await pipeline.retriever.search(query)
                modes[current_dataset][result.mode] += 1
                return [hit.case for hit in result.hits]
            cases = await original_search_cases(query)
            actual_mode = "hybrid" if getattr(pipeline.retriever, "_case_vecs", None) is not None else "keyword"
            modes[current_dataset][actual_mode] += 1
            return cases

        pipeline.retriever.search_cases = observed_search
        for dataset in datasets:
            current_dataset = Path(dataset).name
            samples = load_dataset(dataset)
            y_true, y_pred = [], []
            degraded_count = no_verdict_count = 0
            pipeline_errors: Counter[str] = Counter()
            for sample in samples:
                current_sample_id = sample.id
                stage = "ingest"
                content = ("\n".join(f"【第{i}轮】{turn}" for i, turn in enumerate(sample.turns, 1))
                           if sample.turns else sample.text)
                try:
                    intake = await ingest(deps.repos, user_id=user.id, content=content)
                    stage = "pipeline"
                    result = await pipeline.run(intake.message, intake.query_id)
                except Exception as exc:
                    error_type = f"{stage}:{type(exc).__name__}"
                    pipeline_errors[error_type] += 1
                    y_true.append(sample.label.value)
                    y_pred.append("safe")  # match the existing eval runner's no-verdict mapping
                    no_verdict_count += 1
                    rows.append({"dataset": current_dataset, "sample_id": sample.id,
                                 "label": sample.label.value, "level": "safe", "degraded": None,
                                 "has_verdict": False, "pipeline_error": error_type})
                    continue
                verdict = result.verdict
                level = verdict.level.value if verdict else "safe"
                degraded_count += int(result.degraded)
                no_verdict_count += int(verdict is None)
                y_true.append(sample.label.value)
                y_pred.append(level)
                rows.append({"dataset": current_dataset, "sample_id": sample.id, "label": sample.label.value,
                             "level": level, "degraded": result.degraded, "has_verdict": verdict is not None,
                             "latency_ms": result.latency_ms})
            scams = sum(label == "scam" for label in y_true)
            negatives = sum(label in ("benign", "edge") for label in y_true)
            summaries.append({"dataset": dataset, "count": len(samples), "scam_count": scams,
                              "negative_count": negatives,
                              "recall": recall(y_true, y_pred) if scams else None,
                              "fpr_strict": user_visible_false_positive_rate(y_true, y_pred) if negatives else None,
                              "degraded_count": degraded_count, "no_verdict_count": no_verdict_count,
                              "pipeline_error_count": sum(pipeline_errors.values()),
                              "pipeline_error_types": dict(pipeline_errors),
                              "retrieval_mode_counts": modes[current_dataset],
                              "status": "incomplete" if pipeline_errors else "candidate"})
    except Exception as exc:
        out.mkdir(parents=True, exist_ok=True)
        diag = getattr(exc, "diag", None)
        (out / "summary.json").write_text(json.dumps({"status": "incomplete", "error_type": type(exc).__name__,
            "error_code": getattr(exc, "sqlstate", None),
            "constraint": getattr(diag, "constraint_name", None),
            "table": getattr(diag, "table_name", None),
            "dataset": current_dataset, "sample_id": current_sample_id}, indent=2))
        print(f"end-to-end evaluation incomplete: {type(exc).__name__}", file=sys.stderr)
        return 2
    finally:
        await deps.pool.close()
    out.mkdir(parents=True, exist_ok=True)
    report = {"status": "candidate_only", "pipeline_version": PIPELINE_VERSION,
              "config": "PipelineConfig.product_default()", "provider": settings.get_chat_provider().name if settings.llm_enabled else "mock",
              "model": settings.get_chat_provider().model if settings.llm_enabled else None,
              "embedding_provider": settings.get_embedding_provider().name if settings.get_embedding_provider() else None,
              "embedding_model": settings.get_embedding_provider().model if settings.get_embedding_provider() else None,
              "embedding_dimensions": settings.embed_dimensions,
              "top_k": 2, "keyword_weight": 0.6, "started_at_epoch": started,
              "elapsed_seconds": round(time.time() - started, 3),
              "spec_sha256": _sha(ROOT / "spec/spec-design-case-retrieval.md"),
              "source_sha256": {str(path.relative_to(ROOT)): _sha(path) for path in
                  (ROOT / "src/homeshield/core/pipeline.py", ROOT / "src/homeshield/core/retrieval.py",
                   ROOT / "src/homeshield/core/features.py", ROOT / "src/homeshield/core/judge.py",
                   ROOT / "src/homeshield/core/llm.py")},
              "datasets": [{"path": name, "sha256": _sha(Path(name))} for name in datasets],
              "label_review": {"sample_id": "CW1-S08", "old_label": "fake_fee",
                               "candidate_label": "fake_refund", "status": "pending_data_owner_review"},
              "results": summaries,
              "old_new_comparison": {"status": "incomplete", "reason": "same-label pre-change product_default run not captured"}}
    (out / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "examples.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    print(json.dumps({"status": report["status"], "summary": str(out / "summary.json"),
                      "examples": str(out / "examples.jsonl")}, ensure_ascii=False))
    return 2


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", action="append", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--database-url")
    parser.add_argument("--database-suffix", default="retrieval_eval")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.dataset, Path(args.out), args.database_url, args.database_suffix)))


if __name__ == "__main__":
    main()
