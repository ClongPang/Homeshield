"""分级判定:Judge 协议的 Mock 与 LLM 实现,加引用校验、重试与降级。

- MockJudge:关键词与特征计数打分,确定性;
- LLMJudge:constrained=True 时 reason 只准引用特征 ID;
- judge_with_validation:引用校验与 safe 置信门槛,失败自动重试,耗尽抛 DegradeError。
"""
import json
import logging
from typing import Protocol

from homeshield.core.errors import DegradeError
from homeshield.core.llm import LLMPort
from homeshield.core.models import FeatureType, JudgeInput, JudgeOutput, Level, Mode

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
    # 机制 id 权重(重构三 LLM 补抽;与 FeatureType 同构语义)
    "isolation": 3, "money": 2, "sensitive": 2, "control": 2, "antiverify": 2,
    "urgency": 1, "identity": 1, "bait": 1, "fear": 1, "emotion": 1, "escape": 1,
    "escalation": 2,
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

    # 重构五:分级语义(《判定模型_设计教训》§3.2)——识别靠机制,定级靠核实结构
    _GRADED_SEMANTICS = (
        "分级语义(必须遵守):"
        "1) dangerous=消息存在索取(要求转账/垫付/先交费/提供验证码密码/屏幕共享),"
        "且同时出现核实抑制(别告诉家人/官方查不到/必须马上/脱离平台私下交易);"
        "2) suspicious=有索取或不可核实的强声称,但没有核实抑制——此时不得判 safe,"
        "依据与建议应给出核实路径;"
        "3) safe=既无索取也无核实抑制。"
        "注意:'建议核实/再观察'类谨慎表态不等于识别出欺诈,不得据此判 safe。"
    )
    _ANNOTATION_NOTE = (
        "原文中的 <机制名>…</机制名> 标注是系统抽取的证据位置,仅供定位与归类,"
        "标注≠结论;未标注的部分同样需要审读。"
    )

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
        if inp.annotated_text:
            system += self._ANNOTATION_NOTE
        if inp.graded_semantics:
            system += self._GRADED_SEMANTICS
        if "\n【第2轮】" in inp.text or inp.text.startswith("【第2轮】"):
            system += (
                "消息为多轮会话,按轮次顺序审读;"
                "前期建立信任、后期出现索取的递进模式是多轮欺诈的典型信号。"
            )
        payload: dict = {
            "text": inp.text[:2000],
            "features": [f.model_dump() for f in inp.features],
            "cases": [c.model_dump() for c in inp.cases],
        }
        if inp.annotated_text:
            cap = 3600 if "\n【第2轮】" in inp.text else 2400
            payload["annotated_text"] = inp.annotated_text[:cap]
        data = await self.llm.chat_json("judge", system, json.dumps(payload, ensure_ascii=False), self._SCHEMA)
        if isinstance(data, list):  # 偶发顶层 list:取首个对象
            data = data[0] if data and isinstance(data[0], dict) else {}
        if not isinstance(data, dict):
            data = {}
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


def _confidence_ok(out: JudgeOutput, floor: int) -> bool:
    """safe 置信门槛:误判 safe 的代价不对称(漏报伤信任),低置信不得出 safe。

    floor<=0 关闭;只约束 safe——suspicious/dangerous 宁误报不漏报,无门槛。
    Mock 的置信分为合成值,门槛是否传入由装配层按模式决定。
    """
    return floor <= 0 or out.level is not Level.SAFE or out.confidence >= floor


async def judge_with_validation(
    inp: JudgeInput, judge: Judge, *, constrained: bool, retries: int = 2,
    safe_confidence_floor: int = 0,
) -> JudgeOutput:
    known = {f.id for f in inp.features}
    last: JudgeOutput | None = None
    for attempt in range(retries + 1):
        out = await judge.judge(inp, constrained=constrained)
        if constrained and not citations_valid(out, known):
            logger.warning(
                "citation validation failed (attempt %d/%d): cited=%s known=%s",
                attempt + 1, retries + 1, out.cited_ids, sorted(known),
            )
        elif not _confidence_ok(out, safe_confidence_floor):
            logger.warning(
                "safe confidence gate failed (attempt %d/%d): level=%s confidence=%d floor=%d",
                attempt + 1, retries + 1, out.level.value, out.confidence, safe_confidence_floor,
            )
        else:
            return out
        last = out
    logger.warning("validation exhausted, degrade to manual review")
    raise DegradeError(
        "这条消息我拿不准,请把内容给家人看看再决定",
        f"validation exhausted: {last}",
    )
