"""dangerous 告警按查询时相关群生成,按接收用户去重。"""
import asyncio
from copy import deepcopy
import logging
from typing import Protocol

from homeshield.core.events import EventBus, VerdictCompleted
from homeshield.core.models import Level, utc_timestamp
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
        if q in self._subs.get(user_id, []):
            self._subs[user_id].remove(q)

    def publish_alert(self, user_id: int, payload: dict) -> None:
        for q in self._subs.get(user_id, []):
            try:
                # 每个 SSE 订阅独立持有载荷；消费者会按当下权限过滤群列表。
                q.put_nowait(deepcopy(payload))
            except asyncio.QueueFull:
                pass


class TemplateSender(Protocol):
    async def send_template(self, openid: str, data: dict, url: str | None = None,
                            template_id: str | None = None) -> None: ...


class AlertRouter:
    def __init__(self, broker: AlertBroker, repos: Repos, base_url: str = "",
                 template_id: str = "", multi_template_id: str = ""):
        self.broker = broker
        self.repos = repos
        self.base_url = base_url.rstrip("/")
        self.template_id = template_id
        self.multi_template_id = multi_template_id
        self.wechat: TemplateSender | None = None

    async def __call__(self, event: VerdictCompleted) -> None:
        if event.verdict.level is not Level.DANGEROUS:
            return
        fanout = self.repos.alert.record_alerts_for_verdict(event.verdict_id,event.query_id)
        for recipient in fanout["recipients"]:
            user_id = recipient["user_id"]
            groups = recipient["groups"]
            group_ids = sorted({g["group_id"] for g in groups})
            group_names = []
            for gid in group_ids:
                group_names.append(next(g["name"] for g in groups if g["group_id"]==gid))
            self.broker.publish_alert(user_id,{
                "verdict_id":event.verdict_id,"level":"dangerous",
                "summary":event.message.content[:50],"group_ids":group_ids,
                "group_names":group_names,"delivered_at":utc_timestamp(),
            })
        if fanout["query_group_count"] > 1:
            names = fanout["generated_group_names"]
            event.queryer_notice = "已向" + "、".join(names) + "发出高危提醒" if names else "本次未通知群成员"
        elif fanout["generated_group_names"]:
            event.queryer_notice = "已为防护群发出高危提醒"
        else:
            event.queryer_notice = "本次未通知群成员"
        if self.wechat is not None:
            task = asyncio.create_task(self._send_alert_notifications(event.verdict_id,event.message.content,fanout["recipients"]))
            _BACKGROUND_TASKS.add(task)
            task.add_done_callback(_BACKGROUND_TASKS.discard)

    async def _send_alert_notifications(self, verdict_id: int, content: str, recipients: list[dict]) -> None:
        for recipient in recipients:
            user_id = recipient["user_id"]
            # 合成身份只用于演示与测试,永不触发真实微信推送。
            if recipient["openid"].startswith(("demo:", "test:")):
                continue
            context = self.repos.alert.get_push_context_for_user(verdict_id,user_id)
            if context is None:
                continue
            if context["active_group_count"] == 1:
                template_id = self.template_id
                data = {"thing1":{"value":_clip(content)},"phrase1":{"value":"高危预警"}}
            else:
                template_id = self.multi_template_id
                if not template_id:
                    logger.warning("multi-group template missing; skip openid=%s",context["openid"])
                    continue
                names = [g["name"] for g in context["groups"]]
                first = names[0]
                label = f"{first}等{len(names)}群" if len(names)>1 else first
                data = {"thing1":{"value":_clip(content)},"phrase1":{"value":"高危预警"},
                        "thing2":{"value":_clip(label)}}
            if not template_id:
                continue
            url = f"{self.base_url}/alert/{verdict_id}?token={context['token']}" if self.base_url else None
            try:
                await self.wechat.send_template(context["openid"],data,url=url,template_id=template_id)
            except Exception:
                logger.warning("wechat template send failed openid=%s",context["openid"],exc_info=True)


def _clip(text: str, limit: int = 20) -> str:
    """微信模板字段限长,截断补省略号。"""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def wire_alerts(bus: EventBus, broker: AlertBroker, repos: Repos, base_url: str = "",
                template_id: str = "", multi_template_id: str = "") -> AlertRouter:
    router = AlertRouter(broker,repos,base_url,template_id,multi_template_id)
    bus.subscribe(VerdictCompleted,router)
    return router
