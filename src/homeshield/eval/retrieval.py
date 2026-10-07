"""Reproducible offline retrieval evaluation; no judge, reply, or DB access.

退出码:0=要求的验收完成且通过;1=完成但门槛失败;2=配置/数据/服务原因未完成
(含 hybrid/vector 发生回退——按规格该轮质量状态 incomplete)。

--label-reviewer/--label-review-rationale 为数据负责人的自证签字:命令行只
记录签字人姓名与依据,不做身份核验;其作用是把 casework_v1 相关数据集从
"待复核"(status 含 pending_data_owner_review)推进到"已记录复核"。正式的
防篡改控制在规格 B 阶段:标注文件与分组清单冻结哈希(REQ-009)。
"""
import argparse
import asyncio
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from homeshield.core.config import Settings
from homeshield.core.deps import load_cases
from homeshield.core.knowledge.taxonomy import REGISTRY
from homeshield.core.llm import OpenAICompatLLM
from homeshield.core.models import Conversation, Turn
from homeshield.core.pipeline import PIPELINE_VERSION, extract_conversation_rule_specs
from homeshield.core.retrieval import DEFAULT_TOP_K, Retriever, build_retrieval_query
from homeshield.eval.dataset import Sample, load_dataset

ROOT = Path(__file__).resolve().parents[3]
SPEC_PATH = ROOT / "spec/spec-design-case-retrieval.md"
CASES_PATH = ROOT / "src/homeshield/core/knowledge/cases.json"


def _embed_identity(llm: object) -> tuple[str | None, str | None]:
    """从 LLMPort 的公开 embedding_label 解析 (provider, model);不可得时 (None, None)。"""
    label = getattr(llm, "embedding_label", "")
    if not label or "unknown" in label:
        return None, None
    fields = dict(part.split("=", 1) for part in label.split() if "=" in part)
    return fields.get("provider"), fields.get("model")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_hashes() -> dict[str, str]:
    files = [ROOT / "src/homeshield/core/retrieval.py", ROOT / "src/homeshield/core/features.py",
             ROOT / "src/homeshield/core/pipeline.py", ROOT / "src/homeshield/core/llm.py",
             ROOT / "src/homeshield/core/judge.py", ROOT / "src/homeshield/core/deps.py",
             ROOT / "src/homeshield/eval/retrieval.py", CASES_PATH]
    return {str(path.relative_to(ROOT)): _sha(path) for path in files if path.is_file()}


def _conversation(sample: Sample) -> Conversation:
    if sample.turns:
        return Conversation(turns=[Turn(text=text) for text in sample.turns])
    return Conversation.from_marked(sample.text)


def _labeled_scams(samples: list[Sample]) -> list[Sample]:
    return [sample for sample in samples if sample.label.value == "scam" and sample.scam_type]


async def _evaluate_dataset(path: Path, retriever: Retriever, mode: str) -> tuple[dict, list[dict]]:
    samples = load_dataset(path)
    rows, hit_count, fallbacks, errors, misses = [], 0, 0, 0, []
    started = time.perf_counter()
    for sample in samples:
        conversation = _conversation(sample)
        specs = extract_conversation_rule_specs(conversation)
        query = build_retrieval_query(conversation, specs)
        tic = time.perf_counter()
        try:
            result = await retriever.search(query)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            errors += 1
            if sample.label.value == "scam" and sample.scam_type:
                misses.append(sample.id)
            rows.append({"sample_file": path.name, "sample_id": sample.id,
                         "label": sample.label.value, "scam_type": sample.scam_type,
                         "relevant_case_ids": None, "mode": "error",
                         "fallback_reason": type(exc).__name__, "hits": [],
                         "same_type_hit": False if sample.label.value == "scam" and sample.scam_type else None,
                         "latency_ms": round((time.perf_counter() - tic) * 1000, 3)})
            continue
        elapsed_ms = round((time.perf_counter() - tic) * 1000, 3)
        hits = result.hits
        if mode == "vector" and result.mode == "keyword_fallback":
            hits = [type(hit)(hit.case, hit.keyword_score, None, 0.6 * hit.keyword_score)
                    for hit in hits]
        if result.mode == "keyword_fallback":
            fallbacks += 1
        matched = bool(sample.scam_type and any(hit.case.scam_type == sample.scam_type for hit in hits))
        if sample.label.value == "scam" and sample.scam_type:
            if matched:
                hit_count += 1
            else:
                misses.append(sample.id)
        rows.append({
            "sample_file": path.name, "sample_id": sample.id,
            "label": sample.label.value, "scam_type": sample.scam_type,
            "relevant_case_ids": None, "mode": result.mode,
            "fallback_reason": result.fallback_reason,
            "hits": [{"case_id": hit.case.id, "keyword_score": hit.keyword_score,
                      "vector_score": hit.vector_score, "score": hit.score} for hit in hits],
            "same_type_hit": matched if sample.label.value == "scam" and sample.scam_type else None,
            "latency_ms": elapsed_ms,
        })
    scams = _labeled_scams(samples)
    is_proxy = path.as_posix().endswith("fraud_r1/base.jsonl")
    required = 0.9 if path.name == "samples.jsonl" else (0.55 if is_proxy else None)
    rate = hit_count / len(scams) if scams else None
    status = "incomplete" if errors or (mode != "keyword" and fallbacks) else "pass"
    if required is not None and rate is not None and rate < required:
        status = "fail"
    summary = {
        "dataset": str(path), "count": len(samples), "scam_count": len(scams),
        "same_type_hit_at_2": rate, "hits": hit_count, "misses": misses,
        "threshold": required, "fallback_count": fallbacks, "error_count": errors,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3), "status": status,
    }
    return summary, rows


