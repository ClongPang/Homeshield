"""企业微信(微信客服)通道:回调验活 + sync_msg 轮询 + 消息派发。

消息获取走拉取模式(轮询器带游标,不依赖回调推送;回调仅作验活与
后续实时性增强)。身份复用 user.openid,external_userid 加 wxkf: 前缀,
与合成账号的 demo:/test: 前缀同一约定;关系指令处理直接复用
api/wechat 的 _handle_relation_command。判定业务在 verification/pipeline。
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
from homeshield.core.triage import classify
from homeshield.core.verification import VerificationService
# 公众号通道的关系指令处理(邀请/绑定/我的联防/解除)与企微完全同构,直接复用
from homeshield.api.wechat import _handle_relation_command

logger = logging.getLogger(__name__)
OPENID_PREFIX = "wxkf:"
POLL_SECONDS = 5


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
            except (WeComCryptoError, ET.ParseError):
                logger.warning("wecom callback: decrypt/parse failed", exc_info=True)
        return PlainTextResponse("success")

    return router


async def wecom_poller(deps: Deps, verification: VerificationService) -> None:
    """每 POLL_SECONDS 拉一轮全部客服账号;游标仅存内存,重启后靠
    ingest 的 msg_id 幂等去重安全重放。"""
    cursors: dict[str, str] = {}
    while True:
        try:
            await poll_once(deps, verification, cursors)
        except Exception:
            logger.warning("wecom poll cycle failed", exc_info=True)
        await asyncio.sleep(POLL_SECONDS)


async def poll_once(deps: Deps, verification: VerificationService, cursors: dict[str, str]) -> None:
    ch = deps.wecom
    for account in await ch.kf_accounts():
        kfid = account.get("open_kfid", "")
        if not kfid:
            continue
        res = await ch.kf_sync_msg(kfid, cursors.get(kfid, ""))
        if res.get("errcode") != 0:
            logger.warning("wecom sync_msg failed %s %s", res.get("errcode"), res.get("errmsg"))
            continue
        cursors[kfid] = res.get("next_cursor") or cursors.get(kfid, "")
        if res.get("msg_list"):
            await handle_wecom_messages(deps, verification, res["msg_list"])


async def handle_wecom_messages(deps: Deps, verification: VerificationService, msgs: list[dict]) -> None:
    for m in msgs:
        try:
            await _handle_one(deps, verification, m)
        except Exception:
            logger.warning("wecom message handling failed", exc_info=True)


async def _handle_one(deps: Deps, verification: VerificationService, m: dict) -> None:
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
    existing = deps.repos.users.get_by_openid(openid)
    user = existing or deps.repos.users.get_or_create(openid)
    first_message = existing is None

    async def reply(text: str) -> None:
        try:
            await ch.kf_send_msg(kfid, eid, text)
        except Exception:
            logger.warning("wecom reply failed", exc_info=True)

    if m.get("msgtype") == "text":
        text = m.get("text", {}).get("content", "")
        response = _handle_relation_command(deps, user.id, openid, "text", text)
        if response is not None:
            await reply(response)
            await _send_first_link(deps, ch, kfid, eid, user.token,
                                   first_message and text.strip() != "我的联防")
            return
        triage = classify(text)
        if triage == "reset":
            deps.repos.incident.close_open_incident(user.id, msg_id=m.get("msgid"))
            await reply(messages.SESSION_RESET_REPLY)
            await _send_first_link(deps, ch, kfid, eid, user.token, first_message)
            return
        if triage == "ack":
            try:
                await verification.verify(user=user, content=text, content_type="text",
                                          channel="wecom", msg_id=m.get("msgid"))
            except Exception:
                logger.warning("wecom ack persistence failed", exc_info=True)
            await reply(messages.ACK_QUERY_REPLY)
            await _send_first_link(deps, ch, kfid, eid, user.token, first_message)
            return
        await _run_query(deps, verification, ch, kfid, eid, user, text, "text", m.get("msgid"))
        await _send_first_link(deps, ch, kfid, eid, user.token, first_message)
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
        await _send_first_link(deps, ch, kfid, eid, user.token, first_message)
        return

    if m.get("msgtype") == "link":
        link = m.get("link", {})
        content = "\n".join(filter(None, [link.get("title", ""), link.get("description", ""),
                                          link.get("url", "")]))
        if content:
            await _run_query(deps, verification, ch, kfid, eid, user, content, "text", m.get("msgid"))
            await _send_first_link(deps, ch, kfid, eid, user.token, first_message)
            return

    if m.get("msgtype") == "merged_msg":
        content = _flatten_merged(m.get("merged_msg", {}))
        if content:
            await _run_query(deps, verification, ch, kfid, eid, user, content, "text", m.get("msgid"))
            await _send_first_link(deps, ch, kfid, eid, user.token, first_message)
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
    session_epoch = deps.repos.incident.current_epoch(user.id)
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


async def _send_first_link(deps: Deps, ch, kfid: str, eid: str, token: str, first_message: bool) -> None:
    if not first_message or not deps.settings.public_base_url:
        return
    url = f"{deps.settings.public_base_url.rstrip('/')}/console?token={token}"
    try:
        await ch.kf_send_msg(kfid, eid, f"你的个人控制台:{url}")
    except Exception:
        logger.warning("wecom personal link delivery failed", exc_info=True)
