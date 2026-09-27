"""特征抽取:规则引擎抽硬信号 + LLM 按机制体系补抽(source=llm)。

规则下限:isolation 命中至少 suspicious;isolation 与 transfer 共现为 dangerous。
重构三:LLM 补抽按机制封闭集合出证据与置信分(type=机制 id),
与离线挖掘(kbbuild fr-mine,待建)共用 MECHANIC_EXTRACTION_PROMPT 保证同源。
"""
import re

from pydantic import BaseModel

from homeshield.core.knowledge.mechanics import REGISTRY
from homeshield.core.llm import LLMPort
from homeshield.core.models import Feature, FeatureType, Level

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
    confidence: int = 5  # 机制置信分(0-10);规则特征默认 5
    turn: int = 0  # 所在会话轮次(1 起);0=单轮/全局


def _match_feature_keywords(text: str, words: tuple[str, ...], ftype: str) -> list[FeatureSpec]:
    return [
        FeatureSpec(type=ftype, value=w, evidence_span=w) for w in words if w in text
    ]


def extract_rule_features(text: str) -> list[FeatureSpec]:
    specs: list[FeatureSpec] = []
    specs += _match_feature_keywords(text, ISOLATION_WORDS, FeatureType.ISOLATION.value)
    specs += _match_feature_keywords(text, TRANSFER_WORDS, FeatureType.TRANSFER.value)
    specs += _match_feature_keywords(text, URGENCY_WORDS, FeatureType.URGENCY.value)
    specs += _match_feature_keywords(text, IDENTITY_WORDS, FeatureType.IDENTITY_CLAIM.value)
    specs += _match_feature_keywords(text, FEE_WORDS, FeatureType.FEE.value)
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


def get_rule_risk_floor(specs: list[FeatureSpec]) -> Level:
    """规则结果为下限。"""
    types = {s.type for s in specs}
    if FeatureType.ISOLATION.value in types and FeatureType.TRANSFER.value in types:
        return Level.DANGEROUS
    if FeatureType.ISOLATION.value in types:
        return Level.SUSPICIOUS
    return Level.SAFE


_ASK_MECHANICS = ("money", "sensitive", "control")
_TRUST_MECHANICS = ("identity", "bait", "fear", "emotion")


def detect_escalation_feature(specs: list[FeatureSpec]) -> FeatureSpec | None:
    """跨轮升级信号(重构四):前轮建立信任、后轮出现索取——多轮欺诈的典型结构。

    仅多轮会话计算(存在 ≥2 个不同轮次);单轮返回 None。
    机制归属经 annotate.FEATURE_MECHANIC(特征类型→机制)。
    """
    from homeshield.core.annotate import FEATURE_MECHANIC

    turns = {s.turn for s in specs if s.turn}
    if len(turns) < 2:
        return None
    ask = [s.turn for s in specs if s.turn and FEATURE_MECHANIC.get(s.type) in _ASK_MECHANICS]
    trust = [s.turn for s in specs if s.turn and FEATURE_MECHANIC.get(s.type) in _TRUST_MECHANICS]
    if not ask or not trust:
        return None
    t_ask, t_trust = min(ask), min(trust)
    if t_ask <= t_trust:
        return None
    return FeatureSpec(
        type="escalation", turn=t_ask,
        value=f"第{t_trust}轮起建立信任铺垫,第{t_ask}轮出现索取(渐进式话术)",
        evidence_span="", source="rule", confidence=8,
    )


def assign_feature_ids(specs: list[FeatureSpec]) -> list[Feature]:
    return [
        Feature(
            id=f"F{i + 1:02d}",
            type=s.type,
            value=s.value,
            evidence_span=s.evidence_span,
            source=s.source,
            confidence=s.confidence,
            turn=s.turn or None,
        )
        for i, s in enumerate(specs)
    ]


MECHANIC_EXTRACTION_PROMPT = (
    "你是家庭反诈判定引擎的特征抽取器。从消息中按以下机制抽取欺诈证据:\n"
    "- sensitive 敏感索求:索要验证码/密码/人脸/银行卡号\n"
    "- money 资金动作:要求转账/垫付/先交费/刷流水\n"
    "- control 控制权索取:屏幕共享/远程控制\n"
    "- identity 身份冒充:自称公检法/客服/领导/亲友\n"
    "- bait 利益诱饵:高收益/中奖/低价/返利\n"
    "- fear 恐惧威胁:涉案/冻结/逾期后果\n"
    "- emotion 情感操纵:卖惨/恋情/亲情施压\n"
    "- urgency 紧迫施压:限时/马上/最后期限\n"
    "- isolation 隔离封口:别告诉家人/保密\n"
    "- antiverify 阻断核实:官方查不到/别报警\n"
    "- escape 渠道逃逸:加微信/下载App/脱离平台/点链接\n"
    "每条证据输出 {mechanic, value, evidence_span, confidence(0-10,越高越确定)};"
    "evidence_span 必须是消息原文的连续片段。没有欺诈证据返回空数组。只输出 JSON。"
)


async def supplement_features_with_llm(llm: LLMPort, text: str) -> list[FeatureSpec]:
    """LLM 按机制封闭集合补抽(重构三):type=机制 id,附 confidence。

    失败/机制外/空值一律丢弃,返回空列表由管线降级为纯规则。
    """
    schema = {
        "type": "object",
        "properties": {
            "features": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "mechanic": {"type": "string"},
                        "value": {"type": "string"},
                        "evidence_span": {"type": "string"},
                        "confidence": {"type": "integer"},
                    },
                },
            }
        }
    }
    try:
        data = await llm.chat_json(
            "features",
            MECHANIC_EXTRACTION_PROMPT,
            text[:2000],
            schema,
        )
    except Exception:
        return []
    # 形态归一:供应商偶发返回顶层 list 而非 {"features": [...]}
    feats = data if isinstance(data, list) else data.get("features", [])
    if not isinstance(feats, list):
        feats = []
    out: list[FeatureSpec] = []
    for f in feats:
        if not isinstance(f, dict):
            continue
        mid = str(f.get("mechanic", "")).strip()
        value = str(f.get("value", "")).strip()
        if mid not in REGISTRY or not value:
            continue  # 机制封闭集合:机制外与空值一律丢弃
        try:
            conf = max(0, min(10, int(f.get("confidence", 5))))
        except (TypeError, ValueError):
            conf = 5
        out.append(
            FeatureSpec(
                type=mid,
                value=value[:80],
                evidence_span=str(f.get("evidence_span", ""))[:80],
                source="llm",
                confidence=conf,
            )
        )
    return out
