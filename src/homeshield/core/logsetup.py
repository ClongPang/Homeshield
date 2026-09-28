"""日志装配:应用入口调用一次,basicConfig 幂等。

降级与外部调用失败记录 warning,不打断主链路。
"""
import logging


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # httpx 的 INFO 请求日志带完整 URL(含 access_token),降为 WARN 防止密钥落盘
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
