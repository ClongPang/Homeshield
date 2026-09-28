"""WeChat callback transport. Every first text/image message creates a private user."""
import base64
import logging

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import PlainTextResponse

from homeshield.core import messages
from homeshield.core.deps import Deps
from homeshield.core.errors import DuplicateMessage
from homeshield.core.relations import (RelationError, is_old_group_command, parse_bind_command,
                                       parse_end_command, parse_invite_command)
from homeshield.core.triage import classify
from homeshield.core.verification import VerificationService

logger = logging.getLogger(__name__)
OLD_COMMAND_HINT = "命令已更新：回复「邀请 称呼」发起联防，「我的联防」查看，「解除 称呼」停止。"


def build_wechat_router(deps: Deps, verification: VerificationService) -> APIRouter:
    router = APIRouter()

    @router.get("/wechat/callback")
    async def wechat_verify(request: Request):
        ch = deps.wechat
        params = dict(request.query_params)
        if ch and ch.verify_signature(params.get("signature", ""), params.get("timestamp", ""), params.get("nonce", "")):
            return PlainTextResponse(params.get("echostr", ""))
        raise HTTPException(403, "bad signature")

    @router.post("/wechat/callback")
    async def wechat_callback(request: Request, background: BackgroundTasks):
        ch = deps.wechat
        if ch is None: return PlainTextResponse("success")
        body, params = await request.body(), dict(request.query_params)
        if not ch.verify_signature(params.get("signature", ""), params.get("timestamp", ""), params.get("nonce", "")):
            raise HTTPException(403, "bad signature")
        data = ch.parse_wechat_callback_xml(body)
        kind = ch.classify_callback_message(data)
        if kind == "subscribe":
            background.add_task(_welcome_wechat, deps, ch, data)
            return PlainTextResponse("success")
        if kind in {"unsupported", "ignore"}:
            if kind == "unsupported": return PlainTextResponse(ch.passive_text_reply(data, messages.UNSUPPORTED_TYPE))
            return PlainTextResponse("success")

        openid = data.get("FromUserName", "")
        existing = deps.repos.users.get_by_openid(openid)
        user = existing or deps.repos.users.get_or_create(openid)
        first_message = existing is None
        text = data.get("Content", "")
        response = _handle_relation_command(deps, user.id, openid, kind, text)
        if response is not None:
            background.add_task(_send_first_link, deps, ch, openid, user.token,
                                first_message and text.strip() != "我的联防")
            return PlainTextResponse(ch.passive_text_reply(data, response))

        if kind == "text":
            triage = classify(text)
            if triage == "reset":
                deps.repos.incident.close_open_incident(user.id, msg_id=data.get("MsgId") or None)
                background.add_task(_send_first_link, deps, ch, openid, user.token, first_message)
                return PlainTextResponse(ch.passive_text_reply(data, messages.SESSION_RESET_REPLY))
            if triage == "ack":
                background.add_task(_record_ack, verification, data, user)
                background.add_task(_send_first_link, deps, ch, openid, user.token, first_message)
                return PlainTextResponse(ch.passive_text_reply(data, messages.ACK_QUERY_REPLY))

        # Reply inside WeChat's callback window, then classify asynchronously.
        session_epoch = deps.repos.incident.current_epoch(user.id)
        background.add_task(_handle_wechat_message, deps, verification, ch, data, user, session_epoch)
        background.add_task(_send_first_link, deps, ch, openid, user.token, first_message)
        return PlainTextResponse(ch.passive_text_reply(data, messages.RECEIVED_ACK))

    return router


