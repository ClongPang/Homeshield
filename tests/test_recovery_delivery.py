"""Crash and replay acceptance for the durable query/outbound protocol."""
import asyncio

import pytest

from homeshield.core.errors import ValidationError
from homeshield.core.db import make_pool
from homeshield.core.deps import build_deps, initialize_deps
from homeshield.core.intake import ingest
from homeshield.core.models import ContentType, Message


class DeliveryChannel:
    api_ready = True

    def __init__(self):
        self.replies = []
        self.pushes = []
        self.reply_response = {"errcode": 0}
        self.push_response = {"errcode": 0}
        self.push_gate = None

    async def kf_send_msg(self, kfid, eid, text):
        self.replies.append((kfid, eid, text))
        return self.reply_response

    async def send_app_message(self, users, text):
        self.pushes.append((users, text))
        if self.push_gate is not None:
            await self.push_gate.wait()
        return self.push_response

    async def send_session_message_result(self, openid, text):
        self.pushes.append(([openid], text))
        if self.push_gate is not None:
            await self.push_gate.wait()
        return self.push_response


async def _wecom_user(deps, suffix="one"):
    user = await deps.repos.users.get_or_create(f"wxkf:recovery-{suffix}")
    return user


async def _claimed(deps, user, msg_id="recovery-message", kind="query"):
    return await deps.repos.query.insert(
        user.id, "text", "别告诉家人，马上转账五万" if kind == "query" else "收到",
        msg_id, kind, channel="wecom", open_kfid="kf-recovery", session_epoch=0,
    )


async def _expired(deps, query_id):
    await _execute(deps,
        "UPDATE query SET lease_until=now()-interval '1 second' WHERE id=%s", (query_id,)
    )


async def _execute(deps, statement, params=()):
    async with deps.pool.connection() as conn:
        await conn.execute(statement, params)


async def _outbound(deps, query_id, kind="reply"):
    return next(r for r in await deps.repos.outbound.for_query(query_id) if r["kind"] == kind)


async def _drain(deps):
    for _ in range(4):
        tasks = list(deps.recovery.tasks)
        if not tasks:
            return
        await asyncio.gather(*tasks)


async def test_claimed_query_recovers_once_without_reingest(deps):
    channel = DeliveryChannel()
    deps.wecom = channel
    user = await _wecom_user(deps)
    query_id = await _claimed(deps, user)
    await _expired(deps, query_id)
    await deps.recovery.sweep_once()
    await _drain(deps)
    query = await deps.repos.query.get(query_id)
    assert query["outcome_kind"] == "verdict"
    assert (await deps.repos.verdict.for_query(query_id)) is not None
    assert (await _outbound(deps, query_id))["state"] == "accepted"
    assert len(channel.replies) == 1
    await deps.recovery.sweep_once()
    await _drain(deps)
    assert len(channel.replies) == 1


async def test_four_workers_share_one_query_result_and_outbound_claim(deps):
    channel = DeliveryChannel()
    deps.wecom = channel
    user = await _wecom_user(deps)
    query_id = await _claimed(deps, user, "four-worker-claim")
    await _expired(deps, query_id)
    workers = [deps]
    extra_pools = []
    try:
        for _ in range(3):
            pool = make_pool(deps.settings.database_url)
            extra_pools.append(pool)
            worker = build_deps(deps.settings, pool=pool)
            worker.wecom = channel
            await initialize_deps(worker)
            workers.append(worker)
        await asyncio.gather(*(worker.recovery.sweep_once() for worker in workers))
        for _ in range(4):
            await asyncio.gather(*(
                asyncio.gather(*list(worker.recovery.tasks))
                for worker in workers if worker.recovery.tasks
            ))
        assert (await deps.repos.query.get(query_id))["outcome_kind"] == "verdict"
        assert (await _outbound(deps, query_id))["state"] == "accepted"
        assert len(channel.replies) == 1
        count = await (await deps.conn.execute(
            "SELECT COUNT(*) n FROM verdict WHERE query_id=%s", (query_id,)
        )).fetchone()
        assert count["n"] == 1
    finally:
        for worker in workers[1:]:
            await worker.recovery.close()
        for pool in extra_pools:
            await pool.close()


async def test_result_intents_commit_atomically(deps, relations, monkeypatch):
    protected, _, _ = relations
    intake = await ingest(deps.repos, user_id=protected.id, content="请转账")

    async def fail(*_args):
        raise RuntimeError("fault after alert insert")

    monkeypatch.setattr(deps.repos.outbound, "create_push", fail)
    with pytest.raises(RuntimeError, match="fault after alert"):
        await deps.pipeline.run(intake.message, intake.query_id)
    query = await deps.repos.query.get(intake.query_id)
    assert query["outcome_kind"] is None
    assert await deps.repos.verdict.for_query(intake.query_id) is None
    assert await deps.repos.outbound.for_query(intake.query_id) == []
    count = await (await deps.conn.execute("SELECT COUNT(*) n FROM alert")).fetchone()
    assert count["n"] == 0


