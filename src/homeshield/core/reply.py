"""回复生成:固定三段式(结论/依据/建议),口语化,≤150 字。

权威内容归属:【结论】行、safe 兜底建议与 dangerous 的 96110 后缀由代码按判定
级别生成,LLM 只写【依据】【建议】两段(表达者不拥有权威内容);两段解析失败
重试 ≤2 次,仍失败回退模板生成。
回复不包含固定的通知声明;送达说明由协同层根据实际生成的告警补充。
safe 口径:结论只说"未发现"(陈述检索结果),不说"安全"(担保);
兜底建议是常驻核实习惯提醒,兼作覆盖范围免责。
dangerous 口径:结论只断言"典型骗术"(消息命中已知骗术特征),不断言发送者
身份(误报时不构成对他人的事实指控);96110 后缀是常驻官方兜底提示,兼作判定
能力边界免责。【依据】行特征类型以中文标签呈现,机器 id 不出用户面。
"""
import json
import re
from typing import Protocol

from homeshield.core.llm import LLMPort
from homeshield.core.models import Feature, JudgeOutput, KbCase, Level

_HEADS = {
    Level.SAFE.value: "没发现已知骗术的特征",
    Level.SUSPICIOUS.value: "⚠️ 这条消息有问题，多留个心眼",
    Level.DANGEROUS.value: "⚠️ 这是典型骗术，千万别转钱",
}
_SAFE_ADVICE = "涉及转账、验证码，永远先和家人核实"
_DANGEROUS_TAIL = "；紧急可拨反诈专线96110"
_REPLY_BUDGET = 150

# 特征类型→用户面中文标签:规则 FeatureType、LLM 机制 id 与 escalation 共用一张表;
# 表外 id 一律退到"可疑特征",不把机器 id 直接给用户。
_TYPE_LABELS = {
    # 规则特征(FeatureType)
    "transfer": "要求转账", "isolation": "不让告诉家人", "identity_claim": "冒充身份",
    "urgency": "催得很急", "fee": "收费名目", "amount": "金额", "account": "账号",
    "url": "链接", "semantic": "可疑话术",
    # LLM 机制 id(knowledge.mechanics.REGISTRY)
    "sensitive": "索要验证码密码", "money": "资金动作", "control": "要远程控制",
    "identity": "冒充身份", "bait": "利益诱饵", "fear": "恐吓施压",
    "emotion": "打感情牌", "antiverify": "不让你核实", "escape": "带你离开平台",
    # 跨轮升级(规则派生)
    "escalation": "逐步升级话术",
}


def _type_label(ftype: str) -> str:
    return _TYPE_LABELS.get(ftype, "可疑特征")

# 完整产出校验(FR-5):三段式齐全 + 长度
_FULL_RE = re.compile(r"【结论】[\s\S]*【依据】[\s\S]*【建议】")
# LLM 产出解析:只认【依据】【建议】两段,各 ≤60 字,段内不得再出现段标记
_SECTIONS_RE = re.compile(r"【依据】(?P<basis>[^【]{1,60})【建议】(?P<advice>[^【]{1,60})")


def validate_reply(text: str) -> bool:
    return bool(_FULL_RE.search(text)) and 10 <= len(text) <= _REPLY_BUDGET


def _basis(features: list[Feature], cited: list[str]) -> str:
    cited_set = set(cited)
    ordered = [f for f in features if f.id in cited_set] or features[:2]
    return "；".join(f"{_type_label(f.type)}：{f.value}" for f in ordered[:2]) or "无明显特征"


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 1)].rstrip() + "…"


def _assemble(level: Level, basis: str, advice: str) -> str:
    """拼装三段式:结论行、safe 兜底建议与 dangerous 96110 后缀代码所有;
    超长时按预算截两段正文(截断补省略号),三段式结构完整。"""
    if level is Level.SAFE:
        advice = _SAFE_ADVICE  # safe 无可引用的骗术案例,建议即常驻核实提醒,不由 LLM 生成
    tail = _DANGEROUS_TAIL if level is Level.DANGEROUS else ""
    head = f"【结论】{_HEADS[level.value]}"
    budget = _REPLY_BUDGET - len(head) - 10  # 10 = 两个换行 + 【依据】【建议】段标记
    half = max(budget, 10) // 2
    basis = _clip(basis, half)
    advice = _clip(advice, max(budget - half, 10) - len(tail)) + tail
    return f"{head}\n【依据】{basis}\n【建议】{advice}"


def add_delivery_notice(reply: str, notice: str) -> str:
    """在回复预算内附加本次实际告警范围,不改变判定与三段式结构。"""
    if not notice:
        return reply
    if len(reply) + 1 + len(notice) <= _REPLY_BUDGET:
        return reply + "\n" + notice
    parts = reply.rsplit("【建议】", 1)
    if len(parts) != 2:
        return reply[:_REPLY_BUDGET-1] + "…"
    head, advice = parts
    fixed = len(head) + len("【建议】") + 1  # 末尾换行
    max_notice = max(1, _REPLY_BUDGET - fixed - 1)
    if len(notice) > max_notice:
        notice = notice[:max_notice-1].rstrip() + "…"
    advice_budget = max(0, _REPLY_BUDGET - fixed - len(notice))
    return head + "【建议】" + advice[:advice_budget].rstrip() + "\n" + notice


class ReplyGenerator(Protocol):
    async def generate(
        self,
        verdict: JudgeOutput,
        features: list[Feature],
        cases: list[KbCase],
    ) -> str: ...


class TemplateReply:
    """模板三段式,LLM 解析失败时的回退实现。"""

    async def generate(
        self,
        verdict: JudgeOutput,
        features: list[Feature],
        cases: list[KbCase],
    ) -> str:
        advice = (cases[0].advice if cases else "先别转钱，和家人商量一下")[:60]
        return _assemble(verdict.level, _basis(features, verdict.cited_ids), advice)


class LLMReply:
    def __init__(self, llm: LLMPort):
        self.llm = llm

    async def generate(
        self,
        verdict: JudgeOutput,
        features: list[Feature],
        cases: list[KbCase],
    ) -> str:
        system = (
            "家庭反诈助手:用长辈能懂的大白话,只输出两段——【依据】…【建议】…,"
            "各一句话。不要写【结论】,结论由系统生成。"
        )
        user = json.dumps(
            {
                "level": verdict.level.value,
                "features": [{"type": _type_label(f.type), "value": f.value} for f in features],
                "case_advice": [c.advice for c in cases],
            },
            ensure_ascii=False,
        )
        fallback = TemplateReply()
        for _ in range(2):
            parsed = _parse_sections(await self.llm.chat_text("reply", system, user))
            if parsed:
                return _assemble(verdict.level, *parsed)
        return await fallback.generate(verdict, features, cases)


def _parse_sections(text: str) -> tuple[str, str] | None:
    m = _SECTIONS_RE.search(text)
    if not m:
        return None
    basis, advice = m.group("basis").strip(), m.group("advice").strip()
    if not basis or not advice:
        return None
    return basis, advice


def make_reply(settings, llm: LLMPort) -> ReplyGenerator:
    return LLMReply(llm) if settings.use_llm else TemplateReply()
