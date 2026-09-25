"""消息接入与归一化:text/url/image → Message;msg_id 幂等。

先查后插;并发双插由 msg_id 唯一索引与 IntegrityError 兜住。
"""
import re
from dataclasses import dataclass

from core.models import ContentType, Message
from core.repo import Repos

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


@dataclass(frozen=True)
class IntakeResult:
    message: Message | None
    duplicate: bool
    query_id: int | None = None


def detect_content_type(content: str, declared: str | None = None) -> ContentType:
    if declared and declared in {t.value for t in ContentType}:
        return ContentType(declared)
    if _URL_RE.search(content or ""):
        return ContentType.URL
    return ContentType.TEXT


def ingest(
    repos: Repos,
    *,
    member_id: int,
    family_id: int,
    content: str,
    content_type: str | None = None,
    channel: str = "web",
    msg_id: str | None = None,
) -> IntakeResult:
    if not content or not content.strip():
        raise ValueError("empty content")  # → API 层转 400
    if msg_id:
        existed = repos.query.exists_by_msg_id(msg_id)
        if existed:
            return IntakeResult(message=None, duplicate=True, query_id=int(existed["id"]))
    ctype = detect_content_type(content, content_type)
    message = Message(
        member_id=member_id,
        family_id=family_id,
        content_type=ctype,
        content=content,
        channel=channel,
        msg_id=msg_id,
    )
    query_id = repos.query.insert(
        family_id, member_id, ctype.value, content, msg_id
    )
    return IntakeResult(message=message, duplicate=False, query_id=query_id)
