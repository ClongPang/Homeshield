"""推送通道自助开通:绑定凭证、OAuth 映射、送达单信号闭环与 EARS 场景映射。

覆盖 spec-delta 的可自动化场景;真机相关(插件二维码可达性、微信端 OAuth
行为)由阶段 0 前置验证与端到端验收承担。
"""
from dataclasses import replace
from urllib.parse import parse_qs, urlparse

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from homeshield.core.config import Settings
from homeshield.core.notifier import AlertBroker, AlertRouter
from homeshield.core.push import (
    CONFIRM_TIMEOUT_SECONDS, MOBILE_PROBE_FAILURE_LIMIT, PushService, compute_status,
)
from homeshield.core.models import Level, Mode
from homeshield.server import create_app

QR_BYTES = b"\x89PNG-fake-qr"


class StubPushChannel:
    configured = True
    api_ready = True

    def __init__(self):
        self.app_messages = []      # (corp_userids, text)
        self.app_result = {"errcode": 0}
        self.qr_uploads = []        # image bytes
        self.qr_sent = []           # (open_kfid, external_userid, media_id)
        self.session_texts = []     # (open_kfid, touser, text)
        self.exchange_results = {"*": {"userid": "CorpX"}}
        self.mobile_results = {}    # mobile -> userid | None
        self.created_members = []   # mobile
        self.accounts = [{"open_kfid": "wkA001", "name": "小盾"}]

    async def exchange_code(self, code):
        return self.exchange_results.get(code, self.exchange_results.get("*", {}))

    async def send_app_message(self, corp_userids, text):
        self.app_messages.append((corp_userids, text))
        return dict(self.app_result)

    async def upload_media(self, image, filename="qr.png"):
        self.qr_uploads.append(image)
        return f"media-{len(self.qr_uploads)}"

    async def kf_send_image(self, open_kfid, external_userid, media_id):
        self.qr_sent.append((open_kfid, external_userid, media_id))
        return {"errcode": 0}

    async def kf_send_msg(self, open_kfid, touser, text):
        self.session_texts.append((open_kfid, touser, text))
        return {"errcode": 0}

    async def list_kf_accounts(self):
        return self.accounts

    async def kf_sync_msg(self, open_kfid, cursor):
        return {"errcode": 0, "msg_list": [], "next_cursor": cursor}

    def get_oauth_url(self, state):
        return f"https://open.weixin.qq.com/connect/oauth2/authorize?state={state}&scope=snsapi_base"

    async def get_userid_by_mobile(self, mobile):
        return self.mobile_results.get(mobile, "MISSING-SENTINEL")

    async def create_member(self, mobile):
        self.created_members.append(mobile)
        return f"hs{mobile[-6:]}"


def _location(resp) -> str:
    return resp.headers["location"]


def _state_of(url: str) -> str:
    return parse_qs(urlparse(url).query)["state"][0]


@pytest_asyncio.fixture(loop_scope="session")
async def client(tmp_path):
    qr_path = tmp_path / "qr.png"
    qr_path.write_bytes(QR_BYTES)
    settings = replace(Settings.load(), mode="mock",
                       wecom_corpid="ww1", wecom_agent_id="1000002", wecom_app_secret="s1",
                       wecom_kf_secret="", wecom_token="", wecom_aes_key="",
                       public_base_url="https://shield.example", wecom_plugin_qr_path=str(qr_path))
    app = create_app(settings)
    stub = StubPushChannel()
    deps = app.state.deps
    deps.wecom = stub
    deps.alert_router.wecom = stub
    app.state.push.channel = stub
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://shield.example") as c:
            c.app, c.stub, c.qr_path = app, stub, qr_path
            yield c


async def _user(client, openid):
    return await client.app.state.deps.repos.users.get_or_create(openid)


# ---- Requirement: 绑定入口统一为带凭证的开通链接 --------------------------

async def test_oauth_start_requires_token(client):
    assert (await client.get("/wecom/oauth/start")).status_code == 401
    assert (await client.get("/wecom/oauth/start", params={"token": "bad"})).status_code == 401


