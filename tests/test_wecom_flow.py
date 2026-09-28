"""企微(微信客服)消息流:派发器 + 轮询单次,离线 mock 全链路。"""
import asyncio

import pytest

from homeshield.api import wecom as wecom_api
from homeshield.core import messages
from homeshield.core.channels.wecom import WeComChannel
from homeshield.core.config import Settings
from homeshield.core.deps import build_deps

EID = "wmEtest0001"
KFID = "wkAtest0001"


class StubWecomChannel:
    configured = True
    api_ready = True

    def __init__(self):
        self.sent = []        # (open_kfid, touser, text)
        self.welcomes = []    # (welcome_code, text)
        self.app_messages = []  # (corp_userids, text)
        self.session_alerts = []  # (openid, text)
        self.media = {}
        self.next_sync = {"errcode": 0, "msg_list": [], "next_cursor": ""}

    async def kf_send_msg(self, open_kfid, touser, text):
        self.sent.append((open_kfid, touser, text))

    async def kf_send_welcome(self, code, text):
        self.welcomes.append((code, text))

    async def send_app_message(self, corp_userids, text):
        self.app_messages.append((corp_userids, text))

    async def send_session_message(self, openid, text):
        self.session_alerts.append((openid, text))

    async def download_media(self, media_id):
        return self.media[media_id]

    async def list_kf_accounts(self):
        return [{"open_kfid": KFID, "name": "小盾"}]

    async def kf_sync_msg(self, open_kfid, cursor):
        return dict(self.next_sync, msg_list=list(self.next_sync["msg_list"]))


def _deps(**kw):
    settings = Settings(db_path=":memory:", mode="mock", public_base_url="https://shield.example", **kw)
    deps = build_deps(settings)
    ch = StubWecomChannel()
    deps.wecom = ch
    return deps, ch


def _text_msg(content: str, msg_id: str = "m1") -> dict:
    return {"origin": 3, "external_userid": EID, "open_kfid": KFID, "msgid": msg_id,
            "msgtype": "text", "text": {"content": content}}


def _run(coro):
    return asyncio.run(coro)


def test_first_text_creates_user_replies_and_links():
    deps, ch = _deps()
    _run(wecom_api.handle_wecom_messages(deps, deps.verification, [_text_msg("帮我看看这条消息")]))
    user = deps.repos.users.get_by_openid("wxkf:" + EID)
    assert user is not None
    assert len(ch.sent) == 2  # 判定回复 + 个人控制台链接
    assert "console?token=" in ch.sent[1][2]


def test_duplicate_msgid_not_reprocessed():
    deps, ch = _deps()
    _run(wecom_api.handle_wecom_messages(deps, deps.verification, [_text_msg("帮我看看这条消息")]))
    first = len(ch.sent)
    _run(wecom_api.handle_wecom_messages(deps, deps.verification, [_text_msg("帮我看看这条消息")]))
    assert len(ch.sent) == first  # msg_id 幂等去重,无新增回复


def test_enter_session_sends_backend_welcome():
    deps, ch = _deps()
    event = {"origin": 4, "external_userid": EID, "open_kfid": KFID, "msgtype": "event",
             "event_type": "enter_session", "welcome_code": "WC1"}
    _run(wecom_api.handle_wecom_messages(deps, deps.verification, [event]))
    assert ch.welcomes == [("WC1", messages.WELCOME)]
    assert ch.sent == []  # 事件不产生判定回复
    assert deps.repos.users.get_by_openid("wxkf:" + EID) is None  # 事件不建身份


def test_relation_command_invite():
    deps, ch = _deps()
    _run(wecom_api.handle_wecom_messages(deps, deps.verification, [_text_msg("邀请 妈妈")]))
    assert "邀请码" in ch.sent[0][2]


def test_unsupported_msgtype_gets_guidance():
    deps, ch = _deps()
    msg = {"origin": 3, "external_userid": EID, "open_kfid": KFID, "msgid": "m9",
           "msgtype": "voice", "voice": {"media_id": "v1"}}
    _run(wecom_api.handle_wecom_messages(deps, deps.verification, [msg]))
    assert ch.sent == [(KFID, EID, messages.UNSUPPORTED_TYPE)]


def test_image_query_downloads_media():
    deps, ch = _deps()
    ch.media["med1"] = b"\x89PNG-fake"
    msg = {"origin": 3, "external_userid": EID, "open_kfid": KFID, "msgid": "mi1",
           "msgtype": "image", "image": {"media_id": "med1"}}
    _run(wecom_api.handle_wecom_messages(deps, deps.verification, [msg]))
    assert len(ch.sent) == 2 and ch.sent[0][2]  # 判定回复 + 链接


