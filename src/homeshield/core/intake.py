"""消息接入与归一化:text/url/image → Message;msg_id 幂等。

先查后插;并发双插由 msg_id 唯一索引与 IntegrityError 兜住。
"""
import re
from dataclasses import dataclass

from homeshield.core.errors import ValidationError
from homeshield.core.models import ContentType, Member, Message
from homeshield.core.repo import Repos

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
    user_id: int,
    memberships: list[Member],
    content: str,
    content_type: str | None = None,
    channel: str = "web",
    msg_id: str | None = None,
    kind: str = "query",
) -> IntakeResult:
    if not content or not content.strip():
        raise ValueError("empty content")  # → API 层转 400
    if not memberships:
        raise ValueError("user has no active group")
    if msg_id:
        existed = repos.query.find_by_msg_id(msg_id)
        if existed:
            return IntakeResult(message=None, duplicate=True, query_id=int(existed["id"]))
    ctype = detect_content_type(content, content_type)
    try:
        query_id = repos.query.insert(user_id, memberships, ctype.value, content, msg_id, kind)
    except ValidationError as exc:
        # 成员关系可能在入口读取后、查询快照写入前被终止。
        # 将这个并发结果按正常的“当前不在群内”处理，不能泄漏为 500。
        if str(exc) == "user has no active group":
            raise ValueError("user has no active group") from exc
        raise
    snapshot = repos.query.list_groups_for_query(query_id)
    message = Message(
        user_id=user_id,
        group_ids=[g["group_id"] for g in snapshot],
        membership_ids=[g["query_member_id"] for g in snapshot],
        content_type=ctype,
        content=content,
        channel=channel,
        msg_id=msg_id,
    )
    return IntakeResult(message=message, duplicate=False, query_id=query_id)
