"""微信公众号适配器。

回调 5s 窗口内必须 ACK success,判定结果经客服接口异步回复
(48h 内 5 条额度);模板消息无 48h 限制。
本模块只做协议翻译与发送,业务判断在领域层。
"""
import hashlib
import time
import xml.etree.ElementTree as ET

import httpx

from homeshield.core.config import Settings


class WeChatChannel:
    API = "https://api.weixin.qq.com"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.s = settings
        self._client = client or httpx.AsyncClient(timeout=10)
        self._token = ""
        self._token_exp = 0.0

    # ---- 回调侧 -------------------------------------------------------
    def verify_signature(self, signature: str, timestamp: str, nonce: str) -> bool:
        raw = "".join(sorted([self.s.wechat_token, timestamp, nonce]))
        return hashlib.sha1(raw.encode()).hexdigest() == signature

    @staticmethod
    def parse_wechat_callback_xml(xml_bytes: bytes) -> dict:
        root = ET.fromstring(xml_bytes)
        return {child.tag: (child.text or "") for child in root}

    @staticmethod
    def classify_callback_message(data: dict) -> str:
        """回调分类:subscribe / text / image / unsupported / ignore。"""
        msg_type = data.get("MsgType", "")
        if msg_type == "event":
            return "subscribe" if data.get("Event") == "subscribe" else "ignore"
        if msg_type in ("text", "image"):
            return msg_type
        return "unsupported"

    @staticmethod
    def passive_text_reply(data: dict, text: str) -> str:
        """5s 窗口内的被动文本回复,收发双方字段互换。"""
        return (
            "<xml>"
            f"<ToUserName><![CDATA[{data.get('FromUserName', '')}]]></ToUserName>"
            f"<FromUserName><![CDATA[{data.get('ToUserName', '')}]]></FromUserName>"
            f"<CreateTime>{int(time.time())}</CreateTime>"
            "<MsgType><![CDATA[text]]></MsgType>"
            f"<Content><![CDATA[{text}]]></Content>"
            "</xml>"
        )

    # ---- 发送侧 -------------------------------------------------------
    async def get_access_token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        resp = await self._client.get(
            f"{self.API}/cgi-bin/token",
            params={
                "grant_type": "client_credential",
                "appid": self.s.wechat_appid,
                "secret": self.s.wechat_secret,
            },
        )
        data = resp.json()
        self._token = data.get("access_token", "")
        self._token_exp = time.time() + int(data.get("expires_in", 0))
        return self._token

    async def send_customer_service(self, openid: str, text: str) -> None:
        token = await self.get_access_token()
        await self._client.post(
            f"{self.API}/cgi-bin/message/custom/send",
            params={"access_token": token},
            json={"touser": openid, "msgtype": "text", "text": {"content": text[:2000]}},
        )

    async def send_template(self, openid: str, data: dict, url: str | None = None,
                            template_id: str | None = None) -> None:
        token = await self.get_access_token()
        payload = {"touser": openid, "template_id": template_id or self.s.wechat_multi_template_id, "data": data}
        if url:
            payload["url"] = url  # 点击通知直达控制台
        await self._client.post(
            f"{self.API}/cgi-bin/message/template/send",
            params={"access_token": token},
            json=payload,
        )
