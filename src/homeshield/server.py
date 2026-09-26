"""
HTTP 组合根(FastAPI):路由薄壳,业务在 core
家人凭证 = 不可枚举 token:所有家人 API 以 token 定位成员,无/错 token 一律
401;接口不返回任何 token。多租户:成员经「开通/绑定码」入家(见 core/binding),
网页 token 链接通道保留;所有家人数据按 member.family_id 隔离
微信回调以平台签名校验;开通/绑定命令同步回复,其余 5s 窗口内先回执再异步判定
"""
import base64
import json
import logging
import pathlib
import re

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel as PayloadModel

from homeshield.core import messages
from homeshield.core.binding import BindingError
from homeshield.core.config import Settings
from homeshield.core.deps import Deps, build_deps
from homeshield.core.errors import DuplicateMessage, HomeshieldError
from homeshield.core.feedback import CorrectionService, weekly_report
from homeshield.core.logsetup import setup_logging
from homeshield.core.models import CorrectionLabel, Member, Role
from homeshield.core.verification import VerificationService

logger = logging.getLogger(__name__)

WEB_DIR = pathlib.Path(__file__).parent / "web"

OPEN_COMMANDS = {"开通", "开通家庭"}
_BIND_RE = re.compile(r"^(?:绑定|綁定)\s*[::]?\s*([0-9A-Za-z]{4,16})$")


class QueryIn(PayloadModel):
    token: str
    content: str
    content_type: str | None = None
    msg_id: str | None = None


class CorrectionIn(PayloadModel):
    token: str
    verdict_id: int
    label: str
    note: str = ""


class DecideIn(PayloadModel):
    token: str
    decision: str = "confirm"  # confirm | reject


class TokenIn(PayloadModel):
    token: str


