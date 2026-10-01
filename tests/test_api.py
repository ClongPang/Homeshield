"""Acceptance tests for personal-token relation and query APIs."""
import asyncio
from dataclasses import replace

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from homeshield.api.relations import build_relation_router
from homeshield.core.messages import ACK_QUERY_REPLY
from homeshield.core.models import Level, Mode
from homeshield.core.feedback import CorrectionService
from homeshield.core.repo import repository_transaction

from homeshield.core.config import Settings
from homeshield.server import create_app


@pytest_asyncio.fixture(loop_scope="session")
async def client(tmp_path):
    settings = replace(Settings.load(), mode="mock",
                       wecom_corpid="", wecom_agent_id="", wecom_app_secret="", wecom_kf_secret="",
                       wecom_token="", wecom_aes_key="", public_base_url="")
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            client.app = app
            yield client


async def _user(deps, openid): return await deps.repos.users.get_or_create(openid)


async def _relation(deps, protector, protected, name="家人"):
    invite = await deps.relations.issue_invite(protector.id, name)
    return (await deps.relations.join(protected.openid, invite["code"]))[1]


async def test_bare_query_is_private_and_has_history(client):
    deps = client.app.state.deps
    user = await _user(deps, "api:bare")
    assert (await client.get("/api/relations", params={"token": user.token})).json() == {"guardings": [], "guardians": []}
    result = await client.post("/api/query", json={"token": user.token, "content": "别告诉家人，马上转账5万"})
    assert result.status_code == 200 and result.json()["level"] == "dangerous"
    async with deps.pool.connection() as conn:
        assert (await (await conn.execute("SELECT COUNT(*) FROM alert")).fetchone())["count"] == 0
    history = (await client.get("/api/my-queries", params={"token": user.token})).json()["queries"]
    assert history[0]["verdict_id"] == result.json()["verdict_id"]
    detail = await client.get(f"/api/my-queries/{result.json()['verdict_id']}", params={"token": user.token})
    assert detail.status_code == 200 and "别告诉家人" in detail.json()["content"]
    assert (await client.get("/api/relations")).status_code == 401
    assert (await client.get("/api/my-queries")).status_code == 401
    assert (await client.get("/api/my-queries", params={"token": "bad"})).status_code == 401
    assert (await client.post("/api/query", json={"token": "bad", "content": "x"})).status_code == 401


async def test_ack_api_keeps_ack_shape_without_verdict(client):
    user = await _user(client.app.state.deps, "api:ack")
    result = await client.post("/api/query", json={"token": user.token, "content": "谢谢"})
    assert result.status_code == 200
    assert result.json() == {
        "kind": "ack", "query_id": result.json()["query_id"], "verdict_id": None,
        "level": None, "cited_ids": [], "reply": ACK_QUERY_REPLY, "latency_ms": 0,
    }


async def test_web_msg_id_collision_with_another_user_returns_409(client):
    deps = client.app.state.deps
    first = await _user(deps, "api:collision:first")
    second = await _user(deps, "api:collision:second")
    body = {"content": "请转账", "msg_id": "api-user-collision"}
    assert (await client.post("/api/query", json={**body, "token": first.token})).status_code == 200
    response = await client.post("/api/query", json={**body, "token": second.token})
    assert response.status_code == 409


async def test_web_response_survives_immediate_dispatch_lookup_failure(client, monkeypatch):
    deps = client.app.state.deps
    protected = await _user(deps, "api:dispatch-failure")

    async def fail(*_args, **_kwargs):
        raise RuntimeError("dispatch lookup unavailable")

    monkeypatch.setattr(deps.recovery, "dispatch_query", fail)
    response = await client.post("/api/query", json={"token": protected.token, "content": "请转账"})
    assert response.status_code == 200
    assert (await deps.repos.query.get(response.json()["query_id"]))["outcome_kind"] == "verdict"


async def test_invite_preview_and_personal_relationship_management(client):
    deps = client.app.state.deps
    a, b = await _user(deps, "api:a"), await _user(deps, "api:b")
    created = (await client.post("/api/relations/invites", json={"token": a.token, "name": "妈妈"})).json()
    preview = await client.get(f"/api/join/{created['code']}")
    assert preview.status_code == 200 and preview.json()["name"] == "妈妈"
    assert "查询提醒" in preview.json()["sharing"] and "投票查看" in preview.json()["sharing"]
    assert "token" not in preview.json()
    _, rid, _ = await deps.relations.join(b.openid, created["code"])
    assert (await client.get("/api/relations", params={"token": a.token})).json()["guardings"][0]["name"] == "妈妈"
    assert (await client.get("/api/relations", params={"token": b.token})).json()["guardians"][0]["name"] == "联防者"  # 未设置反向称呼的兜底,不内嵌编号
    assert (await client.patch(f"/api/relations/{rid}", json={"token": b.token, "inverse_name": "儿子"})).status_code == 200
    assert (await client.get("/api/relations", params={"token": b.token})).json()["guardians"][0]["name"] == "儿子"
    invites = (await client.get("/api/relations/invites", params={"token": a.token})).json()["invites"]
    assert invites[0]["code"] == created["code"] and invites[0]["used_at"]


