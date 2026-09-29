"""企业微信(微信客服)通道:回调验活 + sync_msg 轮询 + 消息派发。

消息获取走拉取模式(轮询器带游标,不依赖回调推送;回调仅作验活与
后续实时性增强)。身份复用 user.openid,external_userid 加 wxkf: 前缀,与合成账号的
demo:/test: 前缀同一约定;关系指令处理在 core/commands(通道无关)。判定业务
在 verification/pipeline。
"""
import asyncio
import base64
import logging
import xml.etree.ElementTree as ET

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from homeshield.core import messages
from homeshield.core.channels.wecom import WeComCryptoError
from homeshield.core.deps import Deps
from homeshield.core.errors import DuplicateMessage
from homeshield.core.pg_events import KF_PULL_CHANNEL
from homeshield.core.triage import classify
from homeshield.core.verification import VerificationService
from homeshield.core.commands import handle_relation_command

logger = logging.getLogger(__name__)
OPENID_PREFIX = "wxkf:"
FALLBACK_POLL_SECONDS = 60  # 漏报兜底周期;正常路径由回调通知即时触发拉取
POLLER_LOCK_KEY = 0x484F4D4553484945  # "HOMESHE", process-independent PostgreSQL advisory lock.
POLLER_RETRY_SECONDS = 2


async def handle_kf_notify(deps: Deps) -> None:
    """唤醒 leader poller 立即拉取:本进程信号即时生效;leader 在别的 worker 时,
    经 Postgres NOTIFY 跨进程送达(worker 都在 bridge 里 LISTEN 同一通道)。
    非 leader 收到 NOTIFY 只置位无人等待的 poll_signal,不会重复拉取。"""
    if deps.wecom is None or not deps.wecom.api_ready:
        return
    deps.poll_signal.set()
    try:
        async with deps.pool.connection() as conn:
            await conn.execute("SELECT pg_notify(%s, '')", (KF_PULL_CHANNEL,))
    except Exception:
        logger.warning("kf pull wakeup broadcast failed", exc_info=True)


def build_wecom_router(deps: Deps) -> APIRouter:
    router = APIRouter()

    @router.get("/wecom/callback")
    async def wecom_verify(request: Request):
        ch = deps.wecom
        p = request.query_params
        if ch is None or not ch.configured:
            return PlainTextResponse("wecom not configured")
        if not ch.verify_signature(
            p.get("msg_signature", ""), p.get("timestamp", ""), p.get("nonce", ""), p.get("echostr", "")
        ):
            raise HTTPException(403, "bad signature")
        return PlainTextResponse(ch.decrypt(p.get("echostr", "")))

    @router.post("/wecom/callback")
    async def wecom_callback(request: Request):
        ch = deps.wecom
        if ch is None or not ch.configured:
            return PlainTextResponse("success")
        p = request.query_params
        body = (await request.body()).decode()
        encrypt = ""
        try:
            node = ET.fromstring(body).find("Encrypt")
            if node is not None and node.text:
                encrypt = node.text
        except ET.ParseError:
            logger.warning("wecom callback: non-XML body")
        if not ch.verify_signature(
            p.get("msg_signature", ""), p.get("timestamp", ""), p.get("nonce", ""), encrypt
        ):
            raise HTTPException(403, "bad signature")
        if encrypt:
            try:
                data = ET.fromstring(ch.decrypt(encrypt))
                kind = data.findtext("Event") or data.findtext("MsgType") or "?"
                logger.info("wecom event kind=%s", kind)
                if kind == "kf_msg_or_event":
                    asyncio.create_task(handle_kf_notify(deps))
            except (WeComCryptoError, ET.ParseError):
                logger.warning("wecom callback: decrypt/parse failed", exc_info=True)
        return PlainTextResponse("success")

    return router


async def _try_poller_lock(conn) -> bool:
    row = await (await conn.execute("SELECT pg_try_advisory_lock(%s) AS acquired", (POLLER_LOCK_KEY,))).fetchone()
    return bool(row["acquired"])


