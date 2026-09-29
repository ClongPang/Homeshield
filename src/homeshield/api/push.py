"""推送通道自助开通 API:状态、二维码资产、手机号辅映射、确认、重测与解绑。

个人 token 授权(与 api/relations 同一口径);OAuth 主映射的 start/callback 在
api/wecom(与回调同源),本路由只承载控制台侧的查询与操作。状态变更经既有 SSE
流以 push_status 事件推送。
"""
import logging

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, RedirectResponse

from homeshield.api.schemas import MobileIn, TokenIn
from homeshield.core.deps import Deps
from homeshield.core.models import User
from homeshield.core.push import PushService

logger = logging.getLogger(__name__)


def _publish_status(deps: Deps, user_id: int, status: str) -> None:
    deps.broker.publish_alert(user_id, {"kind": "push_status", "status": status})


def build_push_router(deps: Deps, push: PushService) -> APIRouter:
    router = APIRouter()

    async def require_user(token: str) -> User:
        user = await deps.repos.users.get_by_token(token or "")
        if user is None: raise HTTPException(401, "invalid token")
        return user

    async def _status(user_id: int) -> dict:
        result = await push.status_for(user_id)
        result["qr_available"] = push.qr_available()
        # OAuth 依赖可信域名（需备案）；HTTP 过渡期如实隐藏，避免按钮点了必失败
        result["oauth_available"] = bool(
            deps.wecom is not None and deps.wecom.api_ready
            and deps.settings.public_base_url.startswith("https://")
            and deps.settings.wecom_agent_id)
        result["mobile_available"] = bool(deps.wecom is not None and deps.wecom.api_ready)
        result["self_enroll"] = bool(deps.settings.push_self_enroll and push.contact_available)
        return result

    @router.get("/api/push/status")
    async def push_status(token: str = Query("")):
        user = await require_user(token)
        return await _status(user.id)

    @router.get("/api/push/qr")
    async def push_qr(token: str = Query("")):
        await require_user(token)
        from pathlib import Path
        path = Path(push.s.wecom_plugin_qr_path)
        if not path.is_file(): raise HTTPException(404, "qr asset missing")
        media_type = "image/jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "image/png"
        return FileResponse(path, media_type=media_type)

    @router.post("/api/push/enroll-mobile")
    async def enroll_mobile(body: MobileIn):
        user = await require_user(body.token)
        outcome = await push.enroll_mobile(user, body.mobile)
        if outcome.get("ok"):
            await push.send_test_message(user)
            _publish_status(deps, user.id, "bound")
            return {"status": "bound", "text": outcome["text"]}
        codes = {"invalid": 400, "guarded": 429, "unavailable": 503}
        if outcome["status"] in codes:
            raise HTTPException(codes[outcome["status"]], outcome["text"])
        return {"status": outcome["status"], "text": outcome["text"]}

    @router.post("/api/push/verify")
    async def verify(body: TokenIn):
        user = await require_user(body.token)
        member = await deps.repos.wecom_member.get_member(user.id)
        if member is None: raise HTTPException(404, "push not provisioned")
        await deps.repos.wecom_member.mark_verified(user.id)
        _publish_status(deps, user.id, "verified")
        return {"status": "verified", "text": "确认完成，预警提醒将从这里送达。"}

    @router.get("/api/push/verify")
    async def verify_link(token: str = Query("")):
        """测试应用消息里的确认链接:点击即确认,随后回到控制台。"""
        user = await require_user(token)
        member = await deps.repos.wecom_member.get_member(user.id)
        if member is None: raise HTTPException(404, "push not provisioned")
        await deps.repos.wecom_member.mark_verified(user.id)
        _publish_status(deps, user.id, "verified")
        return RedirectResponse(f"/console?token={token}&push=verified")

    @router.post("/api/push/retest")
    async def retest(body: TokenIn):
        user = await require_user(body.token)
        member = await deps.repos.wecom_member.get_member(user.id)
        if member is None: raise HTTPException(404, "push not provisioned")
        await deps.repos.wecom_member.touch_confirm(user.id)
        sent = await push.send_test_message(user)
        status = "bound" if sent else "abnormal"
        _publish_status(deps, user.id, status)
        return {"status": status, "text": "测试消息已重新发送，请留意微信插件。" if sent else "测试消息发送失败，请稍后重试。"}

    @router.post("/api/push/unbind")
    async def unbind(body: TokenIn):
        user = await require_user(body.token)
        removed = await deps.repos.wecom_member.unbind(user.id)
        _publish_status(deps, user.id, "unbound")
        return {"status": "unbound", "text": "已关闭推送" if removed else "推送本就未开通"}

    return router