def validate_relevance_annotations(path: Path, samples_by_file: dict[str, list[Sample]], case_ids: set[str]) -> list[dict]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    seen = set()
    known = {(name, sample.id) for name, samples in samples_by_file.items() for sample in samples}
    if any(sample.scam_type and sample.scam_type not in REGISTRY
           for samples in samples_by_file.values() for sample in samples):
        raise ValueError("dataset contains unknown scam taxonomy")
    for row in rows:
        key = (row["sample_file"], row["sample_id"])
        if key in seen or key not in known:
            raise ValueError(f"duplicate or unknown annotation sample: {key}")
        seen.add(key)
        if row.get("reviewed") is not True:
            if row.get("relevant_case_ids") is not None:
                raise ValueError("unreviewed relevance annotation must use null")
            continue
        if not row.get("reviewer") or not row.get("rationale") or not isinstance(row.get("relevant_case_ids"), list):
            raise ValueError("reviewed annotation requires reviewer, rationale, and case list")
        ids = row["relevant_case_ids"]
        if len(ids) != len(set(ids)) or not set(ids) <= case_ids:
            raise ValueError("annotation contains duplicate or unknown case IDs")
        if not row.get("group_id"):
            raise ValueError("annotation requires group_id")
    return rows


def _relevance_calibration(rows: list[dict], annotation_path: Path) -> dict:
    # Prevent an unreviewed or unfrozen annotation file from being treated as calibration.
    # 校准与验证集切分(SHA-256(group_id) mod 5)须待分组清单冻结后按 REQ-009 执行,
    # 在此之前一律报告 pending;同一 group_id 的哈希是确定值,此处无从校验切分本身。
    reviewed = [row for row in rows if row.get("reviewed") is True]
    return {"status": "pending", "reviewed_count": len(reviewed),
            "annotation_sha256": _sha(annotation_path),
            "reason": "frozen group manifest and actual retrieval scores are required before calibration"}