async def _release_poller_lock(conn) -> None:
    await conn.execute("SELECT pg_advisory_unlock(%s)", (POLLER_LOCK_KEY,))


async def wecom_poller(deps: Deps, verification: VerificationService, *,
                       poll_interval: float = FALLBACK_POLL_SECONDS,
                       retry_interval: float = POLLER_RETRY_SECONDS) -> None:
    """Only the worker holding a session advisory lock polls; peers retry for handoff."""
    while True:
        try:
            async with deps.pool.connection() as lock_conn:
                if not await _try_poller_lock(lock_conn):
                    await asyncio.sleep(retry_interval)
                    continue
                try:
                    while True:
                        deps.poll_signal.clear()
                        await poll_once(deps, verification)
                        try:
                            await asyncio.wait_for(deps.poll_signal.wait(), timeout=poll_interval)
                        except asyncio.TimeoutError:
                            pass
                finally:
                    await _release_poller_lock(lock_conn)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("wecom poller failed; retrying leader election", exc_info=True)
            await asyncio.sleep(retry_interval)


async def poll_once(deps: Deps, verification: VerificationService) -> None:
    ch = deps.wecom
    if ch is None or not ch.api_ready:
        return
    for account in await ch.list_kf_accounts():
        kfid = account.get("open_kfid", "")
        if not kfid:
            continue
        cursor = await deps.repos.kf_cursor.get(kfid) or ""
        res = await ch.kf_sync_msg(kfid, cursor)
        if res.get("errcode") != 0:
            logger.warning("wecom sync_msg failed %s %s", res.get("errcode"), res.get("errmsg"))
            continue
        next_cursor = res.get("next_cursor") or cursor
        processed = True
        if res.get("msg_list"):
            processed = await handle_wecom_messages(deps, verification, res["msg_list"])
        if processed:
            if next_cursor and next_cursor != cursor:
                await deps.repos.kf_cursor.set(kfid, str(next_cursor))


async def handle_wecom_messages(deps: Deps, verification: VerificationService, msgs: list[dict]) -> bool:
    processed = True
    for m in msgs:
        try:
            await _handle_message(deps, verification, m)
        except Exception:
            logger.warning("wecom message handling failed", exc_info=True)
            processed = False
    return processed


