"""Personal-token APIs for directed relations, alerts, queries and correction votes."""
import asyncio
import json
import time

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from homeshield.api.schemas import CorrectionIn, InviteIn, QueryIn, RelationPatchIn, TokenIn
from homeshield.core.deps import Deps
from homeshield.core.errors import DuplicateMessage, HomeshieldError
from homeshield.core.feedback import CorrectionService
from homeshield.core.models import User
from homeshield.core.verification import VerificationService

# 控制台直接展示 detail;服务层机器码在此收口为可读文案(v2.8 先例),未知错误兜底同口径
CORRECTION_ERROR_TEXT = {
    "verdict not found": "这条判定不存在，或不是你可见的记录。",
    "verdict is not eligible for correction": "这条消息暂时无法反馈，请稍后重试。",
    "only queryer can open low-risk correction": "低风险判定只有查询者本人可以反馈。",
    "no active voting relation": "你已不在这条提醒的相关关系中，无法投票。",
    "relation is not eligible for this correction": "这条纠正开启后才加入的关系不能补投，感谢你的关注。",
    "queryer feedback cannot be changed": "你的反馈已提交，不能更改。",
    "vote cannot be changed": "你已投过票，不能更改。",
    "correction case is closed": "这次纠正已收口，不能再投票。",
    "correction case not found": "纠正记录不存在。",
    "label must be real or false_positive": "反馈标签无效，请重新提交。",
}


