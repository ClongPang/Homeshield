"""特征抽取:规则引擎抽硬信号 + LLM 补抽语义特征(source=llm)。

规则下限:isolation 命中至少 suspicious;isolation 与 transfer 共现为 dangerous。
"""
import re

from pydantic import BaseModel

from core.llm import LLMPort
from core.models import Feature, FeatureType, Level

ISOLATION_WORDS = (
    "别告诉家人", "别告诉子女", "不要告诉家人", "保密", "这是我们俩的事", "影响他工作", "偷偷",
)
TRANSFER_WORDS = ("转账", "打款", "汇款", "转入", "先付", "垫付", "给我验证码")
URGENCY_WORDS = ("立即", "马上", "立刻", "尽快", "最后一天", "限时", "逾期", "紧急")
IDENTITY_WORDS = (
    "公检法", "公安局", "检察院", "法院", "安全账户", "清查", "银监会", "客服", "领导", "老师",
)
FEE_WORDS = ("保证金", "解冻费", "认证金", "手续费", "会员费", "激活费", "包装费", "解冻金")

AMOUNT_RE = re.compile(r"[0-9一二三四五六七八九十百千万亿,，.]+\s*[元块钱]")
URL_RE = re.compile(r"https?://\S+|(?:[\w-]+\.)+(?:com|cn|net|top|xyz|vip|site|online)\S*")
PHONE_RE = re.compile(r"1[3-9]\d{9}")
CARD_RE = re.compile(r"\b\d{16,19}\b")


class FeatureSpec(BaseModel):
    """未分配 ID 的特征草稿。"""

    type: str
    value: str
    evidence_span: str = ""
    source: str = "rule"


def _word_hits(text: str, words: tuple[str, ...], ftype: str) -> list[FeatureSpec]:
    return [
        FeatureSpec(type=ftype, value=w, evidence_span=w) for w in words if w in text
    ]


def extract_rules(text: str) -> list[FeatureSpec]:
    specs: list[FeatureSpec] = []
    specs += _word_hits(text, ISOLATION_WORDS, FeatureType.ISOLATION.value)
    specs += _word_hits(text, TRANSFER_WORDS, FeatureType.TRANSFER.value)
    specs += _word_hits(text, URGENCY_WORDS, FeatureType.URGENCY.value)
    specs += _word_hits(text, IDENTITY_WORDS, FeatureType.IDENTITY_CLAIM.value)
    specs += _word_hits(text, FEE_WORDS, FeatureType.FEE.value)
    for pat, ftype in (
        (AMOUNT_RE, FeatureType.AMOUNT.value),
        (URL_RE, FeatureType.URL.value),
        (PHONE_RE, FeatureType.ACCOUNT.value),
        (CARD_RE, FeatureType.ACCOUNT.value),
    ):
        for m in pat.finditer(text):
            span = m.group(0)[:40]
            specs.append(FeatureSpec(type=ftype, value=span, evidence_span=span))
    return specs


def rule_floor(specs: list[FeatureSpec]) -> Level:
    """规则结果为下限。"""
    types = {s.type for s in specs}
    if FeatureType.ISOLATION.value in types and FeatureType.TRANSFER.value in types:
        return Level.DANGEROUS
    if FeatureType.ISOLATION.value in types:
        return Level.SUSPICIOUS
    return Level.SAFE


def assign_ids(specs: list[FeatureSpec]) -> list[Feature]:
    return [
        Feature(
            id=f"F{i + 1:02d}",
            type=s.type,
            value=s.value,
            evidence_span=s.evidence_span,
            source=s.source,
        )
        for i, s in enumerate(specs)
    ]


async def supplement_llm(llm: LLMPort, text: str) -> list[FeatureSpec]:
    """LLM 补抽语义特征,失败时返回空列表。"""
    schema = {
        "type": "object",
        "properties": {
            "features": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string"},
                        "value": {"type": "string"},
                        "evidence_span": {"type": "string"},
                    },
                },
            }
        },
    }
    try:
        data = await llm.chat_json(
            "features",
            "从用户消息中抽取诈骗语义特征(身份冒充、诱导隔离、情绪操纵等)。"
            "没有则返回空数组。只输出 JSON。",
            text[:2000],
            schema,
        )
    except Exception:
        return []
    out: list[FeatureSpec] = []
    for f in data.get("features", []):
        if isinstance(f, dict) and f.get("value"):
            out.append(
                FeatureSpec(
                    type=str(f.get("type", FeatureType.SEMANTIC.value))[:30],
                    value=str(f["value"])[:80],
                    evidence_span=str(f.get("evidence_span", ""))[:80],
                    source="llm",
                )
            )
    return out
