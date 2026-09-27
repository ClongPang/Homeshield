"""按个人身份鉴权的家人与防护群 API。"""
import json

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from homeshield.api.schemas import (
    CorrectionIn, DecideIn, GroupIn, MemberIn, MemberPatchIn, QueryIn, TokenIn, TrustIn, MuteIn,
)
from homeshield.core.deps import Deps
from homeshield.core.errors import DuplicateMessage, HomeshieldError, ValidationError
from homeshield.core.feedback import CorrectionService, weekly_report
from homeshield.core.models import CorrectionLabel, Member, User
from homeshield.core.repo import WRITE_LOCK
from homeshield.core.verification import VerificationService


def build_group_router(
    deps: Deps, verification: VerificationService, corrections: CorrectionService
) -> APIRouter:
    router = APIRouter()

    def user_by_token(token: str) -> User:
        user = deps.repos.users.get_by_token(token or "")
        if user is None:
            raise HTTPException(401, "invalid token")
        return user

    def group_for_user(user: User, group_id: int | None) -> dict:
        groups = deps.repos.group.list_for_user(user.id)
        if group_id is None:
            if len(groups) != 1:
                if not groups:
                    raise HTTPException(404, "no active group")
                raise HTTPException(400, "group_id is required") # 若用户在多个群组中，需要提供群号标识
            return groups[0]
        group = next((g for g in groups if g["id"] == group_id), None)  # 多个群组，需要进行群号标识的过滤，找到对应的群组
        if group is None:
            raise HTTPException(404, "group not found")
        return group

    def membership(user: User, group_id: int) -> Member:
        row = next((m for m in deps.repos.member.list_for_user(user.id) if m.group_id == group_id), None)
        if row is None:
            raise HTTPException(404, "group not found")
        return row

    def require_trusted(user: User, group_id: int) -> Member:
        member = membership(user, group_id)
        if not member.trusted:
            raise HTTPException(403, "only trusted member can manage group")
        return member

    @router.get("/api/join/{code}")
    def api_join_info(code: str):
        row = deps.repos.bind_code.peek(code)
        if row is None:
            raise HTTPException(404, "invitation unavailable")
        target = deps.repos.member.get(int(row["member_id"]))
        group = deps.repos.group.get(target.group_id) if target else None
        if target is None or group is None:
            raise HTTPException(404, "invitation unavailable")
        return {"group_name": group["name"], "member_name": target.name,
                "expires_at": row["expires_at"], "command": f"绑定 {row['code']}"}

    def translate_validation(fn, *args):
        try:
            return fn(*args)
        except (HomeshieldError, ValueError) as e:
            raise HTTPException(400, str(e)) from e

    @router.get("/api/groups")
    def api_groups(token: str = Query("")):
        user = user_by_token(token)
        return {"groups": [
            {"id": g["id"], "name": g["name"], "trusted": bool(g["trusted"]),
             "mute": bool(g["mute"]), "member_count": g["member_count"],
             "is_creator": deps.repos.group.get(g["id"])["created_by_user_id"] == user.id}
            for g in deps.repos.group.list_for_user(user.id)
        ]}

    @router.post("/api/groups")
    def api_create_group(body: GroupIn):
        user = user_by_token(body.token)
        try:
            admin = deps.binding.create_group(user.id, body.name)
        except HomeshieldError as e:
            raise HTTPException(400, str(e)) from e
        group = deps.repos.group.get(admin.group_id)
        return {"id": admin.group_id, "name": group["name"], "trusted": True}

    @router.patch("/api/groups/{gid}")
    def api_rename_group(gid: int, body: GroupIn):
        user = user_by_token(body.token)
        with WRITE_LOCK:
            require_trusted(user, gid)
            translate_validation(deps.groups.rename, gid, body.name)
        return {"id": gid, "name": body.name.strip()}

    @router.delete("/api/groups/{gid}")
    def api_disband_group(gid: int, body: TokenIn):
        user = user_by_token(body.token)
        translate_validation(deps.groups.disband, user.id, gid)
        return {"disbanded": True, "group_id": gid}

    @router.post("/api/query")
    async def api_query(body: QueryIn):
        user = user_by_token(body.token)
        memberships = deps.repos.member.list_for_user(user.id)
        if not memberships:
            raise HTTPException(400, "no active group; join or create a group before querying")
        try:
            outcome = await verification.verify(
                user=user, memberships=memberships, content=body.content,
                content_type=body.content_type, channel="web", msg_id=body.msg_id,
            )
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except DuplicateMessage as e:
            raise HTTPException(409, f"duplicate msg_id={e.msg_id}") from e
        if outcome.duplicate:
            raise HTTPException(409, "duplicate msg_id")
        result = outcome.result
        return {
            "query_id": result.query_id, "verdict_id": result.verdict_id,
            "level": result.verdict.level.value if result.verdict else None,
            "cited_ids": result.verdict.cited_ids if result.verdict else [],
            "reply": result.reply, "latency_ms": result.latency_ms,
        }

    @router.get("/api/stream")
    async def api_stream(token: str = Query("")):
        user = user_by_token(token)
        q = deps.broker.subscribe(user.id)

        async def gen():
            try:
                while True:
                    payload = await q.get()
                    current = deps.repos.alert.active_group_ids_for_user(user.id)
                    names_by_id = dict(zip(payload["group_ids"], payload["group_names"]))
                    allowed = [gid for gid in payload["group_ids"] if gid in current]
                    if not allowed:
                        continue
                    payload["group_ids"] = allowed
                    payload["group_names"] = [names_by_id.get(gid, "") for gid in allowed]
                    yield f"event: alert\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            finally:
                deps.broker.unsubscribe(user.id, q)

        return StreamingResponse(gen(), media_type="text/event-stream")

    @router.get("/api/alerts")
    def api_alerts(token: str = Query(""), group_id: int | None = Query(None)):
        user = user_by_token(token)
        with WRITE_LOCK:
            group = group_for_user(user, group_id)
            rows = deps.repos.alert.list_for_user_group(user.id, group["id"])
        return {"group_id": group["id"], "alerts": [
            {"verdict_id": r["verdict_id"], "level": r["level"],
             "summary": r["content"][:50], "delivered_at": r["delivered_at"],
             "group_name": r["group_name_at_alert"]}
            for r in rows
        ]}

    @router.get("/api/alerts/{verdict_id}")
    def api_alert_detail(verdict_id: int, token: str = Query("")):
        user = user_by_token(token)
        detail, denial = deps.repos.alert.access_detail(user.id, verdict_id)
        if denial:
            raise HTTPException(410, {"reason": denial})
        if detail is None:
            raise HTTPException(404, "alert not found")
        mine = deps.repos.correction.get_by_verdict_and_user(verdict_id, user.id)
        return {
            "verdict_id": verdict_id, "level": detail["level"], "reply": detail["reply"],
            "summary": detail["content"][:80], "created_at": detail["created_at"],
            "group_names": detail["group_names"],
            "my_feedback": ({"label": mine.label.value, "status": mine.status.value} if mine else None),
        }

    @router.get("/api/corrections")
    def api_corrections_list(token: str = Query(""), group_id: int | None = Query(None)):
        user = user_by_token(token)
        with WRITE_LOCK:
            group = group_for_user(user, group_id)
            viewer = membership(user, group["id"])
            rows = deps.repos.correction.list_pending_with_context(group["id"]) if viewer.trusted else []
        return {"group_id": group["id"], "viewer_trusted": viewer.trusted, "pending": rows}

    @router.post("/api/corrections")
    def api_corrections(body: CorrectionIn):
        user = user_by_token(body.token)
        try:
            cid, status = corrections.submit(
                body.verdict_id, user.id, CorrectionLabel(body.label), body.note
            )
        except (HomeshieldError, ValueError) as e:
            raise HTTPException(400, str(e)) from e
        return {"correction_id": cid, "status": status.value}

    @router.post("/api/corrections/{cid}/confirm")
    def api_confirm(cid: int, body: DecideIn):
        user = user_by_token(body.token)
        with WRITE_LOCK:
            record, groups = deps.repos.correction.get_with_groups(cid)
            if record is None:
                raise HTTPException(404, "correction not found")
            active = {m.group_id: m for m in deps.repos.member.list_for_user(user.id)}
            actor = next((active[g["group_id"]] for g in groups
                          if g["group_id"] in active and active[g["group_id"]].trusted), None)
            if actor is None:
                raise HTTPException(400, "correction not in a related group")
            try:
                status = corrections.decide(cid, actor.id, body.decision)
            except HomeshieldError as e:
                raise HTTPException(400, str(e)) from e
        return {"status": status.value}

    @router.get("/api/weekly")
    def api_weekly(token: str = Query(""), group_id: int | None = Query(None)):
        user = user_by_token(token)
        with WRITE_LOCK:
            group = group_for_user(user, group_id)
            return weekly_report(deps.repos, group["id"])

    @router.get("/api/groups/{gid}/members")
    def api_members(gid: int, token: str = Query("")):
        user = user_by_token(token)
        with WRITE_LOCK:
            viewer = membership(user, gid)
            return {
                "group_id": gid, "viewer_id": viewer.id, "viewer_trusted": viewer.trusted,
                "members": [
                    {"id": m.id, "name": m.name, "trusted": m.trusted,
                     "bound": m.user_id is not None, "is_me": m.user_id == user.id,
                     "mute": m.mute}
                    for m in deps.repos.member.list_members(gid)
                ],
            }

    @router.post("/api/groups/{gid}/members")
    def api_add_member(gid: int, body: MemberIn):
        user = user_by_token(body.token)
        name = body.name.strip()
        if not name:
            raise HTTPException(400, "name is empty")
        with WRITE_LOCK:
            actor = require_trusted(user, gid)
            try:
                target_id = deps.groups.create_member_slot(gid, name)
                target = deps.repos.member.get(target_id)
                code = deps.binding.issue_code(target, created_by=actor.id)
            except HomeshieldError as e:
                raise HTTPException(400, str(e)) from e
        return {
            "member_id": target.id, "name": target.name, "trusted": target.trusted,
            "bind_code": code["code"], "bind_expires_at": code["expires_at"],
            "entry_url": (f"{deps.settings.public_base_url.rstrip('/')}/join/{code['code']}"
                          if deps.settings.public_base_url else None),
        }

    @router.post("/api/groups/{gid}/members/{member_id}/bind-code")
    def api_reissue_bind_code(gid: int, member_id: int, body: TokenIn):
        user = user_by_token(body.token)
        with WRITE_LOCK:
            actor = require_trusted(user, gid)
            target = deps.repos.member.get(member_id)
            if target is None or target.group_id != gid or target.ended_at is not None:
                raise HTTPException(404, "member not found")
            try:
                code = deps.binding.issue_code(target, created_by=actor.id)
            except HomeshieldError as e:
                raise HTTPException(400, str(e)) from e
        return {"bind_code": code["code"], "bind_expires_at": code["expires_at"]}

    @router.post("/api/groups/{gid}/members/{member_id}/trust")
    def api_set_trust(gid: int, member_id: int, body: TrustIn):
        user = user_by_token(body.token)
        with WRITE_LOCK:
            actor = require_trusted(user, gid)
            translate_validation(deps.groups.set_trust, gid, member_id, body.trusted, actor.id)
        return {"member_id": member_id, "trusted": body.trusted}

    @router.delete("/api/groups/{gid}/members/{member_id}")
    def api_remove_member(gid: int, member_id: int, body: TokenIn):
        user = user_by_token(body.token)
        with WRITE_LOCK:
            actor = membership(user, gid)
            if actor.id == member_id:
                result = translate_validation(deps.groups.leave, user.id, gid)
            else:
                require_trusted(user, gid)
                result = translate_validation(deps.groups.remove, gid, member_id)
        return {"status": result}

    @router.patch("/api/groups/{gid}/members/{member_id}")
    def api_rename_member(gid: int, member_id: int, body: MemberPatchIn):
        user = user_by_token(body.token)
        name = body.name.strip()
        if not name:
            raise HTTPException(400, "name is empty")
        with WRITE_LOCK:
            actor = membership(user, gid)
            target = deps.repos.member.get(member_id)
            if target is None or target.group_id != gid or target.ended_at is not None:
                raise HTTPException(404, "member not found")
            if target.user_id != user.id and not actor.trusted:
                raise HTTPException(403, "only member or trusted member can rename")
            deps.repos.member.rename(member_id, name)
        return {"member_id": member_id, "name": name}

    @router.post("/api/groups/{gid}/mute")
    def api_mute(gid: int, body: MuteIn):
        user = user_by_token(body.token)
        with WRITE_LOCK:
            translate_validation(deps.groups.set_mute, gid, user.id, body.mute)
        return {"group_id": gid, "mute": body.mute}

    return router
