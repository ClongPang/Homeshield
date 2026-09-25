"""回复生成:固定三段式(结论/依据/建议),口语化,≤150 字。

LLM 生成 + 正则校验,失败回退模板生成。
回复中不出现成员名:非 safe 结论固定提示"家人已知悉"。
"""
import json
import re
from typing import Protocol

from core.llm import LLMPort
from core.models import Feature, JudgeOutput, KbCase, Level

SECTION_RE = re.compile(r"【结论】[\s\S]*【依据】[\s\S]*【建议】[\s\S]*")

_HEADS = {
    Level.SAFE.value: "看起来没什么问题",
    Level.SUSPICIOUS.value: "⚠️ 这条消息有问题,多留个心眼",
    Level.DANGEROUS.value: "⚠️ 是骗子,别转钱",
}


def validate_reply(text: str) -> bool:
    return bool(SECTION_RE.search(text)) and 10 <= len(text) <= 150


def _basis(features: list[Feature], cited: list[str]) -> str:
    cited_set = set(cited)
    ordered = [f for f in features if f.id in cited_set] or features[:2]
    return ";".join(f"{f.type}:{f.value}" for f in ordered[:2]) or "无明显特征"


class ReplyGenerator(Protocol):
    async def generate(
        self,
        verdict: JudgeOutput,
        features: list[Feature],
        cases: list[KbCase],
    ) -> str: ...


class TemplateReply:
    """模板三段式,LLM 格式失败时的回退实现。"""

    async def generate(
        self,
        verdict: JudgeOutput,
        features: list[Feature],
        cases: list[KbCase],
    ) -> str:
        advice = (cases[0].advice if cases else "先别转钱,和家人商量一下")[:60]
        text = (
            f"【结论】{_HEADS[verdict.level.value]}\n"
            f"【依据】{_basis(features, verdict.cited_ids)}\n"
            f"【建议】{advice}"
        )
        if verdict.level is not Level.SAFE:
            text += "\n我已经把这条消息告诉了你的家人"
        return text[:150]


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
            "家庭反诈助手:用长辈能懂的大白话,固定三段式,总长不超过150字。"
            "格式:【结论】…【依据】…【建议】…"
        )
        user = json.dumps(
            {
                "verdict": verdict.model_dump(),
                "features": [f.model_dump() for f in features],
                "case_advice": [c.advice for c in cases],
                "family_notified": verdict.level.value != "safe",
            },
            ensure_ascii=False,
        )
        fallback = TemplateReply()
        for _ in range(2):
            text = await self.llm.chat_text("reply", system, user)
            if validate_reply(text):
                return text
        return await fallback.generate(verdict, features, cases)


def make_reply(settings, llm: LLMPort) -> ReplyGenerator:
    return LLMReply(llm) if settings.use_llm else TemplateReply()
