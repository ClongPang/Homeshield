"""推送通道自助开通的领域逻辑:一次性绑定凭证、四态状态计算、探测护栏与流程编排。

四态(未绑定/已绑定未确认/送达就绪/异常)完全由 wecom_member 行计算得出,不落
状态列:异常 = 同步投递报错(last_fail_at 晚于 verified_at)或确认超时(计算态)。
企微官方没有应用消息投递失败回调(errcode=0 只代表提交成功),因此失败信号只在
同步报错时写入,送达闭环只认用户确认。通道适配器以协议注入,本模块不做 IO 之外
的接口调用;一次性 state 凭证沿用单进程假设(提案决策表)。
"""
import logging
import secrets
import time
from pathlib import Path

from homeshield.core import messages
from homeshield.core.models import User

logger = logging.getLogger(__name__)

# 绑定后等待用户确认测试消息的窗口;超时按异常态呈现(计算态,不引入定时器)
CONFIRM_TIMEOUT_SECONDS = 48 * 3600
# getuserid 官方风控:错误次数超企业人数上限 20% 封 1 天;未验证企业人数上限 200,
# 护栏取 40 次/24h 滑动窗,格式预校验失败不计数
MOBILE_PROBE_FAILURE_LIMIT = 40
MOBILE_PROBE_WINDOW_SECONDS = 86400
# 临时素材 media_id 缓存时长(素材本身 3 天有效,取保守值)
QR_MEDIA_TTL_SECONDS = 2 * 86400

STATUS_UNBOUND = "unbound"
STATUS_BOUND = "bound"
STATUS_READY = "verified"
STATUS_ABNORMAL = "abnormal"

STATUS_TEXTS = {
    STATUS_UNBOUND: "未开通推送",
    STATUS_BOUND: "已绑定，等待确认测试消息",
    STATUS_READY: "送达就绪",
    STATUS_ABNORMAL: "通道异常",
}

MOBILE_GUIDE_TEXT = "这个手机号不在企业通讯录里。请联系运营者把你加入通讯录后重试，或改用微信授权开通。"


class PushStateStore:
    """OAuth state 一次性凭证:进程内存放,超 TTL 或已消费即失效。"""

    def __init__(self, ttl: int = 600):
        self._ttl = ttl
        self._store: dict[str, tuple[int, float]] = {}

    def issue(self, user_id: int) -> str:
        state = secrets.token_urlsafe(24)
        self._store[state] = (user_id, time.time() + self._ttl)
        return state

    def consume(self, state: str) -> int | None:
        item = self._store.pop(state or "", None)
        if item is None or item[1] < time.time():
            return None
        return item[0]


class MobileProbeGuard:
    """仅用户自报触发的失败计数护栏;格式预校验失败不计数(见 enroll 调用方)。"""

    def __init__(self, limit: int = MOBILE_PROBE_FAILURE_LIMIT, window: int = MOBILE_PROBE_WINDOW_SECONDS):
        self._limit, self._window = limit, window
        self._window_start = 0.0
        self._failures = 0

    def allow(self) -> bool:
        self._roll()
        return self._failures < self._limit

    def record_failure(self) -> None:
        self._roll()
        self._failures += 1

    def _roll(self) -> None:
        now = time.time()
        if now - self._window_start >= self._window:
            self._window_start, self._failures = now, 0


def compute_status(member: dict | None, now: float, confirm_timeout: int = CONFIRM_TIMEOUT_SECONDS) -> dict:
    """把 wecom_member 行折算成四态 + 下一步动作;reason 供控制台如实呈现。"""
    if member is None:
        return {"status": STATUS_UNBOUND, "text": STATUS_TEXTS[STATUS_UNBOUND], "next": "provision", "reason": None}
    last_fail = member.get("last_fail_at")
    verified = member.get("verified_at")
    bound_at = member.get("bound_at")
    if last_fail and (verified is None or int(last_fail) > int(verified)):
        return {"status": STATUS_ABNORMAL, "text": STATUS_TEXTS[STATUS_ABNORMAL], "next": "retest",
                "reason": member.get("last_fail_reason") or "投递异常"}
    if verified:
        return {"status": STATUS_READY, "text": STATUS_TEXTS[STATUS_READY], "next": None, "reason": None}
    if bound_at and now - int(bound_at) > confirm_timeout:
        return {"status": STATUS_ABNORMAL, "text": STATUS_TEXTS[STATUS_ABNORMAL], "next": "retest",
                "reason": "确认超时：未在时限内确认测试消息"}
    return {"status": STATUS_BOUND, "text": STATUS_TEXTS[STATUS_BOUND], "next": "confirm", "reason": None}


