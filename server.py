"""
HTTP 组合根(FastAPI):路由薄壳,业务在 core
家人凭证 = 不可枚举 token(部署者经 CLI 分发链接):所有家人 API 以 token
定位成员,无/错 token 一律 401;接口不返回任何 token
微信回调以平台签名校验;回调按消息类别派发,家人在 5s 窗口内得到可见回应
"""
import base64
import json
import logging
import pathlib

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel as PayloadModel

from core import messages
from core.config import Settings
from core.deps import Deps, build_deps
from core.errors import DuplicateMessage, HomeshieldError
from core.feedback import CorrectionService, weekly_report
from core.logsetup import setup_logging
from core.models import CorrectionLabel, Member, Role
from core.verification import VerificationService

logger = logging.getLogger(__name__)

WEB_DIR = pathlib.Path(__file__).parent / "web"


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
                {"name": m.name, "role": m.role.value}
                for m in deps.repos.member.list_members(member.family_id)
            ]
        }

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

        # text / image:5s 窗口内先回可见回执,正式判定经客服接口异步送达
        background.add_task(_handle_wechat_message, deps, verification, ch, data)
        return PlainTextResponse(ch.passive_text_reply(data, messages.RECEIVED_ACK))

    return app


def _ensure_member(deps: Deps, openid: str) -> Member | None:
    """openid → 成员;首个陌生 openid 自动绑为 elder,成员数达上限后不再新增。"""
    member = deps.repos.member.get_by_openid(openid)
    if member is not None:
        return member
    fam = deps.repos.family.get(1)
    fid = int(fam["id"]) if fam else deps.repos.family.create("我的家庭")
    if len(deps.repos.member.list_members(fid)) >= deps.settings.max_members:
        logger.warning("auto-bind rejected: family %s reached max members", fid)
        return None
    return deps.repos.member.get(deps.repos.member.add(fid, "家人", Role.ELDER, openid=openid))


async def _welcome_wechat(deps: Deps, ch, data: dict) -> None:
    """关注事件:绑定成员并发送欢迎语(含知情说明与使用方法)。"""
    openid = data.get("FromUserName", "")
    member = _ensure_member(deps, openid)
    if member is None:
        return
    try:
        await ch.send_customer_service(openid, messages.WELCOME)
    except Exception:
        logger.warning("wechat welcome failed openid=%s", openid, exc_info=True)


async def _handle_wechat_message(deps: Deps, verification: VerificationService, ch, data: dict) -> None:
    """消息判定链路:幂等 ingest → 管线 → 客服接口异步回消息。"""
    openid = data.get("FromUserName", "")
    member = _ensure_member(deps, openid)
    if member is None:
        return
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