def _handle_relation_command(deps: Deps, user_id: int, openid: str, kind: str, text: str) -> str | None:
    if kind != "text": return None
    if is_old_group_command(text): return OLD_COMMAND_HINT
    code = parse_bind_command(text)
    invite_name = parse_invite_command(text)
    end_selector = parse_end_command(text)
    if code is not None:
        try:
            _, relation_id, _ = deps.relations.join(openid, code)
        except RelationError as exc:
            return {
                "invalid": "这个邀请码无效。请让邀请者检查是否过期、已撤销或已使用。",
                "expired": "这个邀请码已过期，请让邀请者重新邀请。",
                "used": "这个邀请码已被使用。",
                "revoked": "这个邀请码已撤销。",
                "self": "不能绑定自己发出的邀请码。",
                "already_exists": "这条联防关系已经建立，无需重复绑定。",
                "limit": f"你或邀请者的活跃联防已达上限（{deps.settings.max_relations} 条）。",
            }.get(exc.reason, "绑定暂时未完成，请稍后重试。")
        return ("已建立联防关系。对方会收到你的高危提醒；你主动纠正其他判定时，原查询内容也会供对方投票查看。"
                "回复「我的联防」查看，回复「解除 #" + str(relation_id) + "」可停止。")
    if invite_name is not None:
        try:
            invite = deps.relations.issue_invite(user_id, invite_name)
        except RelationError as exc:
            return f"活跃联防已达上限（{deps.settings.max_relations} 条），请先解除一条再邀请。" if exc.reason == "limit" else "暂时无法生成邀请码。"
        url = f"{deps.settings.public_base_url.rstrip('/')}/join/{invite['code']}" if deps.settings.public_base_url else ""
        link = f"\n邀请链接：{url}" if url else ""
        return (f"邀请码：{invite['code']}{link}\nTA 绑定后，你将收到 TA 的高危提醒；"
                "TA 主动纠正低风险判定时，原查询也会供你投票查看。")
    if text.strip() == "我的联防":
        data = deps.relations.list_for_user(user_id)
        outgoing = [f"#{r['id']} {r['name']}" + ("（已静音）" if r["mute"] else "") for r in data["guardings"]]
        incoming = [f"#{r['id']} {r['name']}" for r in data["guardians"]]
        result = "我护着：" + ("、".join(outgoing) if outgoing else "暂无") + "\n护着我：" + ("、".join(incoming) if incoming else "暂无")
        if not outgoing and not incoming:
            result += "\n还没有联防。回复「邀请 称呼」发起联防；也可以直接转发可疑消息给我查。"
        url = deps.repos.users.get(user_id).entry_url(deps.settings.public_base_url)
        if url: result += f"\n个人控制台：{url}"
        return result
    if end_selector is not None:
        status, matches = deps.relations.end_by_selector(user_id, end_selector)
        if status == "ambiguous":
            options = "、".join(f"#{r['id']} {r['display_name']}" for r in matches)
            return f"称呼重复，请用关系编号解除：{options}"
        if status == "not_found": return "没有找到这条联防关系。回复「我的联防」查看关系编号。"
        relation = matches[0]
        if status == "by_protector": return f"已解除 #{relation['id']}：对方不再收到你的高危提醒。"
        if status == "by_protected": return f"已解除 #{relation['id']}：你不再收到对方的高危提醒。"
        return "这条联防关系已解除。"
    return None


async def _welcome_wechat(deps: Deps, ch, data: dict) -> None:
    openid = data.get("FromUserName", "")
    try: await ch.send_customer_service(openid, messages.WELCOME)
    except Exception: logger.warning("wechat welcome failed openid=%s", openid, exc_info=True)


async def _send_first_link(deps: Deps, ch, openid: str, token: str, first_message: bool) -> None:
    # Passive-reply-only and synthetic accounts must not receive a second reply.
    if (not first_message or not deps.settings.public_base_url or not deps.settings.wechat_appid
            or not deps.settings.wechat_secret or openid.startswith(("demo:", "test:"))):
        return
    url = f"{deps.settings.public_base_url.rstrip('/')}/console?token={token}"
    try: await ch.send_customer_service(openid, f"你的个人控制台：{url}")
    except Exception: logger.warning("personal link delivery failed openid=%s", openid, exc_info=True)


async def _record_ack(verification: VerificationService, data: dict, user) -> None:
    try:
        await verification.verify(user=user, content=data.get("Content", ""), content_type="text",
                                  channel="wechat", msg_id=data.get("MsgId") or None)
    except Exception: logger.warning("wechat ack persistence failed", exc_info=True)


async def _handle_wechat_message(deps: Deps, verification: VerificationService, ch, data: dict,
                                 user, session_epoch: int | None = None) -> None:
    openid = data.get("FromUserName", "")
    content, ctype = "", None
    if data.get("MsgType") == "image" and data.get("PicUrl"):
        try:
            async with httpx.AsyncClient(timeout=10) as hc:
                response = await hc.get(data["PicUrl"])
            content, ctype = base64.b64encode(response.content).decode(), "image"
        except Exception: logger.warning("wechat image download failed", exc_info=True)
    else: content = data.get("Content", "")
    if not content:
        try: await ch.send_customer_service(openid, messages.LOOK_FAILED)
        except Exception: logger.warning("wechat reply failed openid=%s", openid, exc_info=True)
        return
    try:
        outcome = await verification.verify(user=user, content=content, content_type=ctype, channel="wechat",
                                            msg_id=data.get("MsgId") or None, session_epoch=session_epoch)
    except DuplicateMessage: return
    except ValueError:
        await ch.send_customer_service(openid, messages.LOOK_FAILED)
        return
    if outcome.duplicate or outcome.result is None: return
    result = outcome.result
    logger.info("wechat query processed openid=%s level=%s", openid,
                result.verdict.level.value if result.verdict else "degraded")
    try: await ch.send_customer_service(openid, result.reply)
    except Exception: logger.warning("wechat reply failed openid=%s", openid, exc_info=True)
