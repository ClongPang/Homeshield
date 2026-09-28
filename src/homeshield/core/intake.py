"""
消息接入与归一化:text/url/image → Message;msg_id 幂等。
先查后插;并发双插由 msg_id 唯一索引与 IntegrityError 兜住。
"""
import re
from dataclasses import dataclass

from homeshield.core.models import ContentType, Message
from homeshield.core.repo import Repos

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


@dataclass(frozen=True)
class IntakeResult:
    message: Message | None
    duplicate: bool
    query_id: int | None = None


def detect_content_type(content: str, declared: str | None = None) -> ContentType:
    if declared and declared in {t.value for t in ContentType}: # 类型声明在三种类型之中
        return ContentType(declared)
    if _URL_RE.search(content or ""):                           # 网址链接的规则匹配识别
        return ContentType.URL
    return ContentType.TEXT                                     # 其它类型兜底为text


def ingest(
    repos: Repos,
    *,
    user_id: int,
    content: str,
    content_type: str | None = None,
    channel: str = "web",
    msg_id: str | None = None,
    kind: str = "query",
) -> IntakeResult:
    if not content or not content.strip():
        raise ValueError("empty content")  # → API 层转 400
    if msg_id:
        existed = repos.query.find_by_msg_id(msg_id)
        if existed:
            return IntakeResult(message=None, duplicate=True, query_id=int(existed["id"]))  # 该消息Id已经存在处理
    ctype = detect_content_type(content, content_type)
    query_id = repos.query.insert(user_id, ctype.value, content, msg_id, kind)
    snapshot = repos.query.list_relations_for_query(query_id)
    message = Message(
        user_id=user_id,
        relation_ids=[g["id"] for g in snapshot],
        content_type=ctype,
        content=content,
        channel=channel,
        msg_id=msg_id,
    )
    return IntakeResult(message=message, duplicate=False, query_id=query_id)
