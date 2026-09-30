"""
消息接入与归一化:text/url/image → Message;msg_id 幂等。
先查后插;唯一索引兜住的并发双插窗口按重查收敛为幂等重复,此处是
msg_id 去重的唯一收口,通道层不自行解读重复消息。
"""
import re
from dataclasses import dataclass

from homeshield.core.models import ContentType, Message
from homeshield.core.repo import Repos
from homeshield.core.errors import DuplicateMessage, ValidationError

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


async def ingest(
    repos: Repos,
    *,
    user_id: int,
    content: str,
    content_type: str | None = None,
    channel: str = "web",
    msg_id: str | None = None,
    kind: str = "query",
    open_kfid: str | None = None,
    session_epoch: int | None = None,
    lease_seconds: int = 300,
) -> IntakeResult:
    if not content or not content.strip():
        raise ValueError("empty content")  # → API 层转 400
    if msg_id:
        existed = await _claimed_query(repos, msg_id, channel, open_kfid, user_id)
        if existed:
            return IntakeResult(message=None, duplicate=True, query_id=int(existed["id"]))  # 该消息Id已经存在处理
    ctype = detect_content_type(content, content_type)
    try:
        query_id = await repos.query.insert(user_id, ctype.value, content, msg_id, kind,
                                            channel=channel, open_kfid=open_kfid,
                                            session_epoch=session_epoch, lease_seconds=lease_seconds)
    except DuplicateMessage:
        # 先查后插与唯一索引之间的并发窗口:另一 worker 抢先落账了同一渠道键,
        # 读取其持久状态按幂等重复处理;命中他人 msg_id 视为数据完整性错误
        raced = await _claimed_query(repos, msg_id, channel, open_kfid, user_id)
        if raced is None:
            raise
        return IntakeResult(message=None, duplicate=True, query_id=int(raced["id"]))
    snapshot = await repos.query.list_relations_for_query(query_id)
    message = Message(
        user_id=user_id,
        relation_ids=[g["id"] for g in snapshot],
        content_type=ctype,
        content=content,
        channel=channel,
        msg_id=msg_id,
    )
    return IntakeResult(message=message, duplicate=False, query_id=query_id)


async def _claimed_query(repos: Repos, msg_id: str, channel: str,
                         open_kfid: str | None, user_id: int) -> dict | None:
    """按渠道键读取已认领的同 msg_id 行;命中不同用户的行视为完整性错误。"""
    existed = await repos.query.find_by_msg_id(msg_id, channel=channel, open_kfid=open_kfid)
    if existed and int(existed["user_id"]) != user_id:
        raise ValidationError("message id belongs to another user")
    return existed
