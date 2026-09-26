"""管线端到端(mock 模式):判定、规则下限、回复格式、告警落库、图片降级。"""
import asyncio

from homeshield.core.deps import make_pipeline
from homeshield.core.intake import ingest
from homeshield.core.models import Level
from homeshield.core.reply import validate_reply


def test_end_to_end_dangerous(deps, family):
    fid, elder, adult = family
    intake = ingest(
        deps.repos,
        member_id=elder,
        family_id=fid,
        content="妈,是我,别告诉家人,立即转账5万到安全账户,手续费2000",
    )
    result = asyncio.run(make_pipeline(deps).run(intake.message, intake.query_id))
    assert result.verdict is not None and result.verdict_id is not None
    assert result.verdict.level is Level.DANGEROUS
    assert result.rule_floor_level is Level.DANGEROUS  # isolation+transfer 共现
    assert validate_reply(result.reply)
    assert "家人" in result.reply  # 非 safe 结论告知长辈:家人已知悉
    # dangerous → alert 表留痕
    n = deps.conn.execute("SELECT COUNT(*) c FROM alert").fetchone()["c"]
    assert n == 1


def test_image_transcribe_degrade(deps, family):
    fid, elder, _ = family
    intake = ingest(
        deps.repos,
        member_id=elder,
        family_id=fid,
        content="DEGRADEME",
        content_type="image",
    )
    result = asyncio.run(make_pipeline(deps).run(intake.message, intake.query_id))
    assert result.degraded and result.verdict is None
    assert "请把内容打成文字" in result.reply


def test_suspicious_not_alerting(deps, family):
    """suspicious 不产生 alert。"""
    fid, elder, _ = family
    intake = ingest(deps.repos, member_id=elder, family_id=fid, content="最后一天限时优惠,马上下单")
    result = asyncio.run(make_pipeline(deps).run(intake.message, intake.query_id))
    assert result.verdict.level is Level.SUSPICIOUS
    n = deps.conn.execute("SELECT COUNT(*) c FROM alert").fetchone()["c"]
    assert n == 0
