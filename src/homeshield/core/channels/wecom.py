"""企业微信(微信客服)适配器:回调验签加解密 + kf 消息收发客户端。

回调协议:GET 验活的 echostr 为密文,解密后必须原样返回明文;POST 报文含
<Encrypt> 密文,验签(msg_signature)后解密再解析。密文结构:AES-CBC
(key=EncodingAESKey 补位 base64,IV=key 前 16 字节),明文 = random(16B)
+ msg_len(4B 大端) + msg + receive_id(应为 corpid)。
kf 接口:消息经 sync_msg 拉取(应用身份必须显式传 open_kfid),回复走
send_msg;欢迎语走事件响应(welcome_code,不占 48h 窗口)。
本模块只做协议翻译与收发,业务判断在 core(verification/pipeline)。
"""
import base64
import hashlib
import logging
import secrets
import struct
import time
from secrets import token_bytes
from urllib.parse import quote

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from homeshield.core.config import Settings

logger = logging.getLogger(__name__)
API = "https://qyapi.weixin.qq.com/cgi-bin"


class WeComCryptoError(ValueError):
    """回调密文解密失败(密钥不符 / 填充非法 / receive_id 不匹配)。"""


class WeComDuplicateMobile(RuntimeError):
    """自助入录时手机号已存在于通讯录(视为"已入录"信号,调用方降级 getuserid)。"""


