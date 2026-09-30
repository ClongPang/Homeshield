"""WeCom alert delivery through the durable outbound ledger."""

from dataclasses import replace

from homeshield.core.events import VerdictCompleted
from homeshield.core.models import ContentType, JudgeOutput, Level, Message, Mode
from homeshield.core.notifier import AlertBroker, AlertRouter
from homeshield.core.repo import repository_transaction


class FakeWecomSender:
    def __init__(self):
        self.app_messages = []
        self.session_alerts = []

    async def send_app_message(self, corp_userids, text):
        self.app_messages.append((corp_userids, text))
        return {"errcode": 0}

    async def send_session_message_result(self, openid, text):
        self.session_alerts.append((openid, text))
        return {"errcode": 0}


async def _fanout(deps, suffix, names=("妈妈",), level=Level.DANGEROUS):
    """queryer(wxkf 身份) + protectors(指定称呼,发邀请方),返回 (recipients, protectors)。"""
    queryer = await deps.repos.users.get_or_create(f"wxkf:notifier:queryer:{suffix}")
    protectors = []
    for index, name in enumerate(names):
        protector = await deps.repos.users.get_or_create(f"wxkf:notifier:protector:{suffix}:{index}")
        invite = await deps.relations.issue_invite(protector.id, name)
        await deps.relations.join(queryer.openid, invite["code"])
        protectors.append(protector)
    query_id = await deps.repos.query.insert(queryer.id, "text", "原始查询内容", None)
    async with repository_transaction(deps.repos):
        verdict_id = await deps.repos.verdict.insert(query_id, level, [], [], "理由", "回复", 1, Mode.MOCK)
        fanout = await deps.repos.alert.record_alerts_for_verdict(verdict_id, query_id)
        for recipient in fanout["recipients"]:
            recipient["query_id"] = query_id
            recipient["outbound_id"] = await deps.repos.outbound.create_push(
                query_id, recipient["alert_id"], recipient["user_id"])
    return fanout["recipients"], protectors


async def _dispatch(deps, sender, recipients, base_url=""):
    deps.wecom = sender
    deps.recovery.settings = replace(deps.settings, public_base_url=base_url)
    for recipient in recipients:
        await deps.recovery.dispatch(recipient["outbound_id"])


async def test_app_message_uses_alert_name_snapshot(deps):
    long_name = "关系称呼超过二十个字符的应用消息测试用例超过限制"
    recipients, protectors = await _fanout(deps, "snapshot", (long_name,))
    await deps.repos.relation.update(recipients[0]["relation_id"], protectors[0].id, name="改过的称呼")
    await deps.repos.wecom_member.link(protectors[0].id, "CorpZhang")
    sender = FakeWecomSender()
    await _dispatch(deps, sender, recipients, "https://shield.test")

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
    await _dispatch(deps, sender, recipients, "https://shield.test")

    assert sender.app_messages == []  # push_context 对静音/已解除返回 None
    rows = await deps.repos.outbound.for_query(recipients[0]["query_id"])
    assert {row["last_error"] for row in rows} == {"relation_muted", "relation_ended"}


async def test_session_fallback_for_unmapped_wxkf_protector(deps):
    recipients, _ = await _fanout(deps, "fallback", ("妈妈",))
    sender = FakeWecomSender()
    await _dispatch(deps, sender, recipients)

    assert sender.app_messages == []
    assert len(sender.session_alerts) == 1
    openid, text = sender.session_alerts[0]
    assert openid.startswith("wxkf:") and "高危预警" in text


def _event(queryer, query_id, verdict_id, level=Level.DANGEROUS) -> VerdictCompleted:
    message = Message(user_id=queryer.id, content_type=ContentType.TEXT, content="危险内容", channel="wecom")
    return VerdictCompleted(message=message, verdict=JudgeOutput(level=level, confidence=90),
                            reply="回复", query_id=query_id, verdict_id=verdict_id)


