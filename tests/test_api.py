"""个人 token 与群上下文 API 的验收用例。"""
import pytest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from fastapi.testclient import TestClient

from homeshield.core.config import Settings
from homeshield.core.errors import ValidationError
from homeshield.server import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(Settings(mode="mock", db_path=str(tmp_path / "api.db")))
    return TestClient(app)


@pytest.fixture()
def group(client):
    deps = client.app.state.deps
    group_id = deps.repos.group.create("F")
    mom_member = deps.repos.member.get(deps.repos.member.add(group_id, "妈妈", openid="test:mom"))
    trusted_member = deps.repos.member.get(deps.repos.member.add(group_id, "儿子", True, "test:son"))
    mom = _actor(deps, mom_member)
    trusted = _actor(deps, trusted_member)
    return group_id, mom, trusted


def _actor(deps, member):
    user=deps.repos.users.get(member.user_id) if member.user_id is not None else None
    return SimpleNamespace(member=member,id=member.id,user_id=user.id if user else None,
                           token=user.token if user else None,openid=user.openid if user else None,
                           group_id=member.group_id,name=member.name,trusted=member.trusted)


def test_query_alert_feedback_and_weekly(client, group):
    group_id, mom, trusted = group
    result = client.post("/api/query", json={"token": mom.token, "content": "别告诉家人,立即转账"}).json()
    assert result["level"] == "dangerous"
    assert result["kind"] == "query"
    assert result["reply"].startswith("【结论】") and "已为防护群发出高危提醒" in result["reply"]

    history = client.get("/api/alerts", params={"token": trusted.token}).json()
    assert [a["verdict_id"] for a in history["alerts"]] == [result["verdict_id"]]
    detail = client.get(f"/api/alerts/{result['verdict_id']}", params={"token": trusted.token}).json()
    assert detail["group_names"] == ["F"] and detail["my_feedback"] is None

    correction = client.post("/api/corrections", json={
        "token": trusted.token, "verdict_id": result["verdict_id"], "label": "false_positive",
    }).json()
    assert correction["status"] == "confirmed"
    weekly = client.get("/api/weekly", params={"token": trusted.token}).json()
    assert weekly["group_id"] == group_id
    assert weekly["queries"] == weekly["dangerous"] == weekly["corrections"] == weekly["false_positives"] == 1


def test_ack_then_query_and_web_reset_is_query(client, group):
    _, mom, _ = group
    ack = client.post("/api/query", json={"token": mom.token, "content": "谢谢"}).json()
    assert ack["kind"] == "ack" and ack["verdict_id"] is None and ack["level"] is None
    assert ack["cited_ids"] == [] and ack["latency_ms"] == 0
    query = client.post("/api/query", json={"token": mom.token, "content": "请转账"}).json()
    assert query["kind"] == "query" and query["verdict_id"] is not None
    reset = client.post("/api/query", json={"token": mom.token, "content": "新的"}).json()
    assert reset["kind"] == "query" and reset["verdict_id"] is not None


def test_invalid_token_is_401(client):
    assert client.post("/api/query", json={"token":"nope","content":"x"}).status_code == 401
    for path in ("/api/groups", "/api/weekly", "/api/stream", "/api/alerts", "/api/corrections"):
        assert client.get(path, params={"token":"nope"}).status_code == 401


def test_query_without_active_group_does_not_enter_pipeline(client):
    deps = client.app.state.deps
    user = deps.repos.users.get_or_create("test:outside")
    assert client.get("/api/groups", params={"token":user.token}).json() == {"groups": []}
    response = client.post("/api/query", json={"token":user.token,"content":"这是骗子吗"})
    assert response.status_code == 400
    assert deps.conn.execute("SELECT COUNT(*) FROM query WHERE user_id=?", (user.id,)).fetchone()[0] == 0


def test_query_membership_ended_during_intake_returns_400(client, group, monkeypatch):
    group_id, mom, _ = group
    deps = client.app.state.deps
    original = deps.repos.member.list_for_user

    def return_snapshot_then_leave(user_id, active_only=True):
        memberships = original(user_id, active_only)
        if user_id == mom.user_id:
            monkeypatch.setattr(deps.repos.member, "list_for_user", original)
            deps.groups.leave_group(user_id, group_id)
            monkeypatch.setattr(deps.repos.member, "list_for_user", return_snapshot_then_leave)
        return memberships

    monkeypatch.setattr(deps.repos.member, "list_for_user", return_snapshot_then_leave)
    response = client.post("/api/query", json={"token": mom.token, "content": "请帮我看看"})

    assert response.status_code == 400
    assert deps.conn.execute("SELECT COUNT(*) FROM query WHERE user_id=?", (mom.user_id,)).fetchone()[0] == 0


