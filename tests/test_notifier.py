"""Template delivery uses the active relation's immutable alert snapshot."""
import asyncio

from homeshield.core.events import VerdictCompleted
from homeshield.core.models import ContentType, JudgeOutput, Level, Message, Mode
from homeshield.core.notifier import AlertBroker, AlertRouter


class FakeTemplateSender:
    def __init__(self):
        self.sent = []

    async def send_template(self, openid, data, url=None, template_id=None):
        self.sent.append((openid, data, url, template_id))


def _fanout(deps, suffix, names=("妈妈",)):
    queryer = deps.repos.users.get_or_create(f"notifier:queryer:{suffix}")
    recipients = []
    for index, name in enumerate(names):
        protector = deps.repos.users.get_or_create(f"notifier:protector:{suffix}:{index}")
        invite = deps.relations.issue_invite(protector.id, name)
        relation_id = deps.relations.join(queryer.openid, invite["code"])[1]
        recipients.append((protector, relation_id))
    query_id = deps.repos.query.insert(queryer.id, "text", "原始查询内容", None)
    verdict_id = deps.repos.verdict.insert(query_id, Level.DANGEROUS, [], [], "理由", "回复", 1, Mode.MOCK)
    fanout = deps.repos.alert.record_alerts_for_verdict(verdict_id, query_id)
    return fanout["recipients"], recipients


def test_template_fields_clip_and_use_alert_name_snapshot(deps):
    name_at_alert = "关系称呼超过二十个字符的模板测试用例超过限制"
    recipients, relations = _fanout(deps, "fields", (name_at_alert,))
    protector, relation_id = relations[0]
    deps.repos.relation.update(relation_id, protector.id, name="改过的称呼")
    sender = FakeTemplateSender()
    router = AlertRouter(AlertBroker(), deps.repos, "https://shield.test", "template-id")
    router.wechat = sender

    asyncio.run(router._send_alert_notifications(recipients, "风险摘要" * 6))

    assert len(sender.sent) == 1
    openid, data, url, template_id = sender.sent[0]
    assert openid == protector.openid and template_id == "template-id"
    assert data["thing1"]["value"] == "风险摘要" * 4 + "风险摘…"
    assert len(data["thing1"]["value"]) <= 20
    assert data["phrase1"]["value"] == "高危预警"
    assert data["thing2"]["value"] == name_at_alert[:19] + "…"
    assert len(data["thing2"]["value"]) <= 20
    assert url == f"https://shield.test/alert/{recipients[0]['alert_id']}?token={protector.token}"


def test_template_send_rechecks_mute_and_relation_activity(deps):
    recipients, relations = _fanout(deps, "permissions", ("妈妈", "爸爸"))
    muted_user, muted_relation = relations[0]
    ended_user, ended_relation = relations[1]
    deps.repos.relation.update(muted_relation, muted_user.id, mute=True)
    deps.relations.end(ended_user.id, ended_relation)
    sender = FakeTemplateSender()
    router = AlertRouter(AlertBroker(), deps.repos, "https://shield.test", "template-id")
    router.wechat = sender

    asyncio.run(router._send_alert_notifications(recipients, "危险内容"))

    assert sender.sent == []


def _event(deps, queryer, query_id, verdict_id) -> VerdictCompleted:
    message = Message(user_id=queryer.id, content_type=ContentType.TEXT, content="危险内容", channel="wechat")
    return VerdictCompleted(message=message, verdict=JudgeOutput(level=Level.DANGEROUS, confidence=90),
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
    event = _event(deps, queryer, query_id, verdict_id)

    router = AlertRouter(AlertBroker(), deps.repos)
    asyncio.run(router(event))
    assert event.queryer_notice == "高危提醒已加入儿子、联防者 #%d的提醒列表" % drop_relation

    assert deps.relations.end(drop.id, drop_relation) == "by_protector"
    asyncio.run(router(event))
    assert event.queryer_notice == "高危提醒已加入儿子的提醒列表"
