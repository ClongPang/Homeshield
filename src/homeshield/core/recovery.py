"""Durable query recovery and at-least-once outbound delivery."""
import asyncio
import logging
from collections.abc import Callable
from typing import Protocol

from homeshield.core import messages
from homeshield.core.config import Settings
from homeshield.core.errors import ValidationError
from homeshield.core.models import Level
from homeshield.core.notifier import ALERT_TEXTS
from homeshield.core.repo import Repos
from homeshield.core.reply import add_delivery_notice
from homeshield.core.verification import VerificationService

logger = logging.getLogger(__name__)
_BACKOFF = (5, 30, 120, 300)


class DeliveryChannel(Protocol):
    """持久派发对通道适配器的最小契约。所有方法必须保留平台错误码
    (errcode/fail_list):布尔近似无法区分窗口类拒绝,契约里不允许出现。"""

    async def kf_send_msg(self, open_kfid: str, touser: str, text: str) -> dict: ...
    async def send_app_message(self, corp_userids: list[str], text: str) -> dict: ...
    async def send_session_message_result(self, openid: str, text: str) -> dict | None: ...


class DeliveryFailure(Exception):
    def __init__(self, category: str):
        self.category = category[:100]
        super().__init__(self.category)


def _accepted(response) -> bool:
    return isinstance(response, dict) and response.get("errcode") == 0 and not response.get("fail_list")


def _failure_category(response) -> str:
    if not isinstance(response, dict):
        return "empty_or_invalid_response"
    code = response.get("errcode")
    if code is None:
        return "missing_errcode"
    if code == 95002:
        return "wecom_reply_window_95002"
    if code == 95018:
        return "wecom_session_or_window_95018"
    return f"wecom_err_{code}" if not response.get("fail_list") else f"wecom_err_{code}_partial"