def build_relation_router(deps: Deps, verification: VerificationService,
                          corrections: CorrectionService) -> APIRouter:
    router = APIRouter()

    async def require_user(token: str) -> User:
        user = await deps.repos.users.get_by_token(token or "")
        if user is None: raise HTTPException(401, "invalid token")
        return user

    @router.post("/api/query")
    async def api_query(body: QueryIn):
        """
            用户提交查询内容，系统验证内容并返回查询结果
        """
        user = await require_user(body.token)
        try:
            result = await verification.verify(user=user, content=body.content, content_type=body.content_type,
                                               channel="web", msg_id=body.msg_id)
        except DuplicateMessage as exc:
            raise HTTPException(409, "duplicate message") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if result.duplicate or result.result is None:
            raise HTTPException(409, "duplicate message")
        pipeline_result = result.result
        return {
            "kind": result.kind,
            "query_id": pipeline_result.query_id,
            "verdict_id": pipeline_result.verdict_id,
            "level": pipeline_result.verdict.level.value if pipeline_result.verdict else None,
            "cited_ids": pipeline_result.verdict.cited_ids if pipeline_result.verdict else [],
            "reply": pipeline_result.reply,
            "latency_ms": pipeline_result.latency_ms,
        }

    @router.get("/api/relations")
    async def list_relations(token: str = Query("")):
        user = await require_user(token)
        return await deps.relations.list_for_user(user.id)

    @router.get("/api/join/{code}")
    async def join_info(code: str):
        invite = await deps.repos.invite.get_valid(code)
        if invite is None: raise HTTPException(404, "invitation unavailable")
        preview = deps.relations.join_preview(invite)
        return {**preview, "expires_at": invite["expires_at"], "command": f"绑定 {invite['code']}"}

    @router.post("/api/relations/invites")
    async def create_invite(body: InviteIn):
        user = await require_user(body.token)
        name = body.name.strip() or "家人"
        try: invite = await deps.relations.issue_invite(user.id, name)
        except HomeshieldError as exc: raise HTTPException(400, getattr(exc, "reason", str(exc))) from exc
        base = deps.settings.public_base_url.rstrip("/")
        return {"id": invite["id"], "code": invite["code"],
                "join_url": f"{base}/join/{invite['code']}" if base else None,
                "expires_at": invite["expires_at"]}

    @router.get("/api/relations/invites")
    async def list_invites(token: str = Query("")):
        user = await require_user(token)
        now = time.time()
        rows = await deps.repos.invite.list_for_creator(user.id)
        for row in rows:
            row["expired"] = row["expires_at"] <= now
        return {"invites": rows}

    @router.delete("/api/relations/invites/{invite_id}")
    async def revoke_invite(invite_id: int, body: TokenIn):
        user = await require_user(body.token)
        status = await deps.repos.invite.revoke(invite_id, user.id)
        if status == "not_found": raise HTTPException(404, "invite not found")
        if status == "used": raise HTTPException(409, "invite already used")
        return {"status": status}

    @router.patch("/api/relations/{relation_id}")
    async def update_relation(relation_id: int, body: RelationPatchIn):
        user = await require_user(body.token)
        name = body.name.strip() if body.name is not None else None
        inverse_name = body.inverse_name.strip() if body.inverse_name is not None else None
        if name == "" or inverse_name == "": raise HTTPException(400, "name is empty")
        status = await deps.repos.relation.update(relation_id, user.id, name=name, inverse_name=inverse_name, mute=body.mute)
        if status == "relation_ended": raise HTTPException(410, "relation_ended")
        if status == "not_participant": raise HTTPException(404, "relation not found")
        if status == "wrong_side": raise HTTPException(403, "field cannot be changed from this side")
        return {"status": status}

    @router.delete("/api/relations/{relation_id}")
    async def end_relation(relation_id: int, body: TokenIn):
        user = await require_user(body.token)
        status = await deps.relations.end(user.id, relation_id)
        if status == "not_found": raise HTTPException(404, "relation not found")
        if status == "already_ended": raise HTTPException(410, "relation_ended")
        return {"status": status}

    @router.get("/api/my-queries")
    async def my_queries(token: str = Query("")):
        user = await require_user(token)
        await deps.repos.correction.close_expired()
        return {"queries": await deps.repos.query.list_for_user(user.id)}

    @router.get("/api/my-queries/{verdict_id}")
    async def my_query_detail(verdict_id: int, token: str = Query("")):
        user = await require_user(token)
        await deps.repos.correction.close_expired()
        detail = await deps.repos.query.get_my_detail(user.id, verdict_id)
        if detail is None: raise HTTPException(404, "query not found")
        return detail

    @router.get("/api/alerts")
    async def alerts(token: str = Query(""), relation_id: int | None = Query(None)):
        user = await require_user(token)
        return {"alerts": await deps.repos.alert.list_for_user(user.id, relation_id)}

    @router.get("/api/alerts/{alert_id}")
    async def alert_detail(alert_id: int, token: str = Query("")):
        user = await require_user(token)
        await deps.repos.correction.close_expired()
        detail, denial = await deps.repos.alert.detail_for_user(user.id, alert_id)
        if denial == "relation_ended": raise HTTPException(410, "relation_ended")
        if detail is None: raise HTTPException(404, "alert not found")
        case = await deps.repos.correction.get_case_for_verdict(detail["verdict_id"])
        if case:
            detail["correction"] = case
            relation = await deps.repos.relation.get(detail["relation_id"])
            detail["may_vote"] = bool(relation and relation["ended_at"] is None and
                                      await deps.repos.correction.detail_for_relation(case["case_id"], relation["id"], user.id))
        else: detail["may_vote"] = False
        await deps.repos.alert.mark_read(alert_id)
        return detail

    @router.get("/api/corrections")
    async def correction_queue(token: str = Query("")):
        user = await require_user(token)
        rows = await deps.repos.correction.list_pending_for_user(user.id)
        return {"corrections": rows}

    @router.post("/api/corrections")
    async def submit_correction(body: CorrectionIn):
        user = await require_user(body.token)
        try:
            return await corrections.submit(body.verdict_id, user.id, body.label, body.note)
        except (HomeshieldError, ValueError) as exc:
            text = CORRECTION_ERROR_TEXT.get(str(exc), "这次反馈没能完成，请稍后重试。")
            raise HTTPException(400, text) from exc

    @router.get("/api/stream")
    async def stream(token: str = Query("")):
        user = await require_user(token)
        queue = deps.broker.subscribe(user.id)
        async def generate():
            sent: set[int] = set()
            try:
                yield "event: ready\ndata: {}\n\n"
                while True:
                    try: item = await asyncio.wait_for(queue.get(), timeout=20)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"; continue
                    if item.get("kind") == "push_status":
                        yield "event: push_status\ndata: " + json.dumps(item, ensure_ascii=False) + "\n\n"
                        continue
                    verdict_id = int(item["verdict_id"])
                    if verdict_id in sent: continue
                    if await deps.repos.alert.event_context(int(item["alert_id"])) is None: continue
                    sent.add(verdict_id)
                    yield "event: alert\ndata: " + json.dumps(item, ensure_ascii=False) + "\n\n"
            finally: deps.broker.unsubscribe(user.id, queue)
        return StreamingResponse(generate(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return router
