"""消融矩阵:配置即数据,A/B/C 是同一条管线的开关组合。"""
from collections.abc import Awaitable, Callable

from core.pipeline import PipelineConfig
from eval.dataset import Sample
from eval.metrics import confusion3x3, fpr, fpr_strict, latency_percentiles, recall

ABLATION_CONFIGS: dict[str, PipelineConfig] = {
    "A_zero_shot": PipelineConfig.ablation_a(),
    "B_rag": PipelineConfig.ablation_b(),
    "C_full": PipelineConfig.product_default(),
}

RunFn = Callable[[Sample], Awaitable[tuple[str, int, int]]]  # -> (pred_level, score, latency_ms)


async def run_matrix(
    factory: Callable[[PipelineConfig], RunFn], samples: list[Sample]
) -> dict[str, dict]:
    results: dict[str, dict] = {}
    for name, config in ABLATION_CONFIGS.items():
        run = factory(config)
        rows = [await run(s) for s in samples]
        y_true = [s.label.value for s in samples]
        y_pred = [r[0] for r in rows]
        results[name] = {
            "confusion": confusion3x3(y_true, y_pred),
            "recall": round(recall(y_true, y_pred), 3),
            "fpr": round(fpr(y_true, y_pred), 3),
            "fpr_strict": round(fpr_strict(y_true, y_pred), 3),
            "latency": latency_percentiles([r[2] for r in rows]),
        }
    return results
