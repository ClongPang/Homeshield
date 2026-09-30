"""Query alert delivery through active directed relations.

扇出由查询事件触发、不按判定等级过滤:查询本身就说明用户起了疑心,
判定漏报时不能让家人被系统挡在外面;等级只影响提醒措辞。
"""
import asyncio
from copy import deepcopy

from homeshield.core.events import EventBus, VerdictCompleted
from homeshield.core.models import Level
from homeshield.core.repo import Repos

ALERT_TEXTS: dict[Level, str] = {
    Level.DANGEROUS: "⚠️ 高危预警：你护着的「{name}」查询了可疑消息，请尽快联系TA核实。",
    Level.SUSPICIOUS: "⚠️ 留意：你护着的「{name}」查了一条消息，判定为可疑，建议联系TA核实。",
    Level.SAFE: "你护着的「{name}」查证了一条消息，未发现典型骗术特征，但是不排除欺诈的可能。",
}


class AlertBroker:
    def __init__(self, maxsize: int = 100):
        self._subs: dict[int, list[asyncio.Queue]] = {}
        self._maxsize = maxsize

    def subscribe(self, user_id: int) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._maxsize)
        self._subs.setdefault(user_id, []).append(q)
        return q

    def unsubscribe(self, user_id: int, q: asyncio.Queue) -> None:
        if q in self._subs.get(user_id, []): self._subs[user_id].remove(q)

    def publish_alert(self, user_id: int, payload: dict) -> None:
        for q in self._subs.get(user_id, []):
            try: q.put_nowait(deepcopy(payload))
            except asyncio.QueueFull: pass


class AlertRouter:
    def __init__(self, broker: AlertBroker, repos: Repos):
        self.broker, self.repos = broker, repos

    async def __call__(self, event: VerdictCompleted) -> None:
        # Durable alert and outbound rows were committed with the verdict, and the
        # queryer notice was persisted on the query in the same transaction.
        # 本路由只负责 SSE 扇出,不重算任何提交时已确定的事实。
        recipients = await self.repos.alert.existing_for_verdict(event.verdict_id)
        for recipient in recipients:
            self.broker.publish_alert(recipient["user_id"], {
                "alert_id": recipient["alert_id"], "verdict_id": event.verdict_id,
                "relation_id": recipient["relation_id"], "name_at_alert": recipient["name_at_alert"],
                "level": event.verdict.level.value, "summary": event.message.content[:50],
                "delivered_at": recipient["delivered_at"],
            })

def wire_alerts(bus: EventBus, broker: AlertBroker, repos: Repos) -> AlertRouter:
    router = AlertRouter(broker, repos)
    bus.subscribe(VerdictCompleted, router)
    return router
