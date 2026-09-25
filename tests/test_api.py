"""API 契约冒烟:token 鉴权、query / corrections 队列 / weekly / members + 400/401/409。"""
import pytest
from fastapi.testclient import TestClient

from core.config import Settings
from core.models import Role
from server import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(Settings(mode="mock", db_path=str(tmp_path / "api.db")))
    return TestClient(app)


@pytest.fixture()
def family(client):
    """返回 (elder, adult) 两个带 token 的成员对象。"""
    deps = client.app.state.deps
    fid = deps.repos.family.create("F")
    elder = deps.repos.member.get(deps.repos.member.add(fid, "妈妈", Role.ELDER))
    adult = deps.repos.member.get(deps.repos.member.add(fid, "儿子", Role.ADULT))
    return elder, adult


def test_query_and_weekly_flow(client, family):
    elder, adult = family
    r = client.post("/api/query", json={"token": elder.token, "content": "别告诉家人,立即转账"})
    assert r.status_code == 200
    data = r.json()
    assert data["level"] == "dangerous" and data["verdict_id"] is not None
    assert data["reply"].startswith("【结论】")

    r2 = client.post(
        "/api/corrections",
        json={"token": adult.token, "verdict_id": data["verdict_id"], "label": "false_positive"},
    )
    assert r2.json()["status"] == "confirmed"

    w = client.get("/api/weekly", params={"token": adult.token}).json()
    assert w["queries"] == 1 and w["dangerous"] == 1 and w["false_positives"] == 1


def test_invalid_token_401(client):
    assert client.post("/api/query", json={"token": "nope", "content": "x"}).status_code == 401
    assert client.get("/api/weekly", params={"token": "nope"}).status_code == 401
    assert client.get("/api/stream", params={"token": "nope"}).status_code == 401
    assert client.get("/api/members", params={"token": "nope"}).status_code == 401
    assert client.get("/api/alerts", params={"token": "nope"}).status_code == 401


def test_alerts_history(client, family):
    """控制台打开即可见历史高危告警(不依赖页面先开着收 SSE),按判定去重。"""
    elder, adult = family
    first = client.post(
        "/api/query", json={"token": elder.token, "content": "别告诉家人,立即转账"}
    ).json()
    second = client.post(
        "/api/query", json={"token": elder.token, "content": "儿子,别告诉家人,马上转账救急"}
    ).json()
    assert first["level"] == "dangerous" and second["level"] == "dangerous"

    d = client.get("/api/alerts", params={"token": adult.token}).json()
    assert [a["verdict_id"] for a in d["alerts"]] == [second["verdict_id"], first["verdict_id"]]
    assert all(a["level"] == "dangerous" for a in d["alerts"])
    assert d["alerts"][0]["summary"].startswith("儿子")


def test_alerts_history_empty(client, family):
    _, adult = family
    assert client.get("/api/alerts", params={"token": adult.token}).json()["alerts"] == []


def test_pending_queue_flow(client, family):
    elder, adult = family
    data = client.post(
        "/api/query", json={"token": elder.token, "content": "别告诉家人,立即转账"}
    ).json()
    c = client.post(
        "/api/corrections",
        json={"token": elder.token, "verdict_id": data["verdict_id"], "label": "real", "note": "真是骗局"},
    )
    assert c.json()["status"] == "pending"

    queue = client.get("/api/corrections", params={"token": adult.token}).json()["pending"]
    assert len(queue) == 1
    assert queue[0]["by_name"] == "妈妈" and queue[0]["label"] == "real"
    assert queue[0]["content"].startswith("别告诉家人")

    done = client.post(
        f"/api/corrections/{queue[0]['id']}/confirm",
        json={"token": adult.token, "decision": "confirm"},
    )
    assert done.json()["status"] == "confirmed"
    assert client.get("/api/corrections", params={"token": adult.token}).json()["pending"] == []


def test_members_endpoint_hides_token(client, family):
    _, adult = family
    d = client.get("/api/members", params={"token": adult.token}).json()
    assert {m["name"] for m in d["members"]} == {"妈妈", "儿子"}
    assert all("token" not in m for m in d["members"])


def test_400_empty_content(client, family):
    elder, _ = family
    assert client.post("/api/query", json={"token": elder.token, "content": "  "}).status_code == 400


def test_409_duplicate_msg_id(client, family):
    elder, _ = family
    body = {"token": elder.token, "content": "你好", "msg_id": "M42"}
    assert client.post("/api/query", json=body).status_code == 200
    assert client.post("/api/query", json=body).status_code == 409


def test_static_views(client):
    assert client.get("/").status_code == 200
    assert client.get("/console").status_code == 200