class MemberIn(PayloadModel):
    token: str
    name: str
    role: str = "elder"  # elder | adult


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.load()
    setup_logging()
    deps = build_deps(settings)
    verification = deps.verification
    corrections = CorrectionService(deps.repos)
    app = FastAPI(title="Homeshield")
    app.state.deps = deps

    def member_by_token(token: str) -> Member:
        member = deps.repos.member.get_by_token(token or "")
        if member is None:
            raise HTTPException(401, "invalid token")
        return member

    @app.post("/api/query")
    async def api_query(body: QueryIn):
        member = member_by_token(body.token)
        try:
            outcome = await verification.verify(
                member=member,
                content=body.content,
                content_type=body.content_type,
                channel="web",
                msg_id=body.msg_id,
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        except DuplicateMessage as e:
            raise HTTPException(409, f"duplicate msg_id={e.msg_id}")
        if outcome.duplicate:
            raise HTTPException(409, "duplicate msg_id")
        result = outcome.result
        return {
            "query_id": result.query_id,
            "verdict_id": result.verdict_id,
            "level": result.verdict.level.value if result.verdict else None,
            "cited_ids": result.verdict.cited_ids if result.verdict else [],
            "reply": result.reply,
            "latency_ms": result.latency_ms,
        }

    @app.get("/api/stream")
    async def api_stream(token: str = Query("")):
        member = member_by_token(token)
        q = deps.broker.subscribe(member.family_id)

        async def gen():
            try:
                while True:
                    payload = await q.get()
                    yield f"event: alert\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            finally:
                deps.broker.unsubscribe(member.family_id, q)

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/api/alerts")
    def api_alerts(token: str = Query("")):
        """高危告警历史:控制台打开时先渲染历史,SSE 只补增量。"""
        member = member_by_token(token)
        rows = deps.repos.alert.list_by_family(member.family_id)
        return {
            "alerts": [
                {
                    "verdict_id": r["verdict_id"],
                    "level": r["level"],
                    "summary": r["content"][:50],
                    "delivered_at": r["delivered_at"],
                }
                for r in rows
            ]
        }

    @app.get("/api/alerts/{verdict_id}")
    def api_alert_detail(verdict_id: int, token: str = Query("")):
        """单条告警详情(落地页数据):结论、依据与本人反馈状态。"""
        member = member_by_token(token)
        detail = deps.repos.verdict.get_with_context(verdict_id)
        if detail is None or detail["family_id"] != member.family_id:
            raise HTTPException(404, "alert not found")
        mine = deps.repos.correction.get_by_verdict_and_member(verdict_id, member.id)
        return {
            "verdict_id": verdict_id,
            "level": detail["level"],
            "reply": detail["reply"],
            "summary": detail["content"][:80],
            "created_at": detail["created_at"],
            "my_feedback": (
                {"label": mine.label.value, "status": mine.status.value} if mine else None
            ),
        }

    @app.get("/alert/{verdict_id}")
    def alert_page(verdict_id: int):
        return FileResponse(WEB_DIR / "alert.html")

    @app.get("/api/corrections")
    def api_corrections_list(token: str = Query("")):
        member = member_by_token(token)
        rows = deps.repos.correction.list_pending_with_context(member.family_id)
        return {"pending": rows}

    @app.post("/api/corrections")
    def api_corrections(body: CorrectionIn):
        member = member_by_token(body.token)
        try:
            cid, status = corrections.submit(
                body.verdict_id, member.id, CorrectionLabel(body.label), body.note
            )
        except (HomeshieldError, ValueError) as e:
            raise HTTPException(400, str(e))
        return {"correction_id": cid, "status": status.value}

    @app.post("/api/corrections/{cid}/confirm")
    def api_confirm(cid: int, body: DecideIn):
        member = member_by_token(body.token)
        try:
            status = corrections.decide(cid, member.id, body.decision)
        except HomeshieldError as e:
            raise HTTPException(400, str(e))
        return {"status": status.value}

    @app.get("/api/members")
    def api_members(token: str = Query("")):
        member = member_by_token(token)
        return {
            "members": [
                {
                    "id": m.id,
                    "name": m.name,
                    "role": m.role.value,
                    "bound": m.openid is not None,
                }
                for m in deps.repos.member.list_members(member.family_id)
            ]
        }

    @app.post("/api/members")
    def api_add_member(body: MemberIn):
        """创建成员位并签发绑定码;仅 adult 管理员,成员数封顶。"""
        member = member_by_token(body.token)
        if member.role is not Role.ADULT:
            raise HTTPException(403, "only adult can manage members")
        try:
            target_role = Role(body.role)
        except ValueError:
            raise HTTPException(400, "role must be elder or adult")
        try:
            deps.binding.ensure_member_capacity(member.family_id)
        except BindingError:
            raise HTTPException(400, "family reached max members")
        name = body.name.strip()
        if not name:
            raise HTTPException(400, "name is empty")
        target = deps.repos.member.get(
            deps.repos.member.add(member.family_id, name, target_role)
        )
        code = deps.binding.issue_code(target, created_by=member.id)
        return {
            "member_id": target.id,
            "name": target.name,
            "role": target.role.value,
            "bind_code": code["code"],
            "bind_expires_at": code["expires_at"],
            "entry_url": _entry_url(deps.settings.public_base_url, target) or None,
        }

    @app.post("/api/members/{member_id}/bind-code")
    def api_reissue_bind_code(member_id: int, body: TokenIn):
        """重发绑定码(作废旧码);管理员给自己重发即是本人微信绑定入口。"""
        member = member_by_token(body.token)
        if member.role is not Role.ADULT:
            raise HTTPException(403, "only adult can manage members")
        target = deps.repos.member.get(member_id)
        if target is None or target.family_id != member.family_id:
            raise HTTPException(404, "member not found")
        try:
            code = deps.binding.issue_code(target, created_by=member.id)
        except BindingError as e:
            raise HTTPException(400, e.reason)
        return {"bind_code": code["code"], "bind_expires_at": code["expires_at"]}

    @app.get("/api/weekly")
    def api_weekly(token: str = Query("")):
        member = member_by_token(token)
        return weekly_report(deps.repos, member.family_id)

    @app.get("/")
    def index():
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/console")
    def console():
        return FileResponse(WEB_DIR / "console.html")

    # ---- 微信通道:按消息类别派发,家人在 5s 窗口内得到可见回应 ---------
    # ch 由组合根按凭证创建(deps.wechat);未配置时回调为空壳,直接 ACK
    @app.get("/wechat/callback")
    async def wechat_verify(request: Request):
        ch = deps.wechat
        p = dict(request.query_params)
        if ch and ch.verify_signature(p.get("signature", ""), p.get("timestamp", ""), p.get("nonce", "")):
            return PlainTextResponse(p.get("echostr", ""))
        raise HTTPException(403, "bad signature")

    @app.post("/wechat/callback")
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

    return app


def _parse_bind_code(text: str) -> str | None:
    """「绑定 <码>」→ 码;其余返回 None。大小写不敏感,认领时统一归一。"""
    m = _BIND_RE.match(text.strip())
    return m.group(1).upper() if m else None


def _entry_url(base_url: str, member: Member) -> str:
    """成员个人网页入口(adult→控制台,elder→兜底聊天页);未配对外地址返回空。"""
    if not base_url:
        return ""
    base = base_url.rstrip("/")
    if member.role is Role.ADULT:
        return f"{base}/console?token={member.token}"
    return f"{base}/?token={member.token}"


def _binding_reply(deps: Deps, member: Member | None, openid: str, kind: str, text: str) -> str | None:
    """开通/绑定命令的同步回复;非命令消息返回 None 交回判定链路。

    未绑定:命令即时执行,其他文本返回 None 由调用方引导。
    已绑定:命令提示已在家庭中(防止把邀请码当可疑消息判定);其余交回判定。
    """
    if kind != "text":
        return None
    code = _parse_bind_code(text)
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
        link = _entry_url(deps.settings.public_base_url, bound)
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
    url = (
        f"{deps.settings.public_base_url.rstrip('/')}/console?token={admin.token}"
        if deps.settings.public_base_url
        else ""
    )
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


app = create_app()
