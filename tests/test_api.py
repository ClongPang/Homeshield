"""API 契约冒烟:token 鉴权、query / corrections 队列 / weekly / members + 400/401/409。"""
import pytest
from fastapi.testclient import TestClient

from homeshield.core.config import Settings
from homeshield.server import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(Settings(mode="mock", db_path=str(tmp_path / "api.db")))
    return TestClient(app)


@pytest.fixture()
def family(client):
    """返回 (untrusted, trusted) 两个带 token 的成员对象。"""
    deps = client.app.state.deps
    fid = deps.repos.family.create("F")
    untrusted = deps.repos.member.get(deps.repos.member.add(fid, "妈妈"))
    trusted = deps.repos.member.get(deps.repos.member.add(fid, "儿子", trusted=True))
    return untrusted, trusted


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


def test_alert_detail_feedback_and_scoping(client, family):
    elder, adult = family
    data = client.post(
        "/api/query", json={"token": elder.token, "content": "别告诉家人,立即转账"}
    ).json()
    vid = data["verdict_id"]

    d = client.get(f"/api/alerts/{vid}", params={"token": adult.token}).json()
    assert d["level"] == "dangerous" and d["reply"].startswith("【结论】")
    assert d["my_feedback"] is None

    client.post("/api/corrections", json={"token": adult.token, "verdict_id": vid, "label": "real"})
    d2 = client.get(f"/api/alerts/{vid}", params={"token": adult.token}).json()
    assert d2["my_feedback"]["label"] == "real"
    assert d2["my_feedback"]["status"] == "confirmed"

    # 别的家庭看不到这条告警
    deps = client.app.state.deps
    other_fid = deps.repos.family.create("别家")
    other = deps.repos.member.get(deps.repos.member.add(other_fid, "外人"))
    assert client.get(f"/api/alerts/{vid}", params={"token": other.token}).status_code == 404


def test_alert_history_listing(client, family):
    elder, adult = family
    client.post("/api/query", json={"token": elder.token, "content": "别告诉家人,立即转账"})
    history = client.get("/api/alerts", params={"token": adult.token}).json()["alerts"]
    assert len(history) == 1 and history[0]["level"] == "dangerous"


def test_alert_fanout_covers_all_members(client, family):
    """群模型告警面:dangerous 对全体成员落 alert(不再按角色过滤)。"""
    untrusted, _ = family
    deps = client.app.state.deps
    data = client.post(
        "/api/query", json={"token": untrusted.token, "content": "别告诉家人,立即转账"}
    ).json()
    rows = deps.conn.execute(
        "SELECT member_id FROM alert WHERE verdict_id=?", [data["verdict_id"]]
    ).fetchall()
    assert {r["member_id"] for r in rows} == {m.id for m in deps.repos.member.list_members(1)}


def test_add_member_and_bind_code_flow(client, family):
    """信任成员创建成员位拿邀请码;新位默认不受信任;重发作废旧码由绑定域测试覆盖。"""
    _, trusted = family
    r = client.post("/api/members", json={"token": trusted.token, "name": "爸爸"})
    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "爸爸" and data["trusted"] is False
    assert len(data["bind_code"]) == 8 and data["bind_expires_at"] > 0
    assert data["entry_url"] is None  # 测试环境未配 PUBLIC_BASE_URL

    members = client.get("/api/members", params={"token": trusted.token}).json()["members"]
    dad = next(m for m in members if m["name"] == "爸爸")
    assert dad["bound"] is False and dad["id"] == data["member_id"]

    r2 = client.post(f"/api/members/{data['member_id']}/bind-code", json={"token": trusted.token})
    assert r2.status_code == 200
    assert r2.json()["bind_code"] != data["bind_code"]


def test_add_member_untrusted_forbidden(client, family):
    untrusted, _ = family
    r = client.post("/api/members", json={"token": untrusted.token, "name": "x"})
    assert r.status_code == 403
    assert (
        client.post("/api/members/1/bind-code", json={"token": untrusted.token}).status_code == 403
    )


def test_add_member_rejects_empty_name(client, family):
    _, trusted = family
    assert client.post("/api/members", json={"token": trusted.token, "name": "  "}).status_code == 400


