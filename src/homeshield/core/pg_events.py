"""Postgres LISTEN/NOTIFY bridge for events that must reach every worker."""
import asyncio
import json
import logging

import psycopg
from psycopg.conninfo import make_conninfo

from homeshield.core.db import DB_STATEMENT_TIMEOUT_MS
from homeshield.core.deps import Deps
from homeshield.core.events import VerdictCompleted
from homeshield.core.models import ContentType, JudgeOutput, Level, Message

logger = logging.getLogger(__name__)
VERDICT_CHANNEL = "verdict_completed"
KF_PULL_CHANNEL = "wecom_kf_pull"


async def _event_from_notification(deps: Deps, payload: str) -> VerdictCompleted | None:
    try:
        data = json.loads(payload)
        verdict_id = int(data["verdict_id"])
        query_id = int(data["query_id"])
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        logger.warning("invalid %s notification payload", VERDICT_CHANNEL)
        return None

    row = await deps.repos.verdict.notification_context(verdict_id)
    if row is None or int(row["query_id"]) != query_id:
        logger.warning("%s references missing or mismatched verdict %s", VERDICT_CHANNEL, verdict_id)
        return None
    cited_ids = row["cited_ids"]
    if isinstance(cited_ids, str):
        cited_ids = json.loads(cited_ids)
    message = Message(
        user_id=int(row["user_id"]),
        relation_ids=[],
        content_type=ContentType(row["content_type"]),
        content=row["content"],
        created_at=int(row["created_at"]),
    )
    verdict = JudgeOutput(level=Level(row["level"]), confidence=50, cited_ids=cited_ids)
    return VerdictCompleted(message, verdict, "", query_id, verdict_id)


async def notification_bridge(deps: Deps, ready: asyncio.Event | None = None) -> None:
    """Listen on a dedicated non-pool connection and fan each broadcast out locally.

    verdict_completed → 告警分发;wecom_kf_pull → 置位本进程 poll_signal,
    只有持有 advisory lock 的 leader poller 在等它,其余 worker 置位无副作用。
    """
    conninfo = make_conninfo(
        deps.settings.database_url,
        options=f"-c timezone=UTC -c statement_timeout={DB_STATEMENT_TIMEOUT_MS}",
    )
    while True:
        try:
            async with await psycopg.AsyncConnection.connect(conninfo, autocommit=True) as conn:
                await conn.execute(f"LISTEN {VERDICT_CHANNEL}")
                await conn.execute(f"LISTEN {KF_PULL_CHANNEL}")
                if ready is not None:
                    ready.set()
                async for notification in conn.notifies():
                    try:
                        if notification.channel == KF_PULL_CHANNEL:
                            deps.poll_signal.set()
                            continue
                        event = await _event_from_notification(deps, notification.payload)
                        if event is not None:
                            await deps.alert_router(event)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.warning("%s event dispatch failed", VERDICT_CHANNEL, exc_info=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("notification listener disconnected; retrying", exc_info=True)
            await asyncio.sleep(1)
