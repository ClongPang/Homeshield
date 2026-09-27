"""测试夹具:mock 模式全链路可跑,临时库隔离。包化后无需 sys.path hack。"""
import pytest

from homeshield.core.config import Settings
from homeshield.core.deps import build_deps
from homeshield.core.intake import ingest

@pytest.fixture()
def settings(tmp_path) -> Settings:
    return Settings(mode="mock", db_path=str(tmp_path / "test.db"))


@pytest.fixture()
def deps(settings):
    return build_deps(settings)


@pytest.fixture()
def family(deps):
    """(family_id, untrusted_id, trusted_id)——纠正信任位的两端各一。"""
    fid = deps.repos.family.create("测试家庭")
    untrusted = deps.repos.member.add(fid, "妈妈", openid="test:mom")
    trusted = deps.repos.member.add(fid, "儿子", trusted=True, openid="test:son")
    return fid, untrusted, trusted


def ingest_member(repos, member_id, **kwargs):
    member = repos.member.get(member_id)
    return ingest(
        repos, user_id=member.user_id, memberships=repos.member.list_for_user(member.user_id), **kwargs
    )