def test_trust_toggle(client, family):
    """信任成员翻转他人信任位;不能改自己;未受信任调用 403。"""
    untrusted, trusted = family
    deps = client.app.state.deps
    other = deps.repos.member.get(deps.repos.member.add(untrusted.family_id, "爸爸"))

    r = client.post(f"/api/members/{other.id}/trust", json={"token": trusted.token, "trusted": True})
    assert r.status_code == 200
    assert deps.repos.member.get(other.id).trusted is True

    assert client.post(
        f"/api/members/{other.id}/trust", json={"token": untrusted.token, "trusted": False}
    ).status_code == 403

    r = client.post(f"/api/members/{other.id}/trust", json={"token": trusted.token, "trusted": False})
    assert r.status_code == 200
    assert deps.repos.member.get(other.id).trusted is False

    # 不能改自己:防止最后一个可管理者自我降级后无人管理
    assert client.post(
        f"/api/members/{trusted.id}/trust", json={"token": trusted.token, "trusted": False}
    ).status_code == 400

    # 跨家庭目标 404
    other_fid = deps.repos.family.create("别家")
    outsider = deps.repos.member.get(deps.repos.member.add(other_fid, "外人"))
    assert client.post(
        f"/api/members/{outsider.id}/trust", json={"token": trusted.token, "trusted": True}
    ).status_code == 404

    # 并发互降护栏:条件更新保证群内恒有信任成员;对普通成员重复降级不生效
    assert deps.repos.member.get(trusted.id).trusted is True
    assert deps.repos.member.demote_with_guard(untrusted.id, untrusted.family_id) is False  # 目标本已普通
    deps.repos.member.set_trust(untrusted.id, True)
    assert deps.repos.member.demote_with_guard(untrusted.id, untrusted.family_id) is True
    assert deps.repos.member.demote_with_guard(untrusted.id, untrusted.family_id) is False
    assert deps.repos.member.get(untrusted.id).trusted is False

    # 信任位决定纠正即时生效与否
    vid = client.post(
        "/api/query", json={"token": untrusted.token, "content": "别告诉家人,立即转账"}
    ).json()["verdict_id"]
    assert client.post(
        "/api/corrections", json={"token": other.token, "verdict_id": vid, "label": "real"}
    ).json()["status"] == "pending"
    client.post(f"/api/members/{other.id}/trust", json={"token": trusted.token, "trusted": True})
    vid2 = client.post(
        "/api/query", json={"token": untrusted.token, "content": "儿子,别告诉家人,马上转账救急"}
    ).json()["verdict_id"]
    assert client.post(
        "/api/corrections", json={"token": other.token, "verdict_id": vid2, "label": "real"}
    ).json()["status"] == "confirmed"


def test_add_member_caps_at_max_members(client, family):
    untrusted, trusted = family
    deps = client.app.state.deps
    for _ in range(deps.settings.max_members - 2):
        deps.repos.member.add(untrusted.family_id, "占位")
    r = client.post("/api/members", json={"token": trusted.token, "name": "再来一个"})
    assert r.status_code == 400


def test_cross_family_correction_submit_rejected(client, family):
    """别家成员对别家判定提交纠正:必须被拒(多租户隔离)。"""
    elder, _ = family
    data = client.post("/api/query", json={"token": elder.token, "content": "别告诉家人,立即转账"}).json()

    deps = client.app.state.deps
    other_fid = deps.repos.family.create("别家")
    stranger = deps.repos.member.get(deps.repos.member.add(other_fid, "外人"))
    r = client.post(
        "/api/corrections",
        json={"token": stranger.token, "verdict_id": data["verdict_id"], "label": "false_positive"},
    )
    assert r.status_code == 400


def test_cross_family_correction_decide_rejected(client, family):
    """别家管理员不能裁决本家的待确认纠正。"""
    elder, adult = family
    data = client.post("/api/query", json={"token": elder.token, "content": "别告诉家人,立即转账"}).json()
    c = client.post(
        "/api/corrections",
        json={"token": elder.token, "verdict_id": data["verdict_id"], "label": "real"},
    ).json()

    deps = client.app.state.deps
    other_fid = deps.repos.family.create("别家")
    stranger = deps.repos.member.get(deps.repos.member.add(other_fid, "外人"))
    r = client.post(f"/api/corrections/{c['correction_id']}/confirm", json={"token": stranger.token})
    assert r.status_code == 400
    # 本家管理员裁决不受影响
    ok = client.post(
        f"/api/corrections/{c['correction_id']}/confirm", json={"token": adult.token}
    )
    assert ok.json()["status"] == "confirmed"
