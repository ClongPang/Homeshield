"""查询验证服务:web 与微信通道共用的主链路。

通道层只做协议翻译;这里的序列是唯一的:
幂等 ingest → 管线 → 结果语义(重复 / 判定 / 降级)。
"""
import logging
from dataclasses import dataclass

from homeshield.core import messages
from homeshield.core.intake import ingest
from homeshield.core.models import Member, User
from homeshield.core.pipeline import Pipeline, PipelineResult
from homeshield.core.repo import Repos

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VerificationOutcome:
    query_id: int
    duplicate: bool
    result: PipelineResult | None  # duplicate 时为 None


class VerificationService:
    def __init__(self, repos: Repos, pipeline: Pipeline):
        self.repos = repos
        self.pipeline = pipeline

    async def verify(
        self,
        *,
        member: Member | None = None,
        user: User | None = None,
        memberships: list[Member] | None = None,
        content: str,
        content_type: str | None = None,
        channel: str = "web",
        msg_id: str | None = None,
    ) -> VerificationOutcome:
        if user is None and member is not None and member.user_id is not None:
            user = self.repos.users.get(member.user_id)
        if user is None:
            raise ValueError("user has no active group")
        if memberships is None:
            memberships = self.repos.member.list_for_user(user.id)
        if not memberships:
            raise ValueError("user has no active group")
        intake = ingest(
            self.repos,
            user_id=user.id,
            memberships=memberships,
            content=content,
            content_type=content_type,
            channel=channel,
            msg_id=msg_id,
        )
        if intake.duplicate:
            return VerificationOutcome(query_id=intake.query_id or 0, duplicate=True, result=None)
        try:
            result = await self.pipeline.run(intake.message, intake.query_id)
        except Exception:
            # 可预期降级已在管线内处理;此处兜住意外故障,保证通道必有回复
            logger.warning("pipeline failed unexpectedly", exc_info=True)
            result = PipelineResult(
                query_id=intake.query_id,
                reply=messages.LOOK_FAILED,
                latency_ms=0,
                degraded=True,
            )
        return VerificationOutcome(query_id=result.query_id, duplicate=False, result=result)
