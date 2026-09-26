"""家人 API 路由:token 鉴权 + 家庭数据读写。

凭证 = 不可枚举 token(链接即凭证);无/错 token 一律 401,接口不回传 token。
所有数据按 member.family_id 隔离(告警跨家庭 404,纠正跨家庭 400);成员管理仅 adult。
"""
import json

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from homeshield.api.schemas import CorrectionIn, DecideIn, MemberIn, QueryIn, TokenIn
from homeshield.core.binding import BindingError
from homeshield.core.deps import Deps
from homeshield.core.errors import DuplicateMessage, HomeshieldError
from homeshield.core.feedback import CorrectionService, weekly_report
from homeshield.core.models import CorrectionLabel, Member, Role
from homeshield.core.verification import VerificationService


def build_family_router(
    deps: Deps, verification: VerificationService, corrections: CorrectionService
) -> APIRouter:
    router = APIRouter()

    def member_by_token(token: str) -> Member:
        member = deps.repos.member.get_by_token(token or "")
        if member is None:
            raise HTTPException(401, "invalid token")
        return member

    @router.post("/api/query")
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

    @router.get("/api/stream")
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

    @router.get("/api/alerts")
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

    @router.get("/api/alerts/{verdict_id}")
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

    @router.get("/api/corrections")
    def api_corrections_list(token: str = Query("")):
        member = member_by_token(token)
        rows = deps.repos.correction.list_pending_with_context(member.family_id)
        return {"pending": rows}

    @router.post("/api/corrections")
    def api_corrections(body: CorrectionIn):
        member = member_by_token(body.token)
        try:
            cid, status = corrections.submit(
                body.verdict_id, member.id, CorrectionLabel(body.label), body.note
            )
        except (HomeshieldError, ValueError) as e:
            raise HTTPException(400, str(e))
        return {"correction_id": cid, "status": status.value}

    @router.post("/api/corrections/{cid}/confirm")
    def api_confirm(cid: int, body: DecideIn):
        member = member_by_token(body.token)
        try:
            status = corrections.decide(cid, member.id, body.decision)
        except HomeshieldError as e:
            raise HTTPException(400, str(e))
        return {"status": status.value}

    @router.get("/api/members")
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

    @router.post("/api/members")
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
            "entry_url": target.entry_url(deps.settings.public_base_url) or None,
        }

    @router.post("/api/members/{member_id}/bind-code")
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

    @router.get("/api/weekly")
    def api_weekly(token: str = Query("")):
        member = member_by_token(token)
        return weekly_report(deps.repos, member.family_id)

    return router
