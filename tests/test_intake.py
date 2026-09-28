"""Query intake is private and snapshots zero or more incoming relations."""
import pytest

from homeshield.core.intake import ingest
from homeshield.core.models import ContentType


def test_normalize_idempotency_and_bare_query(deps, user):
    one = ingest(deps.repos, user_id=user.id, content="别告诉家人,立即转账5万", msg_id="M1")
    assert not one.duplicate and one.message.content_type is ContentType.TEXT
    assert one.message.relation_ids == []
    two = ingest(deps.repos, user_id=user.id, content="重复", msg_id="M1")
    assert two.duplicate and two.query_id == one.query_id


def test_url_detection_empty_content_and_image(deps, user):
    url = ingest(deps.repos, user_id=user.id, content="看这个 http://a.cn/x")
    assert url.message.content_type is ContentType.URL
    image = ingest(deps.repos, user_id=user.id, content="AAAA", content_type="image")
    assert image.message.content_type is ContentType.IMAGE
    with pytest.raises(ValueError, match="empty content"):
        ingest(deps.repos, user_id=user.id, content="   ")


def test_snapshot_is_fixed_at_query_acceptance(deps, relations):
    protected, protector, relation_id = relations
    first = ingest(deps.repos, user_id=protected.id, content="before link")
    assert first.message.relation_ids == [relation_id]
    deps.repos.relation.end(relation_id, protector.id)
    second = ingest(deps.repos, user_id=protected.id, content="after end")
    assert second.message.relation_ids == []
    assert deps.repos.query.list_relations_for_query(first.query_id)[0]["id"] == relation_id
