"""机制内联标注与分级语义注入:重构二+五的单元与契约测试。"""
import asyncio

from homeshield.core.annotate import annotate_text
from homeshield.core.judge import LLMJudge, MockJudge
from homeshield.core.llm import MockLLM
from homeshield.core.models import Feature, JudgeInput, Level


class CaptureLLM(MockLLM):
    """记录 system/user 透传内容,断言 prompt 组装。"""

    def __init__(self):
        super().__init__()
        self.captured: list[tuple[str, str]] = []

    async def chat_json(self, task: str, system: str, user: str, schema: dict) -> dict:
        self.captured.append((system, user))
        return await super().chat_json(task, system, user, schema)


def _feats() -> list[Feature]:
    return [
        Feature(id="F01", type="isolation", value="别告诉家人", evidence_span="别告诉家人"),
        Feature(id="F02", type="transfer", value="转账", evidence_span="转账"),
        Feature(id="F03", type="url", value="http://x.top/a", evidence_span="http://x.top/a"),
        Feature(id="F04", type="semantic", value="屏幕共享", evidence_span="屏幕共享"),  # 无映射
        Feature(id="F05", type="urgency", value="马上", evidence_span="马上"),
    ]


def test_annotate_wraps_mechanic_names():
    text = "别告诉家人,马上转账,点 http://x.top/a 开屏幕共享填信息"
    out = annotate_text(text, _feats())
    assert "<隔离封口>别告诉家人</隔离封口>" in out
    assert "<紧迫施压>马上</紧迫施压>" in out
    assert "<资金动作>转账</资金动作>" in out
    assert "<渠道逃逸>http://x.top/a</渠道逃逸>" in out
    assert "屏幕共享" in out and "<屏幕共享" not in out  # 无映射类型不标注但保留


def test_annotate_overlap_keeps_longest_at_same_start():
    feats = [
        Feature(id="F1", type="transfer", value="转账5万元", evidence_span="转账5万元"),
        Feature(id="F2", type="urgency", value="转账", evidence_span="转账"),
    ]
    out = annotate_text("请转账5万元", feats)
    assert out == "请<资金动作>转账5万元</资金动作>"
    assert "<紧迫施压>" not in out  # 重叠的短跨度被吞掉,不嵌套


def test_annotate_multiple_occurrences_and_untouched_text():
    feats = [Feature(id="F1", type="transfer", value="转账", evidence_span="转账")]
    assert annotate_text("转账转账", feats) == "<资金动作>转账</资金动作><资金动作>转账</资金动作>"
    assert annotate_text("今天天气不错", _feats()) == "今天天气不错"
    assert annotate_text("今天天气不错", []) == "今天天气不错"


def test_llm_judge_prompt_semantics_and_annotation():
    llm = CaptureLLM()
    text = "别告诉家人,马上转账"
    inp = JudgeInput(
        text=text,
        features=_feats(),
        cases=[],
        annotated_text=annotate_text(text, _feats()),
        graded_semantics=True,
    )
    asyncio.run(LLMJudge(llm).judge(inp, constrained=True))
    system, user = llm.captured[0]
    assert "分级语义" in system and "不得据此判 safe" in system
    assert "标注≠结论" in system
    assert "annotated_text" in user and "<隔离封口>" in user


def test_llm_judge_prompt_clean_without_flags():
    llm = CaptureLLM()
    inp = JudgeInput(text="t", features=_feats(), cases=[])
    asyncio.run(LLMJudge(llm).judge(inp, constrained=True))
    system, user = llm.captured[0]
    assert "分级语义" not in system and "标注≠结论" not in system
    assert "annotated_text" not in user


def test_mock_judge_ignores_new_fields():
    out = asyncio.run(
        MockJudge().judge(
            JudgeInput(text="转账", features=_feats(), cases=[],
                       annotated_text="<资金动作>转账</资金动作>", graded_semantics=True),
            constrained=True,
        )
    )
    assert out.level in list(Level)


def test_annotate_direct_mechanic_id():
    """重构三:LLM 补抽的机制 id 特征直接标注。"""
    feats = [Feature(id="F01", type="money", value="转账", evidence_span="转账",
                     source="llm", confidence=9)]
    assert "<资金动作>转账</资金动作>" in annotate_text("请立即转账", feats)
