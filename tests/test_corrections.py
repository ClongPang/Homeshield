"""纠正状态机与周报。"""
import asyncio
import time

import pytest

from core.deps import make_pipeline
from core.errors import ValidationError
from core.feedback import CorrectionService, weekly_report
from core.intake import ingest
from core.models import CorrectionLabel, CorrectionStatus


def _make_verdict(deps, family):
    fid, elder, _ = family
    intake = ingest(deps.repos, member_id=elder, family_id=fid, content="别告诉家人,立即转账")
    return asyncio.run(make_pipeline(deps).run(intake.message, intake.query_id)).verdict_id


def test_elder_pending_adult_confirm(deps, family):
    fid, elder, adult = family
    vid = _make_verdict(deps, family)
    svc = CorrectionService(deps.repos)
    cid, status = svc.submit(vid, elder, CorrectionLabel.REAL, note="这是真的骗局")
    assert status is CorrectionStatus.PENDING
    with pytest.raises(ValidationError):
        svc.decide(cid, elder, "confirm")  # elder 不能决定
    assert svc.decide(cid, adult, "confirm") is CorrectionStatus.CONFIRMED
    with pytest.raises(ValidationError):
        svc.decide(cid, adult, "reject")  # confirmed 后非法转移


def test_adult_submit_directly_confirmed(deps, family):
    fid, elder, adult = family
    vid = _make_verdict(deps, family)
    _, status = CorrectionService(deps.repos).submit(vid, adult, CorrectionLabel.FALSE_POSITIVE)
    assert status is CorrectionStatus.CONFIRMED


def test_weekly_report(deps, family):
    fid, elder, adult = family
    vid = _make_verdict(deps, family)
    svc = CorrectionService(deps.repos)
    cid, _ = svc.submit(vid, adult, CorrectionLabel.FALSE_POSITIVE, note="正常消息")
    report = weekly_report(deps.repos, fid)
    assert report["queries"] == 1
    assert report["corrections"] == 1
    assert report["false_positives"] == 1


def test_expire_pending(deps, family):
    fid, elder, adult = family
    vid = _make_verdict(deps, family)
    svc = CorrectionService(deps.repos)
    cid, _ = svc.submit(vid, elder, CorrectionLabel.REAL)
    # 未超时:不动
    assert svc.expire_pending(max_age_days=7) == 0
    # 超时(模拟 8 天前创建):置 rejected — 直接改库中 created_at 验证转移
    deps.conn.execute("UPDATE correction SET created_at=?", [time.time() - 8 * 86400])
    deps.conn.commit()
    assert svc.expire_pending(max_age_days=7) == 1
    assert deps.repos.correction.get(cid).status is CorrectionStatus.REJECTED


def test_stale_pending_cannot_be_confirmed(deps, family):
    """惰性清算:决定前先清算超时,过期 pending 不可再确认。"""
    fid, elder, adult = family
    vid = _make_verdict(deps, family)
    svc = CorrectionService(deps.repos)
    cid, _ = svc.submit(vid, elder, CorrectionLabel.REAL)
    deps.conn.execute("UPDATE correction SET created_at=?", [time.time() - 8 * 86400])
    deps.conn.commit()
    with pytest.raises(ValidationError, match="expired"):
        svc.decide(cid, adult, "confirm")
    assert deps.repos.correction.get(cid).status is CorrectionStatus.REJECTED


def test_fresh_pending_still_confirmable(deps, family):
    """惰性清算不误伤:未超时的 pending 正常确认。"""
    fid, elder, adult = family
    vid = _make_verdict(deps, family)
    svc = CorrectionService(deps.repos)
    cid, _ = svc.submit(vid, elder, CorrectionLabel.REAL)
    assert svc.decide(cid, adult, "confirm") is CorrectionStatus.CONFIRMED

