"""重构四:会话一等公民——分轮、升级特征、判定渲染、评测接入。"""
import json

from homeshield.core.deps import make_pipeline
from homeshield.core.features import FeatureSpec, detect_escalation_feature
from conftest import ingest_user
from homeshield.core.models import Conversation
from homeshield.core.pipeline import _to_conversation


async def test_from_marked_and_single():
    c = Conversation.from_marked("【第1轮】你好\n【第2轮·对方】转账")
    assert c.is_multi_turn and c.turns[1].speaker == "对方"
    c2 = Conversation.from_marked("普通消息")
    assert not c2.is_multi_turn and c2.render() == "普通消息"


async def test_render_round_trip():
    c = Conversation.from_marked("【第1轮】你好\n【第2轮·对方】转账")
    assert Conversation.from_marked(c.render()).turns == c.turns


async def test_to_conversation_image_speaker_lines():
    c = _to_conversation("对方:你好\n我:有事?\n对方:急用钱", "image")
    assert c.is_multi_turn and len(c.turns) == 3 and c.turns[0].speaker == "对方"
    # 单行通知 → 单轮兜底
    assert not _to_conversation("这是一条单行通知", "image").is_multi_turn


async def test_escalation_detects_trust_then_ask():
    esc = detect_escalation_feature([
        FeatureSpec(type="identity_claim", value="我是你领导", turn=1),
        FeatureSpec(type="transfer", value="转账", turn=2),
    ])
    assert esc is not None and esc.type == "escalation" and esc.turn == 2
    assert "第1轮" in esc.value and "第2轮" in esc.value


async def test_escalation_absent_cases():
    # 同轮不构成升级
    assert detect_escalation_feature([
        FeatureSpec(type="identity_claim", value="x", turn=1),
        FeatureSpec(type="transfer", value="y", turn=1),
    ]) is None
    # 单轮
    assert detect_escalation_feature([FeatureSpec(type="transfer", value="y", turn=1)]) is None
    # 索取先于信任铺垫(先要钱后自证身份)——非渐进式
    assert detect_escalation_feature([
        FeatureSpec(type="transfer", value="y", turn=1),
        FeatureSpec(type="identity_claim", value="x", turn=2),
    ]) is None


async def test_pipeline_multi_turn_assigns_turns_and_escalation(deps, relations):
    user, _, _ = relations
    content = "【第1轮】我是你领导,这是我的新号\n【第2轮】在开会不方便接电话,帮我垫付5万合同款,马上"
    intake = await ingest_user(deps.repos, user, content=content)
    result = await make_pipeline(deps).run(intake.message, intake.query_id)
    assert result.verdict is not None
    snap = (await (await deps.conn.execute(
        'SELECT features FROM verdict WHERE id=%s', (result.verdict_id,)
    )).fetchone())["features"]
    parsed = json.loads(snap)
    types = {f["type"] for f in parsed}
    assert "escalation" in types
    turns = {f["turn"] for f in parsed if f["turn"]}
    assert 1 in turns and 2 in turns  # 特征按轮归属


async def test_sample_turns_feed_conversation():
    """评测样本 turns 字段 → 轮次标记 content → 会话解析。"""
    from homeshield.eval.dataset import load_dataset

    s = load_dataset("data/samples/fraud_r1_conversations.jsonl")[0]
    assert s.turns and len(s.turns) == 4
    content = "\n".join(f"【第{i}轮】{t}" for i, t in enumerate(s.turns, 1))
    c = Conversation.from_marked(content)
    assert len(c.turns) == 4 and c.is_multi_turn
