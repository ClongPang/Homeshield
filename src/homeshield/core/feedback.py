"""跨群一次性纠正与按群周报。"""
from homeshield.core.errors import ValidationError
from homeshield.core.models import CorrectionLabel, CorrectionStatus, Level, utcnow
from homeshield.core.repo import Repos, WRITE_LOCK

PENDING_TIMEOUT_DAYS = 7


class CorrectionService:
    def __init__(self, repos: Repos):
        self.repos = repos

    def submit(self, verdict_id: int, user_id: int, label: CorrectionLabel,
               note: str = "") -> tuple[int, CorrectionStatus]:
        with WRITE_LOCK:
            detail = self.repos.verdict.get_with_context(verdict_id)
            if detail is None:
                raise ValidationError("verdict not found")
            active = self.repos.member.list_for_user(user_id)
            if detail["level"] == Level.DANGEROUS.value:
                eligible = [m for m in active if self.repos.alert.group_was_recipient(verdict_id,user_id,m.group_id)]
            else:
                if detail["user_id"] != user_id:
                    raise ValidationError("only queryer can correct a private verdict")
                qgroups = {g["group_id"] for g in self.repos.query.groups(detail["query_id"])}
                eligible = [m for m in active if m.group_id in qgroups]
            if not eligible:
                raise ValidationError("no active related group for correction")
            existing = self.repos.correction.get_by_verdict_and_user(verdict_id,user_id)
            if existing:
                return existing.id, existing.status
            trusted = [m for m in eligible if m.trusted]
            decided_by = min(trusted,key=lambda m:m.id).id if trusted else None
            status = CorrectionStatus.CONFIRMED if trusted else CorrectionStatus.PENDING
            cid = self.repos.correction.insert(verdict_id,user_id,eligible,label,note,status,decided_by)
            # 同一用户重复点击并发到达时,以库里已提交的全局状态为准。
            saved = self.repos.correction.get(cid)
            return cid, saved.status

    def decide(self, correction_id: int, decided_by_membership_id: int, decision: str) -> CorrectionStatus:
        with WRITE_LOCK:
            actor = self.repos.member.get(decided_by_membership_id)
            if actor is None or actor.ended_at is not None or not actor.user_id or not actor.trusted:
                raise ValidationError("only active trusted member can decide")
            record, groups = self.repos.correction.get_with_groups(correction_id)
            if record is None:
                raise ValidationError("correction not found")
            allowed = any(g["group_id"]==actor.group_id and g["disbanded_at"] is None for g in groups)
            if not allowed:
                raise ValidationError("correction not in a related group")
            rec = self.repos.correction.get(correction_id)
            if rec.status is CorrectionStatus.PENDING:
                self.expire_pending()
                rec = self.repos.correction.get(correction_id)
                if rec.status is CorrectionStatus.REJECTED:
                    raise ValidationError("correction expired: pending 超过 7 天已自动拒绝")
            target = CorrectionStatus.CONFIRMED if decision=="confirm" else CorrectionStatus.REJECTED
            if rec.status is not CorrectionStatus.PENDING or decision not in ("confirm","reject"):
                raise ValidationError(f"illegal transition {rec.status.value} -> {target.value}")
            self.repos.correction.decide(correction_id,target,decided_by_membership_id)
            return target

    def expire_pending(self, max_age_days: int = PENDING_TIMEOUT_DAYS) -> int:
        return self.repos.correction.expire_older_than(utcnow()-max_age_days*86400)


def weekly_report(repos: Repos, group_id: int, days: int = 7) -> dict:
    since = utcnow()-days*86400
    return {
        "group_id":group_id,
        "days":days,
        "queries":repos.query.count(group_id,since),
        "dangerous":repos.verdict.count_dangerous(group_id,since),
        "false_positives":repos.correction.count_group(group_id,since,"confirmed","false_positive"),
        "corrections":repos.correction.count_group(group_id,since,"confirmed"),
    }
