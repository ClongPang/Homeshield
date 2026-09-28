"""Isolated in-memory test fixtures."""
import os
import pytest

os.environ["DB_PATH"] = ":memory:"

from homeshield.core.config import Settings
from homeshield.core.deps import build_deps
from homeshield.core.intake import ingest


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(mode="mock", db_path=str(tmp_path / "test.db"))


@pytest.fixture
def deps(settings):
    return build_deps(settings)


@pytest.fixture
def user(deps):
    return deps.repos.users.get_or_create("test:queryer")


@pytest.fixture
def relations(deps):
    protector = deps.repos.users.get_or_create("test:protector")
    protected = deps.repos.users.get_or_create("test:protected")
    invite = deps.relations.issue_invite(protector.id, "妈妈")
    _, relation_id, _ = deps.relations.join(protected.openid, invite["code"])
    deps.repos.relation.update(relation_id, protected.id, inverse_name="儿子")
    return protected, protector, relation_id


def ingest_user(repos, user, **kwargs):
    return ingest(repos, user_id=user.id, **kwargs)
