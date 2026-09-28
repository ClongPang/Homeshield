"""Domain models and enums. User tokens are personal; timestamps are Unix seconds."""
from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, Field


def utc_timestamp() -> int:
    return int(datetime.now(timezone.utc).timestamp())


class ContentType(StrEnum):
    TEXT = "text"
    URL = "url"
    IMAGE = "image"


class Level(StrEnum):
    SAFE = "safe"
    SUSPICIOUS = "suspicious"
    DANGEROUS = "dangerous"


class Mode(StrEnum):
    MOCK = "mock"
    LLM = "llm"


class CorrectionLabel(StrEnum):
    """提交时的用户主张,永不改写;"是否已核实"由 CorrectionStatus 表达。"""

    REAL = "real"                       # 漏报主张:判轻了,实际是诈骗
    FALSE_POSITIVE = "false_positive"   # 误报主张:判重了,实际不是诈骗


class CorrectionStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    NO_CONSENSUS = "no_consensus"       # 未达成共识


LEVEL_RANK: dict[Level, int] = {
    Level.SAFE: 0,
    Level.SUSPICIOUS: 1,
    Level.DANGEROUS: 2,
}


def max_level(a: Level, b: Level) -> Level:
    """规则结果为下限:取两者中较高的一级。"""
    return a if LEVEL_RANK[a] >= LEVEL_RANK[b] else b


class Message(BaseModel):
    """Intake result and the directed relations snapshotted at acceptance."""

    user_id: int
    relation_ids: list[int] = Field(default_factory=list)
    content_type: ContentType
    content: str
    channel: str = "web"  # wechat | web
    msg_id: str | None = None  # 幂等键(微信 MsgId / 网页客户端生成)
    created_at: int = Field(default_factory=utc_timestamp)


class Turn(BaseModel):
    """会话中的一轮(重构四)。speaker 为空表示单条消息或未标注发言人。"""

    speaker: str = ""
    text: str


_TURN_MARKED_RE = None  # 延迟编译见 Conversation.from_marked


class Conversation(BaseModel):
    """会话一等公民(重构四):单条消息 = 1 轮;聊天截图转写/多轮样本 = N 轮。"""

    turns: list[Turn]

    @classmethod
    def single(cls, text: str) -> "Conversation":
        return cls(turns=[Turn(text=text)])

    @classmethod
    def from_marked(cls, text: str) -> "Conversation":
        """解析【第N轮】/【第N轮·发言人】标记;无标记则整体为单轮。"""
        import re

        pat = re.compile(r"【第(\d+)轮(?:·([^】]+))?】\n?")
        parts = pat.split(text)
        if len(parts) == 1:
            return cls.single(text)
        turns: list[Turn] = []
        # parts: [前导, n1, speaker1, text1, n2, speaker2, text2, ...]
        for i in range(1, len(parts) - 2, 3):
            turns.append(Turn(speaker=(parts[i + 1] or "").strip(), text=parts[i + 2].strip()))
        if not turns:
            return cls.single(text)
        return cls(turns=turns)

    def render(self) -> str:
        """渲染为带轮次标记的判定文本(单轮原样返回,不加标记)。"""
        if len(self.turns) == 1 and not self.turns[0].speaker:
            return self.turns[0].text
        lines = []
        for i, t in enumerate(self.turns, 1):
            head = f"【第{i}轮】" if not t.speaker else f"【第{i}轮·{t.speaker}】"
            lines.append(head + t.text)
        return "\n".join(lines)

    @property
    def is_multi_turn(self) -> bool:
        return len(self.turns) > 1


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
    type: str  # FeatureType / 机制 id(重构三 LLM 补抽)
    value: str
    evidence_span: str = ""
    source: str = "rule"  # rule | llm
    confidence: int | None = None  # LLM 补抽的机制置信分(0-10);规则特征为 None
    turn: int | None = None  # 重构四:特征所在会话轮次(1 起);单轮/全局特征为 None
    origin: str = Field(default="self", exclude_if=lambda value: value == "self")


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
    annotated_text: str | None = None  # 重构二:机制内联标注后的原文(None=关闭)
    graded_semantics: bool = False  # 重构五:分级语义注入判定提示(证伪通过前默认关)


class JudgeOutput(BaseModel):
    """分级判定输出。"""

    level: Level
    confidence: int = Field(ge=0, le=100)
    cited_ids: list[str] = Field(default_factory=list)
    reason: str = ""


class User(BaseModel):
    """A person's global identity. openid maps to WeChat; token authenticates the console."""

    id: int
    openid: str
    token: str
    created_at: int

    def entry_url(self, base_url: str) -> str:
        if not base_url:
            return ""
        return f"{base_url.rstrip('/')}/console?token={self.token}"