async def test_web_verdict_also_creates_push_intent(deps, relations):
    protected, _, _ = relations
    result = await deps.verification.verify(user=protected, content="请转账", msg_id="web-push")
    query = await deps.repos.query.get(result.query_id)
    assert query["channel"] == "web" and query["outcome_kind"] == "verdict"
    rows = await deps.repos.outbound.for_query(result.query_id)
    assert [r["kind"] for r in rows] == ["push"]


async def test_lost_sse_event_does_not_lose_alert_or_push_intent(deps, monkeypatch):
    protected, protector, _ = await _relation(deps)

    async def fail_event(_event):
        raise RuntimeError("simulated notification loss")

    monkeypatch.setattr(deps.pipeline.bus, "publish", fail_event)
    result = await deps.verification.verify(user=protected, content="请转账")
    alerts = await deps.repos.alert.list_for_user(protector.id)
    assert len(alerts) == 1
    push = await _outbound(deps, result.query_id, "push")
    assert push["alert_id"] == alerts[0]["alert_id"] and push["state"] == "pending"


async def test_committed_result_is_dispatched_after_process_restart_without_rejudge(deps, monkeypatch):
    channel = DeliveryChannel()
    deps.wecom = channel
    user = await _wecom_user(deps)
    result = await deps.verification.verify(
        user=user, content="请转账", channel="wecom", msg_id="committed-before-crash",
        session_epoch=0, open_kfid="kf-recovery",
    )

    async def forbidden(_query):
        raise AssertionError("committed query must not be judged again")

    monkeypatch.setattr(deps.verification, "recover_query", forbidden)
    await deps.recovery.sweep_once()
    await _drain(deps)
    assert (await _outbound(deps, result.query_id))["state"] == "accepted"
    assert len(channel.replies) == 1


async def test_business_rejection_retries_then_accepts(deps):
    channel = DeliveryChannel()
    channel.reply_response = {"errcode": 95001}
    deps.wecom = channel
    user = await _wecom_user(deps)
    result = await deps.verification.verify(
        user=user, content="请转账", channel="wecom", msg_id="retry-reply",
        session_epoch=0, open_kfid="kf-recovery",
    )
    row = await _outbound(deps, result.query_id)
    await deps.recovery.dispatch(row["id"])
    failed = await _outbound(deps, result.query_id)
    assert failed["state"] == "pending" and failed["sent_at"] is None
    assert failed["last_error"] == "wecom_err_95001"
    channel.reply_response = {"errcode": 0}
    await _execute(deps, "UPDATE outbound SET next_attempt_at=now() WHERE id=%s", (row["id"],))
    await deps.recovery.dispatch(row["id"])
    accepted = await _outbound(deps, result.query_id)
    assert accepted["state"] == "accepted" and accepted["sent_at"] is not None
    assert accepted["lease_until"] is None


async def test_timeout_reaches_failed_and_window_code_is_classified(deps):
    channel = DeliveryChannel()
    channel.reply_response = {"errcode": 95002}
    deps.wecom = channel
    user = await _wecom_user(deps)
    result = await deps.verification.verify(
        user=user, content="请转账", channel="wecom", msg_id="window-reject",
        session_epoch=0, open_kfid="kf-recovery",
    )
    row = await _outbound(deps, result.query_id)
    await deps.recovery.dispatch(row["id"])
    assert (await _outbound(deps, result.query_id))["last_error"] == "wecom_reply_window_95002"

    async def timeout(*_args):
        raise TimeoutError("simulated network timeout")

    channel.kf_send_msg = timeout
    await _execute(deps,
        "UPDATE outbound SET attempts=%s,next_attempt_at=now() WHERE id=%s",
        (deps.settings.outbound_max_attempts - 1, row["id"]),
    )
    await deps.recovery.dispatch(row["id"])
    failed = await _outbound(deps, result.query_id)
    assert failed["state"] == "failed" and failed["sent_at"] is None
    assert failed["last_error"] == "TimeoutError"


