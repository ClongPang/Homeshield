"""WeChat acceptance: first-message identity, private bare checks and directed commands."""
import xml.etree.ElementTree as ET

from fastapi.testclient import TestClient

from homeshield.core.channels.wechat import WeChatChannel
from homeshield.core.config import Settings
from homeshield.core import messages
from homeshield.server import create_app


class FakeChannel:
    classify_callback_message = staticmethod(WeChatChannel.classify_callback_message)
    parse_wechat_callback_xml = staticmethod(WeChatChannel.parse_wechat_callback_xml)
    def __init__(self): self.sent = []
    def verify_signature(self, *args): return True
    @staticmethod
    def passive_text_reply(data, text): return text
    async def send_customer_service(self, openid, text): self.sent.append((openid, text))
    async def send_template(self, openid, data, url=None, template_id=None): pass


def _callback(client, openid, content, msg_id="m1", msg_type="text", event=None, pic_url=None):
    fields = {"ToUserName": "gh", "FromUserName": openid, "CreateTime": "1", "MsgType": msg_type,
              "Content": content, "MsgId": msg_id}
    if pic_url: fields["PicUrl"] = pic_url
    if event: fields.update({"MsgType": "event", "Event": event})
    xml = "<xml>" + "".join(f"<{k}><![CDATA[{v}]]></{k}>" for k, v in fields.items()) + "</xml>"
    return client.post("/wechat/callback?signature=x&timestamp=1&nonce=2", content=xml)


def _app(tmp_path, **kwargs):
    app = create_app(Settings(mode="mock", db_path=str(tmp_path / "wechat.db"), wechat_token="t", **kwargs))
    fake = FakeChannel()
    app.state.deps.wechat = fake
    return TestClient(app), fake


def test_first_text_message_creates_user_and_runs_bare_query(tmp_path):
    client, fake = _app(tmp_path)
    response = _callback(client, "wx:first", "别告诉家人，马上转账5万")
    assert response.text == messages.RECEIVED_ACK
    deps = client.app.state.deps
    user = deps.repos.users.get_by_openid("wx:first")
    assert user is not None
    query = deps.conn.execute("SELECT * FROM query WHERE user_id=?", (user.id,)).fetchone()
    assert query is not None and query["content_type"] == "text"
    assert deps.conn.execute("SELECT COUNT(*) FROM verdict WHERE query_id=?", (query["id"],)).fetchone()[0] == 1
    assert deps.conn.execute("SELECT COUNT(*) FROM alert").fetchone()[0] == 0
    assert fake.sent and fake.sent[0][1].startswith("【结论】")
    repeated = _callback(client, "wx:first", "这条消息用相同 MsgId 重发", msg_id="m1")
    assert repeated.text == messages.RECEIVED_ACK
    assert deps.repos.users.get_by_openid("wx:first").id == user.id
    assert deps.conn.execute("SELECT COUNT(*) FROM user WHERE openid='wx:first'").fetchone()[0] == 1
    assert deps.conn.execute("SELECT COUNT(*) FROM query WHERE user_id=?", (user.id,)).fetchone()[0] == 1


def test_first_processing_sends_console_link_separately_when_configured(tmp_path):
    client, fake = _app(tmp_path, public_base_url="https://shield.test", wechat_appid="app", wechat_secret="secret")
    response = _callback(client, "wx:link", "今天天气不错")
    assert response.text == messages.RECEIVED_ACK
    assert len(fake.sent) == 2
    assert fake.sent[0][1].startswith("【结论】")
    assert fake.sent[1][1].startswith("你的个人控制台：https://shield.test/console?token=")


def test_first_url_message_creates_user_runs_query_and_sends_link(tmp_path):
    client, fake = _app(tmp_path, public_base_url="https://shield.test", wechat_appid="app", wechat_secret="secret")
    response = _callback(client, "wx:url", "https://example.com/refund", "url1")
    assert response.text == messages.RECEIVED_ACK
    deps = client.app.state.deps
    user = deps.repos.users.get_by_openid("wx:url")
    query = deps.conn.execute("SELECT * FROM query WHERE user_id=?", (user.id,)).fetchone()
    assert query and query["content_type"] == "url"
    assert any(text.startswith("你的个人控制台：https://shield.test") for _, text in fake.sent)


def test_first_image_message_creates_user_runs_query_and_sends_link(tmp_path, monkeypatch):
    class Response:
        content = b"image-bytes"

    class FakeHTTPClient:
        def __init__(self, *args, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, url): return Response()

    monkeypatch.setattr("homeshield.api.wechat.httpx.AsyncClient", FakeHTTPClient)
    client, fake = _app(tmp_path, public_base_url="https://shield.test", wechat_appid="app", wechat_secret="secret")
    response = _callback(client, "wx:image", "", "image1", msg_type="image", pic_url="https://img.test/a.jpg")
    assert response.text == messages.RECEIVED_ACK
    deps = client.app.state.deps
    user = deps.repos.users.get_by_openid("wx:image")
    query = deps.conn.execute("SELECT * FROM query WHERE user_id=?", (user.id,)).fetchone()
    assert query and query["content_type"] == "image" and query["content"]
    assert any("/console?token=" in text for _, text in fake.sent)


def test_subscribe_only_sends_welcome_without_creating_identity(tmp_path):
    client, fake = _app(tmp_path)
    assert _callback(client, "wx:subscribe", "", event="subscribe").text == "success"
    assert fake.sent == [("wx:subscribe", messages.WELCOME)]
    assert client.app.state.deps.repos.users.get_by_openid("wx:subscribe") is None


def test_invite_bind_direction_and_old_group_migration_hint(tmp_path):
    client, _ = _app(tmp_path)
    invite_reply = _callback(client, "wx:protector", "邀请 妈妈", "invite1").text
    assert "邀请码：" in invite_reply and "高危提醒" in invite_reply and "投票查看" in invite_reply
    code = invite_reply.split("邀请码：", 1)[1].splitlines()[0]
    bind_reply = _callback(client, "wx:protected", f"绑定 {code}", "bind1").text
    assert "已建立联防关系" in bind_reply and "解除" in bind_reply and "投票查看" in bind_reply
    deps = client.app.state.deps
    protector = deps.repos.users.get_by_openid("wx:protector")
    protected = deps.repos.users.get_by_openid("wx:protected")
    rel = deps.conn.execute("SELECT * FROM guard_relation WHERE protector_user_id=? AND protected_user_id=?",
                            (protector.id, protected.id)).fetchone()
    assert rel and rel["name"] == "妈妈"
    assert _callback(client, "wx:protected", "我的群", "old1").text.startswith("命令已更新")


def test_my_relations_allows_direct_query_and_exposes_personal_link(tmp_path):
    client, _ = _app(tmp_path, public_base_url="https://shield.test")
    response = _callback(client, "wx:solo", "我的联防", "mine1").text
    assert "还没有联防" in response and "直接转发" in response
    assert "https://shield.test/console?token=" in response
    assert client.app.state.deps.repos.users.get_by_openid("wx:solo") is not None
