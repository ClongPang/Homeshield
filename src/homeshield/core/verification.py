"""查询验证服务:web 与微信通道共用的主链路。

通道层只做协议翻译;这里的序列是唯一的:
幂等 ingest → 管线 → 结果语义(重复 / 判定 / 降级)。
"""
import logging
from dataclasses import dataclass

from homeshield.core import messages
from homeshield.core.intake import ingest
from homeshield.core.models import User, Message, ContentType
from homeshield.core.pipeline import Pipeline, PipelineResult
from homeshield.core.repo import Repos
from homeshield.core.errors import ValidationError
from homeshield.core.triage import classify

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VerificationOutcome:
    query_id: int
    duplicate: bool
    result: PipelineResult | None  # duplicate 时为 None
    kind: str = "query"


class VerificationService:
    def __init__(self, repos: Repos, pipeline: Pipeline, recovery_stale_seconds: int = 300):
        self.repos = repos
        self.pipeline = pipeline
        self.recovery_stale_seconds = recovery_stale_seconds

    async def verify(
        self,
        *,
        user: User | None = None,
        content: str,
        content_type: str | None = None,
        channel: str = "web",
        msg_id: str | None = None,
        session_epoch: int | None = None,
        open_kfid: str | None = None,
    ) -> VerificationOutcome:
        if user is None:
            raise ValueError("user identity required")
        kind = "query"
        if (content_type is None or content_type == "text") and classify(content) == "ack":
            kind = "ack"
        claim_token = 1 if channel == "wecom" else None  # 认领时写入的初始世代
        intake = await ingest(
            self.repos,
            user_id=user.id,
            content=content,
            content_type=content_type,
            channel=channel,
            msg_id=msg_id,
            kind=kind,
            open_kfid=open_kfid,
            session_epoch=session_epoch,
            lease_seconds=self.recovery_stale_seconds,
        )
        if intake.duplicate:    # 消息已经存在且处理
            previous = await self.repos.query.get(intake.query_id or 0)
            return VerificationOutcome(query_id=intake.query_id or 0, duplicate=True, result=None,
                                       kind=previous["kind"] if previous else kind)
        if kind == "ack":       # 如果收到的是用户的确认型消息，只回复，固定的招呼语
            await self.pipeline.commit_outcome(intake.query_id, claim_token, "ack")
            return VerificationOutcome(
                query_id=intake.query_id, duplicate=False,
                result=PipelineResult(query_id=intake.query_id, reply=messages.ACK_QUERY_REPLY,
                                      latency_ms=0),
                kind="ack",
            )
        try:
            await self.repos.incident.attach_query_to_incident(
                intake.query_id, user.id, self.pipeline.incident_idle_seconds,
                expected_epoch=session_epoch,
            )
        except Exception:
            logger.warning("incident partition failed, judge current message only", exc_info=True)
        try:
            result = await self.pipeline.run(intake.message, intake.query_id, claim_token=claim_token)
        except ValidationError:
            raise
        except Exception:
            # 可预期降级已在管线内处理;此处兜住意外故障,保证通道必有回复
            logger.warning("pipeline failed unexpectedly", exc_info=True)
            result = PipelineResult(
                query_id=intake.query_id,
                reply=messages.LOOK_FAILED,
                latency_ms=0,
                degraded=True,
            )
            await self.pipeline.commit_outcome(
                result.query_id, claim_token, "degraded", degraded_reply=result.reply)
        return VerificationOutcome(query_id=result.query_id, duplicate=False, result=result, kind="query")

    async def recover_query(self, query: dict) -> None:
        """Complete an expired WeCom claim from its durable input, without re-ingest."""
        query_id = int(query["id"])
        token = int(query["claim_token"])
        if query["kind"] == "ack":
            await self.pipeline.commit_outcome(query_id, token, "ack")
            return
        if query["incident_id"] is None:
            try:
                await self.repos.incident.attach_query_to_incident(
                    query_id, int(query["user_id"]), self.pipeline.incident_idle_seconds,
                    expected_epoch=query["session_epoch_at_claim"],
                )
            except Exception:
                logger.warning("incident recovery failed query_id=%s", query_id, exc_info=True)
        relations = await self.repos.query.list_relations_for_query(query_id)
        message = Message(
            user_id=int(query["user_id"]),
            relation_ids=[int(r["id"]) for r in relations],
            content_type=ContentType(query["content_type"]),
            content=query["content"],
            channel="wecom",
            msg_id=query["msg_id"],
        )
        try:
            await self.pipeline.run(message, query_id, claim_token=token)
        except ValidationError:
            raise
        except Exception:
            logger.warning("recovered pipeline failed query_id=%s", query_id, exc_info=True)
            result = PipelineResult(query_id=query_id, reply=messages.LOOK_FAILED,
                                    latency_ms=0, degraded=True)
            await self.pipeline.commit_outcome(
                query_id, token, "degraded", degraded_reply=result.reply)
