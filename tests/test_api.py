"""Acceptance tests for personal-token relation and query APIs."""
import asyncio
import os

import pytest
from fastapi.testclient import TestClient
from homeshield.api.relations import build_relation_router
from homeshield.core.messages import ACK_QUERY_REPLY
from homeshield.core.models import Level, Mode
from homeshield.core.feedback import CorrectionService

from homeshield.core.config import Settings
from homeshield.server import _acquire_single_process_lock, _try_lock, create_app


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(Settings(mode="mock", db_path=str(tmp_path / "api.db"))))


def test_single_process_guard_blocks_second_holder_on_same_db(tmp_path):
    lock_path = str(tmp_path / "guarded.db.server.lock")
    fd = _try_lock(lock_path)
    try:
        with pytest.raises(OSError):
            _try_lock(lock_path)
    finally:
        os.close(fd)


def test_single_process_guard_skips_memory_db():
    _acquire_single_process_lock(":memory:")  # 不应抛错、不落锁文件


def _user(deps, openid): return deps.repos.users.get_or_create(openid)


def _relation(deps, protector, protected, name="家人"):
    invite = deps.relations.issue_invite(protector.id, name)
    return deps.relations.join(protected.openid, invite["code"])[1]


def test_bare_query_is_private_and_has_history(client):
    deps = client.app.state.deps
    user = _user(deps, "api:bare")
    assert client.get("/api/relations", params={"token": user.token}).json() == {"guardings": [], "guardians": []}
    result = client.post("/api/query", json={"token": user.token, "content": "别告诉家人，马上转账5万"})
    assert result.status_code == 200 and result.json()["level"] == "dangerous"
    assert deps.conn.execute("SELECT COUNT(*) FROM alert").fetchone()[0] == 0
    history = client.get("/api/my-queries", params={"token": user.token}).json()["queries"]
    assert history[0]["verdict_id"] == result.json()["verdict_id"]
    detail = client.get(f"/api/my-queries/{result.json()['verdict_id']}", params={"token": user.token})
    assert detail.status_code == 200 and "别告诉家人" in detail.json()["content"]
    assert client.get("/api/relations").status_code == 401
    assert client.get("/api/my-queries").status_code == 401
    assert client.get("/api/my-queries", params={"token": "bad"}).status_code == 401
    assert client.post("/api/query", json={"token": "bad", "content": "x"}).status_code == 401


def test_ack_api_keeps_ack_shape_without_verdict(client):
    user = _user(client.app.state.deps, "api:ack")
    result = client.post("/api/query", json={"token": user.token, "content": "谢谢"})
    assert result.status_code == 200
    assert result.json() == {
        "kind": "ack", "query_id": result.json()["query_id"], "verdict_id": None,
        "level": None, "cited_ids": [], "reply": ACK_QUERY_REPLY, "latency_ms": 0,
    }


def test_invite_preview_and_personal_relationship_management(client):
    deps = client.app.state.deps
    a, b = _user(deps, "api:a"), _user(deps, "api:b")
    created = client.post("/api/relations/invites", json={"token": a.token, "name": "妈妈"}).json()
    preview = client.get(f"/api/join/{created['code']}")
    assert preview.status_code == 200 and preview.json()["name"] == "妈妈"
    assert "高危提醒" in preview.json()["sharing"] and "投票查看" in preview.json()["sharing"]
    assert "token" not in preview.json()
    _, rid, _ = deps.relations.join(b.openid, created["code"])
    assert client.get("/api/relations", params={"token": a.token}).json()["guardings"][0]["name"] == "妈妈"
    assert client.get("/api/relations", params={"token": b.token}).json()["guardians"][0]["name"] == f"联防者 #{rid}"
    assert client.patch(f"/api/relations/{rid}", json={"token": b.token, "inverse_name": "儿子"}).status_code == 200
    assert client.get("/api/relations", params={"token": b.token}).json()["guardians"][0]["name"] == "儿子"
    invites = client.get("/api/relations/invites", params={"token": a.token}).json()["invites"]
    assert invites[0]["code"] == created["code"] and invites[0]["used_at"]


