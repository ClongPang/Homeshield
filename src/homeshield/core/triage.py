"""入口优先级:未绑定引导 → 群命令 → 微信 reset → ack → 完整判定。

调用方只对 text 类型调用;URL 与图片一律进入完整判定。
"""
from homeshield.core.intake import _URL_RE

RESET_COMMANDS: frozenset[str] = frozenset({"新的", "新问题"})
ACK_TEXTS: frozenset[str] = frozenset({
    "谢谢", "多谢", "辛苦了", "好的", "好", "嗯", "嗯嗯", "哦",
    "知道了", "收到", "收到了", "明白", "明白了", "了解", "好滴", "好嘞",
})
ACK_ASCII: frozenset[str] = frozenset({"ok", "okay"})


def classify(text: str) -> str:
    t = text.strip()
    if t in RESET_COMMANDS:
        return "reset"
    if t.lower() not in ACK_ASCII and t not in ACK_TEXTS:
        return "query"
    return "ack" if len(t) <= 20 and _URL_RE.search(t) is None else "query"
