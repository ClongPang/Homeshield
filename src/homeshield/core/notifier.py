"""Query alert delivery through active directed relations.

扇出由查询事件触发、不按判定等级过滤:查询本身就说明用户起了疑心,
判定漏报时不能让家人被系统挡在外面;等级只影响提醒措辞。
"""
import asyncio
from copy import deepcopy
import logging
from typing import Protocol

from homeshield.core.events import EventBus, VerdictCompleted
from homeshield.core.models import Level
from homeshield.core.repo import Repos

logger = logging.getLogger(__name__)
_BACKGROUND_TASKS: set[asyncio.Task] = set()

ALERT_TEXTS: dict[Level, str] = {
    Level.DANGEROUS: "⚠️ 高危预警：你护着的「{name}」查询了可疑消息，请尽快联系TA核实。",
    Level.SUSPICIOUS: "⚠️ 留意：你护着的「{name}」查了一条消息，判定为可疑，建议联系TA核实。",
    Level.SAFE: "你护着的「{name}」查证了一条消息，未发现典型骗术特征，但是不排除欺诈的可能。",
}


def _discard_task(task: asyncio.Task) -> None:
    # 集合持引用防任务被 GC;异常在此收口,不靠解释器 never-retrieved 告警
    if not task.cancelled() and task.exception() is not None:
        logger.warning("background alert delivery failed", exc_info=task.exception())
    _BACKGROUND_TASKS.discard(task)


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


class AppMessageSender(Protocol):
    async def send_app_message(self, corp_userids: list[str], text: str) -> None: ...


class AlertRouter:
    def __init__(self, broker: AlertBroker, repos: Repos, base_url: str = ""):
        self.broker, self.repos = broker, repos
        self.base_url = base_url.rstrip("/")
        self.wecom: AppMessageSender | None = None

    async def __call__(self, event: VerdictCompleted) -> None:
        fanout = self.repos.alert.record_alerts_for_verdict(event.verdict_id, event.query_id)
        active_recipients = []
        for recipient in fanout["recipients"]:
            current = self.repos.alert.event_context(recipient["alert_id"])
            if current is None: continue
            active_recipients.append(recipient)
            self.broker.publish_alert(recipient["user_id"], {
                "alert_id": recipient["alert_id"], "verdict_id": event.verdict_id,
                "relation_id": recipient["relation_id"], "name_at_alert": recipient["name_at_alert"],
                "level": event.verdict.level.value, "summary": event.message.content[:50],
                "delivered_at": recipient["delivered_at"],
            })
        names = [r["inverse_name"] or f"联防者 #{r['relation_id']}" for r in active_recipients]
        event.queryer_notice = "查询提醒已加入" + "、".join(names) + "的提醒列表" if names else ""
        if self.wecom and active_recipients:
            task = asyncio.create_task(self._send_wecom_alerts(active_recipients, event.verdict.level))
            _BACKGROUND_TASKS.add(task)
            task.add_done_callback(_discard_task)

    async def _send_wecom_alerts(self, recipients: list[dict], level: Level) -> None:
        """应用消息 → 微信插件;仅触达已登记企微成员映射的联防者。"""
        for alert in recipients:
            corp_userid = self.repos.wecom_member.get(alert["user_id"])
            context = self.repos.alert.push_context(alert["alert_id"])
            if context is None:
                continue
            detail = f"{self.base_url}/alert/{context['alert_id']}?token={context['token']}" if self.base_url else ""
            text = ALERT_TEXTS[level].format(name=context["name_at_alert"]) \
                + (f"\n详情：{detail}" if detail else "")
            if corp_userid:
                try:
                    await self.wecom.send_app_message([corp_userid], text)
                except Exception:
                    logger.warning("wecom app message failed corp=%s", corp_userid, exc_info=True)
            elif alert["openid"].startswith("wxkf:"):
                # 未登记成员映射的 wxkf 联防者:回落其客服会话(48h 窗口内 best-effort)
                try:
                    await self.wecom.send_session_message(alert["openid"], text)
                except Exception:
                    logger.warning("wecom session alert fallback failed openid=%s",
                                   alert["openid"], exc_info=True)


def wire_alerts(bus: EventBus, broker: AlertBroker, repos: Repos, base_url: str = "") -> AlertRouter:
    router = AlertRouter(broker, repos, base_url)
    bus.subscribe(VerdictCompleted, router)
    return router