async def test_api_accept_then_state_write_crash_can_duplicate(deps, monkeypatch):
    channel = DeliveryChannel()
    deps.wecom = channel
    user = await _wecom_user(deps)
    result = await deps.verification.verify(
        user=user, content="请转账", channel="wecom", msg_id="accept-crash",
        session_epoch=0, open_kfid="kf-recovery",
    )
    row = await _outbound(deps, result.query_id)
    original = deps.repos.outbound.finish
    interrupted = False

    async def finish(outbound_id, token, state, error=None, delay_seconds=0):
        nonlocal interrupted
        if state == "accepted" and not interrupted:
            interrupted = True
            raise RuntimeError("state write interrupted")
        return await original(outbound_id, token, state, error, delay_seconds)

    monkeypatch.setattr(deps.repos.outbound, "finish", finish)
    await deps.recovery.dispatch(row["id"])
    assert (await _outbound(deps, result.query_id))["state"] == "pending"
    await _execute(deps, "UPDATE outbound SET next_attempt_at=now() WHERE id=%s", (row["id"],))
    await deps.recovery.dispatch(row["id"])
    assert (await _outbound(deps, result.query_id))["state"] == "accepted"
    assert len(channel.replies) == 2


async def test_outbound_claim_is_exclusive_and_old_token_cannot_finish(deps):
    channel = DeliveryChannel()
    gate = asyncio.Event()
    channel.push_gate = gate
    deps.wecom = channel
    protected, protector, _ = await _relation(deps)
    await deps.repos.wecom_member.link(protector.id, "CorpRecovery")
    result = await deps.verification.verify(user=protected, content="请转账")
    row = await _outbound(deps, result.query_id, "push")
    first = asyncio.create_task(deps.recovery.dispatch(row["id"]))
    for _ in range(100):
        if channel.pushes:
            break
        await asyncio.sleep(0.01)
    await deps.recovery.dispatch(row["id"])
    assert len(channel.pushes) == 1
    leased = await _outbound(deps, result.query_id, "push")
    assert not await deps.repos.outbound.finish(row["id"], leased["lease_token"] - 1, "accepted")
    gate.set()
    await first
    assert (await _outbound(deps, result.query_id, "push"))["state"] == "accepted"


async def test_application_push_rejection_preserves_provisioning_failure_signal(deps):
    channel = DeliveryChannel()
    channel.push_response = {"errcode": 43004}
    deps.wecom = channel
    protected, protector, _ = await _relation(deps)
    await deps.repos.wecom_member.link(protector.id, "CorpRejected")
    result = await deps.verification.verify(user=protected, content="请转账")
    push = await _outbound(deps, result.query_id, "push")
    await deps.recovery.dispatch(push["id"])
    assert (await _outbound(deps, result.query_id, "push"))["state"] == "pending"
    member = await deps.repos.wecom_member.get_member(protector.id)
    assert member["last_fail_at"] is not None and "43004" in member["last_fail_reason"]


async def test_expired_final_lease_moves_to_failed_and_requeues(deps):
    user = await _wecom_user(deps)
    result = await deps.verification.verify(
        user=user, content="请转账", channel="wecom", msg_id="expired-last",
        session_epoch=0, open_kfid="kf-recovery",
    )
    row = await _outbound(deps, result.query_id)
    await _execute(deps,
        "UPDATE outbound SET state='leased',attempts=%s,lease_token=7,"
        "lease_until=now()-interval '1 second' WHERE id=%s",
        (deps.settings.outbound_max_attempts, row["id"]),
    )
    await deps.recovery.dispatch(row["id"])
    failed = await _outbound(deps, result.query_id)
    assert failed["state"] == "failed" and failed["last_error"] == "lease_expired_limit"
    assert await deps.repos.outbound.requeue(row["id"])
    queued = await _outbound(deps, result.query_id)
    assert queued["state"] == "pending" and queued["attempts"] == 0
    assert queued["lease_token"] == 8 and queued["last_error"] == "lease_expired_limit"
    # 再次耗尽时保留最后一次记录的具体错误类别,不回退到占位文案
    await _execute(deps,
        "UPDATE outbound SET state='leased',attempts=%s,lease_token=9,"
        "lease_until=now()-interval '1 second',last_error='wecom_err_95002' WHERE id=%s",
        (deps.settings.outbound_max_attempts, row["id"]),
    )
    await deps.recovery.dispatch(row["id"])
    failed_again = await _outbound(deps, result.query_id)
    assert failed_again["state"] == "failed" and failed_again["last_error"] == "wecom_err_95002"


async def test_stale_query_worker_cannot_commit_after_takeover(deps):
    user = await _wecom_user(deps)
    query_id = await _claimed(deps, user, "query-fence")
    await _expired(deps, query_id)
    recovered = await deps.repos.query.takeover(query_id, 300)
    message = Message(user_id=user.id, content_type=ContentType.TEXT,
                      content="请转账", channel="wecom", msg_id="query-fence")
    with pytest.raises(ValidationError, match="stale query claim"):
        await deps.pipeline.run(message, query_id, claim_token=1)
    assert await deps.repos.verdict.for_query(query_id) is None
    await deps.verification.recover_query(recovered)
    assert (await deps.repos.query.get(query_id))["outcome_kind"] == "verdict"


