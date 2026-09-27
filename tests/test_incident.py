import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from homeshield.core import messages
from homeshield.core.config import Settings
from homeshield.core.deps import build_deps
from homeshield.core.models import Level


def _verify(deps, member, text, msg_id=None):
    return asyncio.run(deps.verification.verify(member=member, content=text, msg_id=msg_id))


def test_partition_ack_timeout_reset(deps, group):
    member = deps.repos.member.get(group[1])
    one = _verify(deps, member, "这个链接是真的吗")
    first = deps.repos.query.get(one.query_id)
    assert first["incident_id"] is not None
    ack = _verify(deps, member, "谢谢")
    assert ack.kind == "ack" and ack.result.reply == messages.ACK_QUERY_REPLY
    assert deps.repos.query.get(ack.query_id)["incident_id"] is None
    assert deps.conn.execute("SELECT COUNT(*) FROM verdict WHERE query_id=?", (ack.query_id,)).fetchone()[0] == 0
    assert deps.repos.incident.list_for_user(member.user_id)[0]["last_query_at"] == first["created_at"]
    two = _verify(deps, member, "这是客服发的吗")
    assert deps.repos.query.get(two.query_id)["incident_id"] == first["incident_id"]
    deps.conn.execute("UPDATE query SET created_at=created_at+21601 WHERE id=?", (two.query_id,))
    deps.conn.execute("UPDATE incident SET last_query_at=last_query_at-21601 WHERE id=?", (first["incident_id"],))
    deps.conn.commit()
    three = _verify(deps, member, "这个号码是真的吗")
    assert deps.repos.query.get(three.query_id)["incident_id"] != first["incident_id"]
    assert deps.repos.incident.list_for_user(member.user_id)[1]["close_reason"] == "timeout"
    deps.repos.incident.close_open_incident(member.user_id)
    deps.repos.incident.close_open_incident(member.user_id)
    assert deps.repos.incident.list_for_user(member.user_id)[0]["close_reason"] == "explicit"


def test_duplicate_ack_and_partition_failure(deps, group, monkeypatch):
    member = deps.repos.member.get(group[1])
    first = _verify(deps, member, "OK", "ack-1")
    second = _verify(deps, member, "OK", "ack-1")
    assert second.duplicate and second.query_id == first.query_id
    monkeypatch.setattr(deps.repos.incident, "attach_query_to_incident", lambda *a: (_ for _ in ()).throw(RuntimeError()))
    outcome = _verify(deps, member, "别告诉家人，现在转账")
    assert outcome.result.verdict.level is Level.DANGEROUS
    assert deps.repos.query.get(outcome.query_id)["incident_id"] is None


def test_concurrent_first_queries_one_open_incident(deps, group):
    member = deps.repos.member.get(group[1])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda n: _verify(deps, member, f"查询消息{n}"), (1, 2)))
    ids = {deps.repos.query.get(r.query_id)["incident_id"] for r in results}
    assert len(ids) == 1
    assert deps.conn.execute("SELECT COUNT(*) FROM incident WHERE closed_at IS NULL").fetchone()[0] == 1


def test_out_of_order_attach_does_not_rewind_idle_clock(deps, group):
    member = deps.repos.member.get(group[1])
    memberships = deps.repos.member.list_for_user(member.user_id)
    one = deps.repos.query.insert(member.user_id, memberships, "text", "较早查询", None)
    two = deps.repos.query.insert(member.user_id, memberships, "text", "较晚查询", None)
    deps.conn.execute("UPDATE query SET created_at=10000 WHERE id=?", (one,))
    deps.conn.execute("UPDATE query SET created_at=10100 WHERE id=?", (two,))
    deps.conn.commit()
    inc = deps.repos.incident.attach_query_to_incident(two, member.user_id, 21600)
    deps.repos.incident.attach_query_to_incident(one, member.user_id, 21600)
    assert deps.repos.incident.list_for_user(member.user_id)[0]["last_query_at"] == 10100
    assert deps.repos.query.get(one)["incident_id"] == inc


