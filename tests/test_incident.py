import asyncio


from homeshield.core import messages
from homeshield.core.config import Settings
from homeshield.core.deps import build_deps
from homeshield.core.models import Level


async def _verify(deps, user, text, msg_id=None):
    return await deps.verification.verify(user=user, content=text, msg_id=msg_id)


async def test_partition_ack_timeout_reset(deps, relations):
    user = relations[0]
    one = await _verify(deps, user, "这个链接是真的吗")
    first = await deps.repos.query.get(one.query_id)
    assert first["incident_id"] is not None
    ack = await _verify(deps, user, "谢谢")
    assert ack.kind == "ack" and ack.result.reply == messages.ACK_QUERY_REPLY
    assert (await deps.repos.query.get(ack.query_id))["incident_id"] is None
    assert (await (await deps.conn.execute('SELECT COUNT(*) FROM verdict WHERE query_id=%s', (ack.query_id,))).fetchone())[0] == 0
    assert (await deps.repos.incident.list_for_user(user.id))[0]["last_query_at"] == first["created_at"]
    two = await _verify(deps, user, "这是客服发的吗")
    assert (await deps.repos.query.get(two.query_id))["incident_id"] == first["incident_id"]
    await deps.conn.execute("UPDATE query SET created_at=created_at+interval '21601 seconds' WHERE id=%s", (two.query_id,))
    await deps.conn.execute("UPDATE incident SET last_query_at=last_query_at-interval '21601 seconds' WHERE id=%s", (first["incident_id"],))
    await deps.conn.commit()
    three = await _verify(deps, user, "这个号码是真的吗")
    assert (await deps.repos.query.get(three.query_id))["incident_id"] != first["incident_id"]
    assert (await deps.repos.incident.list_for_user(user.id))[1]["close_reason"] == "timeout"
    await deps.repos.incident.close_open_incident(user.id)
    await deps.repos.incident.close_open_incident(user.id)
    assert (await deps.repos.incident.list_for_user(user.id))[0]["close_reason"] == "explicit"


async def test_duplicate_ack_and_partition_failure(deps, relations, monkeypatch):
    user = relations[0]
    first = await _verify(deps, user, "OK", "ack-1")
    second = await _verify(deps, user, "OK", "ack-1")
    assert second.duplicate and second.query_id == first.query_id
    monkeypatch.setattr(deps.repos.incident, "attach_query_to_incident", lambda *a: (_ for _ in ()).throw(RuntimeError()))
    outcome = await _verify(deps, user, "别告诉家人，现在转账")
    assert outcome.result.verdict.level is Level.DANGEROUS
    assert (await deps.repos.query.get(outcome.query_id))["incident_id"] is None


async def test_concurrent_first_queries_one_open_incident(deps, relations):
    user = relations[0]
    results = await asyncio.gather(*(_verify(deps, user, f"查询消息{n}") for n in (1, 2)))
    ids = {(await deps.repos.query.get(r.query_id))["incident_id"] for r in results}
    assert len(ids) == 1
    assert (await (await deps.conn.execute("SELECT COUNT(*) FROM incident WHERE closed_at IS NULL")).fetchone())[0] == 1


async def test_out_of_order_attach_does_not_rewind_idle_clock(deps, relations):
    user = relations[0]
    one = await deps.repos.query.insert(user.id, "text", "较早查询", None)
    two = await deps.repos.query.insert(user.id, "text", "较晚查询", None)
    await deps.conn.execute('UPDATE query SET created_at=to_timestamp(10000) WHERE id=%s', (one,))
    await deps.conn.execute('UPDATE query SET created_at=to_timestamp(10100) WHERE id=%s', (two,))
    await deps.conn.commit()
    inc = await deps.repos.incident.attach_query_to_incident(two, user.id, 21600)
    await deps.repos.incident.attach_query_to_incident(one, user.id, 21600)
    assert (await deps.repos.incident.list_for_user(user.id))[0]["last_query_at"] == 10100
    assert (await deps.repos.query.get(one))["incident_id"] == inc


