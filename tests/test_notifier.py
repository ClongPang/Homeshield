"""WeCom alert delivery: app message with snapshot, session fallback, and re-checks."""
import asyncio

from homeshield.core.events import VerdictCompleted
from homeshield.core.models import ContentType, JudgeOutput, Level, Message, Mode
from homeshield.core.notifier import AlertBroker, AlertRouter


class FakeWecomSender:
    def __init__(self):
        self.app_messages = []
        self.session_alerts = []

    async def send_app_message(self, corp_userids, text):
        self.app_messages.append((corp_userids, text))

    async def send_session_message(self, openid, text):
        self.session_alerts.append((openid, text))


def _fanout(deps, suffix, names=("妈妈",)):
    """queryer(wxkf 身份) + protectors(指定称呼,发邀请方),返回 (recipients, protectors)。"""
    queryer = deps.repos.users.get_or_create(f"wxkf:notifier:queryer:{suffix}")
    protectors = []
    for index, name in enumerate(names):
        protector = deps.repos.users.get_or_create(f"wxkf:notifier:protector:{suffix}:{index}")
        invite = deps.relations.issue_invite(protector.id, name)
        deps.relations.join(queryer.openid, invite["code"])
        protectors.append(protector)
    query_id = deps.repos.query.insert(queryer.id, "text", "原始查询内容", None)
    verdict_id = deps.repos.verdict.insert(query_id, Level.DANGEROUS, [], [], "理由", "回复", 1, Mode.MOCK)
    fanout = deps.repos.alert.record_alerts_for_verdict(verdict_id, query_id)
    return fanout["recipients"], protectors


def test_app_message_uses_alert_name_snapshot(deps):
    long_name = "关系称呼超过二十个字符的应用消息测试用例超过限制"
    recipients, protectors = _fanout(deps, "snapshot", (long_name,))
    deps.repos.relation.update(recipients[0]["relation_id"], protectors[0].id, name="改过的称呼")
    deps.repos.wecom_member.link(protectors[0].id, "CorpZhang")
    sender = FakeWecomSender()
    router = AlertRouter(AlertBroker(), deps.repos, "https://shield.test")
    router.wecom = sender

    asyncio.run(router._send_wecom_alerts(recipients, Level.DANGEROUS))

    assert len(sender.app_messages) == 1
    corp, text = sender.app_messages[0]
    assert corp == ["CorpZhang"]
    assert long_name in text            # 快照称呼,不受后续改名影响
    assert "改过的称呼" not in text
    assert f"https://shield.test/alert/{recipients[0]['alert_id']}?token={protectors[0].token}" in text


def test_app_message_rechecks_mute_and_relation_activity(deps):
    recipients, protectors = _fanout(deps, "permissions", ("妈妈", "爸爸"))
    for protector in protectors:
        deps.repos.wecom_member.link(protector.id, f"Corp{protector.id}")
    deps.repos.relation.update(recipients[0]["relation_id"], protectors[0].id, mute=True)
    deps.relations.end(protectors[1].id, recipients[1]["relation_id"])
    sender = FakeWecomSender()
    router = AlertRouter(AlertBroker(), deps.repos, "https://shield.test")
    router.wecom = sender

    asyncio.run(router._send_wecom_alerts(recipients, Level.DANGEROUS))

    assert sender.app_messages == []  # push_context 对静音/已解除返回 None


def test_session_fallback_for_unmapped_wxkf_protector(deps):
    recipients, _ = _fanout(deps, "fallback", ("妈妈",))
    sender = FakeWecomSender()
    router = AlertRouter(AlertBroker(), deps.repos, "")
    router.wecom = sender

    asyncio.run(router._send_wecom_alerts(recipients, Level.DANGEROUS))

    assert sender.app_messages == []
    assert len(sender.session_alerts) == 1
    openid, text = sender.session_alerts[0]
    assert openid.startswith("wxkf:") and "高危预警" in text


