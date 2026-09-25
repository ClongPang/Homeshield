"""纠正回流与周报。

纠正状态机用显式转移表,非法转移直接拒绝。
"""
from core.errors import ValidationError
from core.models import (
    CorrectionLabel,
    CorrectionStatus,
    Role,
    utcnow,
)
from core.repo import Repos

# (当前状态, 目标状态) 合法迁移;PENDING 之外的状态不可再变
LEGAL_TRANSITIONS = {
    (CorrectionStatus.PENDING, CorrectionStatus.CONFIRMED),
    (CorrectionStatus.PENDING, CorrectionStatus.REJECTED),
}

PENDING_TIMEOUT_DAYS = 7  # pending 超时自动 rejected


class CorrectionService:
    def __init__(self, repos: Repos):
        self.repos = repos

    def submit(
        self,
        verdict_id: int,
        by_member_id: int,
        label: CorrectionLabel,
        note: str = "",
    ) -> tuple[int, CorrectionStatus]:
        member = self.repos.member.get(by_member_id)
        if member is None:
            raise ValidationError("member not found")
        if self.repos.verdict.get(verdict_id) is None:
            raise ValidationError("verdict not found")
        # adult 直接生效;elder 进入 pending 等确认
        status = (
            CorrectionStatus.CONFIRMED
            if member.role is Role.ADULT
            else CorrectionStatus.PENDING
        )
        cid = self.repos.correction.insert(verdict_id, by_member_id, label, note, status)
        return cid, status

    def decide(self, correction_id: int, decided_by: int, decision: str) -> CorrectionStatus:
        decider = self.repos.member.get(decided_by)
        if decider is None or decider.role is not Role.ADULT:
            raise ValidationError("only adult can decide")
        rec = self.repos.correction.get(correction_id)
        if rec is None:
            raise ValidationError("correction not found")
        if rec.status is CorrectionStatus.PENDING:
            # 决定前先清算全部过期 pending
            self.expire_pending()
            rec = self.repos.correction.get(correction_id)
            if rec.status is CorrectionStatus.REJECTED:
                raise ValidationError("correction expired: pending 超过 7 天已自动拒绝")
        target = (
            CorrectionStatus.CONFIRMED if decision == "confirm" else CorrectionStatus.REJECTED
        )
        if (rec.status, target) not in LEGAL_TRANSITIONS:
            raise ValidationError(f"illegal transition {rec.status.value} -> {target.value}")
        self.repos.correction.decide(correction_id, target, decided_by)
        return target

    def expire_pending(self, max_age_days: int = PENDING_TIMEOUT_DAYS) -> int:
        return self.repos.correction.expire_older_than(utcnow() - max_age_days * 86400)


def weekly_report(repos: Repos, family_id: int, days: int = 7) -> dict:
    """数字全部来自库内记录。"""
    since = utcnow() - days * 86400
    return {
        "family_id": family_id,
        "days": days,
        "queries": repos.query.count(family_id, since),
        "dangerous": repos.verdict.count_dangerous(family_id, since),
        "false_positives": repos.correction.count(
            family_id, since, status="confirmed", label="false_positive"
        ),
        "corrections": repos.correction.count(family_id, since),
    }
