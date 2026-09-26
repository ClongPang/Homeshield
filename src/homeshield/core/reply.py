"""回复生成:固定三段式(结论/依据/建议),口语化,≤150 字。

权威内容归属:【结论】行、safe 兜底建议与"家人已知悉"后缀由代码按判定
级别生成,LLM 只写【依据】【建议】两段(表达者不拥有权威内容);两段解析失败
重试 ≤2 次,仍失败回退模板生成。
回复中不出现成员名:非 safe 结论固定提示"我已经把这条消息告诉了你的家人"。
safe 口径:结论只说"未发现"(陈述检索结果),不说"安全"(担保);
兜底建议是常驻核实习惯提醒,兼作覆盖范围免责。
"""
import json
import re
from typing import Protocol

from homeshield.core.llm import LLMPort
from homeshield.core.models import Feature, JudgeOutput, KbCase, Level

_HEADS = {
    Level.SAFE.value: "没发现已知骗术的特征",
    Level.SUSPICIOUS.value: "⚠️ 这条消息有问题,多留个心眼",
    Level.DANGEROUS.value: "⚠️ 是骗子,别转钱",
}
_SAFE_ADVICE = "涉及转账、验证码,永远先和家人核实"
_FAMILY_SUFFIX = "\n我已经把这条消息告诉了你的家人"
_REPLY_BUDGET = 150

# 完整产出校验(FR-5):三段式齐全 + 长度
_FULL_RE = re.compile(r"【结论】[\s\S]*【依据】[\s\S]*【建议】")
# LLM 产出解析:只认【依据】【建议】两段,各 ≤60 字,段内不得再出现段标记
_SECTIONS_RE = re.compile(r"【依据】(?P<basis>[^【]{1,60})【建议】(?P<advice>[^【]{1,60})")


def validate_reply(text: str) -> bool:
    return bool(_FULL_RE.search(text)) and 10 <= len(text) <= _REPLY_BUDGET


def _basis(features: list[Feature], cited: list[str]) -> str:
    cited_set = set(cited)
    ordered = [f for f in features if f.id in cited_set] or features[:2]
    return ";".join(f"{f.type}:{f.value}" for f in ordered[:2]) or "无明显特征"


def _assemble(level: Level, basis: str, advice: str) -> str:
    """拼装三段式:结论行、safe 兜底建议与后缀代码所有;超长时按预算截两段正文,三段式结构完整。"""
    if level is Level.SAFE:
        advice = _SAFE_ADVICE  # safe 无可引用的骗术案例,建议即常驻核实提醒,不由 LLM 生成
    suffix = "" if level is Level.SAFE else _FAMILY_SUFFIX
    head = f"【结论】{_HEADS[level.value]}"
    budget = _REPLY_BUDGET - len(suffix) - len(head) - 10  # 10 = 两个换行 + 【依据】【建议】段标记
    half = max(budget, 10) // 2
    basis = basis[:half].rstrip()
    advice = advice[: max(budget - half, 10)].rstrip()
    return f"{head}\n【依据】{basis}\n【建议】{advice}{suffix}"


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
        advice = (cases[0].advice if cases else "先别转钱,和家人商量一下")[:60]
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
                "features": [f.model_dump() for f in features],
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
