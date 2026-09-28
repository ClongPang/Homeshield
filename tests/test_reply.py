"""回复生成:结论行代码所有权、实际告警说明、截断保护、回退。"""
import asyncio

from homeshield.core.models import Feature, JudgeOutput, Level
from homeshield.core.reply import LLMReply, TemplateReply, add_delivery_notice, is_valid_reply


class FakeLLM:
    """只实现 chat_text 的桩,按序返回预置回复。"""

    def __init__(self, replies: list[str]):
        self.replies = replies
        self.calls = 0

    async def chat_text(self, task: str, system: str, user: str) -> str:
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        return reply


def _verdict(level: Level) -> JudgeOutput:
    return JudgeOutput(level=level, confidence=80, cited_ids=[], reason="")


def _features() -> list[Feature]:
    return [
        Feature(id="F01", type="transfer", value="转账"),
        Feature(id="F02", type="urgency", value="马上"),
    ]


def test_llm_cannot_override_conclusion():
    """LLM 把结论写成 safe 语气,产出仍由代码按判定级别决定。"""
    llm = FakeLLM(["【结论】看起来没什么问题\n【依据】出现转账要求\n【建议】不要理会"])
    reply = asyncio.run(LLMReply(llm).generate(_verdict(Level.DANGEROUS), _features(), []))
    assert reply.startswith("【结论】⚠️ 这是典型骗术，千万别转钱")
    assert "看起来没什么问题" not in reply
    assert is_valid_reply(reply)


def test_dangerous_reply_no_identity_claim_and_official_fallback():
    """高危结论不断言发送者身份(免责),96110 官方兜底后缀代码所有、不被 LLM 覆盖。"""
    llm = FakeLLM(["【依据】对方自称公检法\n【建议】挂断并拨110核实"])
    reply = asyncio.run(LLMReply(llm).generate(_verdict(Level.DANGEROUS), _features(), []))
    assert "是骗子" not in reply
    assert "紧急可拨反诈专线96110" in reply
    assert is_valid_reply(reply)


def test_delivery_notice_is_added_only_by_alert_coordinator():
    llm = FakeLLM(["【依据】对方自称公检法\n【建议】挂断并拨110核实"])
    dangerous = asyncio.run(LLMReply(llm).generate(_verdict(Level.DANGEROUS), _features(), []))
    assert "查询提醒已加入妈妈的提醒列表" not in dangerous
    assert add_delivery_notice(dangerous,"查询提醒已加入妈妈的提醒列表").endswith("查询提醒已加入妈妈的提醒列表")
    safe = asyncio.run(LLMReply(llm).generate(_verdict(Level.SAFE), _features(), []))
    assert add_delivery_notice(safe,"") == safe


def test_fallback_to_template_on_garbage():
    llm = FakeLLM(["我不知道你在说什么", "这是一条消息"])
    reply = asyncio.run(LLMReply(llm).generate(_verdict(Level.SUSPICIOUS), _features(), []))
    assert reply.startswith("【结论】⚠️ 这条消息有问题，多留个心眼")
    assert "要求转账：转账" in reply  # 模板兜底的依据取自特征,类型以中文标签呈现
    assert "transfer" not in reply  # 机器 id 不出用户面
    assert is_valid_reply(reply)


def test_unknown_feature_type_falls_back_to_chinese_label():
    features = [Feature(id="F01", type="not_a_mechanic", value="奇怪内容")]
    reply = asyncio.run(TemplateReply().generate(_verdict(Level.SUSPICIOUS), features, []))
    assert "可疑特征：奇怪内容" in reply


def test_delivery_notice_survives_truncation():
    """依据/建议超长时截正文,实际送达说明仍保留。"""
    long_value = "https://very-long-scam-domain.example.com/path?token=" + "x" * 60
    features = [Feature(id=f"F0{i}", type="url", value=long_value, evidence_span=long_value) for i in (1, 2)]
    verdict = JudgeOutput(level=Level.DANGEROUS, confidence=90, cited_ids=["F01", "F02"], reason="")
    reply = add_delivery_notice(asyncio.run(TemplateReply().generate(verdict, features, [])),"查询提醒已加入妈妈的提醒列表")
    assert len(reply) <= 150
    assert reply.endswith("查询提醒已加入妈妈的提醒列表")
    assert is_valid_reply(reply)


def test_template_reply_safe_no_suffix():
    reply = asyncio.run(TemplateReply().generate(_verdict(Level.SAFE), _features(), []))
    assert reply.startswith("【结论】没发现已知骗术的特征")
    assert "查询提醒已加入妈妈的提醒列表" not in reply
    assert is_valid_reply(reply)


def test_safe_advice_code_owned():
    """safe 结论只说"未发现"不说"安全";兜底建议代码所有,LLM 不得代写。"""
    llm = FakeLLM(["【依据】没有风险\n【建议】随便花没关系"])
    reply = asyncio.run(LLMReply(llm).generate(_verdict(Level.SAFE), _features(), []))
    assert reply.startswith("【结论】没发现已知骗术的特征")
    assert "涉及转账、验证码" in reply
    assert "随便花" not in reply
    assert "看起来没什么问题" not in reply
    assert is_valid_reply(reply)


def test_cross_message_basis_override():
    basis = "前情有保密要求，本条又要求转账（跨消息）"
    llm = FakeLLM(["【依据】旧截图里写了秘密账号\\n【建议】先和家人核实"])
    reply = asyncio.run(LLMReply(llm).generate(
        _verdict(Level.DANGEROUS), _features(), [], basis_override=basis
    ))
    assert basis in reply and "秘密账号" not in reply
    assert is_valid_reply(reply)
    plain = asyncio.run(TemplateReply().generate(_verdict(Level.DANGEROUS), _features(), []))
    assert "要求转账：转账" in plain
