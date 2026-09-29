"""Postgres integration coverage for multi-worker coordination and broadcasts."""
import asyncio
from dataclasses import replace

import pytest
from httpx import ASGITransport, AsyncClient

from homeshield.api.relations import build_relation_router
from homeshield.api.wecom import handle_kf_notify, wecom_poller
from homeshield.core.config import Settings
from homeshield.core.feedback import CorrectionService
from homeshield.core.pg_events import KF_PULL_CHANNEL
from homeshield.server import create_app
import homeshield.server as server_module

KFID = "wkAmultiworker"


class PollingChannel:
    api_ready = True

    def __init__(self):
        self.calls = 0
        self.cursors = []

    async def list_kf_accounts(self):
        return [{"open_kfid": KFID}]

    async def kf_sync_msg(self, open_kfid, cursor):
        assert open_kfid == KFID
        self.calls += 1
        self.cursors.append(cursor)
        return {"errcode": 0, "msg_list": [], "next_cursor": f"cursor-{self.calls}"}


async def _until(predicate, timeout=3):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("condition was not reached")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_two_app_pollers_elect_one_and_handoff_with_durable_cursor(deps, monkeypatch):
    settings = replace(deps.settings, wecom_corpid="", wecom_agent_id="")
    app_a, app_b = create_app(settings), create_app(settings)
    channels = [PollingChannel(), PollingChannel()]
    app_a.state.deps.wecom, app_b.state.deps.wecom = channels

    async def fast_poller(deps, verification):
        await wecom_poller(deps, verification, poll_interval=0.05, retry_interval=0.02)

    monkeypatch.setattr(server_module, "wecom_poller", fast_poller)
    async with app_a.router.lifespan_context(app_a):
        async with app_b.router.lifespan_context(app_b):
            await _until(lambda: sum(channel.calls for channel in channels) >= 2)
            await asyncio.sleep(0.06)
            leaders = [index for index, channel in enumerate(channels) if channel.calls]
            assert len(leaders) == 1
            leader_index = leaders[0]
            follower_index = 1 - leader_index
            leader_app, follower_app = (app_a, app_b) if leader_index == 0 else (app_b, app_a)
            durable_cursor = await leader_app.state.deps.repos.kf_cursor.get(KFID)
            assert durable_cursor

            leader_task = leader_app.state.poller_task
            leader_task.cancel()
            await asyncio.gather(leader_task, return_exceptions=True)
            await _until(lambda: channels[follower_index].calls >= 1)
            assert channels[follower_index].cursors[0] == durable_cursor


@pytest.mark.asyncio
async def test_follower_kf_callback_wakes_leader_poller_via_postgres(deps, monkeypatch):
    settings = replace(deps.settings, wecom_corpid="", wecom_agent_id="")
    app_a, app_b = create_app(settings), create_app(settings)
    channels = [PollingChannel(), PollingChannel()]
    app_a.state.deps.wecom, app_b.state.deps.wecom = channels

    async def slow_poller(deps, verification):
        # 兜底周期远超用例时长:第二次拉取只能来自跨进程 NOTIFY 唤醒
        await wecom_poller(deps, verification, poll_interval=30, retry_interval=0.02)

    monkeypatch.setattr(server_module, "wecom_poller", slow_poller)
    async with app_a.router.lifespan_context(app_a):
        async with app_b.router.lifespan_context(app_b):
            await _until(lambda: sum(channel.calls for channel in channels) >= 1)
            leader_index = 0 if channels[0].calls else 1
            follower_index = 1 - leader_index
            leader_deps = (app_a, app_b)[leader_index].state.deps
            follower_deps = (app_a, app_b)[follower_index].state.deps
            # 探测 NOTIFY 确认 leader 进程的 bridge 已 LISTEN,并消耗掉这次唤醒
            async with leader_deps.pool.connection() as conn:
                await conn.execute("SELECT pg_notify(%s, '')", (KF_PULL_CHANNEL,))
            await _until(lambda: channels[leader_index].calls >= 2)
            base_calls = channels[leader_index].calls
            # 回调落在 follower 进程:leader 只能经 NOTIFY 被唤醒,在兜底周期内再次拉取
            await handle_kf_notify(follower_deps)
            await _until(lambda: channels[leader_index].calls >= base_calls + 1, timeout=5)
            assert channels[follower_index].calls == 0


@pytest.mark.asyncio
async def test_two_app_instances_broadcast_sse_alert_once_on_postgres(deps):
    settings = replace(
        Settings.load(),
        database_url=deps.settings.database_url,
        mode="mock",
        wecom_corpid="",
        wecom_agent_id="",
        wecom_app_secret="",
        wecom_kf_secret="",
        wecom_token="",
        wecom_aes_key="",
        public_base_url="",
    )
    app_a, app_b = create_app(settings), create_app(settings)
    async with app_a.router.lifespan_context(app_a):
        async with app_b.router.lifespan_context(app_b):
            deps_a, deps_b = app_a.state.deps, app_b.state.deps
            queryer = await deps_a.repos.users.get_or_create("multiworker:queryer")
            protector = await deps_a.repos.users.get_or_create("multiworker:protector")
            invite = await deps_a.relations.issue_invite(protector.id, "妈妈")
            await deps_a.relations.join(queryer.openid, invite["code"])
            router_b = build_relation_router(
                deps_b, deps_b.verification,
                CorrectionService(deps_b.repos, deps_b.settings.correction_window_days),
            )
            stream_route = next(route for route in router_b.routes if route.path == "/api/stream")
            stream_response = await stream_route.endpoint(token=protector.token)
            stream = stream_response.body_iterator
            assert "event: ready" in await anext(stream)

            async with AsyncClient(
                transport=ASGITransport(app=app_a), base_url="http://worker-a"
            ) as client:
                response = await client.post(
                    "/api/query",
                    json={"token": queryer.token, "content": "别告诉家人，马上转账5万"},
                )
            assert response.status_code == 200
            verdict_id = response.json()["verdict_id"]

            try:
                alert_event = await asyncio.wait_for(anext(stream), timeout=3)
                assert "event: alert" in alert_event
                assert f'"verdict_id": {verdict_id}' in alert_event
            finally:
                await stream.aclose()
            async with deps_b.pool.connection() as conn:
                row = await (await conn.execute(
                    "SELECT COUNT(*) AS n FROM alert WHERE verdict_id=%s", (verdict_id,)
                )).fetchone()
            assert row["n"] == 1