class WeComChannel:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.s = settings
        self._client = client or httpx.AsyncClient(timeout=10)
        self._key = base64.b64decode(settings.wecom_aes_key + "=") if settings.wecom_aes_key else b""
        self._token = ""
        self._token_exp = 0.0
        self._contact_token = ""
        self._contact_exp = 0.0
        self._accounts: tuple[float, list[dict]] = (0.0, [])

    # ---- 状态 ---------------------------------------------------------
    @property
    def configured(self) -> bool:
        """回调验签解密三件套齐备。"""
        return bool(self.s.wecom_token and self.s.wecom_aes_key and self.s.wecom_corpid)

    @property
    def api_ready(self) -> bool:
        """可调用 kf 接口(任一密钥已配置)。"""
        return bool(self.s.wecom_kf_secret or self.s.wecom_app_secret)

    # ---- 回调侧 -------------------------------------------------------
    def verify_signature(self, signature: str, timestamp: str, nonce: str, encrypt: str = "") -> bool:
        raw = "".join(sorted([self.s.wecom_token, timestamp, nonce, encrypt]))
        return hashlib.sha1(raw.encode()).hexdigest() == signature

    def decrypt(self, ciphertext_b64: str) -> str:
        if not self._key:
            raise WeComCryptoError("WECOM_AES_KEY 未配置")
        data = base64.b64decode(ciphertext_b64)
        if len(data) < 32 or len(data) % 16 != 0:
            raise WeComCryptoError("密文长度非法")
        decryptor = Cipher(algorithms.AES(self._key), modes.CBC(self._key[:16])).decryptor()
        plain = decryptor.update(data) + decryptor.finalize()
        pad = plain[-1]
        if not 1 <= pad <= 32:
            raise WeComCryptoError("填充非法")
        plain = plain[:-pad]
        (msg_len,) = struct.unpack(">I", plain[16:20])
        receive_id = plain[20 + msg_len:].decode()
        if self.s.wecom_corpid and receive_id != self.s.wecom_corpid:
            raise WeComCryptoError("receive_id 与 corpid 不匹配")
        return plain[20:20 + msg_len].decode()

    def encrypt(self, plaintext: str) -> str:
        """安全模式下回复报文用;当前回调只 ACK success,预留给后续被动回复。"""
        if not self._key:
            raise WeComCryptoError("WECOM_AES_KEY 未配置")
        msg = plaintext.encode()
        raw = token_bytes(16) + struct.pack(">I", len(msg)) + msg + self.s.wecom_corpid.encode()
        pad = 32 - len(raw) % 32
        raw += bytes([pad]) * pad
        encryptor = Cipher(algorithms.AES(self._key), modes.CBC(self._key[:16])).encryptor()
        return base64.b64encode(encryptor.update(raw) + encryptor.finalize()).decode()

    # ---- kf 接口 ------------------------------------------------------
    async def get_access_token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        secret = self.s.wecom_kf_secret or self.s.wecom_app_secret
        resp = await self._client.get(f"{API}/gettoken",
                                      params={"corpid": self.s.wecom_corpid, "corpsecret": secret})
        data = resp.json()
        if data.get("errcode"):
            raise RuntimeError(f"wecom gettoken failed: {data.get('errcode')} {data.get('errmsg')}")
        self._token = data["access_token"]
        self._token_exp = time.time() + int(data.get("expires_in", 7200))
        return self._token

    async def list_kf_accounts(self, max_age_seconds: int = 600) -> list[dict]:
        """客服账号列表;短缓存避免每个轮询周期重复拉取。"""
        cached_at, cached = self._accounts
        if cached and time.time() - cached_at < max_age_seconds:
            return cached
        token = await self.get_access_token()
        resp = await self._client.post(f"{API}/kf/account/list", params={"access_token": token}, json={})
        data = resp.json()
        if data.get("errcode"):
            raise RuntimeError(f"wecom account/list failed: {data.get('errcode')} {data.get('errmsg')}")
        self._accounts = (time.time(), data.get("account_list", []))
        return self._accounts[1]

    async def kf_sync_msg(self, open_kfid: str, cursor: str = "") -> dict:
        """拉取消息;应用身份必须显式传 open_kfid,否则报 95000。"""
        token = await self.get_access_token()
        resp = await self._client.post(f"{API}/kf/sync_msg", params={"access_token": token},
                                       json={"cursor": cursor, "token": "", "limit": 1000,
                                             "open_kfid": open_kfid})
        return resp.json()

    async def kf_send_msg(self, open_kfid: str, touser: str, text: str) -> dict:
        token = await self.get_access_token()
        resp = await self._client.post(f"{API}/kf/send_msg", params={"access_token": token},
                                       json={"touser": touser, "open_kfid": open_kfid,
                                             "msgtype": "text", "text": {"content": text[:2000]}})
        return resp.json()

    async def kf_send_welcome(self, code: str, text: str) -> dict:
        """enter_session 事件响应消息;不占 48h 窗口,须在事件后及时发送。"""
        token = await self.get_access_token()
        resp = await self._client.post(f"{API}/kf/send_msg", params={"access_token": token},
                                       json={"code": code, "msgtype": "text", "text": {"content": text[:2000]}})
        return resp.json()

    async def download_media(self, media_id: str) -> bytes:
        token = await self.get_access_token()
        resp = await self._client.get(f"{API}/media/get",
                                      params={"access_token": token, "media_id": media_id})
        if resp.headers.get("content-type", "").startswith("application/json"):
            data = resp.json()
            raise RuntimeError(f"wecom media/get failed: {data.get('errcode')} {data.get('errmsg')}")
        return resp.content

    async def send_session_message_result(self, openid: str, text: str) -> dict | None:
        """告警回落:未登记成员映射的 wxkf 联防者,向其客服会话直发(best-effort,
        受 48h 窗口约束)。保留平台错误码,持久派发据此分类失败;None 表示无路由。"""
        if not openid.startswith("wxkf:"):
            return None
        eid = openid[len("wxkf:"):]
        accounts = await self.list_kf_accounts()
        if not accounts:
            return None
        return await self.kf_send_msg(accounts[0]["open_kfid"], eid, text)

    # ---- 应用消息(告警触达微信插件) ----------------------------------
    async def send_app_message(self, corp_userids: list[str], text: str) -> dict:
        token = await self.get_access_token()
        resp = await self._client.post(f"{API}/message/send", params={"access_token": token},
                                       json={"touser": "|".join(corp_userids), "msgtype": "text",
                                             "agentid": int(self.s.wecom_agent_id or 0),
                                             "text": {"content": text[:2000]}})
        return resp.json()

    # ---- 推送开通(OAuth 身份映射 / 插件二维码 / 手机号辅映射) ---------
    def get_oauth_url(self, state: str) -> str:
        """企微网页授权链接(snsapi_base 静默);须已配置可信域名,否则回调 50001。"""
        base = self.s.public_base_url.rstrip("/")
        redirect = quote(f"{base}/wecom/oauth/callback", safe="")
        return ("https://open.weixin.qq.com/connect/oauth2/authorize?appid=" + self.s.wecom_corpid
                + f"&redirect_uri={redirect}&response_type=code&scope=snsapi_base"
                + f"&state={state}&agentid={self.s.wecom_agent_id}#wechat_redirect")

    async def exchange_code(self, code: str) -> dict:
        """网页授权 code 换身份:成员返回 userid;非成员返回 openid(+external_userid)。

        返回原始应答,错误语义(40029/50001/非成员)由领域层解释。"""
        token = await self.get_access_token()
        resp = await self._client.get(f"{API}/auth/getuserinfo",
                                      params={"access_token": token, "code": code})
        return resp.json()

    async def upload_media(self, image: bytes, filename: str = "qr.png") -> str:
        token = await self.get_access_token()
        resp = await self._client.post(f"{API}/media/upload",
                                       params={"access_token": token, "type": "image"},
                                       files={"media": (filename, image, "image/png")})
        data = resp.json()
        if data.get("errcode"):
            raise RuntimeError(f"wecom media/upload failed: {data.get('errcode')} {data.get('errmsg')}")
        return data["media_id"]

    async def kf_send_image(self, open_kfid: str, external_userid: str, media_id: str) -> dict:
        token = await self.get_access_token()
        resp = await self._client.post(f"{API}/kf/send_msg", params={"access_token": token},
                                       json={"touser": external_userid, "open_kfid": open_kfid,
                                             "msgtype": "image", "image": {"media_id": media_id}})
        return resp.json()

    async def get_userid_by_mobile(self, mobile: str) -> str | None:
        """手机号换成员 userid(自建应用 token,需通讯录查看权限);查无返回 None(46004)。

        错误率风控(超企业人数 20% 封 1 天)由调用方护栏约束。"""
        token = await self.get_access_token()
        resp = await self._client.post(f"{API}/user/getuserid", params={"access_token": token},
                                       json={"mobile": mobile})
        data = resp.json()
        if data.get("errcode") == 46004:
            return None
        if data.get("errcode"):
            raise RuntimeError(f"wecom getuserid failed: {data.get('errcode')} {data.get('errmsg')}")
        return data["userid"]

    async def get_contact_token(self) -> str:
        """通讯录同步助手 token(自助入录写通讯录用);未配置即抛错。"""
        if not self.s.wecom_contact_secret:
            raise RuntimeError("WECOM_CONTACT_SECRET 未配置,自助入录不可用")
        if self._contact_token and time.time() < self._contact_exp - 60:
            return self._contact_token
        resp = await self._client.get(f"{API}/gettoken",
                                      params={"corpid": self.s.wecom_corpid,
                                              "corpsecret": self.s.wecom_contact_secret})
        data = resp.json()
        if data.get("errcode"):
            raise RuntimeError(f"wecom gettoken(contact) failed: {data.get('errcode')} {data.get('errmsg')}")
        self._contact_token = data["access_token"]
        self._contact_exp = time.time() + int(data.get("expires_in", 7200))
        return self._contact_token

    async def create_member(self, mobile: str) -> str:
        """自助入录:创建成员并返回其 userid;手机号已存在抛 WeComDuplicateMobile。"""
        token = await self.get_contact_token()
        userid = "hs" + secrets.token_hex(5)  # 合规 userid:字母开头,限字母数字._-,企业内唯一
        resp = await self._client.post(f"{API}/user/create", params={"access_token": token},
                                       json={"userid": userid, "name": f"联防-{mobile[-4:]}",
                                             "mobile": mobile})
        data = resp.json()
        if data.get("errcode"):
            if data.get("errcode") == 60103:  # mobile 已存在 → "已入录"信号,调用方降级 getuserid
                raise WeComDuplicateMobile(data.get("errmsg", "mobile exists"))
            raise RuntimeError(f"wecom user/create failed: {data.get('errcode')} {data.get('errmsg')}")
        return userid