def _event(queryer, query_id, verdict_id, level=Level.DANGEROUS) -> VerdictCompleted:
    message = Message(user_id=queryer.id, content_type=ContentType.TEXT, content="危险内容", channel="wecom")
    return VerdictCompleted(message=message, verdict=JudgeOutput(level=level, confidence=90),
                            reply="回复", query_id=query_id, verdict_id=verdict_id)


def test_queryer_notice_uses_inverse_names_and_only_active_relations(deps):
    queryer = deps.repos.users.get_or_create("notifier:notice:queryer")
    keep = deps.repos.users.get_or_create("notifier:notice:keep")
    drop = deps.repos.users.get_or_create("notifier:notice:drop")
    invite = deps.relations.issue_invite(keep.id, "妈妈")
    keep_relation = deps.relations.join(queryer.openid, invite["code"])[1]
    invite = deps.relations.issue_invite(drop.id, "孩子")
    drop_relation = deps.relations.join(queryer.openid, invite["code"])[1]
    deps.repos.relation.update(keep_relation, queryer.id, inverse_name="儿子")
    deps.repos.relation.update(drop_relation, queryer.id, mute=True)
    query_id = deps.repos.query.insert(queryer.id, "text", "危险内容", None)
    verdict_id = deps.repos.verdict.insert(query_id, Level.DANGEROUS, [], [], "理由", "回复", 1, Mode.MOCK)
    event = _event(queryer, query_id, verdict_id)

    router = AlertRouter(AlertBroker(), deps.repos)
    asyncio.run(router(event))
    assert event.queryer_notice == "查询提醒已加入儿子、联防者 #%d的提醒列表" % drop_relation

    assert deps.relations.end(drop.id, drop_relation) == "by_protector"
    asyncio.run(router(event))
    assert event.queryer_notice == "查询提醒已加入儿子的提醒列表"


def test_fanout_is_query_driven_not_level_gated(deps):
    """扇出由查询事件触发,不按判定等级过滤——漏报不能是静默的。"""
    queryer = deps.repos.users.get_or_create("notifier:levels:queryer")
    protector = deps.repos.users.get_or_create("notifier:levels:protector")
    invite = deps.relations.issue_invite(protector.id, "妈妈")
    relation_id = deps.relations.join(queryer.openid, invite["code"])[1]
    deps.repos.relation.update(relation_id, queryer.id, inverse_name="儿子")
    router = AlertRouter(AlertBroker(), deps.repos)
    for level in (Level.SAFE, Level.SUSPICIOUS):
        query_id = deps.repos.query.insert(queryer.id, "text", "原始查询内容", None)
        verdict_id = deps.repos.verdict.insert(query_id, level, [], [], "理由", "回复", 1, Mode.MOCK)
        event = _event(queryer, query_id, verdict_id, level)

        asyncio.run(router(event))

        assert deps.conn.execute("SELECT COUNT(*) FROM alert WHERE verdict_id=?",
                                 (verdict_id,)).fetchone()[0] == 1
        assert event.queryer_notice == "查询提醒已加入儿子的提醒列表"


def test_alert_wording_follows_verdict_level(deps):
    recipients, protectors = _fanout(deps, "wording", ("妈妈",))
    deps.repos.wecom_member.link(protectors[0].id, "CorpZhang")
    sender = FakeWecomSender()
    router = AlertRouter(AlertBroker(), deps.repos, "https://shield.test")
    router.wecom = sender

    asyncio.run(router._send_wecom_alerts(recipients, Level.SAFE))
    asyncio.run(router._send_wecom_alerts(recipients, Level.SUSPICIOUS))

    safe_text, suspicious_text = (m[1] for m in sender.app_messages)
    assert "未发现典型骗术特征" in safe_text and "⚠️" not in safe_text and "高危预警" not in safe_text
    assert "判定为可疑" in suspicious_text and "高危预警" not in suspicious_text
