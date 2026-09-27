"""微信身份开通、群创建与邀请码绑定。"""
from dataclasses import dataclass
import re

from homeshield.core.errors import HomeshieldError
from homeshield.core.models import Member
from homeshield.core.repo import Repos, WRITE_LOCK

OPEN_FAMILY_NAME = "我的防护群"
CREATOR_NAME = "群主"
_BIND_RE = re.compile(r"^(?:绑定|綁定)\s*[::]?\s*([0-9A-Za-z]{4,16})$")


def parse_bind_command(text: str) -> str | None:
    match = _BIND_RE.match(text.strip())
    return match.group(1).upper() if match else None


def parse_open_command(text: str) -> str | None:
    match = re.match(r"^(?:开通|開通|开通家庭)(?:\s+(.+))?$", text.strip())
    return match.group(1).strip() if match and match.group(1) else ("" if match else None)


def parse_group_command(text: str, command: str) -> str | None:
    match = re.match(rf"^{re.escape(command)}\s+(.+)$", text.strip())
    return match.group(1).strip() if match else None


@dataclass
class BindingError(HomeshieldError):
    reason: str


class BindingService:
    def __init__(self, repos: Repos, max_families: int, max_members: int,
                 code_ttl_days: int, max_groups: int | None = None):
        self.repos = repos
        self.max_families = max_families
        self.max_members = max_members
        self.max_groups = max_groups or max_families
        self.code_ttl_days = code_ttl_days

    def open_family(self, openid: str, name: str | None = None) -> Member:
        user = self.repos.users.get_or_create(openid)
        with WRITE_LOCK:
            if self.repos.family.list_for_user(user.id):
                raise BindingError("already_has_groups")
            return self.create_group(user.id, name or OPEN_FAMILY_NAME, openid=openid)

    def create_group(self, user_id: int, name: str, openid: str | None = None) -> Member:
        name = name.strip()
        if not name:
            raise BindingError("invalid_name")
        with WRITE_LOCK:
            if self.repos.family.count() >= self.max_families:
                raise BindingError("limit")
            if len(self.repos.family.list_for_user(user_id)) >= self.max_groups:
                raise BindingError("group_limit")
            fid = self.repos.family.create_with_creator(name, CREATOR_NAME, user_id)
            member = next(m for m in self.repos.member.list_for_user(user_id) if m.family_id == fid)
            return member

    def issue_code(self, target: Member, created_by: int) -> dict:
        with WRITE_LOCK:
            current = self.repos.member.get(target.id)
            if current is None or current.user_id is not None or current.ended_at is not None:
                raise BindingError("member_bound")
            self.repos.bind_code.invalidate_for_member(target.id)
            return self.repos.bind_code.create(target.id, created_by, self.code_ttl_days)

    def bind(self, openid: str, code: str) -> Member:
        with WRITE_LOCK:
            row = self.repos.bind_code.peek(code)
            if row is None:
                raise BindingError("invalid")
            target = self.repos.member.get(int(row["member_id"]))
            if target is None or target.user_id is not None or target.ended_at is not None:
                raise BindingError("member_bound")
            user = self.repos.users.get_or_create(openid)
            if any(m.family_id == target.family_id for m in self.repos.member.list_for_user(user.id)):
                raise BindingError("already_in_group")
            try:
                claimed = self.repos.bind_code.claim_and_bind(code,target.id,user.id)
            except HomeshieldError as exc:
                raise BindingError("retry") from exc
            if not claimed:
                raise BindingError("invalid")
            return self.repos.member.get(target.id)

    def ensure_member_capacity(self, family_id: int) -> None:
        if len(self.repos.member.list_members(family_id)) >= self.max_members:
            raise BindingError("limit")
