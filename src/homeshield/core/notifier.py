"""Dangerous verdict delivery through active directed relations."""
import asyncio
from copy import deepcopy
import logging
from typing import Protocol

from homeshield.core.events import EventBus, VerdictCompleted
from homeshield.core.models import Level
from homeshield.core.repo import Repos

logger = logging.getLogger(__name__)
_BACKGROUND_TASKS: set[asyncio.Task] = set()


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


class TemplateSender(Protocol):
    async def send_template(self, openid: str, data: dict, url: str | None = None,
                            template_id: str | None = None) -> None: ...


class AlertRouter:
    def __init__(self, broker: AlertBroker, repos: Repos, base_url: str = "", template_id: str = ""):
        self.broker, self.repos = broker, repos
        self.base_url = base_url.rstrip("/")
        self.template_id = template_id
        self.wechat: TemplateSender | None = None

    async def __call__(self, event: VerdictCompleted) -> None:
        if event.verdict.level is not Level.DANGEROUS: return
        fanout = self.repos.alert.record_alerts_for_verdict(event.verdict_id, event.query_id)
        active_recipients = []
        for recipient in fanout["recipients"]:
            current = self.repos.alert.event_context(recipient["alert_id"])
            if current is None: continue
            active_recipients.append(recipient)
            self.broker.publish_alert(recipient["user_id"], {
                "alert_id": recipient["alert_id"], "verdict_id": event.verdict_id,
                "relation_id": recipient["relation_id"], "name_at_alert": recipient["name_at_alert"],
                "level": "dangerous", "summary": event.message.content[:50],
                "delivered_at": recipient["delivered_at"],
            })
        names = [r["inverse_name"] or f"联防者 #{r['relation_id']}" for r in active_recipients]
        event.queryer_notice = "高危提醒已加入" + "、".join(names) + "的提醒列表" if names else ""
        if self.wechat and self.template_id and active_recipients:
            task = asyncio.create_task(self._send_alert_notifications(active_recipients, event.message.content))
            _BACKGROUND_TASKS.add(task)
            task.add_done_callback(_BACKGROUND_TASKS.discard)

    async def _send_alert_notifications(self, recipients: list[dict], content: str) -> None:
        for alert in recipients:
            if alert["openid"].startswith(("demo:", "test:")): continue
            context = self.repos.alert.push_context(alert["alert_id"])
            if context is None: continue
            data = {"thing1": {"value": _clip(content)}, "phrase1": {"value": "高危预警"},
                    "thing2": {"value": _clip(context["name_at_alert"])} }
            url = f"{self.base_url}/alert/{context['alert_id']}?token={context['token']}" if self.base_url else None
            try: await self.wechat.send_template(context["openid"], data, url=url, template_id=self.template_id)
            except Exception: logger.warning("wechat template send failed openid=%s", context["openid"], exc_info=True)


def _clip(text: str, limit: int = 20) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def wire_alerts(bus: EventBus, broker: AlertBroker, repos: Repos, base_url: str = "",
                template_id: str = "") -> AlertRouter:
    router = AlertRouter(broker, repos, base_url, template_id)
    bus.subscribe(VerdictCompleted, router)
    return router
