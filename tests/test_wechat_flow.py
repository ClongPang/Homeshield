"""微信通道家人体验:消息分类、可见回执、开通/绑定码入家、意外故障兜底。"""
import asyncio
import hashlib

from fastapi.testclient import TestClient

from homeshield.core import messages
from homeshield.core.channels.wechat import WeChatChannel
from homeshield.core.config import Settings
from homeshield.core.deps import build_deps, make_pipeline
from homeshield.core.events import VerdictCompleted
from homeshield.core.intake import ingest
from homeshield.core.models import Role
from homeshield.api.wechat import _welcome_wechat
from homeshield.server import create_app


class FakeChannel:
    def __init__(self):
        self.sent = []
        self.templates = []

    async def send_customer_service(self, openid, text):
        self.sent.append((openid, text))

    async def send_template(self, openid, data, url=None):
        self.templates.append((openid, data, url))


def test_classify():
    assert WeChatChannel.classify({"MsgType": "event", "Event": "subscribe"}) == "subscribe"
    assert WeChatChannel.classify({"MsgType": "event", "Event": "unsubscribe"}) == "ignore"
    assert WeChatChannel.classify({"MsgType": "text", "Content": "x"}) == "text"
    assert WeChatChannel.classify({"MsgType": "image"}) == "image"
    assert WeChatChannel.classify({"MsgType": "voice"}) == "unsupported"


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
    assert deps.repos.member.get_by_openid("o_new") is None


def test_pipeline_crash_becomes_fallback_reply(deps, family, monkeypatch):
    fid, elder_id, _ = family

    async def boom(message, query_id):
        raise RuntimeError("llm down")

    monkeypatch.setattr(deps.pipeline, "run", boom)
    outcome = asyncio.run(deps.verification.verify(member=deps.repos.member.get(elder_id), content="hello"))
    assert outcome.result is not None
    assert outcome.result.degraded and outcome.result.reply == messages.LOOK_FAILED


def test_template_message_carries_console_link(deps, family):
    fid, elder_id, _ = family
    elder = deps.repos.member.get(elder_id)
    adult = deps.repos.member.get(deps.repos.member.add(fid, "女儿", Role.ADULT, openid="o_adult"))
    fake = FakeChannel()
    router = deps.alert_router
    router.wechat = fake
    router.base_url = "http://shield.test"

    # alert 外键依赖真实 query/verdict,先走一遍管线产生它们
    intake = ingest(deps.repos, member_id=elder_id, family_id=fid, content="别告诉家人,立即转账")
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
    _, _, url = fake.templates[0]
    assert url == f"http://shield.test/alert/{result.verdict_id}?token={adult.token}"


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


def test_open_command_creates_family_and_admin(tmp_path):
    """「开通」自助建家:同步回控制台链接,openid 直落为 adult 管理员。"""
    client = _client(tmp_path, public_base_url="http://shield.test")
    r = _post_callback(client, _qs(), _xml("text", "<Content><![CDATA[开通]]></Content>"))
    assert r.status_code == 200

    deps = client.app.state.deps
    admin = deps.repos.member.get_by_openid("o_user")
    assert admin is not None and admin.role is Role.ADULT
    fam = deps.repos.family.get(admin.family_id)
    assert fam is not None
    assert admin.token in r.text and f"/console?token={admin.token}" in r.text


def test_open_twice_rejected(tmp_path):
    client = _client(tmp_path)
    _post_callback(client, _qs(), _xml("text", "<Content><![CDATA[开通]]></Content>"))
    r = _post_callback(client, _qs(), _xml("text", "<Content><![CDATA[开通]]></Content>"))
    assert messages.BIND_ALREADY in r.text
    deps = client.app.state.deps
    assert len(deps.repos.member.list_members(1)) == 1


def test_bind_command_joins_family(tmp_path):
    """家人回复「绑定 码」:openid 落到成员位,回绑定成功。"""
    client = _client(tmp_path)
    deps = client.app.state.deps
    fid = deps.repos.family.create("测试家庭")
    adult_id = deps.repos.member.add(fid, "儿子", Role.ADULT)
    elder_id = deps.repos.member.add(fid, "妈妈", Role.ELDER)
    code = deps.binding.issue_code(deps.repos.member.get(elder_id), created_by=adult_id)

    r = _post_callback(client, _qs(), _xml("text", f"<Content><![CDATA[绑定 {code['code'].lower()}]]></Content>"))
    assert "绑定成功" in r.text and "测试家庭" in r.text and "妈妈" in r.text  # 家庭名+成员名
    assert deps.repos.member.get(elder_id).openid == "o_user"

    # 领取后再发同一码:已在家庭,不判定
    r2 = _post_callback(client, _qs(), _xml("text", f"<Content><![CDATA[绑定 {code['code']}]]></Content>"))
    assert messages.BIND_ALREADY in r2.text


def test_bind_invalid_code_replies_hint(tmp_path):
    client = _client(tmp_path)
    r = _post_callback(client, _qs(), _xml("text", "<Content><![CDATA[绑定 BADCODE0]]></Content>"))
    assert messages.BIND_INVALID in r.text
    assert client.app.state.deps.repos.member.get_by_openid("o_user") is None


def test_unbound_text_gets_guide_not_judged(tmp_path):
    """未绑定发普通消息:只引导不判定,不落库不建成员。"""
    client = _client(tmp_path)
    r = _post_callback(client, _qs(), _xml("text", "<Content><![CDATA[这是骗子吗]]></Content>"))
    assert messages.BIND_GUIDE in r.text
    deps = client.app.state.deps
    assert deps.repos.member.get_by_openid("o_user") is None
    assert deps.repos.query.count(1, 0) == 0  # 无判定落库


def test_bound_member_text_still_ack_and_judged(tmp_path):
    """已绑定成员的普通消息走既有链路:5s 回执 + 异步判定。"""
    client = _client(tmp_path)
    deps = client.app.state.deps
    fid = deps.repos.family.create("测试家庭")
    deps.repos.member.add(fid, "妈妈", Role.ELDER, openid="o_user")

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
    assert deps.repos.member.get_by_openid("o_user") is None

    # 错误签名:拒绝
    bad = "signature=bad&timestamp=1&nonce=n"
    assert _post_callback(client, bad, _xml("text")).status_code == 403