async def test_ack_and_degraded_results_reuse_durable_reply(deps):
    channel = DeliveryChannel()
    deps.wecom = channel
    user = await _wecom_user(deps)
    ack_id = await _claimed(deps, user, "ack-crash", kind="ack")
    await _expired(deps, ack_id)
    ack = await deps.repos.query.takeover(ack_id, 300)
    await deps.verification.recover_query(ack)
    await deps.recovery.dispatch((await _outbound(deps, ack_id))["id"])
    assert (await deps.repos.query.get(ack_id))["outcome_kind"] == "ack"
    assert await deps.repos.verdict.for_query(ack_id) is None
    degraded = await deps.verification.verify(
        user=user, content="DEGRADEME", content_type="image", channel="wecom",
        msg_id="degraded-crash", session_epoch=0, open_kfid="kf-recovery",
    )
    query = await deps.repos.query.get(degraded.query_id)
    assert query["outcome_kind"] == "degraded" and query["degraded_reply"]
    await deps.recovery.dispatch((await _outbound(deps, degraded.query_id))["id"])
    assert channel.replies[-1][2] == query["degraded_reply"]


async def test_cross_channel_msg_id_and_legacy_web_fallback(deps):
    web = await deps.repos.users.get_or_create("test:cross-channel-web")
    wecom = await _wecom_user(deps)
    web_result = await deps.verification.verify(user=web, content="请转账", msg_id="same-channel-key")
    wc_result = await deps.verification.verify(
        user=wecom, content="请转账", channel="wecom", msg_id="same-channel-key",
        session_epoch=0, open_kfid="kf-recovery",
    )
    assert web_result.query_id != wc_result.query_id
    await _execute(deps,
        "INSERT INTO query(user_id,content_type,content,msg_id,created_at,kind,channel) "
        "VALUES(%s,'text','历史网页','legacy-key',now(),'query','legacy')", (web.id,),
    )
    duplicate = await deps.verification.verify(user=web, content="请转账", msg_id="legacy-key")
    assert duplicate.duplicate


async def test_push_rechecks_relation_and_unresolved_query_is_visible(deps, caplog):
    channel = DeliveryChannel()
    deps.wecom = channel
    protected, protector, relation_id = await _relation(deps)
    result = await deps.verification.verify(user=protected, content="请转账")
    push = await _outbound(deps, result.query_id, "push")
    await deps.repos.relation.update(relation_id, protector.id, mute=True)
    await deps.recovery.dispatch(push["id"])
    skipped = await _outbound(deps, result.query_id, "push")
    assert skipped["state"] == "skipped" and skipped["last_error"] == "relation_muted"
    assert channel.pushes == []
    user = await _wecom_user(deps, "overdue")
    query_id = await _claimed(deps, user, "overdue-query")
    await _execute(deps,
        "UPDATE query SET created_at=now()-interval '20 minutes' WHERE id=%s", (query_id,)
    )
    overdue = await deps.repos.query.overdue_without_result()
    assert query_id in {row["id"] for row in overdue}
    await deps.recovery.sweep_once()
    assert f"wecom query overdue query_id={query_id}" in caplog.text
    assert "category=outcome_unresolved" in caplog.text


async def test_repeated_recovery_error_keeps_query_claim_and_logs_category(deps, monkeypatch, caplog):
    user = await _wecom_user(deps)
    query_id = await _claimed(deps, user, "recovery-repeated-failure")
    await _expired(deps, query_id)

    async def fail(_query):
        raise RuntimeError("simulated rebuild failure")

    monkeypatch.setattr(deps.verification, "recover_query", fail)
    await deps.recovery.sweep_once()
    await _drain(deps)
    query = await deps.repos.query.get(query_id)
    assert query["outcome_kind"] is None and query["claim_token"] == 2
    assert f"wecom query recovery failed query_id={query_id} category=RuntimeError" in caplog.text
    await _expired(deps, query_id)
    await deps.recovery.sweep_once()
    await _drain(deps)
    query = await deps.repos.query.get(query_id)
    assert query["outcome_kind"] is None and query["claim_token"] == 3
    assert f"wecom query overdue query_id={query_id}" in caplog.text


async def _relation(deps):
    protector = await deps.repos.users.get_or_create("wxkf:recovery-protector")
    protected = await deps.repos.users.get_or_create("wxkf:recovery-protected")
    invite = await deps.relations.issue_invite(protector.id, "妈妈")
    _, relation_id, _ = await deps.relations.join(protected.openid, invite["code"])
    return protected, protector, relation_id
