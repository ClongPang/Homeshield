"""微信通道路由:平台签名校验 + 消息派发。

5s 回调窗口内必须 ACK:开通/绑定命令与未绑定引导走被动回复(纯 DB 操作),
正式判定经客服接口异步送达;通道只做协议翻译,业务在 core(binding/pipeline)。
"""
import base64
import logging

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import PlainTextResponse

from homeshield.core import messages
from homeshield.core.binding import BindingError, parse_bind_command
from homeshield.core.deps import Deps
from homeshield.core.errors import DuplicateMessage
from homeshield.core.models import Member
from homeshield.core.verification import VerificationService

logger = logging.getLogger(__name__)

OPEN_COMMANDS = {"开通", "开通家庭"}


def build_wechat_router(deps: Deps, verification: VerificationService) -> APIRouter:
    router = APIRouter()

    # ch 由组合根按凭证创建(deps.wechat);未配置时回调为空壳,直接 ACK
    @router.get("/wechat/callback")
    async def wechat_verify(request: Request):
        ch = deps.wechat
        p = dict(request.query_params)
        if ch and ch.verify_signature(p.get("signature", ""), p.get("timestamp", ""), p.get("nonce", "")):
            return PlainTextResponse(p.get("echostr", ""))
        raise HTTPException(403, "bad signature")

    @router.post("/wechat/callback")
    async def wechat_callback(request: Request, background: BackgroundTasks):
        ch = deps.wechat
        if ch is None:
            return PlainTextResponse("success")
        body = await request.body()
        p = dict(request.query_params)
        if not ch.verify_signature(p.get("signature", ""), p.get("timestamp", ""), p.get("nonce", "")):
            raise HTTPException(403, "bad signature")
        data = ch.parse_callback(body)
        kind = ch.classify(data)

        if kind == "subscribe":
            background.add_task(_welcome_wechat, deps, ch, data)
            return PlainTextResponse("success")
        if kind == "unsupported":
            return PlainTextResponse(ch.passive_text_reply(data, messages.UNSUPPORTED_TYPE))
        if kind == "ignore":
            return PlainTextResponse("success")

        # 开通/绑定是纯 DB 操作,5s 窗口内同步回结果;未绑定者只引导不判定
        openid = data.get("FromUserName", "")
        member = deps.repos.member.get_by_openid(openid)
        command_reply = _binding_reply(deps, member, openid, kind, data.get("Content", ""))
        if command_reply is not None:
            return PlainTextResponse(ch.passive_text_reply(data, command_reply))
        if member is None:
            return PlainTextResponse(ch.passive_text_reply(data, messages.BIND_GUIDE))

        # text / image:5s 窗口内先回可见回执,正式判定经客服接口异步送达
        background.add_task(_handle_wechat_message, deps, verification, ch, data, member)
        return PlainTextResponse(ch.passive_text_reply(data, messages.RECEIVED_ACK))

    return router


def _binding_reply(deps: Deps, member: Member | None, openid: str, kind: str, text: str) -> str | None:
    """开通/绑定命令的同步回复;非命令消息返回 None 交回判定链路。

    未绑定:命令即时执行,其他文本返回 None 由调用方引导。
    已绑定:命令提示已在家庭中(防止把邀请码当可疑消息判定);其余交回判定。
    """
    if kind != "text":
        return None
    code = parse_bind_command(text)
    is_open = text.strip() in OPEN_COMMANDS
    if code is None and not is_open:
        return None
    if member is not None:
        return messages.BIND_ALREADY
    if code is not None:
        try:
            bound = deps.binding.bind(openid, code)
        except BindingError:
            return messages.BIND_INVALID
        fam = deps.repos.family.get(bound.family_id)
        link = bound.entry_url(deps.settings.public_base_url)
        return messages.BIND_SUCCESS.format(
            family=fam["name"] if fam else "我的家庭",
            name=bound.name,
            link=f"\n个人网页入口:{link}" if link else "",
        )
    try:
        admin = deps.binding.open_family(openid)
    except BindingError as e:
        return messages.OPEN_LIMIT if e.reason == "limit" else messages.BIND_ALREADY
    fam = deps.repos.family.get(admin.family_id)
    url = admin.entry_url(deps.settings.public_base_url)
    if url:
        return messages.OPEN_SUCCESS.format(family=fam["name"] if fam else "我的家庭", url=url)
    return messages.OPEN_NO_URL


async def _welcome_wechat(deps: Deps, ch, data: dict) -> None:
    """关注事件:发欢迎语(含开通/绑定指引);不再自动建成员。"""
    openid = data.get("FromUserName", "")
    try:
        await ch.send_customer_service(openid, messages.WELCOME)
    except Exception:
        logger.warning("wechat welcome failed openid=%s", openid, exc_info=True)


async def _handle_wechat_message(
    deps: Deps, verification: VerificationService, ch, data: dict, member: Member
) -> None:
    """消息判定链路:幂等 ingest → 管线 → 客服接口异步回消息。成员已由回调入口解析。"""
    openid = data.get("FromUserName", "")
    content, ctype = "", "text"
    if data.get("MsgType") == "image" and data.get("PicUrl"):
        try:
            async with httpx.AsyncClient(timeout=10) as hc:
                resp = await hc.get(data["PicUrl"])
            content = base64.b64encode(resp.content).decode()
            ctype = "image"
        except Exception:
            logger.warning("wechat image download failed", exc_info=True)
    else:
        content = data.get("Content", "")
    if not content:
        try:
            await ch.send_customer_service(openid, messages.LOOK_FAILED)
        except Exception:
            logger.warning("wechat reply failed openid=%s", openid, exc_info=True)
        return
    try:
        outcome = await verification.verify(
            member=member,
            content=content,
            content_type=ctype,
            channel="wechat",
            msg_id=data.get("MsgId") or None,
        )
    except DuplicateMessage:
        return
    if outcome.duplicate or outcome.result is None:
        return
    result = outcome.result
    logger.info("wechat query processed openid=%s level=%s", openid,
                result.verdict.level.value if result.verdict else "degraded")
    try:
        await ch.send_customer_service(openid, result.reply)
    except Exception:
        logger.warning("wechat reply failed openid=%s", openid, exc_info=True)
