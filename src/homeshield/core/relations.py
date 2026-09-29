"""Directed protection relations and invitation rules."""
from dataclasses import dataclass
import re

from homeshield.core.errors import HomeshieldError, ValidationError
from homeshield.core.repo import Repos

INVITE_RE = re.compile(r"^(?:绑定|綁定)\s*[::]?\s*([0-9A-Za-z]{4,16})$")
INVITE_CREATE_RE = re.compile(r"^邀请(?:\s+(.+))?$")
END_RE = re.compile(r"^解除\s+(.+)$")
OLD_GROUP_COMMAND_RE = re.compile(r"^(?:开通|開通|我的群|我的防护群|退出|退群|解散)(?:\s+.*)?$")


def parse_bind_command(text: str) -> str | None:
    match = INVITE_RE.match(text.strip())
    return match.group(1).upper() if match else None


def parse_invite_command(text: str) -> str | None:
    match = INVITE_CREATE_RE.match(text.strip())
    return (match.group(1) or "家人").strip() if match else None


def parse_end_command(text: str) -> str | None:
    match = END_RE.match(text.strip())
    return match.group(1).strip() if match else None


def is_old_group_command(text: str) -> bool:
    return bool(OLD_GROUP_COMMAND_RE.match(text.strip()))


@dataclass
class RelationError(HomeshieldError):
    reason: str


class RelationService:
    def __init__(self, repos: Repos, max_relations: int = 10, invite_ttl_days: int = 7):
        self.repos = repos
        self.max_relations = max_relations
        self.invite_ttl_days = invite_ttl_days

    async def issue_invite(self, user_id: int, name: str = "家人") -> dict:
        name = name.strip() or "家人"
        try:
            return await self.repos.invite.create(user_id, name, self.invite_ttl_days, self.max_relations)
        except ValidationError as exc:
            if str(exc) == "relation limit reached": raise RelationError("limit") from exc
            raise

    async def join(self, openid: str, code: str) -> tuple[int, int, str]:
        user = await self.repos.users.get_or_create(openid)
        reason, relation_id = await self.repos.invite.claim(code, user.id, self.max_relations)
        if reason == "created": return user.id, int(relation_id), reason
        raise RelationError(reason)

    async def list_for_user(self, user_id: int) -> dict:
        return await self.repos.relation.list_for_user(user_id)

    async def end(self, user_id: int, relation_id: int) -> str:
        return await self.repos.relation.end(relation_id, user_id)

    async def end_by_selector(self, user_id: int, selector: str) -> tuple[str, list[dict]]:
        data = await self.repos.relation.list_for_user(user_id)
        all_rows = []
        for row in data["guardings"]:
            all_rows.append({**row, "role": "protector", "display_name": row["name"]})
        for row in data["guardians"]:
            all_rows.append({**row, "role": "protected", "display_name": row["name"]})
        if selector.startswith("#") and selector[1:].isdigit():
            matches = [r for r in all_rows if r["id"] == int(selector[1:])]
        else:
            matches = [r for r in all_rows if r["display_name"] == selector]
        if len(matches) != 1: return ("ambiguous" if matches else "not_found", matches)
        return await self.end(user_id, matches[0]["id"]), matches

    @staticmethod
    def join_preview(invite: dict) -> dict:
        return {"direction": "邀请者将联防你", "name": invite["name"],
                "sharing": "接受后,你的查询提醒将同步给发码者;你主动纠正其他判定时,原查询会供其投票查看"}