def test_link_message_becomes_text_query():
    deps, ch = _deps()
    msg = {"origin": 3, "external_userid": EID, "open_kfid": KFID, "msgid": "ml1",
           "msgtype": "link", "link": {"title": "领红包", "description": "点链接", "url": "https://evil.example/a"}}
    _run(wecom_api.handle_wecom_messages(deps, deps.verification, [msg]))
    assert len(ch.sent) == 2
    user = deps.repos.users.get_by_openid("wxkf:" + EID)
    rows = deps.conn.execute("SELECT content FROM query WHERE user_id=?", (user.id,)).fetchall()
    assert any("https://evil.example/a" in r["content"] for r in rows)


def test_merged_msg_flattens_text_items():
    deps, ch = _deps()
    msg = {"origin": 3, "external_userid": EID, "open_kfid": KFID, "msgid": "mm1",
           "msgtype": "merged_msg",
           "merged_msg": {"title": "聊天记录", "items": [
               {"msgtype": "text", "content": {"content": "第一步:加微信"}},
               {"msgtype": "image", "content": {"media_id": "x"}},
           ]}}
    _run(wecom_api.handle_wecom_messages(deps, deps.verification, [msg]))
    user = deps.repos.users.get_by_openid("wxkf:" + EID)
    rows = deps.conn.execute("SELECT content FROM query WHERE user_id=?", (user.id,)).fetchall()
    assert any("第一步:加微信" in r["content"] for r in rows)


def test_poll_once_advances_cursor_and_dispatches():
    deps, ch = _deps()
    ch.next_sync = {"errcode": 0, "msg_list": [_text_msg("帮我看看")], "next_cursor": "cursor-2"}
    cursors: dict[str, str] = {}
    _run(wecom_api.poll_once(deps, deps.verification, cursors))
    assert cursors[KFID] == "cursor-2"
    assert any("console?token=" in t for _, _, t in ch.sent)


def test_alert_router_wecom_wiring():
    settings = Settings(db_path=":memory:", mode="mock", public_base_url="",
                        wecom_token="tok", wecom_aes_key="a" * 43, wecom_corpid="ww1",
                        wecom_app_secret="s1", wecom_agent_id="1000002")
    deps = build_deps(settings)
    assert isinstance(deps.wecom, WeComChannel)
    assert deps.alert_router.wecom is deps.wecom


def test_wecom_member_repo_roundtrip():
    deps, _ = _deps()
    user = deps.repos.users.get_or_create("wxkf:repo-test")
    assert deps.repos.wecom_member.get(user.id) is None
    deps.repos.wecom_member.link(user.id, "ZhangSan")
    assert deps.repos.wecom_member.get(user.id) == "ZhangSan"
    deps.repos.wecom_member.link(user.id, "LiSi")
    assert deps.repos.wecom_member.get(user.id) == "LiSi"


def test_wecom_member_reassignment_moves_mapping():
    deps, _ = _deps()
    user_a = deps.repos.users.get_or_create("wxkf:reassign-a")
    user_b = deps.repos.users.get_or_create("wxkf:reassign-b")
    deps.repos.wecom_member.link(user_a.id, "WangWu")
    deps.repos.wecom_member.link(user_b.id, "WangWu")  # 同一企微成员改绑到 B
    assert deps.repos.wecom_member.get(user_a.id) is None
    assert deps.repos.wecom_member.get(user_b.id) == "WangWu"


def test_channel_constructs_with_partial_config():
    """只配密钥(未配回调三件套)时通道仍构造,拉取/告警可用,回调自门控关闭。"""
    settings = Settings(db_path=":memory:", mode="mock", public_base_url="",
                        wecom_corpid="ww1", wecom_app_secret="s1")
    deps = build_deps(settings)
    assert deps.wecom is not None
    assert deps.wecom.api_ready and not deps.wecom.configured
    assert deps.alert_router.wecom is None  # 缺 agentid,告警发送不启用


