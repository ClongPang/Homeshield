"""企微回调加解密与验签。"""
import base64
import hashlib

import pytest

from homeshield.core.channels.wecom import WeComChannel, WeComCryptoError
from homeshield.core.config import Settings

AES_KEY = "a" * 43  # 43 位字母数字,补位后 base64 解出 32 字节
CORPID = "ww1234567890abcdef"


def _channel(token: str = "tok") -> WeComChannel:
    return WeComChannel(
        Settings(wecom_token=token, wecom_aes_key=AES_KEY, wecom_corpid=CORPID)
    )


async def test_roundtrip():
    ch = _channel()
    assert ch.configured
    cipher = ch.encrypt("你好，小盾")
    assert ch.decrypt(cipher) == "你好，小盾"


async def test_signature():
    ch = _channel(token="tok")
    good = hashlib.sha1("".join(sorted(["tok", "111", "nonce", "cipher"])).encode()).hexdigest()
    assert ch.verify_signature(good, "111", "nonce", "cipher")
    assert not ch.verify_signature(good, "222", "nonce", "cipher")
    assert not ch.verify_signature("bad" * 10, "111", "nonce", "cipher")


async def test_decrypt_rejects_wrong_corpid():
    ch = _channel()
    cipher = ch.encrypt("hello")
    other = WeComChannel(
        Settings(wecom_token="tok", wecom_aes_key=AES_KEY, wecom_corpid="ww_other")
    )
    with pytest.raises(WeComCryptoError):
        other.decrypt(cipher)


async def test_decrypt_rejects_garbage():
    ch = _channel()
    with pytest.raises(WeComCryptoError):
        ch.decrypt(base64.b64encode(b"short").decode())


async def test_unconfigured_channel():
    ch = WeComChannel(Settings())
    assert not ch.configured
    with pytest.raises(WeComCryptoError):
        ch.decrypt(ch.encrypt("x"))
