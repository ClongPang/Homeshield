"""告警送达:管线发布事实,本模块订阅并执行送达策略。

- AlertBroker:每群一个订阅队列,控制台 SSE 消费;
- AlertRouter:async handler,仅 dangerous 触发:全体成员落 alert 表、SSE 广播,
  模板消息推给已绑定微信的成员(后台化);
- 模板消息经 TemplateSender 端口发送,微信适配器由组合根注入。
"""
import asyncio
import logging
from typing import Protocol

from homeshield.core.events import EventBus, VerdictCompleted
from homeshield.core.models import Level
from homeshield.core.repo import Repos

logger = logging.getLogger(__name__)

# fire-and-forget 任务登记,防止被垃圾回收(asyncio 官方推荐做法)
_BACKGROUND_TASKS: set[asyncio.Task] = set()


class AlertBroker:
    def __init__(self, maxsize: int = 100):
        self._subs: dict[int, list[asyncio.Queue]] = {}
        self._maxsize = maxsize

    def subscribe(self, family_id: int) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._maxsize)
        self._subs.setdefault(family_id, []).append(q)
        return q

    def unsubscribe(self, family_id: int, q: asyncio.Queue) -> None:
        if q in self._subs.get(family_id, []):
            self._subs[family_id].remove(q)

    def publish(self, family_id: int, payload: dict) -> None:
        for q in self._subs.get(family_id, []):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass  # 打扰预算优先于积压


class TemplateSender(Protocol):
    async def send_template(self, openid: str, data: dict, url: str | None = None) -> None: ...


class AlertRouter:
    """async handler:EventBus await 它完成 DB/SSE(快),模板消息后台(慢)。"""

    def __init__(self, broker: AlertBroker, repos: Repos, base_url: str = ""):
        self.broker = broker
        self.repos = repos
        self.base_url = base_url.rstrip("/")
        self.wechat: TemplateSender | None = None  # 组合根注入,未配置则为 None

    async def __call__(self, event: VerdictCompleted) -> None:
        if event.verdict.level is not Level.DANGEROUS:
            return  # suspicious 只进周报
        members = self.repos.member.list_members(event.message.family_id)
        for m in members:
            self.repos.alert.insert(event.verdict_id, m.id)
        payload = {
            "verdict_id": event.verdict_id,
            "level": event.verdict.level.value,
            "summary": event.message.content[:50],
            "reply": event.reply,
        }
        self.broker.publish(event.message.family_id, payload)
        self._spawn_wechat(members, payload)

    def _spawn_wechat(self, members, payload: dict) -> None:
        if self.wechat is None:
            return
        openids = [m.openid for m in members if m.openid]
        if not openids:
            return
        task = asyncio.create_task(self._notify(openids, payload))
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)

    async def _notify(self, openids: list[str], payload: dict) -> None:
        for openid in openids:
            try:
                url = None
                member = self.repos.member.get_by_openid(openid)
                if self.base_url and member is not None and member.token:
                    url = f"{self.base_url}/alert/{payload['verdict_id']}?token={member.token}"
                await self.wechat.send_template(
                    openid,
                    {"thing1": {"value": payload["summary"][:20]}, "phrase1": {"value": "高危预警"}},
                    url=url,
                )
            except Exception:
                logger.warning("wechat template send failed openid=%s", openid, exc_info=True)


def wire_alerts(bus: EventBus, broker: AlertBroker, repos: Repos, base_url: str = "") -> AlertRouter:
    router = AlertRouter(broker, repos, base_url)
    bus.subscribe(VerdictCompleted, router)
    return router
