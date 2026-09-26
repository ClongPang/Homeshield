"""绑定域:公众号多租户的入群流程。

自助开通:陌生 openid 发「开通」→ 建群 + 创建者(trusted)成员位,openid 直落;
邀请绑定:信任成员在控制台为家人创建成员位并生成绑定码,家人回复「绑定 <码>」
把 openid 落到对应成员位(默认不受信任,纠正需确认)。成员位的网页 token 链接通道不受影响。

滥用边界由配置封顶:MAX_FAMILIES(全局群数)× MAX_MEMBERS(每群成员数);
一人一群由 member.openid UNIQUE 天然保证。
"""
from dataclasses import dataclass
import re

from homeshield.core.errors import HomeshieldError, ValidationError
from homeshield.core.models import Member
from homeshield.core.repo import Repos

OPEN_FAMILY_NAME = "我的防护群"
CREATOR_NAME = "群主"

# 「绑定 <码>」命令文法:大小写不敏感,容忍空格/冒号;认领时统一归一为大写
_BIND_RE = re.compile(r"^(?:绑定|綁定)\s*[::]?\s*([0-9A-Za-z]{4,16})$")


def parse_bind_command(text: str) -> str | None:
    """「绑定 <码>」→ 码;其余文本返回 None。文法与 BindingService 同屋。"""
    m = _BIND_RE.match(text.strip())
    return m.group(1).upper() if m else None


@dataclass
class BindingError(HomeshieldError):
    """绑定失败;reason 对应面向用户的文案键(messages.*)。

    already_bound: openid 已属某个家庭;invalid: 码无效/过期/已用;
    member_bound: 成员位已有 openid;limit: 触达家庭数上限。
    """

    reason: str


class BindingService:
    def __init__(self, repos: Repos, max_families: int, max_members: int, code_ttl_days: int):
        self.repos = repos
        self.max_families = max_families
        self.max_members = max_members
        self.code_ttl_days = code_ttl_days

    def open_family(self, openid: str) -> Member:
        """自助开通:一个 openid 只能开一次;群总数封顶护栏;建群+创建者单事务。"""
        if self.repos.member.get_by_openid(openid) is not None:
            raise BindingError("already_bound")
        if self.repos.family.count() >= self.max_families:
            raise BindingError("limit")
        fid = self.repos.family.create_with_creator(OPEN_FAMILY_NAME, CREATOR_NAME, openid)
        return self.repos.member.get_by_openid(openid)

    def issue_code(self, target: Member, created_by: int) -> dict:
        """为未绑定成员位生成绑定码;重发即作废旧码。"""
        if target.openid:
            raise BindingError("member_bound")
        self.repos.bind_code.invalidate_for_member(target.id)
        return self.repos.bind_code.create(target.id, created_by, self.code_ttl_days)

    def bind(self, openid: str, code: str) -> Member:
        """领取绑定码:openid 落到成员位。一次性/时限靠原子认领保证。"""
        if self.repos.member.get_by_openid(openid) is not None:
            raise BindingError("already_bound")
        row = self.repos.bind_code.peek(code)
        if row is None:
            raise BindingError("invalid")
        member = self.repos.member.get(int(row["member_id"]))
        if member is None or member.openid:
            raise BindingError("member_bound")
        if self.repos.bind_code.claim(code) is None:  # 并发下被先领
            raise BindingError("invalid")
        try:
            self.repos.member.set_openid(member.id, openid)
        except ValidationError as e:
            raise BindingError("already_bound") from e
        return self.repos.member.get(member.id)  # 重取,返回带 openid 的新状态

    def ensure_member_capacity(self, family_id: int) -> None:
        """创建成员位前校验上限;BindingError 复用 limit 文案。"""
        if len(self.repos.member.list_members(family_id)) >= self.max_members:
            raise BindingError("limit")
