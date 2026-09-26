"""消融矩阵:配置即数据,A/B/C 是同一条管线的开关组合;支持断点续跑。"""
import json
from collections.abc import Awaitable, Callable
from pathlib import Path

from core.pipeline import PipelineConfig
from eval.dataset import Sample
from eval.metrics import confusion3x3, fpr, fpr_strict, latency_percentiles, recall

ABLATION_CONFIGS: dict[str, PipelineConfig] = {
    "A_zero_shot": PipelineConfig.ablation_a(),
    "B_rag": PipelineConfig.ablation_b(),
    "C_full": PipelineConfig.ablation_c(),
    "D_semantics": PipelineConfig.ablation_d(),  # 重构五:分级语义
    "E_semantics_inline": PipelineConfig.ablation_e(),  # 重构二+五:加机制内联标注
}

RunFn = Callable[[Sample], Awaitable[tuple[str, int, int]]]  # -> (pred_level, score, latency_ms)


async def run_matrix(
    factory: Callable[[PipelineConfig], RunFn], samples: list[Sample],
    names: list[str] | None = None, checkpoint: str | None = None,
) -> dict[str, dict]:
    results: dict[str, dict] = {}
    configs = ABLATION_CONFIGS if names is None else {k: ABLATION_CONFIGS[k] for k in names}
    done: dict[tuple[str, str], tuple[str, int, int]] = {}
    if checkpoint and Path(checkpoint).exists():  # 断点续跑:已完成 (配置,样本) 直接回放
        for line in Path(checkpoint).read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                done[(r["config"], r["id"])] = (r["level"], r["score"], r["latency"])
    ckpt = open(checkpoint, "a", encoding="utf-8") if checkpoint else None
    try:
        for name, config in configs.items():
            run = factory(config)
            rows = []
            for i, s in enumerate(samples, 1):
                key = (name, s.id)
                if key in done:
                    rows.append(done[key])
                    print(f"[{name}] {i}/{len(samples)} {s.id} (checkpoint)", flush=True)
                    continue
                out = await run(s)
                rows.append(out)
                if ckpt:
                    ckpt.write(json.dumps(
                        {"config": name, "id": s.id, "level": out[0],
                         "score": out[1], "latency": out[2]}, ensure_ascii=False) + "\n")
                    ckpt.flush()
                print(f"[{name}] {i}/{len(samples)} {s.id}", flush=True)  # 进度可见性
            # 每配置即时结算(此前误入 finally,导致只留最后一个配置的结果)
            y_true = [s.label.value for s in samples]
            y_pred = [r[0] for r in rows]
            results[name] = {
                "confusion": confusion3x3(y_true, y_pred),
                "recall": round(recall(y_true, y_pred), 3),
                "fpr": round(fpr(y_true, y_pred), 3),
                "fpr_strict": round(fpr_strict(y_true, y_pred), 3),
                "latency": latency_percentiles([r[2] for r in rows]),
            }
    finally:
        if ckpt:
            ckpt.close()
    return results
