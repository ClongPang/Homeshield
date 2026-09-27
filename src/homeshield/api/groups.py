"""按个人身份鉴权的家人与防护群 API。"""
import json

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from homeshield.api.schemas import (
    CorrectionIn, DecideIn, GroupIn, MemberIn, MemberPatchIn, QueryIn, TokenIn, TrustIn, MuteIn,
)
from homeshield.core.deps import Deps
from homeshield.core.errors import DuplicateMessage, HomeshieldError, ValidationError
from homeshield.core.feedback import CorrectionService, build_group_weekly_report
from homeshield.core.models import CorrectionLabel, Member, User
from homeshield.core.repo import WRITE_LOCK
from homeshield.core.verification import VerificationService


def build_group_router(
    deps: Deps, verification: VerificationService, corrections: CorrectionService
) -> APIRouter:
    router = APIRouter()

    def require_user_by_token(token: str) -> User:
        user = deps.repos.users.get_by_token(token or "")
        if user is None:
            raise HTTPException(401, "invalid token")
        return user

    def resolve_user_group(user: User, group_id: int | None) -> dict:
        """
        在用户加入的群里找到目标群，返回群信息。
        没传群 ID 时，只有用户只加入一个群才会自动选中；加入多个群就必须指定。它用于确定“操作哪个群”
        """
        groups = deps.repos.group.list_active_groups_for_user(user.id)
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

    def require_active_membership(user: User, group_id: int) -> Member:
        """
        找到这个用户在目标群里的那条成员记录，返回 Member
        """
        row = next((m for m in deps.repos.member.list_for_user(user.id) if m.group_id == group_id), None)
        if row is None:
            raise HTTPException(404, "group not found")
        return row

    def require_trusted(user: User, group_id: int) -> Member:
        member = require_active_membership(user, group_id)
        if not member.trusted:
            raise HTTPException(403, "only trusted member can manage group")
        return member

    @router.get(

        "/api/join/{code}",

        operation_id="api_join_info_api_join__code__get",

        summary="Api Join Info",

    )
    def api_get_join_invite_details(code: str):
        row = deps.repos.bind_code.get_valid_bind_code(code)
        if row is None:
            raise HTTPException(404, "invitation unavailable")
        target = deps.repos.member.get(int(row["member_id"]))
        group = deps.repos.group.get(target.group_id) if target else None
        if target is None or group is None:
            raise HTTPException(404, "invitation unavailable")
        return {"group_name": group["name"], "member_name": target.name,
                "expires_at": row["expires_at"], "command": f"绑定 {row['code']}"}

    def translate_domain_error_to_http_400(fn, *args):
        try:
            return fn(*args)
        except (HomeshieldError, ValueError) as e:
            raise HTTPException(400, str(e)) from e

    @router.get(

        "/api/groups",

        operation_id="api_groups_api_groups_get",

        summary="Api Groups",

    )
    def api_list_user_groups(token: str = Query("")):
        user = require_user_by_token(token)
        return {"groups": [
            {"id": g["id"], "name": g["name"], "trusted": bool(g["trusted"]),
             "mute": bool(g["mute"]), "member_count": g["member_count"],
             "is_creator": deps.repos.group.get(g["id"])["created_by_user_id"] == user.id}
            for g in deps.repos.group.list_active_groups_for_user(user.id)
        ]}

    @router.post("/api/groups")
    def api_create_group(body: GroupIn):
        user = require_user_by_token(body.token)
        try:
            admin = deps.binding.create_group(user.id, body.name)
        except HomeshieldError as e:
            raise HTTPException(400, str(e)) from e
        group = deps.repos.group.get(admin.group_id)
        return {"id": admin.group_id, "name": group["name"], "trusted": True}

    @router.patch("/api/groups/{gid}")
    def api_rename_group(gid: int, body: GroupIn):
        user = require_user_by_token(body.token)
        with WRITE_LOCK:
            require_trusted(user, gid)
            translate_domain_error_to_http_400(deps.groups.rename_group, gid, body.name)
        return {"id": gid, "name": body.name.strip()}

    @router.delete("/api/groups/{gid}")
    def api_disband_group(gid: int, body: TokenIn):
        user = require_user_by_token(body.token)
        translate_domain_error_to_http_400(deps.groups.disband_group, user.id, gid)
        return {"disbanded": True, "group_id": gid}

    @router.post(

        "/api/query",

        operation_id="api_query_api_query_post",

        summary="Api Query",

    )
    async def api_verify_message(body: QueryIn):
        user = require_user_by_token(body.token)
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

    @router.get(

        "/api/stream",

        operation_id="api_stream_api_stream_get",

        summary="Api Stream",

    )
    async def api_stream_alert_events(token: str = Query("")):
        user = require_user_by_token(token)
        q = deps.broker.subscribe(user.id)

        async def stream_authorized_alert_events():
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

        return StreamingResponse(stream_authorized_alert_events(), media_type="text/event-stream")

    @router.get(

        "/api/alerts",

        operation_id="api_alerts_api_alerts_get",

        summary="Api Alerts",

    )
    def api_list_group_alerts(token: str = Query(""), group_id: int | None = Query(None)):
        user = require_user_by_token(token)
        with WRITE_LOCK:
            group = resolve_user_group(user, group_id)
            rows = deps.repos.alert.list_alerts_for_user_in_group(user.id, group["id"])
        return {"group_id": group["id"], "alerts": [
            {"verdict_id": r["verdict_id"], "level": r["level"],
             "summary": r["content"][:50], "delivered_at": r["delivered_at"],
             "group_name": r["group_name_at_alert"]}
            for r in rows
        ]}

    @router.get("/api/alerts/{verdict_id}")
    def api_alert_detail(verdict_id: int, token: str = Query("")):
        user = require_user_by_token(token)
        detail, denial = deps.repos.alert.get_alert_detail_for_user(user.id, verdict_id)
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

    @router.get(

        "/api/corrections",

        operation_id="api_corrections_list_api_corrections_get",

        summary="Api Corrections List",

    )
    def api_list_pending_group_corrections(token: str = Query(""), group_id: int | None = Query(None)):
        user = require_user_by_token(token)
        with WRITE_LOCK:
            group = resolve_user_group(user, group_id)
            viewer = require_active_membership(user, group["id"])
            rows = deps.repos.correction.list_pending_corrections_with_context(group["id"]) if viewer.trusted else []
        return {"group_id": group["id"], "viewer_trusted": viewer.trusted, "pending": rows}

    @router.post(

        "/api/corrections",

        operation_id="api_corrections_api_corrections_post",

        summary="Api Corrections",

    )
    def api_submit_correction(body: CorrectionIn):
        user = require_user_by_token(body.token)
        try:
            cid, status = corrections.submit_correction(
                body.verdict_id, user.id, CorrectionLabel(body.label), body.note
            )
        except (HomeshieldError, ValueError) as e:
            raise HTTPException(400, str(e)) from e
        return {"correction_id": cid, "status": status.value}

    @router.post(

        "/api/corrections/{cid}/confirm",

        operation_id="api_confirm_api_corrections__cid__confirm_post",

        summary="Api Confirm",

    )
    def api_decide_correction(cid: int, body: DecideIn):
        user = require_user_by_token(body.token)
        with WRITE_LOCK:
            record, groups = deps.repos.correction.get_correction_with_related_groups(cid)
            if record is None:
                raise HTTPException(404, "correction not found")
            active = {m.group_id: m for m in deps.repos.member.list_for_user(user.id)}
            actor = next((active[g["group_id"]] for g in groups
                          if g["group_id"] in active and active[g["group_id"]].trusted), None)
            if actor is None:
                raise HTTPException(400, "correction not in a related group")
            try:
                status = corrections.decide_correction(cid, actor.id, body.decision)
            except HomeshieldError as e:
                raise HTTPException(400, str(e)) from e
        return {"status": status.value}

    @router.get(

        "/api/weekly",

        operation_id="api_weekly_api_weekly_get",

        summary="Api Weekly",

    )
    def api_get_group_weekly_report(token: str = Query(""), group_id: int | None = Query(None)):
        user = require_user_by_token(token)
        with WRITE_LOCK:
            group = resolve_user_group(user, group_id)
            return build_group_weekly_report(deps.repos, group["id"])

    @router.get(

        "/api/groups/{gid}/members",

        operation_id="api_members_api_groups__gid__members_get",

        summary="Api Members",

    )
    def api_list_group_members(gid: int, token: str = Query("")):
        user = require_user_by_token(token)
        with WRITE_LOCK:
            viewer = require_active_membership(user, gid)
            return {
                "group_id": gid, "viewer_id": viewer.id, "viewer_trusted": viewer.trusted,
                "members": [
                    {"id": m.id, "name": m.name, "trusted": m.trusted,
                     "bound": m.user_id is not None, "is_me": m.user_id == user.id,
                     "mute": m.mute}
                    for m in deps.repos.member.list_members(gid)
                ],
            }

    @router.post(

        "/api/groups/{gid}/members",

        operation_id="api_add_member_api_groups__gid__members_post",

        summary="Api Add Member",

    )
    def api_create_member_slot_and_issue_bind_code(gid: int, body: MemberIn):
        user = require_user_by_token(body.token)
        name = body.name.strip()
        if not name:
            raise HTTPException(400, "name is empty")
        with WRITE_LOCK:
            actor = require_trusted(user, gid)
            try:
                target_id = deps.groups.create_member_slot(gid, name)
                target = deps.repos.member.get(target_id)
                code = deps.binding.issue_bind_code(target, created_by=actor.id)
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
        user = require_user_by_token(body.token)
        with WRITE_LOCK:
            actor = require_trusted(user, gid)
            target = deps.repos.member.get(member_id)
            if target is None or target.group_id != gid or target.ended_at is not None:
                raise HTTPException(404, "member not found")
            try:
                code = deps.binding.issue_bind_code(target, created_by=actor.id)
            except HomeshieldError as e:
                raise HTTPException(400, str(e)) from e
        return {"bind_code": code["code"], "bind_expires_at": code["expires_at"]}

    @router.post("/api/groups/{gid}/members/{member_id}/trust")
    def api_set_trust(gid: int, member_id: int, body: TrustIn):
        user = require_user_by_token(body.token)
        with WRITE_LOCK:
            actor = require_trusted(user, gid)
            translate_domain_error_to_http_400(deps.groups.set_member_trust, gid, member_id, body.trusted, actor.id)
        return {"member_id": member_id, "trusted": body.trusted}

    @router.delete(

        "/api/groups/{gid}/members/{member_id}",

        operation_id="api_remove_member_api_groups__gid__members__member_id__delete",

        summary="Api Remove Member",

    )
    def api_end_group_membership(gid: int, member_id: int, body: TokenIn):
        user = require_user_by_token(body.token)
        with WRITE_LOCK:
            actor = require_active_membership(user, gid)
            if actor.id == member_id:
                result = translate_domain_error_to_http_400(deps.groups.leave_group, user.id, gid)
            else:
                require_trusted(user, gid)
                result = translate_domain_error_to_http_400(deps.groups.remove_member, gid, member_id)
        return {"status": result}

    @router.patch("/api/groups/{gid}/members/{member_id}")
    def api_rename_member(gid: int, member_id: int, body: MemberPatchIn):
        user = require_user_by_token(body.token)
        name = body.name.strip()
        if not name:
            raise HTTPException(400, "name is empty")
        with WRITE_LOCK:
            actor = require_active_membership(user, gid)
            target = deps.repos.member.get(member_id)
            if target is None or target.group_id != gid or target.ended_at is not None:
                raise HTTPException(404, "member not found")
            if target.user_id != user.id and not actor.trusted:
                raise HTTPException(403, "only member or trusted member can rename")
            deps.repos.member.rename_member(member_id, name)
        return {"member_id": member_id, "name": name}

    @router.post(

        "/api/groups/{gid}/mute",

        operation_id="api_mute_api_groups__gid__mute_post",

        summary="Api Mute",

    )
    def api_set_group_mute_for_user(gid: int, body: MuteIn):
        user = require_user_by_token(body.token)
        with WRITE_LOCK:
            translate_domain_error_to_http_400(deps.groups.set_member_mute, gid, user.id, body.mute)
        return {"group_id": gid, "mute": body.mute}

    return router