async def test_oauth_start_delivers_qr_first_screen(client):
    """首屏二维码:打开开通链接即经客服会话下发,先于绑定(关注前置)。"""
    user = await _user(client, "wxkf:push-start")
    resp = await client.get("/wecom/oauth/start", params={"token": user.token}, follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert "open.weixin.qq.com" in _location(resp) and "snsapi_base" in _location(resp)
    assert client.stub.qr_uploads and client.stub.qr_sent  # 二维码图片已上传并下发
    assert any("扫码" in t for _, _, t in client.stub.session_texts)  # 配套引导文案


async def test_push_status_requires_token(client):
    assert (await client.get("/api/push/status")).status_code == 401


# ---- Requirement: OAuth 身份映射与绑定条件 -------------------------------

async def test_oauth_callback_binds_member_and_sends_test_message(client):
    user = await _user(client, "wxkf:push-bind")
    start = await client.get("/wecom/oauth/start", params={"token": user.token}, follow_redirects=False)
    resp = await client.get("/wecom/oauth/callback",
                            params={"code": "ok", "state": _state_of(_location(start))},
                            follow_redirects=False)
    assert "/console" in _location(resp) and "push=bound" in _location(resp)
    member = await client.app.state.deps.repos.wecom_member.get_member(user.id)
    assert member["corp_userid"] == "CorpX" and member["bound_via"] == "oauth"
    assert member["verified_at"] is None and member["bound_at"] is not None
    assert len(client.stub.app_messages) == 1  # 绑定完成即发测试消息(确认链接)
    assert "/api/push/verify?token=" in client.stub.app_messages[0][1]


async def test_oauth_callback_rejects_non_member(client):
    client.stub.exchange_results["*"] = {"openid": "oPublic123"}
    user = await _user(client, "wxkf:push-nonmember")
    start = await client.get("/wecom/oauth/start", params={"token": user.token}, follow_redirects=False)
    resp = await client.get("/wecom/oauth/callback",
                            params={"code": "ok", "state": _state_of(_location(start))},
                            follow_redirects=False)
    assert "push_error=not_member" in _location(resp)
    assert await client.app.state.deps.repos.wecom_member.get_member(user.id) is None


async def test_oauth_callback_diagnoses_exchange_failures(client):
    user = await _user(client, "wxkf:push-diag")
    client.stub.exchange_results = {"bad": {"errcode": 40029}, "dom": {"errcode": 50001}}
    for code, key in (("bad", "invalid_code"), ("dom", "domain_mismatch")):
        start = await client.get("/wecom/oauth/start", params={"token": user.token}, follow_redirects=False)
        resp = await client.get("/wecom/oauth/callback",
                                params={"code": code, "state": _state_of(_location(start))},
                                follow_redirects=False)
        assert f"push_error={key}" in _location(resp)
        assert await client.app.state.deps.repos.wecom_member.get_member(user.id) is None


async def test_oauth_state_replay_rejected(client):
    user = await _user(client, "wxkf:push-replay")
    start = await client.get("/wecom/oauth/start", params={"token": user.token}, follow_redirects=False)
    state = _state_of(_location(start))
    first = await client.get("/wecom/oauth/callback", params={"code": "ok", "state": state},
                             follow_redirects=False)
    assert "push=bound" in _location(first)
    second = await client.get("/wecom/oauth/callback", params={"code": "ok", "state": state})
    assert second.status_code == 200 and "已失效" in second.text  # 一次性,重放拒绝


# ---- Requirement: 手机号辅映射 -------------------------------------------

async def test_enroll_mobile_rejects_invalid_format_without_counting(client):
    push = client.app.state.push
    before = push.guard._failures
    resp = await client.post("/api/push/enroll-mobile", json={"token": (await _user(client, "wxkf:m-invalid")).token,
                                                             "mobile": "12345"})
    assert resp.status_code == 400
    assert push.guard._failures == before  # 格式预校验失败不计数


async def test_enroll_mobile_hit_binds_and_sends_test_message(client):
    client.stub.mobile_results = {"13800000001": "CorpMobile"}
    user = await _user(client, "wxkf:m-hit")
    resp = await client.post("/api/push/enroll-mobile", json={"token": user.token, "mobile": "13800000001"})
    assert resp.status_code == 200 and resp.json()["status"] == "bound"
    member = await client.app.state.deps.repos.wecom_member.get_member(user.id)
    assert member["corp_userid"] == "CorpMobile" and member["bound_via"] == "mobile"
    assert len(client.stub.app_messages) == 1


async def test_enroll_mobile_miss_guides_when_self_enroll_off(client):
    client.stub.mobile_results = {"13800000002": None}
    user = await _user(client, "wxkf:m-miss")
    resp = await client.post("/api/push/enroll-mobile", json={"token": user.token, "mobile": "13800000002"})
    assert resp.status_code == 200 and resp.json()["status"] == "not_found"
    assert "通讯录" in resp.json()["text"]
    assert client.stub.created_members == []


async def test_enroll_mobile_self_enroll_creates_member(client, tmp_path):
    settings = replace(client.app.state.deps.settings, push_self_enroll=True,
                       wecom_contact_secret="contact-secret")
    client.app.state.deps.settings = settings
    client.app.state.push.s = settings
    client.stub.mobile_results = {"13800000003": None}
    user = await _user(client, "wxkf:m-enroll")
    resp = await client.post("/api/push/enroll-mobile", json={"token": user.token, "mobile": "13800000003"})
    assert resp.json()["status"] == "bound"
    assert client.stub.created_members == ["13800000003"]
    member = await client.app.state.deps.repos.wecom_member.get_member(user.id)
    assert member["bound_via"] == "mobile"


async def test_enroll_mobile_guard_blocks_probe(client):
    push = client.app.state.push
    for _ in range(MOBILE_PROBE_FAILURE_LIMIT):
        push.guard.record_failure()
    user = await _user(client, "wxkf:m-guard")
    resp = await client.post("/api/push/enroll-mobile", json={"token": user.token, "mobile": "13800000004"})
    assert resp.status_code == 429
    assert client.stub.mobile_results == {}  # 护栏拦截,未触达企微接口


# ---- Requirement: 送达确认单信号闭环 -------------------------------------

async def test_bound_without_confirmation_is_not_ready(client):
    """未关注插件/未确认前,通道不得呈现为送达就绪。"""
    user = await _user(client, "wxkf:push-unconfirmed")
    await client.app.state.deps.repos.wecom_member.link(user.id, "CorpU", via="oauth")
    status = (await client.get("/api/push/status", params={"token": user.token})).json()
    assert status["status"] == "bound" and status["next"] == "confirm"


async def test_confirm_timeout_computes_abnormal(client):
    import time
    now = time.time()
    stale = {"bound_at": now - CONFIRM_TIMEOUT_SECONDS - 10, "verified_at": None,
             "last_fail_at": None, "last_fail_reason": None}
    result = compute_status(stale, now)
    assert result["status"] == "abnormal" and result["next"] == "retest"
    fresh = dict(stale, bound_at=now - 60)
    assert compute_status(fresh, now)["status"] == "bound"


async def test_verify_marks_ready_via_link_and_post(client):
    user = await _user(client, "wxkf:push-verify")
    await client.app.state.deps.repos.wecom_member.link(user.id, "CorpV", via="oauth")
    resp = await client.get("/api/push/verify", params={"token": user.token}, follow_redirects=False)
    assert "/console" in _location(resp)
    status = (await client.get("/api/push/status", params={"token": user.token})).json()
    assert status["status"] == "verified"
    # 解绑重绑后再用 POST 确认(控制台按钮口径)
    await client.app.state.deps.repos.wecom_member.link(user.id, "CorpV2", via="oauth")
    resp = await client.post("/api/push/verify", json={"token": user.token})
    assert resp.json()["status"] == "verified"


async def test_sync_send_error_marks_abnormal_and_retest_recovers(client):
    """同步报错即异常入口:errcode≠0 落失败信号;重测清信号并重发。"""
    user = await _user(client, "wxkf:push-abnormal")
    await client.app.state.deps.repos.wecom_member.link(user.id, "CorpA", via="oauth")
    client.stub.app_result = {"errcode": 43004, "errmsg": "not following plugin"}
    ok = await client.app.state.push.send_test_message(user)
    assert ok is False
    status = (await client.get("/api/push/status", params={"token": user.token})).json()
    assert status["status"] == "abnormal" and "43004" in status["reason"] and status["next"] == "retest"
    client.stub.app_result = {"errcode": 0}
    resp = await client.post("/api/push/retest", json={"token": user.token})
    assert resp.json()["status"] == "bound"
    status = (await client.get("/api/push/status", params={"token": user.token})).json()
    assert status["status"] == "bound" and status["reason"] is None


async def test_alert_app_message_rejection_records_failure_signal(deps):
    """告警腿的同步拒绝同样落失败信号;客服会话回落行为不变。"""
    class RejectingSender:
        async def send_app_message(self, corp_userids, text):
            return {"errcode": 43004}

        async def send_session_message(self, openid, text):
            self.session_alerts = getattr(self, "session_alerts", []) + [(openid, text)]

    queryer = await deps.repos.users.get_or_create("wxkf:pushfail:queryer")
    protector = await deps.repos.users.get_or_create("wxkf:pushfail:protector")
    invite = await deps.relations.issue_invite(protector.id, "妈妈")
    await deps.relations.join(queryer.openid, invite["code"])
    await deps.repos.wecom_member.link(protector.id, "CorpFail")
    query_id = await deps.repos.query.insert(queryer.id, "text", "内容", None)
    verdict_id = await deps.repos.verdict.insert(query_id, Level.DANGEROUS, [], [], "理由", "回复", 1, Mode.MOCK)
    fanout = await deps.repos.alert.record_alerts_for_verdict(verdict_id, query_id)
    sender = RejectingSender()
    router = AlertRouter(AlertBroker(), deps.repos, "https://shield.test")
    router.wecom = sender
    await router._send_wecom_alerts(fanout["recipients"], Level.DANGEROUS)
    member = await deps.repos.wecom_member.get_member(protector.id)
    assert member["last_fail_at"] is not None and "43004" in member["last_fail_reason"]


# ---- Requirement: 控制台状态呈现与解绑 -----------------------------------

async def test_status_flags_and_unbind(client):
    user = await _user(client, "wxkf:push-flags")
    status = (await client.get("/api/push/status", params={"token": user.token})).json()
    assert status["status"] == "unbound" and status["next"] == "provision"
    assert status["qr_available"] is True and status["oauth_available"] is True
    assert (await client.get("/api/push/qr", params={"token": user.token})).status_code == 200
    resp = await client.post("/api/push/unbind", json={"token": user.token})
    assert resp.json()["status"] == "unbound"
    assert (await client.get("/api/push/status", params={"token": user.token})).json()["status"] == "unbound"


async def test_status_change_pushes_sse_event(client):
    user = await _user(client, "wxkf:push-sse")
    queue = client.app.state.deps.broker.subscribe(user.id)
    await client.post("/api/push/verify", json={"token": user.token})  # 未绑定 → 404
    (await client.post("/api/push/enroll-mobile", json={"token": user.token, "mobile": "bad"}))
    await client.app.state.deps.repos.wecom_member.link(user.id, "CorpS", via="oauth")
    await client.post("/api/push/verify", json={"token": user.token})
    kinds = []
    while not queue.empty():
        kinds.append(queue.get_nowait().get("kind"))
    assert "push_status" in kinds
    client.app.state.deps.broker.unsubscribe(user.id, queue)


# ---- Requirement: 未验证企业前提 -----------------------------------------

async def test_provisioning_degrades_when_channel_absent(client):
    client.app.state.push.channel = None
    client.app.state.deps.wecom = None
    user = await _user(client, "wxkf:push-noflag")
    status = (await client.get("/api/push/status", params={"token": user.token})).json()
    assert status["oauth_available"] is False
    assert (await client.get("/wecom/oauth/start", params={"token": user.token})).status_code == 404


# ---- Requirement: 插件关注前置(资产缺失降级) ------------------------------

async def test_qr_asset_missing_degrades_to_console_fallback(client):
    client.qr_path.unlink()
    client.app.state.push._qr_media = None
    user = await _user(client, "wxkf:push-noqr")
    status = (await client.get("/api/push/status", params={"token": user.token})).json()
    assert status["qr_available"] is False
    assert (await client.get("/api/push/qr", params={"token": user.token})).status_code == 404
    resp = await client.get("/wecom/oauth/start", params={"token": user.token}, follow_redirects=False)
    assert resp.status_code in (302, 307)  # 二维码缺失不阻塞开通,控制台纯文字兜底
    assert client.stub.qr_sent == []


async def test_rebind_resets_confirmation_window(deps):
    user = await deps.repos.users.get_or_create("wxkf:rebind-reset")
    repo = deps.repos.wecom_member
    await repo.link(user.id, "CorpR1", via="oauth")
    await repo.mark_verified(user.id)
    member = await repo.get_member(user.id)
    assert member["verified_at"] is not None
    await repo.link(user.id, "CorpR1", via="mobile")  # 换绑重开确认窗口
    member = await repo.get_member(user.id)
    assert member["verified_at"] is None and member["bound_via"] == "mobile"
    await repo.mark_failed(user.id, "测试原因")
    member = await repo.get_member(user.id)
    assert "测试原因" in member["last_fail_reason"]
    await repo.touch_confirm(user.id)
    member = await repo.get_member(user.id)
    assert member["last_fail_at"] is None and member["last_fail_reason"] is None
