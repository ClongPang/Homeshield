"""领域模型与枚举。

member.openid 微信零注册映射;member.token 个人链接凭证;
query.msg_id 幂等键;时间戳为 unix 秒整数。
"""
from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, Field


def utcnow() -> int:
    return int(datetime.now(timezone.utc).timestamp())


class ContentType(StrEnum):
    TEXT = "text"
    URL = "url"
    IMAGE = "image"


class Role(StrEnum):
    ELDER = "elder" # 表示长辈
    ADULT = "adult" # 表示子女


class Level(StrEnum):
    SAFE = "safe"
    SUSPICIOUS = "suspicious"
    DANGEROUS = "dangerous"


class Mode(StrEnum):
    MOCK = "mock"
    LLM = "llm"


class CorrectionLabel(StrEnum):
    REAL = "real"  # 漏报(提交时)
    FALSE_POSITIVE = "false_positive"
    CONFIRMED_SCAM = "confirmed_scam"  # real 经 adult 确认后落库


class CorrectionStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


LEVEL_RANK: dict[Level, int] = {
    Level.SAFE: 0,
    Level.SUSPICIOUS: 1,
    Level.DANGEROUS: 2,
}


def max_level(a: Level, b: Level) -> Level:
    """规则结果为下限:取两者中较高的一级。"""
    return a if LEVEL_RANK[a] >= LEVEL_RANK[b] else b


class Message(BaseModel):
    """intake 归一化产物。"""

    member_id: int
    family_id: int
    content_type: ContentType
    content: str
    channel: str = "web"  # wechat | web
    msg_id: str | None = None  # 幂等键(微信 MsgId / 网页客户端生成)
    created_at: int = Field(default_factory=utcnow)


class FeatureType(StrEnum):
    AMOUNT = "amount"
    ACCOUNT = "account"
    URL = "url"
    URGENCY = "urgency"
    IDENTITY_CLAIM = "identity_claim"
    ISOLATION = "isolation"
    TRANSFER = "transfer"
    FEE = "fee"
    SEMANTIC = "semantic"  # LLM 补抽的语义特征兜底类型


class Feature(BaseModel):
    """特征结构;不可变,id 在单次查询内分配。"""

    id: str  # F01、F02…(每次 query 内分配)
    type: str  # FeatureType 或 LLM 补抽的新类型
    value: str
    evidence_span: str = ""
    source: str = "rule"  # rule | llm


class KbCase(BaseModel):
    """骗术知识库案例"""

    id: str
    scam_type: str  # 对齐 taxonomy
    name: str
    tactic: str  # 话术特征描述
    markers: list[str] = Field(default_factory=list)  # 识别点关键词
    advice: str = ""  # 应对建议


class JudgeInput(BaseModel):
    text: str
    features: list[Feature]
    cases: list[KbCase]


class JudgeOutput(BaseModel):
    """分级判定输出。"""

    level: Level
    confidence: int = Field(ge=0, le=100)
    cited_ids: list[str] = Field(default_factory=list)
    reason: str = ""


class Member(BaseModel):
    id: int
    family_id: int
    name: str
    role: Role
    openid: str | None = None
    token: str | None = None  # 个人链接凭证,仅部署者经 CLI 分发,不出现在家人可见接口


class VerdictRecord(BaseModel):
    id: int
    query_id: int
    level: Level
    cited_ids: list[str]
    reason: str
    reply: str
    latency_ms: int
    mode: Mode
    created_at: int


class CorrectionRecord(BaseModel):
    id: int
    verdict_id: int
    by_member_id: int
    label: CorrectionLabel
    note: str = ""
    status: CorrectionStatus
    decided_by: int | None = None