def test_identity_credentials_live_only_on_user_rows(client):
    conn=client.app.state.deps.conn
    member_columns={row["name"] for row in conn.execute("PRAGMA table_info(member)")}
    user_columns={row["name"] for row in conn.execute("PRAGMA table_info(user)")}
    query_columns={row["name"] for row in conn.execute("PRAGMA table_info(query)")}
    assert {"openid","token"}.issubset(user_columns)
    assert "openid" not in member_columns and "token" not in member_columns
    assert "user_id" in query_columns and "group_id" not in query_columns and "member_id" not in query_columns


def test_multi_group_pages_require_group_id_and_queries_snapshot_all_groups(client, group):
    group_id, mom, trusted = group
    deps = client.app.state.deps
    second = deps.repos.group.create("岳父家")
    second_member = deps.repos.member.get(deps.repos.member.add(second,"妈妈",openid=mom.openid))
    deps.repos.member.add(second,"岳父",True,"test:father")
    assert second_member.user_id==mom.user_id
    assert deps.repos.users.get(second_member.user_id).token == mom.token

    assert client.get("/api/weekly",params={"token":mom.token}).status_code == 400
    assert client.get("/api/alerts",params={"token":mom.token}).status_code == 400
    assert client.get("/api/corrections",params={"token":mom.token}).status_code == 400

    result=client.post("/api/query",json={"token":mom.token,"content":"别告诉家人,立即转账"}).json()
    rows=deps.conn.execute(
        "SELECT qg.group_id FROM query_group qg WHERE qg.query_id=? ORDER BY qg.group_id",
        (result["query_id"],),
    ).fetchall()
    assert [r[0] for r in rows] == [group_id,second]
    assert "F" in result["reply"] and "岳父家" in result["reply"]

    groups=client.get("/api/groups",params={"token":mom.token}).json()["groups"]
    assert {g["id"] for g in groups} == {group_id,second}
    alerts=client.get("/api/alerts",params={"token":trusted.token,"group_id":group_id}).json()
    assert len(alerts["alerts"]) == 1


def test_console_user_can_create_an_additional_group(client, group):
    _, mom, _=group
    created=client.post("/api/groups",json={"token":mom.token,"name":"朋友家"})
    assert created.status_code==200 and created.json()["trusted"] is True
    groups=client.get("/api/groups",params={"token":mom.token}).json()["groups"]
    item=next(g for g in groups if g["id"]==created.json()["id"])
    assert item["name"]=="朋友家" and item["is_creator"] is True
    assert len(groups)==2


def test_alert_access_revoked_on_leave_and_restored_on_rejoin(client, group):
    group_id, mom, trusted = group
    deps=client.app.state.deps
    result=client.post("/api/query",json={"token":mom.token,"content":"别告诉家人,立即转账"}).json()
    vid=result["verdict_id"]
    assert client.get("/api/alerts",params={"token":trusted.token,"group_id":group_id}).status_code == 200

    deps.repos.member.set_trust(mom.id,True)
    assert deps.groups.leave_group(trusted.user_id,group_id)=="left"
    assert deps.repos.users.get_by_token(trusted.token).id == trusted.user_id
    assert client.get("/api/alerts",params={"token":trusted.token,"group_id":group_id}).status_code == 404
    denied=client.get(f"/api/alerts/{vid}",params={"token":trusted.token})
    assert denied.status_code == 410 and denied.json()["detail"]["reason"] == "membership_ended"
    old=deps.repos.member.get(trusted.id)
    assert old.user_id == trusted.user_id and old.ended_at is not None

    slot=deps.repos.member.add(group_id,"儿子")
    code=deps.binding.issue_bind_code(deps.repos.member.get(slot),created_by=mom.id)
    joined=deps.binding.bind_member_with_invite_code(trusted.openid,code["code"])
    assert joined.id != trusted.id and deps.repos.users.get(joined.user_id).token == trusted.token
    assert len(client.get("/api/alerts",params={"token":trusted.token,"group_id":group_id}).json()["alerts"]) == 1


