"""双 Judge 契约测试与引用校验降级。"""

import pytest

from homeshield.core.errors import DegradeError
from homeshield.core.features import assign_feature_ids, extract_rule_features
from homeshield.core.judge import (
    LLMJudge,
    MockJudge,
    citations_valid,
    judge_with_validation,
)
from homeshield.core.llm import MockLLM
from homeshield.core.models import Feature, JudgeInput, JudgeOutput, Level, Mode

TEXT = "别告诉家人,立即转账5万元到安全账户"


def _input() -> JudgeInput:
    return JudgeInput(text=TEXT, features=assign_feature_ids(extract_rule_features(TEXT)), cases=[])


JUDGES = [MockJudge(), LLMJudge(MockLLM())]


@pytest.mark.parametrize("judge", JUDGES, ids=lambda j: j.mode.value)
async def test_judge_contract(judge):
    out = await judge.judge(_input(), constrained=True)
    assert out.level in Level
    assert 0 <= out.confidence <= 100
    assert set(out.cited_ids) <= {f.id for f in _input().features}
    assert out.reason


async def test_citations_valid_rejects_fabricated():
    out = JudgeOutput(level=Level.SUSPICIOUS, confidence=50, cited_ids=["F99"], reason="x")
    assert not citations_valid(out, {"F01"})


async def test_validation_retries_then_degrade():
    class BadJudge:
        mode = Mode.LLM

        async def judge(self, inp, *, constrained):
            return JudgeOutput(level=Level.SUSPICIOUS, confidence=50, cited_ids=["F99"], reason="x")

    inp = JudgeInput(text="t", features=[Feature(id="F01", type="url", value="x")], cases=[])
    with pytest.raises(DegradeError):
        await judge_with_validation(inp, BadJudge(), constrained=True, retries=2)


class FixedJudge:
    """固定输出的桩:门槛行为只取决于产出,与具体 Judge 实现无关。"""

    mode = Mode.LLM

    def __init__(self, out: JudgeOutput):
        self.out = out
        self.calls = 0

    async def judge(self, inp, *, constrained):
        self.calls += 1
        return self.out


async def test_safe_confidence_floor_degrades_low_confidence():
    """低置信 safe 重试耗尽后降级"拿不准",不得出 safe。"""
    judge = FixedJudge(JudgeOutput(level=Level.SAFE, confidence=40, cited_ids=[], reason="x"))
    with pytest.raises(DegradeError):
        await judge_with_validation(_input(), judge, constrained=True, retries=2, safe_confidence_floor=60)
    assert judge.calls == 3


async def test_safe_confidence_floor_passes_high_confidence():
    judge = FixedJudge(JudgeOutput(level=Level.SAFE, confidence=70, cited_ids=["F01"], reason="x"))
    out = await judge_with_validation(_input(), judge, constrained=True, safe_confidence_floor=60)
    assert out.level is Level.SAFE


async def test_safe_confidence_floor_spares_non_safe():
    """门槛只守 safe 下限:dangerous 低置信照常出库(宁误报不漏报)。"""
    judge = FixedJudge(JudgeOutput(level=Level.DANGEROUS, confidence=10, cited_ids=[], reason="x"))
    out = await judge_with_validation(_input(), judge, constrained=True, safe_confidence_floor=60)
    assert out.level is Level.DANGEROUS


async def test_safe_confidence_floor_disabled_when_zero():
    judge = FixedJudge(JudgeOutput(level=Level.SAFE, confidence=5, cited_ids=[], reason="x"))
    out = await judge_with_validation(_input(), judge, constrained=True, safe_confidence_floor=0)
    assert out.level is Level.SAFE


async def test_safe_confidence_floor_independent_of_constrained():
    """门槛是产品安全策略,不受引用约束开关影响。"""
    judge = FixedJudge(JudgeOutput(level=Level.SAFE, confidence=10, cited_ids=["F99"], reason="x"))
    with pytest.raises(DegradeError):
        await judge_with_validation(_input(), judge, constrained=False, safe_confidence_floor=60)