async def _handle_message(deps: Deps, verification: VerificationService, m: dict) -> None:
    ch = deps.wecom
    if m.get("msgtype") == "event":
        if m.get("event_type") == "enter_session" and m.get("welcome_code"):
            try:
                await ch.kf_send_welcome(m["welcome_code"], messages.WELCOME)
            except Exception:
                logger.warning("wecom welcome failed", exc_info=True)
        return
    if m.get("origin") != 3:
        return  # 只处理客户发来的消息;4=API 下发,5=接待人员
    eid = m.get("external_userid", "")
    kfid = m.get("open_kfid", "")
    if not eid or not kfid:
        return

    openid = OPENID_PREFIX + eid
    existing = await deps.repos.users.get_by_openid(openid)
    user = existing or await deps.repos.users.get_or_create(openid)
    first_message = existing is None

    async def reply(text: str) -> None:
        try:
            await ch.kf_send_msg(kfid, eid, text)
        except Exception:
            logger.warning("wecom reply failed", exc_info=True)

    if m.get("msgtype") == "text":
        text = m.get("text", {}).get("content", "")
        response = await handle_relation_command(deps.relations, deps.repos, deps.settings, user, "text", text)
        if response is not None:
            await reply(response)
            await _send_console_link(deps, ch, kfid, eid, user.token,
                                   first_message and text.strip() != "我的联防")
            return
        triage = classify(text)
        if triage == "reset":
            await deps.repos.incident.close_open_incident(user.id, msg_id=m.get("msgid"))
            await reply(messages.SESSION_RESET_REPLY)
            await _send_console_link(deps, ch, kfid, eid, user.token, first_message)
            return
        if triage == "ack":
            try:
                await verification.verify(user=user, content=text, content_type="text",
                                          channel="wecom", msg_id=m.get("msgid"))
            except Exception:
                logger.warning("wecom ack persistence failed", exc_info=True)
            await reply(messages.ACK_QUERY_REPLY)
            await _send_console_link(deps, ch, kfid, eid, user.token, first_message)
            return
        await _run_query(deps, verification, ch, kfid, eid, user, text, None, m.get("msgid"))
        await _send_console_link(deps, ch, kfid, eid, user.token, first_message)
        return

    if m.get("msgtype") == "image":
        content = ""
        media_id = m.get("image", {}).get("media_id", "")
        try:
            content = base64.b64encode(await ch.download_media(media_id)).decode()
        except Exception:
            logger.warning("wecom image download failed", exc_info=True)
        if not content:
            await reply(messages.LOOK_FAILED)
            return
        await _run_query(deps, verification, ch, kfid, eid, user, content, "image", m.get("msgid"))
        await _send_console_link(deps, ch, kfid, eid, user.token, first_message)
        return

    if m.get("msgtype") == "link":
        link = m.get("link", {})
        content = "\n".join(filter(None, [link.get("title", ""), link.get("description", ""),
                                          link.get("url", "")]))
        if content:
            await _run_query(deps, verification, ch, kfid, eid, user, content, None, m.get("msgid"))
            await _send_console_link(deps, ch, kfid, eid, user.token, first_message)
            return

    if m.get("msgtype") == "merged_msg":
        content = _flatten_merged(m.get("merged_msg", {}))
        if content:
            await _run_query(deps, verification, ch, kfid, eid, user, content, "text", m.get("msgid"))
            await _send_console_link(deps, ch, kfid, eid, user.token, first_message)
            return

    await reply(messages.UNSUPPORTED_TYPE)


def _flatten_merged(merged: dict) -> str:
    """合并转发记录抽取文本项;结构防御式解析,取不到就放弃。"""
    texts = []
    try:
        for item in merged.get("items", []):
            if item.get("msgtype") == "text":
                content = item.get("content", {})
                texts.append(content.get("content", "") if isinstance(content, dict) else str(content))
    except Exception:
        logger.warning("merged_msg flatten failed", exc_info=True)
    return "\n".join(t for t in texts if t)


async def _run_query(deps: Deps, verification: VerificationService, ch, kfid: str, eid: str,
                     user, content: str, ctype: str, msg_id: str | None) -> None:
    session_epoch = await deps.repos.incident.current_epoch(user.id)
    try:
        outcome = await verification.verify(user=user, content=content, content_type=ctype,
                                            channel="wecom", msg_id=msg_id, session_epoch=session_epoch)
    except DuplicateMessage:
        return
    except ValueError:
        try:
            await ch.kf_send_msg(kfid, eid, messages.LOOK_FAILED)
        except Exception:
            logger.warning("wecom reply failed", exc_info=True)
        return
    if outcome.duplicate or outcome.result is None:
        return
    result = outcome.result
    logger.info("wecom query processed openid=%s level=%s", OPENID_PREFIX + eid,
                result.verdict.level.value if result.verdict else "degraded")
    try:
        await ch.kf_send_msg(kfid, eid, result.reply)
    except Exception:
        logger.warning("wecom reply failed", exc_info=True)


async def _send_console_link(deps: Deps, ch, kfid: str, eid: str, token: str, first_message: bool) -> None:
    if not first_message or not deps.settings.public_base_url:
        return
    url = f"{deps.settings.public_base_url.rstrip('/')}/console?token={token}"
    try:
        await ch.kf_send_msg(kfid, eid, f"你的个人控制台:{url}")
    except Exception:
        logger.warning("wecom personal link delivery failed", exc_info=True)