def test_wecom_alert_delivery_matrix():
    """有映射走应用消息;wxkf 无映射回落客服会话;公众号 openid 两路都不发。"""
    from homeshield.core.notifier import AlertRouter, AlertBroker
    deps, ch = _deps()
    router = AlertRouter(AlertBroker(), deps.repos, base_url="https://shield.example")
    router.wecom = ch
    u_app = deps.repos.users.get_or_create("wxkf:with-mapping")
    u_ses = deps.repos.users.get_or_create("wxkf:no-mapping")
    u_wx = deps.repos.users.get_or_create("oWxPublicOpenid123")
    deps.repos.wecom_member.link(u_app.id, "CorpZhang")
    deps.repos.alert.push_context = lambda alert_id: {"alert_id": alert_id, "token": "t", "name_at_alert": "妈妈"}
    recipients = [
        {"user_id": u_app.id, "openid": "wxkf:with-mapping", "alert_id": 1},
        {"user_id": u_ses.id, "openid": "wxkf:no-mapping", "alert_id": 2},
        {"user_id": u_wx.id, "openid": "oWxPublicOpenid123", "alert_id": 3},
    ]
    asyncio.run(router._send_wecom_alerts(recipients, "可疑内容"))
    assert len(ch.app_messages) == 1 and ch.app_messages[0][0] == ["CorpZhang"]
    assert "高危预警" in ch.app_messages[0][1] and "妈妈" in ch.app_messages[0][1]
    assert len(ch.session_alerts) == 1 and "高危预警" in ch.session_alerts[0][1]


def test_bare_dangerous_query_records_verdict_without_alerts():
    deps, ch = _deps()
    _run(wecom_api.handle_wecom_messages(deps, deps.verification, [_text_msg("别告诉家人，马上转账5万")]))
    user = deps.repos.users.get_by_openid("wxkf:" + EID)
    query = deps.conn.execute("SELECT * FROM query WHERE user_id=?", (user.id,)).fetchone()
    assert query is not None and query["content_type"] == "text"
    assert deps.conn.execute("SELECT COUNT(*) FROM verdict WHERE query_id=?", (query["id"],)).fetchone()[0] == 1
    assert deps.conn.execute("SELECT COUNT(*) FROM alert").fetchone()[0] == 0  # 无联防关系


def test_url_text_message_inferred_as_url():
    deps, ch = _deps()
    _run(wecom_api.handle_wecom_messages(deps, deps.verification, [_text_msg("https://example.com/refund", "url1")]))
    user = deps.repos.users.get_by_openid("wxkf:" + EID)
    query = deps.conn.execute("SELECT * FROM query WHERE user_id=?", (user.id,)).fetchone()
    assert query["content_type"] == "url"  # intake 依内容推断,通道不声明 text 锁死


def test_invite_bind_direction_and_old_group_hint():
    deps, ch = _deps()
    _run(wecom_api.handle_wecom_messages(deps, deps.verification, [_text_msg("邀请 妈妈", "invite1")]))
    code = ch.sent[0][2].split("邀请码：", 1)[1].splitlines()[0]
    bind_msg = {"origin": 3, "external_userid": "wmOther", "open_kfid": KFID, "msgid": "bind1",
                "msgtype": "text", "text": {"content": f"绑定 {code}"}}
    _run(wecom_api.handle_wecom_messages(deps, deps.verification, [bind_msg]))
    bind_reply = next(t for _, _, t in ch.sent if "已建立联防关系" in t)
    assert "解除" in bind_reply and "投票查看" in bind_reply
    old_msg = dict(bind_msg, msgid="old1", text={"content": "我的群"})
    _run(wecom_api.handle_wecom_messages(deps, deps.verification, [old_msg]))
    assert next(t for _, _, t in ch.sent if t.startswith("命令已更新"))
    protector = deps.repos.users.get_by_openid("wxkf:" + EID)
    protected = deps.repos.users.get_by_openid("wxkf:wmOther")
    rel = deps.conn.execute("SELECT * FROM guard_relation WHERE protector_user_id=? AND protected_user_id=?",
                            (protector.id, protected.id)).fetchone()
    assert rel and rel["name"] == "妈妈"


def test_my_relations_empty_shows_guidance_and_link():
    deps, ch = _deps()
    _run(wecom_api.handle_wecom_messages(deps, deps.verification, [_text_msg("我的联防", "mine1")]))
    mine = next(t for _, _, t in ch.sent if "还没有联防" in t)
    assert "console?token=" in mine


def test_kf_notify_triggers_pull_and_serializes():
    """回调通知触发一次拉取;游标存模块状态,第二次通知拉不到新消息不再派发。"""
    from homeshield.api import wecom as wecom_api
    wecom_api._CURSORS.clear()
    deps, ch = _deps()
    ch.next_sync = {"errcode": 0, "msg_list": [_text_msg("第一轮", "n1")], "next_cursor": "c1"}
    asyncio.run(wecom_api.handle_kf_notify(deps, deps.verification))
    first = len(ch.sent)
    assert wecom_api._CURSORS[KFID] == "c1"
    ch.next_sync = {"errcode": 0, "msg_list": [], "next_cursor": "c1"}
    asyncio.run(wecom_api.handle_kf_notify(deps, deps.verification))
    assert len(ch.sent) == first  # 同游标空拉取,无重复处理
