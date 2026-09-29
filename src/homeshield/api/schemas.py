"""HTTP payloads; the personal token identifies a user, never a group."""
from pydantic import BaseModel as PayloadModel


class QueryIn(PayloadModel):
    token: str
    content: str                        # 要检查的内容，可以是文本、网址，或图片的 Base64 数据
    content_type: str | None = None     # 内容类型，可填 text、url 或 image。不填时程序会尝试识别网址，否则按文本处理。
    msg_id: str | None = None           # 消息唯一标识，用来识别重复提交；网页端会生成 UUID，微信端使用微信消息 ID。


class TokenIn(PayloadModel):
    token: str


class MobileIn(PayloadModel):
    token: str
    mobile: str


class InviteIn(PayloadModel):
    token: str
    name: str = "家人"


class RelationPatchIn(PayloadModel):
    token: str
    name: str | None = None
    inverse_name: str | None = None
    mute: bool | None = None


class CorrectionIn(PayloadModel):
    token: str
    verdict_id: int
    label: str
    note: str = ""
