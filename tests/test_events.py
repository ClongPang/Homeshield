"""EventBus 分派协议:sync 内联、async await、可调用对象(__call__ 为协程)识别。"""
import asyncio

from homeshield.core.events import EventBus


class _Evt:
    pass


def test_dispatches_sync_async_and_async_callable():
    bus = EventBus()
    seen: list[str] = []

    bus.subscribe(_Evt, lambda e: seen.append("sync"))

    async def handler(e):
        seen.append("async")

    class AsyncCallable:
        async def __call__(self, e):
            seen.append("callable-async")

    bus.subscribe(_Evt, handler)
    bus.subscribe(_Evt, AsyncCallable())

    asyncio.run(bus.publish(_Evt()))
    assert seen == ["sync", "async", "callable-async"]


def test_publish_without_subscribers_is_noop():
    asyncio.run(EventBus().publish(_Evt()))  # 不抛错即通过
