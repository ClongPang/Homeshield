"""管线端到端(mock 模式):判定、规则下限、回复格式、告警落库、图片降级。"""
import asyncio

from homeshield.core.deps import make_pipeline
from homeshield.core.intake import ingest
from homeshield.core.models import Level
from homeshield.core.reply import is_valid_reply
from conftest import ingest_member


def test_end_to_end_dangerous(deps, group):
    group_id, elder, adult = group
    intake = ingest_member(
        deps.repos, elder,
        content="妈,是我,别告诉家人,立即转账5万到安全账户,手续费2000",
    )
    result = asyncio.run(make_pipeline(deps).run(intake.message, intake.query_id))
    assert result.verdict is not None and result.verdict_id is not None
    assert result.verdict.level is Level.DANGEROUS
    assert result.rule_floor_level is Level.DANGEROUS  # isolation+transfer 共现
    assert is_valid_reply(result.reply)
    assert "高危提醒" in result.reply  # 通知说明只在实际生成告警后追加
    # dangerous → alert 表留痕,覆盖全体成员(群模型告警面)
    n = deps.conn.execute("SELECT COUNT(*) c FROM alert").fetchone()["c"]
    assert n == len(deps.repos.member.list_members(group_id))


def test_image_transcribe_degrade(deps, group):
    group_id, elder, _ = group
    intake = ingest_member(
        deps.repos, elder,
        content="DEGRADEME",
        content_type="image",
    )
    result = asyncio.run(make_pipeline(deps).run(intake.message, intake.query_id))
    assert result.degraded and result.verdict is None
    assert "请把内容打成文字" in result.reply


def test_suspicious_not_alerting(deps, group):
    """suspicious 不产生 alert。"""
    group_id, elder, _ = group
    intake = ingest_member(deps.repos, elder, content="最后一天限时优惠,马上下单")
    result = asyncio.run(make_pipeline(deps).run(intake.message, intake.query_id))
    assert result.verdict.level is Level.SUSPICIOUS
    assert "已通知" not in result.reply
    n = deps.conn.execute("SELECT COUNT(*) c FROM alert").fetchone()["c"]
    assert n == 0
