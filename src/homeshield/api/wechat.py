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
from homeshield.core.binding import BindingError, parse_bind_command, parse_group_command, parse_open_command
from homeshield.core.deps import Deps
from homeshield.core.errors import DuplicateMessage
from homeshield.core.models import Member
from homeshield.core.verification import VerificationService

logger = logging.getLogger(__name__)

GROUP_LIST_COMMANDS = {"我的群", "我的防护群"}


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
        data = ch.parse_wechat_callback_xml(body)
        kind = ch.classify_callback_message(data)

        if kind == "subscribe":
            background.add_task(_welcome_wechat, deps, ch, data)
            return PlainTextResponse("success")
        if kind == "unsupported":
            return PlainTextResponse(ch.passive_text_reply(data, messages.UNSUPPORTED_TYPE))
        if kind == "ignore":
            return PlainTextResponse("success")

        # 开通/绑定是纯 DB 操作,5s 窗口内同步回结果;未绑定者只引导不判定
        openid = data.get("FromUserName", "")
        user = deps.repos.users.get_by_openid(openid)
        memberships = deps.repos.member.list_for_user(user.id) if user else []
        command_reply = _handle_membership_command(deps, user, openid, kind, data.get("Content", ""))
        if command_reply is not None:
            return PlainTextResponse(ch.passive_text_reply(data, command_reply))
        if user is None:
            return PlainTextResponse(ch.passive_text_reply(data, messages.BIND_GUIDE))
        if not memberships:
            return PlainTextResponse(ch.passive_text_reply(data, messages.BIND_GUIDE_OUTSIDE_GROUP))

        # text / image:5s 窗口内先回可见回执,正式判定经客服接口异步送达
        background.add_task(_handle_wechat_message, deps, verification, ch, data, user, memberships)
        return PlainTextResponse(ch.passive_text_reply(data, messages.RECEIVED_ACK))

    return router


def _handle_membership_command(deps: Deps, user, openid: str, kind: str, text: str) -> str | None:
    """开通/绑定命令的同步回复;非命令消息返回 None 交回判定链路。

    查询消息不选群;命令以当前用户的活跃群列表解析。
    """
    if kind != "text":
        return None
    code = parse_bind_command(text)
    open_name = parse_open_command(text)
    list_groups = text.strip() in GROUP_LIST_COMMANDS
    exit_name = parse_group_command(text,"退出") or parse_group_command(text,"退群")
    disband_name = parse_group_command(text,"解散")
    if code is None and open_name is None and not list_groups and exit_name is None and disband_name is None:
        return None
    if code is not None:
        try:
            bound = deps.binding.bind_member_with_invite_code(openid, code)
        except BindingError as exc:
            if exc.reason == "already_in_group":
                return messages.BIND_ALREADY
            if exc.reason == "retry":
                return messages.BIND_RETRY
            return messages.BIND_INVALID
        group = deps.repos.group.get(bound.group_id)
        identity = deps.repos.users.get(bound.user_id) if bound.user_id is not None else None
        link = identity.entry_url(deps.settings.public_base_url) if identity else ""
        return messages.BIND_SUCCESS.format(
            group=group["name"] if group else "我的防护群",
            name=bound.name,
            link=f"\n个人网页入口：{link}" if link else "",
        )
    if list_groups:
        if user is None:
            return messages.BIND_GUIDE
        groups = deps.repos.group.list_active_groups_for_user(user.id)
        if not groups:
            return messages.BIND_GUIDE_OUTSIDE_GROUP
        return "你加入的防护群：\n" + "\n".join(
            f"{i}. {g['name']}（{'信任成员' if g['trusted'] else '普通成员'}）" for i,g in enumerate(groups,1)
        )
    if open_name is not None:
        if user is None:
            try:
                admin = deps.binding.create_initial_group(openid,open_name or None)
            except BindingError as e:
                return messages.OPEN_LIMIT if e.reason in ("limit","group_limit") else messages.BIND_ALREADY
        elif not open_name:
            groups = deps.repos.group.list_active_groups_for_user(user.id)
            if groups:
                return "你已加入这些防护群：\n" + "\n".join(
                    f"{i}. {g['name']}" for i,g in enumerate(groups,1)
                ) + "\n新建群请回复：开通 群名"
            try:
                admin = deps.binding.create_initial_group(openid)
            except BindingError as e:
                return messages.OPEN_LIMIT if e.reason in ("limit","group_limit") else messages.BIND_ALREADY
        else:
            try:
                admin = deps.binding.create_group(user.id,open_name)
            except BindingError as e:
                return messages.OPEN_LIMIT if e.reason in ("limit","group_limit") else "群名不能为空。"
    elif exit_name is not None or disband_name is not None:
        if user is None:
            return messages.BIND_GUIDE
        group_name = exit_name or disband_name
        target = _resolve_group_selector(deps,user.id,group_name)
        if isinstance(target,str):
            return target
        try:
            if disband_name is not None:
                deps.groups.disband_group(user.id,target["id"])
                return f"「{target['name']}」已解散，群内成员将无法再查看历史提醒。"
            result = deps.groups.leave_group(user.id,target["id"])
            return f"已退出「{target['name']}」。" + ("群内最后一名成员已退出，防护群已自动解散。" if result=="disbanded" else "")
        except Exception as e:
            reason = getattr(e,"message",None) or str(e)
            if "trust" in reason:
                return "你是群内最后一位信任成员。请先把信任权限交给其他成员，再退出。"
            return "无法完成操作，请检查群名和权限。"
    else:
        return None
    group = deps.repos.group.get(admin.group_id)
    identity = deps.repos.users.get(user.id if user else admin.user_id)
    url = identity.entry_url(deps.settings.public_base_url) if identity else ""
    if url:
        return messages.OPEN_SUCCESS.format(group=group["name"] if group else "我的防护群", url=url)
    return messages.OPEN_NO_URL.format(group=group["name"] if group else "我的防护群")


async def _welcome_wechat(deps: Deps, ch, data: dict) -> None:
    """关注事件:发欢迎语(含开通/绑定指引);不再自动建成员。"""
    openid = data.get("FromUserName", "")
    try:
        await ch.send_customer_service(openid, messages.WELCOME)
    except Exception:
        logger.warning("wechat welcome failed openid=%s", openid, exc_info=True)


async def _handle_wechat_message(
    deps: Deps, verification: VerificationService, ch, data: dict, user, memberships: list[Member]
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
            user=user,
            memberships=memberships,
            content=content,
            content_type=ctype,
            channel="wechat",
            msg_id=data.get("MsgId") or None,
        )
    except DuplicateMessage:
        return
    except ValueError:
        await ch.send_customer_service(openid,messages.BIND_GUIDE_OUTSIDE_GROUP)
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


def _resolve_group_selector(deps: Deps, user_id: int, selector: str):
    groups = deps.repos.group.list_active_groups_for_user(user_id)
    matches = [g for g in groups if g["name"] == selector]
    if not matches and selector.isdigit():
        index = int(selector)
        if 1 <= index <= len(groups):
            return groups[index-1]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return "群名重复，请先回复「我的群」，再用列表序号操作。"
    return "没有找到这个防护群。回复「我的群」查看群列表。"