async def test_reset_epoch_blocks_delayed_query_and_retry_is_idempotent(deps, relations):
    user = relations[0]
    epoch = await deps.repos.incident.current_epoch(user.id)
    assert await deps.repos.incident.close_open_incident(user.id, msg_id="reset-1")
    assert await deps.repos.incident.current_epoch(user.id) == epoch + 1
    delayed = await deps.verification.verify(
        user=user, content="先前收到的转账请求", session_epoch=epoch,
    )
    assert delayed.result.verdict is not None
    assert (await deps.repos.query.get(delayed.query_id))["incident_id"] is None
    current = await _verify(deps, user, "现在收到的新请求")
    current_incident = (await deps.repos.query.get(current.query_id))["incident_id"]
    assert current_incident is not None
    assert not await deps.repos.incident.close_open_incident(user.id, msg_id="reset-1")
    assert (await deps.repos.query.get(current.query_id))["incident_id"] == current_incident
    assert (await deps.repos.incident.list_for_user(user.id))[0]["closed_at"] is None


async def test_very_old_query_cannot_attach_to_new_open_incident(deps, relations):
    user = relations[0]
    old = await deps.repos.query.insert(user.id, "text", "迟到查询", None)
    current = await deps.repos.query.insert(user.id, "text", "当前查询", None)
    await deps.conn.execute('UPDATE query SET created_at=to_timestamp(10000) WHERE id=%s', (old,))
    await deps.conn.execute('UPDATE query SET created_at=to_timestamp(40000) WHERE id=%s', (current,))
    await deps.conn.commit()
    incident_id = await deps.repos.incident.attach_query_to_incident(current, user.id, 21600)
    assert await deps.repos.incident.attach_query_to_incident(old, user.id, 21600) is None
    assert (await deps.repos.query.get(old))["incident_id"] is None
    assert (await deps.repos.incident.list_for_user(user.id))[0]["id"] == incident_id


async def test_six_hour_idle_boundary_is_inclusive(deps, relations):
    user = relations[0]
    ids = [await deps.repos.query.insert(user.id, "text", f"q{i}", None)
           for i in range(3)]
    for query_id, when in zip(ids, (10000, 31600, 53201)):
        await deps.conn.execute('UPDATE query SET created_at=to_timestamp(%s) WHERE id=%s', (when, query_id))
    await deps.conn.commit()
    first = await deps.repos.incident.attach_query_to_incident(ids[0], user.id, 21600)
    assert await deps.repos.incident.attach_query_to_incident(ids[1], user.id, 21600) == first
    assert await deps.repos.incident.attach_query_to_incident(ids[2], user.id, 21600) != first
    assert (await deps.repos.incident.list_for_user(user.id))[1]["close_reason"] == "timeout"


async def test_reset_in_other_connection_cannot_pass_epoch_check_mid_attach(tmp_path, monkeypatch):
    settings = Settings.load()
    settings = Settings(mode="mock", database_url=settings.database_url, wecom_corpid="", wecom_agent_id="",
                        wecom_app_secret="", wecom_kf_secret="", wecom_token="", wecom_aes_key="")
    first = build_deps(settings)
    await first.pool.open(wait=True)
    user_id = (await first.repos.users.get_or_create("o1")).id
    second = build_deps(settings)
    query_id = await first.repos.query.insert(user_id, "text", "查询", "q1")
    epoch = await first.repos.incident.current_epoch(user_id)
    checked = asyncio.Event()
    release = asyncio.Event()
    reset_done = asyncio.Event()
    original = first.repos.incident.current_epoch

    async def paused_epoch(uid):
        value = await original(uid)
        checked.set()
        await asyncio.wait_for(release.wait(), timeout=2)
        return value

    monkeypatch.setattr(first.repos.incident, "current_epoch", paused_epoch)

    async def reset():
        async with second.pool.connection() as conn, conn.transaction():
            await conn.execute('UPDATE "user" SET session_epoch=session_epoch+1 WHERE id=%s', (user_id,))
            await conn.execute(
                "UPDATE incident SET closed_at=to_timestamp(99999),close_reason='explicit' "
                'WHERE user_id=%s AND closed_at IS NULL', (user_id,),
            )
        reset_done.set()

    try:
        await second.pool.open(wait=True)
        attach_task = asyncio.create_task(first.repos.incident.attach_query_to_incident(
            query_id, user_id, 21600, epoch))
        await asyncio.wait_for(checked.wait(), timeout=2)
        reset_task = asyncio.create_task(reset())
        await asyncio.sleep(0.1)
        assert not reset_done.is_set()
        release.set()
        await asyncio.wait_for(attach_task, timeout=2)
        await asyncio.wait_for(reset_task, timeout=2)
        assert (await second.repos.incident.list_for_user(user_id))[0]["close_reason"] == "explicit"
    finally:
        release.set()
        await first.pool.close()
        await second.pool.close()
