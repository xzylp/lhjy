"""进程内异步事件总线。

零外部依赖，基于 asyncio.Queue。
支持按 event_type 订阅、优先级排序、优雅关闭。
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime
from typing import Any, Callable, Coroutine

from ..contracts import MarketEvent
from ..logging_config import get_logger

logger = get_logger("data.event_bus")

# handler 类型：同步回调或异步协程
EventHandler = Callable[[MarketEvent], Any] | Callable[[MarketEvent], Coroutine[Any, Any, Any]]


class EventBus:
    """进程内异步事件总线。

    用法::

        bus = EventBus()
        bus.subscribe("NEGATIVE_NEWS", on_negative_news)
        bus.subscribe("PRICE_ALERT", on_price_alert)
        await bus.start()
        await bus.publish(MarketEvent(event_type="NEGATIVE_NEWS", symbol="000001.SZ", ...))
        ...
        await bus.stop()
    """

    def __init__(self, max_queue_size: int = 1000) -> None:
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)
        self._queue: asyncio.PriorityQueue[tuple[int, float, MarketEvent]] = (
            asyncio.PriorityQueue(maxsize=max_queue_size)
        )
        self._running = False
        self._consumer_task: asyncio.Task | None = None
        self._event_count = 0
        self._processed_count = 0

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        """注册事件处理器。

        Args:
            event_type: 事件类型（对应 MarketEvent.event_type）
            handler: 处理函数，可以是同步或异步
        """
        self._handlers[event_type].append(handler)
        logger.debug("订阅事件 %s → %s", event_type, getattr(handler, "__name__", str(handler)))

    async def publish(self, event: MarketEvent) -> None:
        """发布事件到总线。

        高优先级事件（priority 值越大）越先被处理。
        内部用 负priority 作为排序键（PriorityQueue 取最小值）。
        """
        if not event.timestamp:
            event = event.model_copy(update={"timestamp": datetime.now().isoformat()})
        sort_key = (-event.priority, self._event_count)
        self._event_count += 1
        try:
            self._queue.put_nowait((sort_key[0], sort_key[1], event))
        except asyncio.QueueFull:
            logger.warning("事件总线队列已满，丢弃事件: %s %s", event.event_type, event.symbol)
            return
        logger.debug("事件入队: %s %s priority=%d", event.event_type, event.symbol, event.priority)

    async def start(self) -> None:
        """启动事件消费循环。"""
        if self._running:
            return
        self._running = True
        self._consumer_task = asyncio.create_task(self._consume_loop())
        logger.info("事件总线已启动，已注册 %d 个事件类型。", len(self._handlers))

    async def stop(self) -> None:
        """优雅关闭事件总线。"""
        self._running = False
        if self._consumer_task is not None:
            self._consumer_task.cancel()
            try:
                await self._consumer_task
            except asyncio.CancelledError:
                pass
        logger.info("事件总线已关闭，共处理 %d 个事件。", self._processed_count)

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def pending_count(self) -> int:
        return self._queue.qsize()

    async def _consume_loop(self) -> None:
        """持续消费队列中的事件。"""
        while self._running:
            try:
                _, _, event = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            handlers = self._handlers.get(event.event_type, [])
            if not handlers:
                logger.debug("事件 %s 无注册 handler，跳过。", event.event_type)
                continue

            await self._dispatch_event(event, handlers)

    # ── 同步接口（供非异步上下文使用） ──

    def publish_sync(self, event: MarketEvent) -> None:
        """同步发布事件。

        在调度器、CLI 等非异步上下文里，直接发布并立即消费，避免事件只入队不处理。
        """
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.publish(event))
            if not self.is_running:
                loop.create_task(self._drain_once())
        except RuntimeError:
            asyncio.run(self._publish_and_drain(event))

    async def _publish_and_drain(self, event: MarketEvent) -> None:
        await self.publish(event)
        await self._drain_once()

    async def _drain_once(self) -> None:
        if self._queue.empty():
            return
        _, _, event = await self._queue.get()
        handlers = self._handlers.get(event.event_type, [])
        if not handlers:
            logger.debug("事件 %s 无注册 handler，跳过。", event.event_type)
            return
        await self._dispatch_event(event, handlers)

    async def _dispatch_event(self, event: MarketEvent, handlers: list[EventHandler]) -> None:
        for handler in handlers:
            try:
                result = handler(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception(
                    "事件 handler 异常: event_type=%s symbol=%s handler=%s",
                    event.event_type,
                    event.symbol,
                    getattr(handler, "__name__", str(handler)),
                )
        self._processed_count += 1
