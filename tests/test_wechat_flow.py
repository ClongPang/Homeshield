"""微信通道家人体验:消息分类、可见回执、开通/绑定码入家、意外故障兜底。"""
import asyncio
import hashlib

from fastapi.testclient import TestClient

from homeshield.core import messages
from homeshield.core.channels.wechat import WeChatChannel
from homeshield.core.config import Settings
from homeshield.core.deps import build_deps, make_pipeline
from homeshield.core.events import VerdictCompleted
from conftest import ingest_member
from homeshield.api.wechat import _welcome_wechat
from homeshield.server import create_app


class FakeChannel:
    def __init__(self):
        self.sent = []
        self.templates = []

    async def send_customer_service(self, openid, text):
        self.sent.append((openid, text))

    async def send_template(self, openid, data, url=None, template_id=None):
        self.templates.append((openid, data, url, template_id))


def test_classify():
    assert WeChatChannel.classify_callback_message({"MsgType": "event", "Event": "subscribe"}) == "subscribe"
    assert WeChatChannel.classify_callback_message({"MsgType": "event", "Event": "unsubscribe"}) == "ignore"
    assert WeChatChannel.classify_callback_message({"MsgType": "text", "Content": "x"}) == "text"
    assert WeChatChannel.classify_callback_message({"MsgType": "image"}) == "image"
    assert WeChatChannel.classify_callback_message({"MsgType": "voice"}) == "unsupported"


def test_wechat_wiring_off_without_creds(deps):
    assert deps.wechat is None
    assert deps.alert_router.wechat is None


def test_wechat_wiring_token_only_keeps_template_off(tmp_path):
    """只配 wechat_token:回调可用,模板消息保持关闭而非发送时失败。"""
    settings = Settings(mode="mock", db_path=str(tmp_path / "w1.db"), wechat_token="t")
    deps = build_deps(settings)
    assert deps.wechat is not None
    assert deps.alert_router.wechat is None


def test_wechat_wiring_on_with_full_creds(tmp_path):
    settings = Settings(
        mode="mock",
        db_path=str(tmp_path / "w2.db"),
        wechat_token="t",
        wechat_appid="a",
        wechat_secret="s",
        wechat_template_id="TPL",
    )
    deps = build_deps(settings)
    assert deps.wechat is not None
    assert deps.alert_router.wechat is deps.wechat


def test_passive_reply_swaps_parties():
    xml = WeChatChannel.passive_text_reply({"FromUserName": "o1", "ToUserName": "gh1"}, "收到")
    assert "o1" in xml and "gh1" in xml and "收到" in xml


def test_welcome_sends_guide_only_no_autobind(deps):
    """关注只发欢迎语;陌生 openid 不再自动入家。"""
    fake = FakeChannel()
    asyncio.run(_welcome_wechat(deps, fake, {"FromUserName": "o_new"}))
    assert fake.sent == [("o_new", messages.WELCOME)]
    assert deps.repos.users.get_by_openid("o_new") is None


def test_pipeline_crash_becomes_fallback_reply(deps, group, monkeypatch):
    group_id, elder_id, _ = group

    async def boom(message, query_id):
        raise RuntimeError("llm down")

    monkeypatch.setattr(deps.pipeline, "run", boom)
    outcome = asyncio.run(deps.verification.verify(member=deps.repos.member.get(elder_id), content="hello"))
    assert outcome.result is not None
    assert outcome.result.degraded and outcome.result.reply == messages.LOOK_FAILED


def test_template_message_carries_console_link(deps, group):
    group_id, elder_id, _ = group
    elder = deps.repos.member.get(elder_id)
    adult_member = deps.repos.member.get(deps.repos.member.add(group_id, "女儿", openid="o_adult"))
    adult = deps.repos.users.get(adult_member.user_id)
    fake = FakeChannel()
    router = deps.alert_router
    router.wechat = fake
    router.base_url = "http://shield.test"
    router.template_id = "TPL"

    # alert 外键依赖真实 query/verdict,先走一遍管线产生它们
    intake = ingest_member(deps.repos, elder_id, content="别告诉家人,立即转账")
    result = asyncio.run(make_pipeline(deps).run(intake.message, intake.query_id))
    event = VerdictCompleted(
        message=intake.message,
        verdict=result.verdict,
        reply=result.reply,
        query_id=result.query_id,
        verdict_id=result.verdict_id,
    )
    asyncio.run(router(event))
    assert fake.templates, "dangerous 必须触发模板消息"
    _, _, url, template_id = fake.templates[0]
    assert url == f"http://shield.test/alert/{result.verdict_id}?token={adult.token}"
    assert template_id == "TPL"


def _sign(token, ts, nonce):
    return hashlib.sha1("".join(sorted([token, ts, nonce])).encode()).hexdigest()


def _xml(msg_type, extra=""):
    return (
        "<xml><ToUserName><![CDATA[gh_1]]></ToUserName>"
        "<FromUserName><![CDATA[o_user]]></FromUserName>"
        "<CreateTime>1</CreateTime>"
        f"<MsgType><![CDATA[{msg_type}]]></MsgType>{extra}</xml>"
    )


