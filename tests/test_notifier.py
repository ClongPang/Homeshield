"""WeCom alert delivery: app message with snapshot, session fallback, and re-checks."""

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


async def _fanout(deps, suffix, names=("妈妈",)):
    """queryer(wxkf 身份) + protectors(指定称呼,发邀请方),返回 (recipients, protectors)。"""
    queryer = await deps.repos.users.get_or_create(f"wxkf:notifier:queryer:{suffix}")
    protectors = []
    for index, name in enumerate(names):
        protector = await deps.repos.users.get_or_create(f"wxkf:notifier:protector:{suffix}:{index}")
        invite = await deps.relations.issue_invite(protector.id, name)
        await deps.relations.join(queryer.openid, invite["code"])
        protectors.append(protector)
    query_id = await deps.repos.query.insert(queryer.id, "text", "原始查询内容", None)
    verdict_id = await deps.repos.verdict.insert(query_id, Level.DANGEROUS, [], [], "理由", "回复", 1, Mode.MOCK)
    fanout = await deps.repos.alert.record_alerts_for_verdict(verdict_id, query_id)
    return fanout["recipients"], protectors


async def test_app_message_uses_alert_name_snapshot(deps):
    long_name = "关系称呼超过二十个字符的应用消息测试用例超过限制"
    recipients, protectors = await _fanout(deps, "snapshot", (long_name,))
    await deps.repos.relation.update(recipients[0]["relation_id"], protectors[0].id, name="改过的称呼")
    await deps.repos.wecom_member.link(protectors[0].id, "CorpZhang")
    sender = FakeWecomSender()
    router = AlertRouter(AlertBroker(), deps.repos, "https://shield.test")
    router.wecom = sender

    await router._send_wecom_alerts(recipients, Level.DANGEROUS)

    assert len(sender.app_messages) == 1
    corp, text = sender.app_messages[0]
    assert corp == ["CorpZhang"]
    assert long_name in text            # 快照称呼,不受后续改名影响
    assert "改过的称呼" not in text
    assert f"https://shield.test/alert/{recipients[0]['alert_id']}?token={protectors[0].token}" in text


async def test_app_message_rechecks_mute_and_relation_activity(deps):
    recipients, protectors = await _fanout(deps, "permissions", ("妈妈", "爸爸"))
    for protector in protectors:
        await deps.repos.wecom_member.link(protector.id, f"Corp{protector.id}")
    await deps.repos.relation.update(recipients[0]["relation_id"], protectors[0].id, mute=True)
    await deps.relations.end(protectors[1].id, recipients[1]["relation_id"])
    sender = FakeWecomSender()
    router = AlertRouter(AlertBroker(), deps.repos, "https://shield.test")
    router.wecom = sender

    await router._send_wecom_alerts(recipients, Level.DANGEROUS)

    assert sender.app_messages == []  # push_context 对静音/已解除返回 None


async def test_session_fallback_for_unmapped_wxkf_protector(deps):
    recipients, _ = await _fanout(deps, "fallback", ("妈妈",))
    sender = FakeWecomSender()
    router = AlertRouter(AlertBroker(), deps.repos, "")
    router.wecom = sender

    await router._send_wecom_alerts(recipients, Level.DANGEROUS)

    assert sender.app_messages == []
    assert len(sender.session_alerts) == 1
    openid, text = sender.session_alerts[0]
    assert openid.startswith("wxkf:") and "高危预警" in text


def _event(queryer, query_id, verdict_id, level=Level.DANGEROUS) -> VerdictCompleted:
    message = Message(user_id=queryer.id, content_type=ContentType.TEXT, content="危险内容", channel="wecom")
    return VerdictCompleted(message=message, verdict=JudgeOutput(level=level, confidence=90),
                            reply="回复", query_id=query_id, verdict_id=verdict_id)


async def test_queryer_notice_uses_inverse_names_and_only_active_relations(deps):
    queryer = await deps.repos.users.get_or_create("notifier:notice:queryer")
    keep = await deps.repos.users.get_or_create("notifier:notice:keep")
    drop = await deps.repos.users.get_or_create("notifier:notice:drop")
    invite = await deps.relations.issue_invite(keep.id, "妈妈")
    keep_relation = (await deps.relations.join(queryer.openid, invite["code"]))[1]
    invite = await deps.relations.issue_invite(drop.id, "孩子")
    drop_relation = (await deps.relations.join(queryer.openid, invite["code"]))[1]
    await deps.repos.relation.update(keep_relation, queryer.id, inverse_name="儿子")
    await deps.repos.relation.update(drop_relation, queryer.id, mute=True)
    query_id = await deps.repos.query.insert(queryer.id, "text", "危险内容", None)
    verdict_id = await deps.repos.verdict.insert(query_id, Level.DANGEROUS, [], [], "理由", "回复", 1, Mode.MOCK)
    event = _event(queryer, query_id, verdict_id)

    router = AlertRouter(AlertBroker(), deps.repos)
    await router(event)
    assert event.queryer_notice == "查询提醒已加入儿子、联防者 #%d的提醒列表" % drop_relation

    assert await deps.relations.end(drop.id, drop_relation) == "by_protector"
    await router(event)
    assert event.queryer_notice == "查询提醒已加入儿子的提醒列表"


async def test_fanout_is_query_driven_not_level_gated(deps):
    """扇出由查询事件触发,不按判定等级过滤——漏报不能是静默的。"""
    queryer = await deps.repos.users.get_or_create("notifier:levels:queryer")
    protector = await deps.repos.users.get_or_create("notifier:levels:protector")
    invite = await deps.relations.issue_invite(protector.id, "妈妈")
    relation_id = (await deps.relations.join(queryer.openid, invite["code"]))[1]
    await deps.repos.relation.update(relation_id, queryer.id, inverse_name="儿子")
    router = AlertRouter(AlertBroker(), deps.repos)
    for level in (Level.SAFE, Level.SUSPICIOUS):
        query_id = await deps.repos.query.insert(queryer.id, "text", "原始查询内容", None)
        verdict_id = await deps.repos.verdict.insert(query_id, level, [], [], "理由", "回复", 1, Mode.MOCK)
        event = _event(queryer, query_id, verdict_id, level)

        await router(event)

        assert (await (await deps.conn.execute('SELECT COUNT(*) FROM alert WHERE verdict_id=%s',
                                 (verdict_id,))).fetchone())[0] == 1
        assert event.queryer_notice == "查询提醒已加入儿子的提醒列表"


async def test_alert_wording_follows_verdict_level(deps):
    recipients, protectors = await _fanout(deps, "wording", ("妈妈",))
    await deps.repos.wecom_member.link(protectors[0].id, "CorpZhang")
    sender = FakeWecomSender()
    router = AlertRouter(AlertBroker(), deps.repos, "https://shield.test")
    router.wecom = sender

    await router._send_wecom_alerts(recipients, Level.SAFE)
    await router._send_wecom_alerts(recipients, Level.SUSPICIOUS)

    safe_text, suspicious_text = (m[1] for m in sender.app_messages)
    assert "未发现典型骗术特征" in safe_text and "⚠️" not in safe_text and "高危预警" not in safe_text
    assert "判定为可疑" in suspicious_text and "高危预警" not in suspicious_text
