"""分级判定:Judge 协议的 Mock 与 LLM 实现,加引用校验、重试与降级。

- MockJudge:关键词与特征计数打分,确定性;
- LLMJudge:constrained=True 时 reason 只准引用特征 ID;
- judge_with_validation:校验失败自动重试,耗尽抛 DegradeError。
"""
import json
import logging
from typing import Protocol

from core.errors import DegradeError
from core.llm import LLMPort
from core.models import FeatureType, JudgeInput, JudgeOutput, Level, Mode

logger = logging.getLogger(__name__)

MOCK_WEIGHTS: dict[str, int] = {
    FeatureType.ISOLATION.value: 3,
    FeatureType.TRANSFER.value: 2,
    FeatureType.URGENCY.value: 1,
    FeatureType.IDENTITY_CLAIM.value: 1,
    FeatureType.FEE.value: 1,
    FeatureType.AMOUNT.value: 1,
    FeatureType.ACCOUNT.value: 1,
    FeatureType.URL.value: 1,
    FeatureType.SEMANTIC.value: 1,
}


class Judge(Protocol):
    mode: Mode

    async def judge(self, inp: JudgeInput, *, constrained: bool) -> JudgeOutput: ...


class MockJudge:
    mode = Mode.MOCK

    async def judge(self, inp: JudgeInput, *, constrained: bool = False) -> JudgeOutput:
        scored = sorted(
            ((MOCK_WEIGHTS.get(f.type, 0), f) for f in inp.features),
            key=lambda x: x[0],
            reverse=True,
        )
        score = sum(w for w, _ in scored)
        if score >= 5:
            level = Level.DANGEROUS
        elif score >= 2:
            level = Level.SUSPICIOUS
        else:
            level = Level.SAFE
        cited = [f.id for w, f in scored if w > 0][:3]
        known = {f.id: f for f in inp.features}
        reason = (
            ";".join(f"{known[cid].type}:{known[cid].value}" for cid in cited)
            or "未命中已知诈骗特征"
        )
        return JudgeOutput(
            level=level,
            confidence=min(92, 35 + score * 9),
            cited_ids=cited,
            reason=reason,
        )


class LLMJudge:
    mode = Mode.LLM

    _SCHEMA = {
        "type": "object",
        "properties": {
            "level": {"enum": ["safe", "suspicious", "dangerous"]},
            "confidence": {"type": "integer"},
            "cited_ids": {"type": "array", "items": {"type": "string"}},
            "reason": {"type": "string"},
        },
        "required": ["level", "confidence"],
    }

    def __init__(self, llm: LLMPort):
        self.llm = llm

    async def judge(self, inp: JudgeInput, *, constrained: bool) -> JudgeOutput:
        system = (
            "你是家庭反诈判定引擎,只依据给出的特征与检索案例分级:safe/suspicious/dangerous。"
            "输出 JSON。"
        )
        if constrained:
            system += (
                "reason 必须只引用给出的特征 ID(如 F01),不得编造。"
                "检索案例仅供类目知识与应对建议,相似≠诈骗,防止锚定误报。"
            )
        user = json.dumps(
            {
                "text": inp.text[:2000],
                "features": [f.model_dump() for f in inp.features],
                "cases": [c.model_dump() for c in inp.cases],
            },
            ensure_ascii=False,
        )
        data = await self.llm.chat_json("judge", system, user, self._SCHEMA)
        level = Level(data.get("level", "safe"))
        confidence = max(0, min(100, int(data.get("confidence", 50))))
        return JudgeOutput(
            level=level,
            confidence=confidence,
            cited_ids=[str(x) for x in data.get("cited_ids", [])],
            reason=str(data.get("reason", ""))[:300],
        )


def citations_valid(out: JudgeOutput, known_ids: set[str]) -> bool:
    return all(cid in known_ids for cid in out.cited_ids)


async def judge_with_validation(
    inp: JudgeInput, judge: Judge, *, constrained: bool, retries: int = 2
) -> JudgeOutput:
    known = {f.id for f in inp.features}
    last: JudgeOutput | None = None
    for attempt in range(retries + 1):
        out = await judge.judge(inp, constrained=constrained)
        if not constrained or citations_valid(out, known):
            return out
        logger.warning(
            "citation validation failed (attempt %d/%d): cited=%s known=%s",
            attempt + 1, retries + 1, out.cited_ids, sorted(known),
        )
        last = out
    logger.warning("citation validation exhausted, degrade to manual review")
    raise DegradeError(
        "这条消息我拿不准,请把内容给家人看看再决定",
        f"citation validation exhausted: {last}",
    )
