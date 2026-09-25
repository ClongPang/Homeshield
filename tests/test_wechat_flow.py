"""微信通道家人体验:消息分类、可见回执、欢迎绑定、成员上限、意外故障兜底。"""
import asyncio
import hashlib

from fastapi.testclient import TestClient

from core import messages
from core.channels.wechat import WeChatChannel
from core.config import Settings
from core.deps import build_deps, make_pipeline
from core.events import VerdictCompleted
from core.intake import ingest
from core.models import Role
from server import _ensure_member, _welcome_wechat, create_app


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


def test_welcome_binds_member_and_sends(deps):
    fake = FakeChannel()
    asyncio.run(_welcome_wechat(deps, fake, {"FromUserName": "o_new"}))
    member = deps.repos.member.get_by_openid("o_new")
    assert member is not None and member.role is Role.ELDER
    assert fake.sent == [("o_new", messages.WELCOME)]


def test_auto_bind_stops_at_max_members(deps, family):
    fid, _, _ = family
    for i in range(deps.settings.max_members):
        deps.repos.member.add(fid, f"m{i}", Role.ELDER)
    assert _ensure_member(deps, "o_stranger") is None
    assert deps.repos.member.get_by_openid("o_stranger") is None


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
    assert url == f"http://shield.test/console?token={adult.token}"


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


def test_wechat_route_ack_fallback_and_welcome(tmp_path):
    settings = Settings(mode="mock", db_path=str(tmp_path / "w.db"), wechat_token="t")
    client = TestClient(create_app(settings))
    ts, nonce = "1", "n"
    qs = f"signature={_sign('t', ts, nonce)}&timestamp={ts}&nonce={nonce}"

    # 语音:立即得到兜底提示,而不是沉默
    r = _post_callback(client, qs, _xml("voice"))
    assert messages.UNSUPPORTED_TYPE in r.text

    # 文字:5s 窗口内先收到可见回执
    r = _post_callback(client, qs, _xml("text", "<Content><![CDATA[你好]]></Content>"))
    assert messages.RECEIVED_ACK in r.text

    # 关注:回 success,后台完成绑定并发欢迎语
    r = _post_callback(client, qs, _xml("event", "<Event><![CDATA[subscribe]]></Event>"))
    assert r.text == "success"
    deps = client.app.state.deps
    assert deps.repos.member.get_by_openid("o_user") is not None

    # 错误签名:拒绝
    bad = "signature=bad&timestamp=1&nonce=n"
    assert _post_callback(client, bad, _xml("text")).status_code == 403
