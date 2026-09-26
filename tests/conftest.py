"""测试夹具:mock 模式全链路可跑,临时库隔离。包化后无需 sys.path hack。"""
import pytest

from homeshield.core.config import Settings
from homeshield.core.deps import build_deps
from homeshield.core.models import Role


@pytest.fixture()
def settings(tmp_path) -> Settings:
    return Settings(mode="mock", db_path=str(tmp_path / "test.db"))


@pytest.fixture()
def deps(settings):
    return build_deps(settings)


@pytest.fixture()
def family(deps):
    """(family_id, elder_id, adult_id)"""
    fid = deps.repos.family.create("测试家庭")
    elder = deps.repos.member.add(fid, "妈妈", Role.ELDER)
    adult = deps.repos.member.add(fid, "儿子", Role.ADULT)
    return fid, elder, adult
