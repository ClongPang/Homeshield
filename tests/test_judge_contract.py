"""双 Judge 契约测试与引用校验降级。"""
import asyncio

import pytest

from core.errors import DegradeError
from core.features import assign_ids, extract_rules
from core.judge import (
    LLMJudge,
    MockJudge,
    citations_valid,
    judge_with_validation,
)
from core.llm import MockLLM
from core.models import Feature, JudgeInput, JudgeOutput, Level, Mode

TEXT = "别告诉家人,立即转账5万元到安全账户"


def _input() -> JudgeInput:
    return JudgeInput(text=TEXT, features=assign_ids(extract_rules(TEXT)), cases=[])


JUDGES = [MockJudge(), LLMJudge(MockLLM())]


@pytest.mark.parametrize("judge", JUDGES, ids=lambda j: j.mode.value)
def test_judge_contract(judge):
    out = asyncio.run(judge.judge(_input(), constrained=True))
    assert out.level in Level
    assert 0 <= out.confidence <= 100
    assert set(out.cited_ids) <= {f.id for f in _input().features}
    assert out.reason


def test_citations_valid_rejects_fabricated():
    out = JudgeOutput(level=Level.SUSPICIOUS, confidence=50, cited_ids=["F99"], reason="x")
    assert not citations_valid(out, {"F01"})


def test_validation_retries_then_degrade():
    class BadJudge:
        mode = Mode.LLM

        async def judge(self, inp, *, constrained):
            return JudgeOutput(level=Level.SUSPICIOUS, confidence=50, cited_ids=["F99"], reason="x")

    inp = JudgeInput(text="t", features=[Feature(id="F01", type="url", value="x")], cases=[])
    with pytest.raises(DegradeError):
        asyncio.run(judge_with_validation(inp, BadJudge(), constrained=True, retries=2))
