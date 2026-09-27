"""归一化与幂等。"""
import pytest

from conftest import ingest_member
from homeshield.core.intake import ingest
from homeshield.core.models import ContentType


def test_normalize_and_idempotency(deps, family):
    fid, elder, _ = family
    r1 = ingest_member(deps.repos, elder, content="别告诉家人,立即转账", msg_id="M1")
    assert not r1.duplicate
    assert r1.message.content_type is ContentType.TEXT
    r2 = ingest_member(deps.repos, elder, content="别告诉家人,立即转账", msg_id="M1")
    assert r2.duplicate and r2.query_id == r1.query_id


def test_url_detection_and_empty_content(deps, family):
    fid, elder, _ = family
    r = ingest_member(deps.repos, elder, content="看这个 http://a.cn/x")
    assert r.message.content_type is ContentType.URL
    with pytest.raises(ValueError):
        ingest_member(deps.repos, elder, content="   ")


def test_declared_image_passthrough(deps, family):
    fid, elder, _ = family
    r = ingest_member(deps.repos, elder, content="AAAA", content_type="image")
    assert r.message.content_type is ContentType.IMAGE


def test_membership_ended_before_snapshot_is_a_normal_no_group_result(deps, family):
    fid, elder, _ = family
    member = deps.repos.member.get(elder)
    memberships = deps.repos.member.list_for_user(member.user_id)
    deps.groups.leave(member.user_id, fid)

    with pytest.raises(ValueError, match="user has no active group"):
        ingest(deps.repos, user_id=member.user_id, memberships=memberships,
               content="这条消息要判定吗")

    assert deps.conn.execute("SELECT COUNT(*) FROM query WHERE user_id=?", (member.user_id,)).fetchone()[0] == 0
