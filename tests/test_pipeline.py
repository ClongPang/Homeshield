"""Pipeline behavior remains unchanged; alert notes follow relation fanout."""
import asyncio

from homeshield.core.deps import make_pipeline
from homeshield.core.errors import DegradeError
from homeshield.core.intake import ingest
from homeshield.core.judge import JudgeInput
from homeshield.core.models import JudgeOutput, Level, Mode
from homeshield.core.reply import is_valid_reply


def test_end_to_end_dangerous_and_relation_alert(deps, relations):
    protected, _, relation_id = relations
    intake = ingest(deps.repos, user_id=protected.id,
                    content="妈,是我,别告诉家人,立即转账5万到安全账户,手续费2000")
    result = asyncio.run(make_pipeline(deps).run(intake.message, intake.query_id))
    assert result.verdict is not None and result.verdict_id is not None
    assert result.verdict.level is Level.DANGEROUS
    assert result.rule_floor_level is Level.DANGEROUS
    assert is_valid_reply(result.reply)
    assert "儿子" in result.reply and "提醒列表" in result.reply
    rows = deps.conn.execute("SELECT relation_id,name_at_alert FROM alert").fetchall()
    assert [(row["relation_id"], row["name_at_alert"]) for row in rows] == [(relation_id, "妈妈")]


def test_image_transcribe_degrade(deps, user):
    intake = ingest(deps.repos, user_id=user.id, content="DEGRADEME", content_type="image")
    result = asyncio.run(make_pipeline(deps).run(intake.message, intake.query_id))
    assert result.degraded and result.verdict is None
    assert "请把内容打成文字" in result.reply


def test_suspicious_alerts_query_driven(deps, relations):
    """扇出由查询事件触发,判定等级不过滤:可疑判定同样进联防者提醒列表。"""
    protected, _, relation_id = relations
    intake = ingest(deps.repos, user_id=protected.id, content="最后一天限时优惠,马上下单")
    result = asyncio.run(make_pipeline(deps).run(intake.message, intake.query_id))
    assert result.verdict.level is Level.SUSPICIOUS
    assert "查询提醒已加入儿子的提醒列表" in result.reply
    rows = deps.conn.execute("SELECT relation_id,name_at_alert FROM alert").fetchall()
    assert [(row["relation_id"], row["name_at_alert"]) for row in rows] == [(relation_id, "妈妈")]


def test_bare_dangerous_query_has_no_alert_or_notification_claim(deps, user):
    intake = ingest(deps.repos, user_id=user.id, content="别告诉家人，立即转账5万")
    result = asyncio.run(make_pipeline(deps).run(intake.message, intake.query_id))
    assert result.verdict.level is Level.DANGEROUS
    assert deps.conn.execute("SELECT COUNT(*) FROM alert").fetchone()[0] == 0
    assert "提醒列表" not in result.reply


class _JudgeAlwaysDegrades:
    """判定阶段失败的替身:抽取阶段正常完成,规则下限已在手。"""

    mode = Mode.LLM

    async def judge(self, inp: JudgeInput, *, constrained: bool) -> JudgeOutput:
        raise DegradeError("这条消息我拿不准，请把内容给家人看看再决定", "test judge degrade")


def test_judge_degrade_keeps_dangerous_rule_floor(deps, user):
    intake = ingest(deps.repos, user_id=user.id, content="别告诉家人，立即转账5万到安全账户")
    pipeline = make_pipeline(deps)
    pipeline.judge = _JudgeAlwaysDegrades()
    result = asyncio.run(pipeline.run(intake.message, intake.query_id))
    assert result.degraded and result.verdict is None and result.verdict_id is None
    assert result.rule_floor_level is Level.DANGEROUS
    assert "不要转钱" in result.reply and "96110" in result.reply
    assert "拿不准" not in result.reply
    assert deps.conn.execute("SELECT COUNT(*) FROM verdict").fetchone()[0] == 0
    assert deps.conn.execute("SELECT COUNT(*) FROM alert").fetchone()[0] == 0


def test_judge_degrade_keeps_suspicious_rule_floor(deps, user):
    intake = ingest(deps.repos, user_id=user.id, content="这件事要保密，是我们俩的事")
    pipeline = make_pipeline(deps)
    pipeline.judge = _JudgeAlwaysDegrades()
    result = asyncio.run(pipeline.run(intake.message, intake.query_id))
    assert result.degraded and result.verdict is None
    assert result.rule_floor_level is Level.SUSPICIOUS
    assert "保密" in result.reply and "核实" in result.reply
    assert "拿不准" not in result.reply


def test_judge_degrade_safe_floor_keeps_baseline_copy(deps, user):
    intake = ingest(deps.repos, user_id=user.id, content="今天天气不错，出来散步吗")
    pipeline = make_pipeline(deps)
    pipeline.judge = _JudgeAlwaysDegrades()
    result = asyncio.run(pipeline.run(intake.message, intake.query_id))
    assert result.degraded and result.verdict is None
    assert result.rule_floor_level is Level.SAFE
    assert "拿不准" in result.reply
