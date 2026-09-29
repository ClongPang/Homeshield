"""Queryer feedback and relation based correction voting."""
from homeshield.core.errors import ValidationError
from homeshield.core.models import CorrectionLabel
from homeshield.core.repo import Repos


class CorrectionService:
    def __init__(self, repos: Repos, window_days: int = 7):
        self.repos = repos
        self.window_days = window_days

    async def submit(self, verdict_id: int, user_id: int, label: str, note: str = "") -> dict:
        if label not in {item.value for item in CorrectionLabel}:
            raise ValidationError("label must be real or false_positive")
        return await self.repos.correction.submit(verdict_id, user_id, label, note.strip(), self.window_days)

    async def pending_for_user(self, user_id: int) -> list[dict]:
        return await self.repos.correction.list_pending_for_user(user_id)