def test_creator_disband_retains_rows_and_returns_410(client):
    deps=client.app.state.deps
    creator_member=deps.binding.create_initial_group("o_creator","父母家")
    creator=_actor(deps,creator_member)
    member_row=deps.repos.member.get(deps.repos.member.add(creator.group_id,"家人",True,"test:group"))
    member=_actor(deps,member_row)
    result=client.post("/api/query",json={"token":member.token,"content":"别告诉家人,立即转账"}).json()
    assert client.request("DELETE",f"/api/groups/{creator.group_id}",json={"token":member.token}).status_code==400
    response=client.request("DELETE",f"/api/groups/{creator.group_id}",json={"token":creator.token})
    assert response.status_code == 200
    assert deps.repos.member.get(creator.id).end_reason == "disbanded"
    assert deps.repos.member.get(creator.id).user_id==creator.user_id
    assert deps.conn.execute("SELECT COUNT(*) FROM alert WHERE verdict_id=?",(result["verdict_id"],)).fetchone()[0]>0
    assert deps.conn.execute("PRAGMA foreign_key_check").fetchall()==[]
    assert client.get(f"/api/alerts/{result['verdict_id']}",params={"token":member.token}).json()["detail"]["reason"] == "group_disbanded"
    assert client.get("/api/groups",params={"token":creator.token}).json()["groups"] == []


def test_last_bound_member_exit_auto_disbands_but_invite_removal_does_not(client):
    deps=client.app.state.deps
    solo=deps.binding.create_initial_group("o_solo","独居群")
    slot=deps.repos.member.add(solo.group_id,"未绑定邀请")
    code=deps.binding.issue_bind_code(deps.repos.member.get(slot),created_by=solo.id)
    assert deps.groups.remove_member(solo.group_id,slot)=="left"
    assert deps.repos.bind_code.get_valid_bind_code(code["code"]) is None
    assert deps.repos.group.get(solo.group_id)["disbanded_at"] is None
    assert deps.groups.leave_group(solo.user_id,solo.group_id)=="disbanded"
    assert deps.repos.group.get(solo.group_id)["disbanded_at"] is not None


def test_pending_correction_is_shared_across_related_groups(client, group):
    group_id, mom, trusted = group
    deps=client.app.state.deps
    second=deps.repos.group.create("岳父家")
    deps.repos.member.add(second,"妈妈",openid=mom.openid)
    second_trusted=_actor(deps,deps.repos.member.get(deps.repos.member.add(second,"岳父",True,"test:father")))
    result=client.post("/api/query",json={"token":mom.token,"content":"别告诉家人,立即转账"}).json()
    submitted=client.post("/api/corrections",json={"token":mom.token,"verdict_id":result["verdict_id"],"label":"real"}).json()
    assert submitted["status"] == "pending"
    duplicate=client.post("/api/corrections",json={"token":mom.token,"verdict_id":result["verdict_id"],"label":"false_positive"}).json()
    assert duplicate["correction_id"]==submitted["correction_id"] and duplicate["status"]=="pending"
    assert deps.conn.execute("SELECT COUNT(*) FROM correction WHERE verdict_id=?",(result["verdict_id"],)).fetchone()[0]==1
    own_view=client.get("/api/corrections",params={"token":mom.token,"group_id":group_id}).json()
    assert own_view["viewer_trusted"] is False and own_view["pending"]==[]
    first_queue=client.get("/api/corrections",params={"token":trusted.token,"group_id":group_id}).json()["pending"]
    second_queue=client.get("/api/corrections",params={"token":second_trusted.token,"group_id":second}).json()["pending"]
    assert len(first_queue)==len(second_queue)==1
    done=client.post(f"/api/corrections/{submitted['correction_id']}/confirm",json={"token":second_trusted.token}).json()
    assert done["status"]=="confirmed"
    assert client.get("/api/corrections",params={"token":trusted.token,"group_id":group_id}).json()["pending"]==[]


