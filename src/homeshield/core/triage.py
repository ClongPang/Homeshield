"""
Entry priority: relation commands → session reset → acknowledgement → full verdict.
调用方只对 text 类型调用;URL 与图片一律进入完整判定。
把收到的文字分成三类，它本身不判断是否诈骗
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
    if t.lower() not in ACK_ASCII and t not in ACK_TEXTS:           # 用户输入文本不匹配ack语言模式，判断为 query 类型
        return "query"
    return "ack" if len(t) <= 20 and _URL_RE.search(t) is None else "query" # 其它文字较短，又无网址的，判断为 ack;否则最后兜底为 query