def test_alert_fanout_query_history_and_ended_relation_access(client):
    deps = client.app.state.deps
    protector, protected, stranger = (_user(deps, key) for key in ("api:protector", "api:protected", "api:stranger"))
    relation_id = _relation(deps, protector, protected, "妈妈")
    deps.repos.relation.update(relation_id, protected.id, inverse_name="儿子")
    result = client.post("/api/query", json={"token": protected.token, "content": "别告诉家人，马上转账5万"}).json()
    assert result["level"] == "dangerous" and "儿子" in result["reply"]
    alerts = client.get("/api/alerts", params={"token": protector.token}).json()["alerts"]
    assert len(alerts) == 1 and alerts[0]["name_at_alert"] == "妈妈"
    alert_id = alerts[0]["alert_id"]
    assert client.get(f"/api/alerts/{alert_id}", params={"token": stranger.token}).status_code == 404
    detail = client.get(f"/api/alerts/{alert_id}", params={"token": protector.token})
    assert detail.status_code == 200 and detail.json()["content"]
    assert client.request("DELETE", f"/api/relations/{relation_id}", json={"token": protector.token}).json()["status"] == "by_protector"
    assert client.get(f"/api/alerts/{alert_id}", params={"token": protector.token}).status_code == 410
    assert client.get("/api/alerts", params={"token": protector.token}).json()["alerts"] == []
    assert client.get(f"/api/my-queries/{result['verdict_id']}", params={"token": protected.token}).status_code == 200
    assert deps.conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_correction_queue_and_low_risk_feedback_disclosure(client):
    deps = client.app.state.deps
    protector, protected = _user(deps, "api:vote-p"), _user(deps, "api:vote-q")
    relation_id = _relation(deps, protector, protected)
    result = client.post("/api/query", json={"token": protected.token, "content": "今天天气真不错"}).json()
    assert result["level"] == "safe"
    feedback = client.post("/api/corrections", json={"token": protected.token,
        "verdict_id": result["verdict_id"], "label": "false_positive", "note": "ordinary note"})
    assert feedback.status_code == 200 and feedback.json()["eligible_count"] == 1
    repeated = client.post("/api/corrections", json={"token": protected.token,
        "verdict_id": result["verdict_id"], "label": "false_positive", "note": "replacement note"})
    assert repeated.status_code == 200 and repeated.json()["queryer_note"] == "ordinary note"
    changed = client.post("/api/corrections", json={"token": protected.token,
        "verdict_id": result["verdict_id"], "label": "real"})
    assert changed.status_code == 400
    queue = client.get("/api/corrections", params={"token": protector.token}).json()["corrections"]
    assert len(queue) == 1 and "今天天气" in queue[0]["content"] and queue[0]["my_vote"] is None
    vote = client.post("/api/corrections", json={"token": protector.token,
        "verdict_id": result["verdict_id"], "label": "false_positive"})
    assert vote.status_code == 200 and vote.json()["status"] == "confirmed"
    assert client.get("/api/corrections", params={"token": protector.token}).json()["corrections"] == []
    assert client.get("/api/my-queries", params={"token": protected.token}).json()["queries"][0]["correction_status"] == "confirmed"
    assert deps.repos.relation.get(relation_id)["ended_at"] is None


def test_correction_queue_read_closes_expired_case(client):
    deps = client.app.state.deps
    protector, protected = _user(deps, "api:expired-p"), _user(deps, "api:expired-q")
    _relation(deps, protector, protected)
    verdict = client.post("/api/query", json={"token": protected.token, "content": "今天天气真不错"}).json()
    opened = client.post("/api/corrections", json={"token": protected.token,
        "verdict_id": verdict["verdict_id"], "label": "false_positive"}).json()
    assert opened["status"] == "pending"
    deps.conn.execute("UPDATE correction_case SET opened_at=0, closes_at=1 WHERE id=?", (opened["case_id"],))
    assert client.get("/api/corrections", params={"token": protector.token}).json()["corrections"] == []
    assert deps.repos.correction.get_case_for_verdict(verdict["verdict_id"])["status"] == "no_consensus"


def test_retired_group_and_weekly_routes_are_absent(client):
    paths = set(client.get("/openapi.json").json()["paths"])
    assert "/api/groups" not in paths and "/api/weekly" not in paths
    assert "/api/relations" in paths and "/api/my-queries" in paths


def test_stream_event_is_user_scoped_and_contains_alert_relation_snapshot(client):
    deps = client.app.state.deps
    protector, protected = _user(deps, "api:stream-protector"), _user(deps, "api:stream-protected")
    relation_id = _relation(deps, protector, protected, "妈妈")
    query_id = deps.repos.query.insert(protected.id, "text", "查询内容", None)
    verdict_id = deps.repos.verdict.insert(query_id, Level.DANGEROUS, [], [], "理由", "回复", 1, Mode.MOCK)
    alert = deps.repos.alert.record_alerts_for_verdict(verdict_id, query_id)["recipients"][0]
    router = build_relation_router(deps, deps.verification,
                                  CorrectionService(deps.repos, deps.settings.correction_window_days))
    route = next(route for route in router.routes if route.path == "/api/stream")

    async def read_event():
        response = await route.endpoint(token=protector.token)
        stream = response.body_iterator
        try:
            ready = await anext(stream)
            assert "event: ready" in ready
            deps.broker.publish_alert(protector.id, {
                "alert_id": alert["alert_id"], "verdict_id": verdict_id,
                "relation_id": relation_id, "name_at_alert": alert["name_at_alert"],
            })
            event = await asyncio.wait_for(anext(stream), timeout=1)
            assert "event: alert" in event and '"name_at_alert": "妈妈"' in event
        finally:
            await stream.aclose()

    asyncio.run(read_event())
