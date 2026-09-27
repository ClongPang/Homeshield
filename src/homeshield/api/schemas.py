"""API的请求载荷模型(token 凭证统一随体携带)。"""
from pydantic import BaseModel as PayloadModel


class QueryIn(PayloadModel):
    token: str
    content: str
    content_type: str | None = None
    msg_id: str | None = None


class CorrectionIn(PayloadModel):
    token: str
    verdict_id: int
    label: str
    note: str = ""


class DecideIn(PayloadModel):
    token: str
    decision: str = "confirm"  # confirm | reject


class TokenIn(PayloadModel):
    token: str


class MemberIn(PayloadModel):
    token: str
    name: str


class GroupIn(PayloadModel):
    token: str
    name: str


class MemberPatchIn(PayloadModel):
    token: str
    name: str


class TrustIn(PayloadModel):
    token: str
    trusted: bool


class MuteIn(PayloadModel):
    token: str
    mute: bool