class PushService:
    """开通流程编排:二维码前置下发 → OAuth/手机号映射 → 测试消息 → 用户确认。

    通道适配器以协议注入；channel 为 None 时
    全部能力降级为"未启用",状态仍可如实呈现。
    """

    def __init__(self, repos, settings, channel=None):
        self.repos = repos
        self.s = settings
        self.channel = channel
        self.states = PushStateStore(settings.push_state_ttl)
        self.guard = MobileProbeGuard()
        self._qr_media: tuple[float, float, str] | None = None  # (资产 mtime, 上传时间, media_id)

    # ---- 资产与能力 -----------------------------------------------------
    def qr_available(self) -> bool:
        return Path(self.s.wecom_plugin_qr_path).is_file()

    @property
    def contact_available(self) -> bool:
        return bool(self.channel is not None and self.s.wecom_contact_secret)

    # ---- 状态 -----------------------------------------------------------
    async def status_for(self, user_id: int) -> dict:
        member = await self.repos.wecom_member.get_member(user_id)
        result = compute_status(member, time.time())
        result["bound_via"] = member["bound_via"] if member else None
        return result

    # ---- 映射 -----------------------------------------------------------
    async def bind_oauth(self, user: User, code: str) -> dict:
        """code 换身份并绑定;仅成员 userid 绑定,非成员/换取失败如实返回原因键。"""
        if self.channel is None:
            return {"ok": False, "reason": "oauth_unavailable"}
        data = await self.channel.exchange_code(code)
        userid = data.get("userid")
        if not userid:
            errcode = data.get("errcode")
            if errcode in (40029, 42063, 91003):
                reason = "invalid_code"
            elif errcode == 50001:
                reason = "domain_mismatch"
            elif data.get("openid") or data.get("external_userid"):
                reason = "not_member"
            else:
                reason = "oauth_failed"
            logger.info("push oauth bind rejected user=%s errcode=%s", user.id, errcode)
            return {"ok": False, "reason": reason}
        await self.repos.wecom_member.link(user.id, str(userid), via="oauth")
        return {"ok": True, "reason": None}

    async def enroll_mobile(self, user: User, mobile: str) -> dict:
        """手机号辅映射:预校验(不计数) → 探测 → 未命中按开关入录。"""
        if self.channel is None:
            return {"ok": False, "status": "unavailable", "text": "推送开通暂未启用。"}
        mobile = (mobile or "").strip()
        if not _valid_mobile(mobile):
            return {"ok": False, "status": "invalid", "text": "手机号格式不正确，请填写 11 位大陆手机号。"}
        if not self.guard.allow():
            return {"ok": False, "status": "guarded", "text": "尝试次数过多，请明天再试。"}
        userid = None
        try:
            userid = await self.channel.get_userid_by_mobile(mobile)
        except Exception:
            logger.warning("push getuserid failed user=%s", user.id, exc_info=True)
            self.guard.record_failure()
            return {"ok": False, "status": "probe_failed", "text": "开通没有完成，请稍后重试。"}
        if userid is None:
            self.guard.record_failure()
            if self.s.push_self_enroll and self.contact_available:
                try:
                    userid = await self.channel.create_member(mobile)
                except Exception:
                    logger.warning("push self enroll failed user=%s", user.id, exc_info=True)
                    return {"ok": False, "status": "enroll_failed", "text": "登记没有完成，请稍后重试。"}
            else:
                return {"ok": False, "status": "not_found", "text": MOBILE_GUIDE_TEXT}
        await self.repos.wecom_member.link(user.id, str(userid), via="mobile")
        return {"ok": True, "status": "bound", "text": STATUS_TEXTS[STATUS_BOUND]}

    # ---- 触达 -----------------------------------------------------------
    async def send_test_message(self, user: User) -> bool:
        """绑定/重测后发测试应用消息;同步报错写失败信号(单信号闭环的错误入口)。"""
        corp_userid = await self.repos.wecom_member.get(user.id)
        if corp_userid is None or self.channel is None or not self.s.public_base_url:
            return False
        url = f"{self.s.public_base_url.rstrip('/')}/api/push/verify?token={user.token}"
        try:
            res = await self.channel.send_app_message([corp_userid], messages.PUSH_TEST_TEXT.format(url=url))
        except Exception:
            logger.warning("push test message failed user=%s", user.id, exc_info=True)
            await self.repos.wecom_member.mark_failed(user.id, "测试消息发送异常")
            return False
        if isinstance(res, dict) and (res.get("errcode") not in (0, None) or res.get("fail_list")):
            reason = f"测试消息发送失败 errcode={res.get('errcode')}"
            logger.warning("push test message rejected user=%s res=%s", user.id, res)
            await self.repos.wecom_member.mark_failed(user.id, reason)
            return False
        return True

    async def deliver_plugin_qr(self, user: User) -> bool:
        """插件邀请二维码经客服会话下发(第一屏动作);失败静默,控制台兜底展示。"""
        if self.channel is None or not self.qr_available() or not user.openid.startswith("wxkf:"):
            return False
        media_id = await self._plugin_qr_media_id()
        if media_id is None:
            return False
        accounts = await self.channel.list_kf_accounts()
        if not accounts:
            return False
        res = await self.channel.kf_send_image(accounts[0]["open_kfid"], user.openid[len("wxkf:"):], media_id)
        ok = res.get("errcode") == 0 and not res.get("fail_list")
        if ok:
            try:
                await self.channel.kf_send_msg(accounts[0]["open_kfid"], user.openid[len("wxkf:"):],
                                               messages.PUSH_QR_TEXT)
            except Exception:
                logger.warning("push qr text delivery failed", exc_info=True)
        return ok

    async def _plugin_qr_media_id(self) -> str | None:
        path = Path(self.s.wecom_plugin_qr_path)
        try:
            mtime = path.stat().st_mtime
            if self._qr_media and self._qr_media[0] == mtime and time.time() - self._qr_media[1] < QR_MEDIA_TTL_SECONDS:
                return self._qr_media[2]
            media_id = await self.channel.upload_media(path.read_bytes(), path.name)
        except OSError:
            logger.warning("push qr asset unreadable: %s", path)
            return None
        except Exception:
            logger.warning("push qr media upload failed", exc_info=True)
            return None
        self._qr_media = (mtime, time.time(), media_id)
        return media_id


def _valid_mobile(mobile: str) -> bool:
    return len(mobile) == 11 and mobile.startswith("1") and mobile.isdigit()
