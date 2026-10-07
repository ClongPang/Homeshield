"""FR-3 retrieval acceptance at the production top-2 setting."""
import json
from collections import Counter
from pathlib import Path

import pytest

from homeshield.core.knowledge.taxonomy import REGISTRY
from homeshield.core.models import Conversation, KbCase, Turn
from homeshield.core.pipeline import extract_conversation_rule_specs
from homeshield.core.retrieval import DEFAULT_TOP_K, Retriever, build_retrieval_query
from homeshield.eval.dataset import load_dataset

CASES_PATH = Path(__file__).resolve().parents[1] / "src/homeshield/core/knowledge/cases.json"


def _cases() -> list[KbCase]:
    return [KbCase(**c) for c in json.loads(CASES_PATH.read_text(encoding="utf-8"))]


def _query(sample) -> str:
    conv = (Conversation(turns=[Turn(text=text) for text in sample.turns])
            if sample.turns else Conversation.from_marked(sample.text))
    return build_retrieval_query(conv, extract_conversation_rule_specs(conv))


async def _hit_rate(samples: list) -> tuple[float, list[str]]:
    retriever = Retriever(_cases())
    hits, misses = 0, []
    for sample in samples:
        result = await retriever.search(_query(sample))
        if any(hit.case.scam_type == sample.scam_type for hit in result.hits):
            hits += 1
        else:
            misses.append(f"{sample.id}({sample.scam_type})")
    return hits / len(samples) if samples else 0, misses


async def test_registry_and_case_coverage():
    assert len(REGISTRY) == 16
    cases = _cases()
    by_type = Counter(case.scam_type for case in cases)
    assert set(by_type) == set(REGISTRY)
    assert min(by_type.values()) >= 3
    assert all(case.tactic and case.markers and case.advice and case.name for case in cases)
    assert DEFAULT_TOP_K == 2


async def test_fr3_gate_a_smoke_set_over_90():
    samples = [s for s in load_dataset("data/datasets/core/samples.jsonl")
               if s.label.value == "scam" and s.scam_type]
    rate, misses = await _hit_rate(samples)
    assert rate >= 0.9, f"Gate A 未达 90%: {rate:.2%}, miss={misses}"


def _gate_b_samples():
    path = Path("data/datasets/derived/fraud_r1/base.jsonl")
    if not path.exists():
        pytest.skip("缺少 derived/fraud_r1/base.jsonl；发布验收状态为 incomplete")
    samples = [s for s in load_dataset(path) if s.label.value == "scam" and s.scam_type]
    assert len(samples) == 27, f"Gate B 样本数变化，需人工复核规格: {len(samples)}"
    return samples


async def test_fr3_gate_b_release_gate_55():
    """Gate B 发布线（spec:55%，≥15/27），主指标为生产 top-2。

    2026-10-01 达线：知识库补收类型定义词/体裁词后 18/27（66.67%）。
    增强为 C01/C13/C14 补"刷单"（案例 tactic 自述词，检索索引缺收）、
    C28 补"资金调拨/履约保证金"与 C50 补"教育局"（公函体冒充类型的
    特征词，非针对单条样本）；Gate A、casework 与冻结基线逐分对照无回退。
    不得以改 top-k、降门槛或改代理标签维持该门限（spec REQ-008）。
    """
    rate, misses = await _hit_rate(_gate_b_samples())
    assert rate >= 0.55, f"Gate B top-2 发布线未达: {rate:.2%}, miss={misses}"


async def test_fr3_benign_hard_no_anchor_clustering():
    """benign_hard 的第二名关键词分数 <0.1：正常消息至多弱匹配单一案例。

    防止检索给 judge 制造"多案例聚簇"式锚定。0.1 阈值沿用规格对旧实现
    的同口径观察（第二名均 <0.1）；新实现实测最大 0.0896（BH-003）。
    第一名允许更高：官方原型消息（如航班退改签通知）合法命中其同类型参考卡。
    """
    labeled = list(load_dataset("data/datasets/core/benign_hard.jsonl"))
    retriever = Retriever(_cases())
    for sample in labeled:
        result = await retriever.search(_query(sample))
        scores = sorted((hit.keyword_score for hit in result.hits), reverse=True)
        second = scores[1] if len(scores) > 1 else 0.0
        assert second < 0.1, f"{sample.id} 检索出现多案例聚簇: 2nd={second:.4f}"