async def test_alert_fanout_query_history_and_ended_relation_access(client):
    deps = client.app.state.deps
    protector, protected, stranger = await asyncio.gather(*(_user(deps, key) for key in ("api:protector", "api:protected", "api:stranger")))
    relation_id = await _relation(deps, protector, protected, "妈妈")
    await deps.repos.relation.update(relation_id, protected.id, inverse_name="儿子")
    result = (await client.post("/api/query", json={"token": protected.token, "content": "别告诉家人，马上转账5万"})).json()
    assert result["level"] == "dangerous" and "儿子" in result["reply"]
    alerts = (await client.get("/api/alerts", params={"token": protector.token})).json()["alerts"]
    assert len(alerts) == 1 and alerts[0]["name_at_alert"] == "妈妈"
    alert_id = alerts[0]["alert_id"]
    assert (await client.get(f"/api/alerts/{alert_id}", params={"token": stranger.token})).status_code == 404
    detail = await client.get(f"/api/alerts/{alert_id}", params={"token": protector.token})
    assert detail.status_code == 200 and detail.json()["content"]
    assert (await client.request("DELETE", f"/api/relations/{relation_id}", json={"token": protector.token})).json()["status"] == "by_protector"
    assert (await client.get(f"/api/alerts/{alert_id}", params={"token": protector.token})).status_code == 410
    assert (await client.get("/api/alerts", params={"token": protector.token})).json()["alerts"] == []
    assert (await client.get(f"/api/my-queries/{result['verdict_id']}", params={"token": protected.token})).status_code == 200
    async with deps.pool.connection() as conn:
        assert (await (await conn.execute("SELECT COUNT(*) FROM pg_constraint WHERE contype='f'")).fetchone())["count"] > 0


async def test_correction_queue_and_low_risk_feedback_disclosure(client):
    deps = client.app.state.deps
    protector, protected = await _user(deps, "api:vote-p"), await _user(deps, "api:vote-q")
    relation_id = await _relation(deps, protector, protected)
    result = (await client.post("/api/query", json={"token": protected.token, "content": "今天天气真不错"})).json()
    assert result["level"] == "safe"
    feedback = await client.post("/api/corrections", json={"token": protected.token,
        "verdict_id": result["verdict_id"], "label": "false_positive", "note": "ordinary note"})
    assert feedback.status_code == 200 and feedback.json()["eligible_count"] == 1
    repeated = await client.post("/api/corrections", json={"token": protected.token,
        "verdict_id": result["verdict_id"], "label": "false_positive", "note": "replacement note"})
    assert repeated.status_code == 200 and repeated.json()["queryer_note"] == "ordinary note"
    changed = await client.post("/api/corrections", json={"token": protected.token,
        "verdict_id": result["verdict_id"], "label": "real"})
    assert changed.status_code == 400
    queue = (await client.get("/api/corrections", params={"token": protector.token})).json()["corrections"]
    assert len(queue) == 1 and "今天天气" in queue[0]["content"] and queue[0]["my_vote"] is None
    vote = await client.post("/api/corrections", json={"token": protector.token,
        "verdict_id": result["verdict_id"], "label": "false_positive"})
    assert vote.status_code == 200 and vote.json()["status"] == "confirmed"
    assert (await client.get("/api/corrections", params={"token": protector.token})).json()["corrections"] == []
    assert (await client.get("/api/my-queries", params={"token": protected.token})).json()["queries"][0]["correction_status"] == "confirmed"
    assert (await deps.repos.relation.get(relation_id))["ended_at"] is None


async def test_correction_queue_read_closes_expired_case(client):
    deps = client.app.state.deps
    protector, protected = await _user(deps, "api:expired-p"), await _user(deps, "api:expired-q")
    await _relation(deps, protector, protected)
    verdict = (await client.post("/api/query", json={"token": protected.token, "content": "今天天气真不错"})).json()
    opened = (await client.post("/api/corrections", json={"token": protected.token,
        "verdict_id": verdict["verdict_id"], "label": "false_positive"})).json()
    assert opened["status"] == "pending"
    async with deps.pool.connection() as conn, conn.transaction():
        await conn.execute('UPDATE correction_case SET opened_at=to_timestamp(0), closes_at=to_timestamp(1) WHERE id=%s', (opened["case_id"],))
    assert (await client.get("/api/corrections", params={"token": protector.token})).json()["corrections"] == []
    assert (await deps.repos.correction.get_case_for_verdict(verdict["verdict_id"]))["status"] == "no_consensus"


async def test_retired_group_and_weekly_routes_are_absent(client):
    paths = set((await client.get("/openapi.json")).json()["paths"])
    assert "/api/groups" not in paths and "/api/weekly" not in paths
    assert "/api/relations" in paths and "/api/my-queries" in paths


async def test_stream_event_is_user_scoped_and_contains_alert_relation_snapshot(client):
    deps = client.app.state.deps
    protector, protected = await _user(deps, "api:stream-protector"), await _user(deps, "api:stream-protected")
    relation_id = await _relation(deps, protector, protected, "妈妈")
    query_id = await deps.repos.query.insert(protected.id, "text", "查询内容", None)
    async with repository_transaction(deps.repos):
        verdict_id = await deps.repos.verdict.insert(query_id, Level.DANGEROUS, [], [], "理由", "回复", 1, Mode.MOCK)
        fanout = await deps.repos.alert.record_alerts_for_verdict(verdict_id, query_id)
    alert = fanout["recipients"][0]
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

    await read_event()


async def test_kf_landing_page_and_qr_asset(client, tmp_path):
    """客服入口页公开可访问;二维码资产缺失时 404,放置后按扩展名给出图片类型。"""
    page = await client.get("/kf")
    assert page.status_code == 200 and "小盾" in page.text
    assert (await client.get("/kf/qr")).status_code == 404  # 资产未放置时如实降级

    qr = tmp_path / "kf.jpg"
    qr.write_bytes(b"\xff\xd8fake-jpeg")
    settings = replace(Settings.load(), mode="mock", wecom_kf_qr_path=str(qr),
                       wecom_corpid="", wecom_agent_id="", wecom_app_secret="", wecom_kf_secret="",
                       wecom_token="", wecom_aes_key="", public_base_url="")
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/kf/qr")
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("image/jpeg")
