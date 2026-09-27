"""断点续跑:管线版本戳使旧引擎断点失效,回放只认当前版本。"""
import asyncio
import json

from homeshield.core.pipeline import PIPELINE_VERSION, PipelineConfig
from homeshield.eval.ablation import run_ablation_matrix
from homeshield.eval.dataset import Sample


def _samples() -> list[Sample]:
    return [
        Sample(id="T1", text="垫付做任务返佣金", label="scam", scam_type="task_scam", source="adapted:测试"),
        Sample(id="T2", text="今天天气不错", label="benign", source="adapted:测试"),
    ]


def test_checkpoint_version_stamp(tmp_path):
    ckpt = tmp_path / "ckpt.jsonl"
    calls = {"n": 0}

    def factory(cfg):
        async def run(s):
            calls["n"] += 1
            return ("suspicious", 50, 5)
        return run

    # 预置一条旧版本断点:T1 不得回放
    ckpt.write_text(
        json.dumps({"v": "0.0.0-旧引擎", "config": "A_zero_shot", "id": "T1",
                    "level": "safe", "score": 1, "latency": 1}) + "\n",
        encoding="utf-8",
    )
    asyncio.run(run_ablation_matrix(factory, _samples(), names=["A_zero_shot"], checkpoint=str(ckpt)))
    assert calls["n"] == 2  # 旧版本断点被忽略,T1/T2 均重新执行
    rows = [json.loads(l) for l in ckpt.read_text(encoding="utf-8").splitlines() if l.strip()]
    current = [r for r in rows if r["v"] == PIPELINE_VERSION]
    assert {(r["config"], r["id"]) for r in current} == {("A_zero_shot", "T1"), ("A_zero_shot", "T2")}
    # 旧行仍留在追加式日志里,但恢复时被版本校验跳过

    # 二次运行:同版本断点全量回放,零执行
    asyncio.run(run_ablation_matrix(factory, _samples(), names=["A_zero_shot"], checkpoint=str(ckpt)))
    assert calls["n"] == 2