def _post_callback(client, qs, xml):
    return client.post(
        f"/wechat/callback?{qs}",
        content=xml.encode(),
        headers={"Content-Type": "text/xml"},
    )


def _client(tmp_path, **extra):
    settings = Settings(mode="mock", db_path=str(tmp_path / "w.db"), wechat_token="t", **extra)
    return TestClient(create_app(settings))


def _qs():
    ts, nonce = "1", "n"
    return f"signature={_sign('t', ts, nonce)}&timestamp={ts}&nonce={nonce}"


def test_open_command_creates_group_and_admin(tmp_path):
    """「开通」自助建群:同步回控制台链接,创建者在群内拥有信任权限。"""
    client = _client(tmp_path, public_base_url="http://shield.test")
    r = _post_callback(client, _qs(), _xml("text", "<Content><![CDATA[开通]]></Content>"))
    assert r.status_code == 200

    deps = client.app.state.deps
    admin = deps.repos.users.get_by_openid("o_user")
    member = deps.repos.member.list_for_user(admin.id)[0]
    assert admin is not None and member.trusted is True
    group_row = deps.repos.group.get(member.group_id)
    assert group_row is not None
    assert admin.token in r.text and f"/console?token={admin.token}" in r.text


def test_exit_preserves_identity_and_bare_open_reuses_it(tmp_path):
    client=_client(tmp_path)
    _post_callback(client,_qs(),_xml("text","<Content><![CDATA[开通 妈妈家]]></Content>"))
    deps=client.app.state.deps
    user=deps.repos.users.get_by_openid("o_user")
    creator=deps.repos.member.list_for_user(user.id)[0]
    second_id=deps.repos.member.add(creator.group_id,"女儿",openid="o_daughter")
    deps.repos.member.set_trust(second_id,True)

    exited=_post_callback(client,_qs(),_xml("text","<Content><![CDATA[退出 妈妈家]]></Content>"))
    assert "已退出「妈妈家」" in exited.text
    assert user.token and deps.repos.member.get(creator.id).ended_at is not None
    assert deps.repos.member.list_for_user(user.id)==[]

    ordinary=_post_callback(client,_qs(),_xml("text","<Content><![CDATA[这条消息是真的吗]]></Content>"))
    assert messages.BIND_GUIDE_OUTSIDE_GROUP in ordinary.text
    reopened=_post_callback(client,_qs(),_xml("text","<Content><![CDATA[开通]]></Content>"))
    assert "开通成功" in reopened.text
    assert deps.repos.users.get_by_openid("o_user").token==user.token
    assert len(deps.repos.member.list_for_user(user.id))==1


def test_creator_can_disband_by_group_name(tmp_path):
    client=_client(tmp_path)
    _post_callback(client,_qs(),_xml("text","<Content><![CDATA[开通 妈妈家]]></Content>"))
    deps=client.app.state.deps
    user=deps.repos.users.get_by_openid("o_user")
    creator=deps.repos.member.list_for_user(user.id)[0]
    response=_post_callback(client,_qs(),_xml("text","<Content><![CDATA[解散 妈妈家]]></Content>"))
    assert "已解散" in response.text
    assert deps.repos.group.get(creator.group_id)["disbanded_at"] is not None
    assert deps.repos.member.get(creator.id).end_reason=="disbanded"


def test_open_twice_rejected(tmp_path):
    """已有群时裸开通回群列表;带名开通才创建新群。"""
    client = _client(tmp_path)
    _post_callback(client, _qs(), _xml("text", "<Content><![CDATA[开通]]></Content>"))
    r = _post_callback(client, _qs(), _xml("text", "<Content><![CDATA[开通]]></Content>"))
    assert "你已加入这些防护群" in r.text and "开通 群名" in r.text
    deps = client.app.state.deps
    assert len(deps.repos.member.list_members(1)) == 1
    named = _post_callback(client, _qs(), _xml("text", "<Content><![CDATA[开通 岳父家]]></Content>"))
    assert "岳父家" in named.text
    assert len(deps.repos.member.list_for_user(deps.repos.users.get_by_openid("o_user").id)) == 2
    listed = _post_callback(client, _qs(), _xml("text", "<Content><![CDATA[我的群]]></Content>"))
    assert "默认" not in listed.text and "记账" not in listed.text
    assert "我的防护群" in listed.text and "岳父家" in listed.text


def test_open_user_group_limit_shows_current_limit(tmp_path):
    client = _client(tmp_path, max_groups=1)
    _post_callback(client, _qs(), _xml("text", "<Content><![CDATA[开通 第一群]]></Content>"))

    response = _post_callback(client, _qs(), _xml("text", "<Content><![CDATA[开通 第二群]]></Content>"))

    assert "一个用户最多可加入1个防护群" in response.text
    assert client.app.state.deps.repos.group.get(2) is None


