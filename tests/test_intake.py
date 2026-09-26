"""归一化与幂等。"""
import pytest

from core.intake import ingest
from core.models import ContentType


def test_normalize_and_idempotency(deps, family):
    fid, elder, _ = family
    r1 = ingest(deps.repos, member_id=elder, family_id=fid, content="别告诉家人,立即转账", msg_id="M1")
    assert not r1.duplicate
    assert r1.message.content_type is ContentType.TEXT
    r2 = ingest(deps.repos, member_id=elder, family_id=fid, content="别告诉家人,立即转账", msg_id="M1")
    assert r2.duplicate and r2.query_id == r1.query_id


def test_url_detection_and_empty_content(deps, family):
    fid, elder, _ = family
    r = ingest(deps.repos, member_id=elder, family_id=fid, content="看这个 http://a.cn/x")
    assert r.message.content_type is ContentType.URL
    with pytest.raises(ValueError):
        ingest(deps.repos, member_id=elder, family_id=fid, content="   ")


def test_declared_image_passthrough(deps, family):
    fid, elder, _ = family
    r = ingest(deps.repos, member_id=elder, family_id=fid, content="AAAA", content_type="image")
    assert r.message.content_type is ContentType.IMAGE