async def test_delivery_notice_uses_inverse_names_of_commit_time_relations(deps):
    """delivery_notice 是提交事务里固化的快照:静音关系仍入账提示,
    已解除关系不入账;解除发生在提交后不回改提示,补发回复沿用原文案。"""
    queryer = await deps.repos.users.get_or_create("notifier:notice:queryer")
    keep = await deps.repos.users.get_or_create("notifier:notice:keep")
    drop = await deps.repos.users.get_or_create("notifier:notice:drop")
    invite = await deps.relations.issue_invite(keep.id, "妈妈")
    keep_relation = (await deps.relations.join(queryer.openid, invite["code"]))[1]
    invite = await deps.relations.issue_invite(drop.id, "孩子")
    drop_relation = (await deps.relations.join(queryer.openid, invite["code"]))[1]
    await deps.repos.relation.update(keep_relation, queryer.id, inverse_name="儿子")
    await deps.repos.relation.update(drop_relation, queryer.id, mute=True)

    result = await deps.verification.verify(user=queryer, content="请转账")
    query = await deps.repos.query.get(result.query_id)
    assert query["delivery_notice"] == "查询提醒已加入儿子、联防者 #%d的提醒列表" % drop_relation
    assert await deps.relations.end(drop.id, drop_relation) == "by_protector"
    assert (await deps.repos.query.get(result.query_id))["delivery_notice"] == \
        "查询提醒已加入儿子、联防者 #%d的提醒列表" % drop_relation


async def test_fanout_is_query_driven_not_level_gated(deps):
    """扇出由查询事件触发,不按判定等级过滤——漏报不能是静默的。"""
    queryer = await deps.repos.users.get_or_create("notifier:levels:queryer")
    protector = await deps.repos.users.get_or_create("notifier:levels:protector")
    invite = await deps.relations.issue_invite(protector.id, "妈妈")
    relation_id = (await deps.relations.join(queryer.openid, invite["code"]))[1]
    await deps.repos.relation.update(relation_id, queryer.id, inverse_name="儿子")
    broker = AlertBroker()
    router = AlertRouter(broker, deps.repos)
    queue = broker.subscribe(protector.id)
    for level in (Level.SAFE, Level.SUSPICIOUS):
        query_id = await deps.repos.query.insert(queryer.id, "text", "原始查询内容", None)
        async with repository_transaction(deps.repos):
            verdict_id = await deps.repos.verdict.insert(query_id, level, [], [], "理由", "回复", 1, Mode.MOCK)
            await deps.repos.alert.record_alerts_for_verdict(verdict_id, query_id)
        event = _event(queryer, query_id, verdict_id, level)

        await router(event)

        assert (await (await deps.conn.execute('SELECT COUNT(*) FROM alert WHERE verdict_id=%s',
                                 (verdict_id,))).fetchone())[0] == 1
        payload = queue.get_nowait()  # 每个等级都扇出,SSE 载荷等级与判定一致
        assert payload["level"] == level.value and payload["alert_id"]


async def test_alert_wording_follows_verdict_level(deps):
    safe_recipients, safe_protectors = await _fanout(deps, "wording-safe", ("妈妈",), Level.SAFE)
    suspicious_recipients, suspicious_protectors = await _fanout(
        deps, "wording-suspicious", ("妈妈",), Level.SUSPICIOUS)
    await deps.repos.wecom_member.link(safe_protectors[0].id, "CorpSafe")
    await deps.repos.wecom_member.link(suspicious_protectors[0].id, "CorpSuspicious")
    sender = FakeWecomSender()
    await _dispatch(deps, sender, safe_recipients + suspicious_recipients,
                    "https://shield.test")

    safe_text, suspicious_text = (m[1] for m in sender.app_messages)
    assert "未发现典型骗术特征" in safe_text and "⚠️" not in safe_text and "高危预警" not in safe_text
    assert "判定为可疑" in suspicious_text and "高危预警" not in suspicious_text