def test_bind_command_joins_group(tmp_path):
    """家人回复「绑定 码」:openid 落到成员位,回绑定成功。"""
    client = _client(tmp_path)
    deps = client.app.state.deps
    group_id = deps.repos.group.create("测试家庭")
    trusted_id = deps.repos.member.add(group_id, "儿子", trusted=True)
    elder_id = deps.repos.member.add(group_id, "妈妈")
    code = deps.binding.issue_bind_code(deps.repos.member.get(elder_id), created_by=trusted_id)

    r = _post_callback(client, _qs(), _xml("text", f"<Content><![CDATA[绑定 {code['code'].lower()}]]></Content>"))
    assert "绑定成功" in r.text and "测试家庭" in r.text and "妈妈" in r.text  # 家庭名+成员名
    assert deps.repos.users.get(deps.repos.member.get(elder_id).user_id).openid == "o_user"

    # 领取后再发同一码:一次性口令已失效,不进入判定
    r2 = _post_callback(client, _qs(), _xml("text", f"<Content><![CDATA[绑定 {code['code']}]]></Content>"))
    assert messages.BIND_INVALID in r2.text


def test_bind_invalid_code_replies_hint(tmp_path):
    client = _client(tmp_path)
    r = _post_callback(client, _qs(), _xml("text", "<Content><![CDATA[绑定 BADCODE0]]></Content>"))
    assert messages.BIND_INVALID in r.text
    assert client.app.state.deps.repos.users.get_by_openid("o_user") is None


def test_bind_already_in_group_gets_correct_reply(tmp_path):
    client = _client(tmp_path)
    deps = client.app.state.deps
    group_id = deps.repos.group.create("测试家庭")
    existing = deps.repos.member.add(group_id, "妈妈", openid="o_user")
    trusted = deps.repos.member.add(group_id, "儿子", trusted=True, openid="o_son")
    slot = deps.repos.member.add(group_id, "妈妈的新邀请位")
    code = deps.binding.issue_bind_code(deps.repos.member.get(slot), created_by=trusted)

    response = _post_callback(client, _qs(), _xml("text", f"<Content><![CDATA[绑定 {code['code']}]]></Content>"))

    assert messages.BIND_ALREADY in response.text
    assert messages.BIND_INVALID not in response.text
    assert deps.repos.member.get(slot).user_id is None
    assert deps.repos.bind_code.get_valid_bind_code(code["code"]) is not None


def test_bind_group_limit_gets_correct_reply(tmp_path):
    client = _client(tmp_path, max_groups=1)
    deps = client.app.state.deps
    user = deps.repos.users.get_or_create("o_user")
    deps.repos.group.create_with_creator("已有群", "群主", user.id)
    target_group = deps.repos.group.create("待加入群")
    target_id = deps.repos.member.add(target_group, "妈妈")
    code = deps.binding.issue_bind_code(deps.repos.member.get(target_id), created_by=1)

    response = _post_callback(client, _qs(), _xml("text", f"<Content><![CDATA[绑定 {code['code']}]]></Content>"))

    assert messages.BIND_GROUP_LIMIT in response.text
    assert deps.repos.member.get(target_id).user_id is None
    assert deps.repos.bind_code.get_valid_bind_code(code["code"]) is not None


def test_unbound_text_gets_guide_not_judged(tmp_path):
    """未绑定发普通消息:只引导不判定,不落库不建成员。"""
    client = _client(tmp_path)
    r = _post_callback(client, _qs(), _xml("text", "<Content><![CDATA[这是骗子吗]]></Content>"))
    assert messages.BIND_GUIDE in r.text
    deps = client.app.state.deps
    assert deps.repos.users.get_by_openid("o_user") is None
    assert deps.repos.query.count_queries_for_group_since(1, 0) == 0  # 无判定落库


def test_bound_member_text_still_ack_and_judged(tmp_path):
    """已绑定成员的普通消息走既有链路:5s 回执 + 异步判定。"""
    client = _client(tmp_path)
    deps = client.app.state.deps
    group_id = deps.repos.group.create("测试家庭")
    deps.repos.member.add(group_id, "妈妈", openid="o_user")

    r = _post_callback(client, _qs(), _xml("text", "<Content><![CDATA[这是骗子吗]]></Content>"))
    assert messages.RECEIVED_ACK in r.text


def test_wechat_route_fallback_and_signature(tmp_path):
    client = _client(tmp_path)
    qs = _qs()

    # 语音:立即得到兜底提示,而不是沉默
    r = _post_callback(client, qs, _xml("voice"))
    assert messages.UNSUPPORTED_TYPE in r.text

    # 关注:回 success,后台发欢迎语,不建成员
    r = _post_callback(client, qs, _xml("event", "<Event><![CDATA[subscribe]]></Event>"))
    assert r.text == "success"
    deps = client.app.state.deps
    assert deps.repos.users.get_by_openid("o_user") is None

    # 错误签名:拒绝
    bad = "signature=bad&timestamp=1&nonce=n"
    assert _post_callback(client, bad, _xml("text")).status_code == 403