def test_reset_epoch_blocks_delayed_query_and_retry_is_idempotent(deps, group):
    member = deps.repos.member.get(group[1])
    epoch = deps.repos.incident.current_epoch(member.user_id)
    assert deps.repos.incident.close_open_incident(member.user_id, msg_id="reset-1")
    assert deps.repos.incident.current_epoch(member.user_id) == epoch + 1
    delayed = asyncio.run(deps.verification.verify(
        member=member, content="先前收到的转账请求", session_epoch=epoch,
    ))
    assert delayed.result.verdict is not None
    assert deps.repos.query.get(delayed.query_id)["incident_id"] is None
    current = _verify(deps, member, "现在收到的新请求")
    current_incident = deps.repos.query.get(current.query_id)["incident_id"]
    assert current_incident is not None
    assert not deps.repos.incident.close_open_incident(member.user_id, msg_id="reset-1")
    assert deps.repos.query.get(current.query_id)["incident_id"] == current_incident
    assert deps.repos.incident.list_for_user(member.user_id)[0]["closed_at"] is None


def test_very_old_query_cannot_attach_to_new_open_incident(deps, group):
    member = deps.repos.member.get(group[1])
    memberships = deps.repos.member.list_for_user(member.user_id)
    old = deps.repos.query.insert(member.user_id, memberships, "text", "迟到查询", None)
    current = deps.repos.query.insert(member.user_id, memberships, "text", "当前查询", None)
    deps.conn.execute("UPDATE query SET created_at=10000 WHERE id=?", (old,))
    deps.conn.execute("UPDATE query SET created_at=40000 WHERE id=?", (current,))
    deps.conn.commit()
    incident_id = deps.repos.incident.attach_query_to_incident(current, member.user_id, 21600)
    assert deps.repos.incident.attach_query_to_incident(old, member.user_id, 21600) is None
    assert deps.repos.query.get(old)["incident_id"] is None
    assert deps.repos.incident.list_for_user(member.user_id)[0]["id"] == incident_id


def test_six_hour_idle_boundary_is_inclusive(deps, group):
    member = deps.repos.member.get(group[1])
    memberships = deps.repos.member.list_for_user(member.user_id)
    ids = [deps.repos.query.insert(member.user_id, memberships, "text", f"q{i}", None)
           for i in range(3)]
    for query_id, when in zip(ids, (10000, 31600, 53201)):
        deps.conn.execute("UPDATE query SET created_at=? WHERE id=?", (when, query_id))
    deps.conn.commit()
    first = deps.repos.incident.attach_query_to_incident(ids[0], member.user_id, 21600)
    assert deps.repos.incident.attach_query_to_incident(ids[1], member.user_id, 21600) == first
    assert deps.repos.incident.attach_query_to_incident(ids[2], member.user_id, 21600) != first
    assert deps.repos.incident.list_for_user(member.user_id)[1]["close_reason"] == "timeout"


def test_reset_in_other_connection_cannot_pass_epoch_check_mid_attach(tmp_path, monkeypatch):
    settings = Settings(mode="mock", db_path=str(tmp_path / "shared.db"))
    first = build_deps(settings)
    group_id = first.repos.group.create("测试群")
    member_id = first.repos.member.add(group_id, "查询者", openid="o1")
    user_id = first.repos.member.get(member_id).user_id
    second = build_deps(settings)
    query_id = first.repos.query.insert(
        user_id, first.repos.member.list_for_user(user_id), "text", "查询", "q1"
    )
    epoch = first.repos.incident.current_epoch(user_id)
    checked = Event()
    release = Event()
    reset_done = Event()
    original = first.repos.incident.current_epoch

    def paused_epoch(uid):
        value = original(uid)
        checked.set()
        assert release.wait(2)
        return value

    monkeypatch.setattr(first.repos.incident, "current_epoch", paused_epoch)

    def reset():
        # 直接使用第二连接绕开进程内 WRITE_LOCK,模拟另一工作进程。
        with second.conn:
            second.conn.execute("UPDATE user SET session_epoch=session_epoch+1 WHERE id=?", (user_id,))
            second.conn.execute(
                "UPDATE incident SET closed_at=99999,close_reason='explicit' "
                "WHERE user_id=? AND closed_at IS NULL", (user_id,),
            )
        reset_done.set()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            attach_future = pool.submit(first.repos.incident.attach_query_to_incident,
                                        query_id, user_id, 21600, epoch)
            assert checked.wait(2)
            reset_future = pool.submit(reset)
            assert not reset_done.wait(0.1)
            release.set()
            attach_future.result(timeout=2)
            reset_future.result(timeout=2)
        assert second.repos.incident.list_for_user(user_id)[0]["close_reason"] == "explicit"
    finally:
        release.set()
        first.conn.close()
        second.conn.close()
