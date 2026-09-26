"""
进程内领域事件与事件总线

管线只发布 VerdictCompleted,订阅方执行送达;
"判定"与"送达"解耦,告警策略可整体替换。

分派协议:
- sync handler 内联执行;
- async handler(含 __call__ 为协程函数的可调用对象)在当前事件循环内 await,
  慢 I/O 的 handler 自行用 asyncio.create_task 后台化(见 notifier.AlertRouter)。
"""
import inspect
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable

from homeshield.core.models import JudgeOutput, Message


@dataclass(frozen=True)
class VerdictCompleted:
    message: Message
    verdict: JudgeOutput
    reply: str
    query_id: int
    verdict_id: int


Handler = Callable[[Any], Any] # 接收 1 个任意类型参数、返回任意类型的函数


def _is_async(handler: Handler) -> bool:
    if inspect.iscoroutinefunction(handler):
        return True
    call = getattr(handler, "__call__", None)
    return call is not None and inspect.iscoroutinefunction(call)


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[type, list[Handler]] = defaultdict(list)

    def subscribe(self, event_type: type, handler: Handler) -> None: # 事件消息入队
        self._subs[event_type].append(handler)

    async def publish(self, event: Any) -> None:
        for handler in self._subs.get(type(event), []): # 队列的事件消息循环出队处理
            if _is_async(handler):
                await handler(event)
            else:
                handler(event)