class RecoveryService:
    def __init__(self, repos: Repos, verification: VerificationService, settings: Settings,
                 channel: Callable[[], DeliveryChannel | None]):
        self.repos = repos
        self.verification = verification
        self.settings = settings
        self.channel = channel
        self.tasks: set[asyncio.Task] = set()
        self._warned_queries: set[int] = set()
        self._last_backlog_warning: tuple | None = None

    def schedule(self, outbound_id: int) -> None:
        task = asyncio.create_task(self.dispatch(outbound_id))
        self.tasks.add(task)
        task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task) -> None:
        self.tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error("recovery task failed", exc_info=task.exception())

    async def dispatch_query(self, query_id: int, *, wait_reply: bool) -> None:
        rows = await self.repos.outbound.for_query(query_id)
        reply = next((row for row in rows if row["kind"] == "reply"), None)
        pushes = [row for row in rows if row["kind"] == "push"]
        if reply is not None and wait_reply:
            reply_task = asyncio.create_task(self.dispatch(int(reply["id"])))
            for row in pushes:
                self.schedule(int(row["id"]))
            await reply_task
        else:
            for row in rows:
                self.schedule(int(row["id"]))

    async def dispatch(self, outbound_id: int) -> None:
        row = await self.repos.outbound.claim(
            outbound_id, self.settings.outbound_lease_seconds,
            self.settings.outbound_max_attempts,
        )
        if row is None:
            return
        if row["state"] == "failed":
            logger.error("outbound failed id=%s reason=%s", outbound_id, row["last_error"])
            return
        token = int(row["lease_token"])
        try:
            if not await self.repos.outbound.owns_lease(outbound_id, token):
                return
            if row["kind"] == "reply":
                await self._send_reply(row)
            else:
                skip_reason = await self._send_push(row)
                if skip_reason:
                    await self.repos.outbound.finish(outbound_id, token, "skipped", skip_reason)
                    return
            await self.repos.outbound.finish(outbound_id, token, "accepted")
        except asyncio.CancelledError:
            raise  # lease expiry, rather than an unrecorded in-memory retry, drives recovery
        except Exception as exc:
            category = exc.category if isinstance(exc, DeliveryFailure) else type(exc).__name__
            attempts = int(row["attempts"])
            if attempts >= self.settings.outbound_max_attempts:
                changed = await self.repos.outbound.finish(outbound_id, token, "failed", category)
                if changed:
                    logger.error("outbound failed id=%s kind=%s reason=%s", outbound_id, row["kind"], category)
            else:
                delay = _BACKOFF[min(attempts - 1, len(_BACKOFF) - 1)]
                await self.repos.outbound.finish(outbound_id, token, "pending", category, delay)

    async def _send_reply(self, outbound: dict) -> None:
        query = await self.repos.query.get(int(outbound["query_id"]))
        if query is None or query["channel"] != "wecom" or query["outcome_kind"] is None:
            raise DeliveryFailure("reply_result_missing")
        if int(query["user_id"]) != int(outbound["to_user_id"]):
            raise DeliveryFailure("reply_recipient_mismatch")
        user = await self.repos.users.get(int(outbound["to_user_id"]))
        if user is None or not user.openid.startswith("wxkf:"):
            raise DeliveryFailure("reply_identity_missing")
        if query["outcome_kind"] == "ack":
            content = messages.ACK_QUERY_REPLY
        elif query["outcome_kind"] == "degraded":
            content = query["degraded_reply"]
        else:
            verdict = await self.repos.verdict.for_query(int(query["id"]))
            if verdict is None:
                raise DeliveryFailure("verdict_missing")
            content = add_delivery_notice(verdict["reply"], query["delivery_notice"])
        if not content:
            raise DeliveryFailure("reply_content_missing")
        channel = self.channel()
        if channel is None:
            raise DeliveryFailure("wecom_unavailable")
        response = await channel.kf_send_msg(query["open_kfid"], user.openid.removeprefix("wxkf:"), content)
        if not _accepted(response):
            raise DeliveryFailure(_failure_category(response))

    async def _send_push(self, outbound: dict) -> str | None:
        context = await self.repos.alert.push_context(int(outbound["alert_id"]))
        if context is None:
            return await self.repos.alert.push_skip_reason(int(outbound["alert_id"]))
        if int(context["user_id"]) != int(outbound["to_user_id"]):
            raise DeliveryFailure("push_recipient_mismatch")
        verdict = await self.repos.verdict.get(int(context["verdict_id"]))
        if verdict is None:
            raise DeliveryFailure("verdict_missing")
        detail = (f"{self.settings.public_base_url.rstrip('/')}/alert/{context['alert_id']}?token={context['token']}"
                  if self.settings.public_base_url else "")
        content = ALERT_TEXTS[Level(verdict["level"])].format(name=context["name_at_alert"])
        if detail:
            content += f"\n详情：{detail}"
        member = await self.repos.wecom_member.get(int(outbound["to_user_id"]))
        if not member and not context["openid"].startswith("wxkf:"):
            return "unsupported_route"
        channel = self.channel()
        if channel is None:
            raise DeliveryFailure("wecom_unavailable")
        if member:
            try:
                response = await channel.send_app_message([member], content)
            except Exception:
                await self.repos.wecom_member.mark_failed(int(outbound["to_user_id"]), "应用消息发送异常")
                raise
        elif context["openid"].startswith("wxkf:"):
            # 客服会话回落;结果形态与主发送路径一致,失败类别可识别窗口类拒绝
            response = await channel.send_session_message_result(context["openid"], content)
        if not _accepted(response):
            if member:
                await self.repos.wecom_member.mark_failed(
                    int(outbound["to_user_id"]), f"应用消息发送失败 errcode={response.get('errcode') if isinstance(response, dict) else 'unknown'}"
                )
            raise DeliveryFailure(_failure_category(response))
        return None

    async def sweep_once(self) -> None:
        for item in await self.repos.query.list_expired(limit=20):
            query = await self.repos.query.takeover(int(item["id"]), self.settings.recovery_stale_seconds)
            if query is not None:
                task = asyncio.create_task(self._recover(query))
                self.tasks.add(task)
                task.add_done_callback(self._finished)
        for outbound_id in await self.repos.outbound.due_ids():
            self.schedule(outbound_id)
        overdue = await self.repos.query.overdue_without_result()
        for query in overdue:
            query_id = int(query["id"])
            if query_id not in self._warned_queries:
                logger.error("wecom query overdue query_id=%s claim_token=%s category=outcome_unresolved",
                             query_id, query["claim_token"])
        # 只记住仍在超期清单里的 query,集合不随历史无界增长;query 解决后若因
        # 扫描窗口轮换再次出现,重新告警一次
        self._warned_queries &= {int(query["id"]) for query in overdue}
        backlog = await self.repos.outbound.backlog_status()
        warning = (backlog["expired_leased"], backlog["failed"],
                   backlog["oldest_due_seconds"] // 300)
        if warning != self._last_backlog_warning and any(warning):
            logger.warning("outbound backlog due=%s expired=%s failed=%s oldest_due_seconds=%s",
                           backlog["due_pending"], backlog["expired_leased"],
                           backlog["failed"], backlog["oldest_due_seconds"])
        self._last_backlog_warning = warning

    async def _recover(self, query: dict) -> None:
        query_id = int(query["id"])
        try:
            await self.verification.recover_query(query)
            await self.dispatch_query(query_id, wait_reply=False)
        except ValidationError:
            # 世代核对失败 = 接管权已被 lease 更新的接管者拿走,并发下的正常让位
            # 而非故障;结果由持新令牌者提交,这里降级为信息日志
            logger.info("wecom query recovery lost claim query_id=%s", query_id)
        except Exception as exc:
            logger.error("wecom query recovery failed query_id=%s category=%s",
                         query_id, type(exc).__name__, exc_info=True)

    async def run(self) -> None:
        while True:
            try:
                await self.sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("recovery sweep failed", exc_info=True)
            await asyncio.sleep(self.settings.recovery_sweep_seconds)

    async def close(self) -> None:
        for task in list(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