async def run(args: argparse.Namespace) -> int:
    datasets = [Path(p) for p in args.dataset]
    missing = [str(path) for path in datasets if not path.is_file()]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc).isoformat()
    if missing:
        report = {"status": "incomplete", "retrieval_mode_requested": args.retrieval_mode,
                  "pipeline_version": PIPELINE_VERSION, "started_at": started,
                  "missing_data": missing, "source_sha256": _source_hashes(),
                  "cases_sha256": _sha(CASES_PATH), "command": sys.argv}
        (out / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"missing evaluation data: {', '.join(missing)}", file=sys.stderr)
        return 2
    settings = Settings.load()
    llm = None
    if args.retrieval_mode != "keyword":
        try:
            embed_provider = settings.get_embedding_provider() if settings.llm_enabled else None
        except Exception as exc:
            (out / "summary.json").write_text(json.dumps({"status": "incomplete",
                "error_type": type(exc).__name__, "retrieval_mode_requested": args.retrieval_mode}, indent=2), encoding="utf-8")
            print("hybrid/vector evaluation configuration is invalid", file=sys.stderr)
            return 2
        if not settings.llm_enabled or embed_provider is None:
            (out / "summary.json").write_text(json.dumps({"status": "incomplete",
                "error_type": "EmbeddingProviderUnavailable", "retrieval_mode_requested": args.retrieval_mode}, indent=2), encoding="utf-8")
            print("hybrid/vector evaluation requires MODE=llm and EMBED_PROVIDER", file=sys.stderr)
            return 2
        try:
            llm = OpenAICompatLLM(settings)
        except Exception as exc:
            (out / "summary.json").write_text(json.dumps({"status": "incomplete",
                "error_type": type(exc).__name__, "retrieval_mode_requested": args.retrieval_mode}, indent=2), encoding="utf-8")
            return 2
    weight = {"keyword": 0.6, "hybrid": 0.6, "vector": 0.0}[args.retrieval_mode]
    retriever = Retriever(load_cases(), llm, top_k=DEFAULT_TOP_K, keyword_weight=weight)
    data_summaries, all_rows = [], []
    service_error = None
    try:
        for path in datasets:
            summary, rows = await _evaluate_dataset(path, retriever, args.retrieval_mode)
            data_summaries.append(summary)
            all_rows.extend(rows)
    except Exception as exc:
        service_error = type(exc).__name__
        print(f"retrieval evaluation incomplete: {service_error}", file=sys.stderr)
    relevance = None
    annotation_sha = None
    if args.relevance:
        apath = Path(args.relevance)
        try:
            samples_by_file = {path.name: load_dataset(path) for path in datasets if path.is_file()}
            annotations = validate_relevance_annotations(apath, samples_by_file,
                                                         {case.id for case in load_cases()})
            relevance = _relevance_calibration(annotations, apath)
            annotation_index = {(row["sample_file"], row["sample_id"]): row for row in annotations}
            for example in all_rows:
                annotation = annotation_index.get((example["sample_file"], example["sample_id"]))
                if annotation:
                    example["relevant_case_ids"] = annotation.get("relevant_case_ids")
                    example["relevance_reviewed"] = annotation.get("reviewed") is True
            annotation_sha = _sha(apath)
        except Exception as exc:
            (out / "summary.json").write_text(json.dumps({"status": "incomplete",
                "error_type": type(exc).__name__, "retrieval_mode_requested": args.retrieval_mode,
                "relevance_path": args.relevance, "started_at": started,
                "pipeline_version": PIPELINE_VERSION, "command": sys.argv,
                "source_sha256": _source_hashes(), "cases_sha256": _sha(CASES_PATH)},
                ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"relevance annotations invalid: {type(exc).__name__}", file=sys.stderr)
            return 2
    baseline_comparison = {"status": "incomplete", "reason": "paired old-implementation report not supplied"}
    baseline_incomplete = True
    if args.baseline_report:
        try:
            baseline = json.loads(Path(args.baseline_report).read_text(encoding="utf-8"))
            if baseline.get("top_k") != DEFAULT_TOP_K or baseline.get("mode") != args.retrieval_mode:
                raise ValueError("baseline mode/top_k mismatch")
            if baseline.get("cases_sha256") != _sha(CASES_PATH):
                raise ValueError("baseline cases hash mismatch")
            current_provider, current_model = _embed_identity(llm)
            if baseline.get("provider") != current_provider or baseline.get("model") != current_model:
                raise ValueError("baseline embedding provider/model mismatch")
            casework_path = next((path for path in datasets if path.name == "casework_v1.jsonl"), None)
            if casework_path and baseline.get("corrected_casework_sha256") != _sha(casework_path):
                raise ValueError("baseline corrected-label dataset hash mismatch")
            base_by_name = {Path(item["dataset"]).name: item for item in baseline["datasets"]}
            comparisons = []
            for current in data_summaries:
                key = "casework_v1_corrected.jsonl" if Path(current["dataset"]).name == "casework_v1.jsonl" else Path(current["dataset"]).name
                old = base_by_name.get(key)
                if old is None:
                    comparisons.append({"dataset": current["dataset"], "status": "incomplete", "reason": "baseline dataset missing"})
                    continue
                old_rate, candidate_rate = old["hit_at_2"], current["same_type_hit_at_2"]
                if old_rate is None and candidate_rate is None:
                    comparison_status = "not_applicable"
                elif old_rate is None or candidate_rate is None:
                    comparison_status = "incomplete"
                elif current["error_count"] or (args.retrieval_mode != "keyword" and current["fallback_count"]):
                    comparison_status = "incomplete"
                else:
                    comparison_status = "pass" if candidate_rate >= old_rate else "fail"
                comparisons.append({"dataset": current["dataset"], "old_hit_at_2": old["hit_at_2"],
                                    "candidate_hit_at_2": current["same_type_hit_at_2"],
                                    "old_hits": old["hits"], "candidate_hits": current["hits"],
                                    "old_misses": old["misses"], "candidate_misses": current["misses"],
                                    "status": comparison_status})
            baseline_comparison = {"status": "fail" if any(item.get("status") == "fail" for item in comparisons) else
                                   "incomplete" if any(item.get("status") == "incomplete" for item in comparisons) else "pass",
                                   "baseline_report": args.baseline_report,
                                   "baseline_retrieval_sha256": baseline.get("retrieval_sha256"),
                                   "baseline_cases_sha256": baseline.get("cases_sha256"),
                                   "baseline_corrected_casework_sha256": baseline.get("corrected_casework_sha256"),
                                   "comparisons": comparisons}
            casework_comparison = next((item for item in comparisons
                                         if Path(item["dataset"]).name == "casework_v1.jsonl"), None)
            if casework_comparison:
                for item in data_summaries:
                    if Path(item["dataset"]).name == "casework_v1.jsonl":
                        item["status"] = casework_comparison["status"]
            baseline_incomplete = baseline_comparison["status"] == "incomplete"
        except Exception as exc:
            baseline_comparison = {"status": "incomplete", "reason": type(exc).__name__}
    ended = datetime.now(timezone.utc).isoformat()
    fallback_count = sum(item["fallback_count"] for item in data_summaries)
    required_failed = any(item["status"] == "fail" for item in data_summaries) or baseline_comparison["status"] == "fail"
    label_review_complete = bool(args.label_reviewer and args.label_review_rationale)
    label_review_incomplete = any(path.name == "casework_v1.jsonl" for path in datasets) and not label_review_complete
    # missing 非空时已在入口提前返回,此处恒为空,仅保留字段供报告 schema 稳定
    incomplete = (bool(service_error) or baseline_incomplete or label_review_incomplete
                  or any(item["error_count"] or
                         (args.retrieval_mode != "keyword" and item["fallback_count"])
                         for item in data_summaries))
    report = {
        "spec": {"path": "spec/spec-design-case-retrieval.md", "sha256": _sha(SPEC_PATH) if SPEC_PATH.exists() else None},
        "pipeline_version": PIPELINE_VERSION, "retrieval_mode_requested": args.retrieval_mode,
        "provider": _embed_identity(llm)[0], "model": _embed_identity(llm)[1],
        "embed_dimensions": getattr(llm, "_embed_dimensions", None),
        "top_k": DEFAULT_TOP_K, "keyword_weight": weight,
        "started_at": started, "ended_at": ended, "command": sys.argv,
        "source_sha256": _source_hashes(), "cases_sha256": _sha(CASES_PATH),
        "dataset_sha256": {str(path): _sha(path) for path in datasets if path.is_file()},
        "label_review": {"sample_id": "CW1-S08", "old_label": "fake_fee",
                         "candidate_label": "fake_refund",
                         "status": "reviewed" if label_review_complete else "pending_data_owner_review",
                         "reviewer": args.label_reviewer if label_review_complete else None,
                         "rationale": args.label_review_rationale if label_review_complete else None},
        "annotation_sha256": annotation_sha, "missing_data": missing,
        "service_error": service_error,
        "fallback_count": fallback_count,
        "datasets": data_summaries, "relevance_calibration": relevance,
        "baseline_comparison": baseline_comparison,
        "end_to_end_product_default": {"status": "incomplete", "reason": "paired old/new Pipeline runs with degraded counts not captured"},
        "relevance_stage_b": {"status": "pending", "reason": "human-reviewed relevance labels and frozen group manifest not present"},
        "status": "incomplete" if incomplete else "fail" if required_failed else "pass",
    }
    (out / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (out / "examples.jsonl").open("w", encoding="utf-8") as stream:
        for row in all_rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps({"status": report["status"], "summary": str(out / "summary.json"),
                      "examples": str(out / "examples.jsonl")}, ensure_ascii=False))
    return 2 if incomplete else 1 if required_failed else 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--retrieval-mode", choices=("keyword", "hybrid", "vector"), required=True)
    parser.add_argument("--dataset", action="append", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--relevance")
    parser.add_argument("--baseline-report", help="paired pre-change report for the same mode/top-k and corrected labels")
    parser.add_argument("--label-reviewer", help="data owner who approved the CW1-S08 label correction")
    parser.add_argument("--label-review-rationale", help="approval basis for CW1-S08 fake_fee -> fake_refund")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