def test_group_members_and_invitation_page(client, group):
    group_id, _, trusted=group
    response=client.post(f"/api/groups/{group_id}/members",json={"token":trusted.token,"name":"爸爸"})
    assert response.status_code==200
    invite=response.json()
    assert invite["trusted"] is False and invite["entry_url"] is None
    assert client.get("/api/join/NOPE0000").status_code==404
    info=client.get(f"/api/join/{invite['bind_code']}")
    assert info.status_code==200
    assert info.json()["group_name"]=="F" and info.json()["member_name"]=="爸爸"
    assert info.json()["command"]==f"绑定 {invite['bind_code']}" and "token" not in info.json()
    assert client.get(f"/join/{invite['bind_code']}").status_code==200
    assert client.get("/join/NOPE0000").status_code==200
    members=client.get(f"/api/groups/{group_id}/members",params={"token":trusted.token}).json()["members"]
    assert {m["name"] for m in members}=={"妈妈","儿子","爸爸"}


def test_group_management_trust_guard_rename_and_mute(client, group):
    group_id, mom, trusted=group
    deps=client.app.state.deps
    # 无创建者的运维群不能由家人解散;群级数据路由按 user 成员关系授权。
    assert client.request("DELETE",f"/api/groups/{group_id}",json={"token":trusted.token}).status_code==400
    assert client.patch(f"/api/groups/{group_id}",json={"token":trusted.token,"name":"新群名"}).status_code==200
    renamed=client.get("/api/groups",params={"token":mom.token}).json()["groups"]
    assert renamed[0]["name"]=="新群名"
    assert client.patch(f"/api/groups/{group_id}/members/{mom.id}",json={"token":mom.token,"name":"阿姨"}).status_code==200
    muted=client.post(f"/api/groups/{group_id}/mute",json={"token":mom.token,"mute":True}).json()
    assert muted["mute"] is True
    assert client.get("/api/groups",params={"token":mom.token}).json()["groups"][0]["mute"] is True

    # 最后一名信任成员退出或降级会被拒,转移信任后可以操作。
    denied=client.request("DELETE",f"/api/groups/{group_id}/members/{trusted.id}",json={"token":trusted.token})
    assert denied.status_code==400 and "trust" in denied.json()["detail"]
    self_demote=client.post(f"/api/groups/{group_id}/members/{trusted.id}/trust",json={"token":trusted.token,"trusted":False})
    assert self_demote.status_code==400
    assert client.post(f"/api/groups/{group_id}/members/{mom.id}/trust",json={"token":trusted.token,"trusted":True}).status_code==200
    assert client.request("DELETE",f"/api/groups/{group_id}/members/{trusted.id}",json={"token":trusted.token}).json()["status"]=="left"


def test_concurrent_trust_changes_preserve_a_trusted_member(client, group):
    group_id, mom, trusted = group
    deps=client.app.state.deps
    deps.repos.member.set_trust(mom.id,True)
    barrier=Barrier(2)

    def demote(actor, target):
        barrier.wait()
        try:
            deps.groups.set_member_trust(group_id,target.id,False,actor.id)
            return "updated"
        except ValidationError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes=list(pool.map(lambda pair: demote(*pair),[(mom,trusted),(trusted,mom)]))

    assert sorted(outcomes)==["rejected","updated"]
    active_trusted=deps.conn.execute(
        "SELECT COUNT(*) FROM member WHERE group_id=? AND ended_at IS NULL AND user_id IS NOT NULL AND trusted=1",
        (group_id,),
    ).fetchone()[0]
    assert active_trusted==1


def test_parallel_reads_share_sqlite_connection_safely(client, group):
    group_id, mom, _ = group
    deps=client.app.state.deps

    def read_views(_):
        user=deps.repos.users.get_by_token(mom.token)
        memberships=deps.repos.member.list_for_user(user.id)
        groups=deps.repos.group.list_active_groups_for_user(user.id)
        alerts=deps.repos.alert.list_alerts_for_user_in_group(user.id,group_id)
        return user.id,len(memberships),len(groups),len(alerts)

    with ThreadPoolExecutor(max_workers=12) as pool:
        results=list(pool.map(read_views,range(48)))
    assert set(results)=={(mom.user_id,1,1,0)}


def test_static_views(client):
    for path in ("/", "/console", "/alert/1", "/join/ABC23467"):
        assert client.get(path).status_code==200
