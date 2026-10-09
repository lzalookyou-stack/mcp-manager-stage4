"""进程内事件总线（阶段 4）：为 SSE 提供审计/任务事件广播。

**定位与边界**
- 仅用于**本地单进程**（uvicorn 单 worker）场景；不跨进程、不持久化、不做投递保证。
- 只广播**已脱敏**的审计字段（actor / action / target / outcome / detail），
  其中 ``detail`` 在写入审计前已经过 ``sanitize_for_log``。**绝不广播凭据**。
- 发布失败（订阅者队列已满、事件循环不可用等）**绝不影响**调用方：
  ``publish`` 不抛异常，只累加丢弃计数，避免"日志/通知故障拖垮主流程"。

**线程模型**
``PluginService`` 是同步的，且在 FastAPI 的 ``async def`` 路由中于事件循环线程内被调用；
但为防御性起见，本模块记录事件循环并支持从其他线程 ``call_soon_threadsafe`` 投递。
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# 单个订阅者的队列上限：慢消费者只会丢事件，不会被无限缓冲拖垮内存。
DEFAULT_QUEUE_SIZE = 256
# 并发订阅上限：SSE 是长连接，必须有上限，防止连接耗尽。
DEFAULT_MAX_SUBSCRIBERS = 32


@dataclass
class Subscription:
    """一个 SSE 订阅。"""

    id: int
    queue: asyncio.Queue[dict[str, Any]] = field(
        default_factory=lambda: asyncio.Queue(maxsize=DEFAULT_QUEUE_SIZE)
    )
    dropped: int = 0


class EventBus:
    """极简发布/订阅总线。"""

    def __init__(
        self,
        *,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        max_subscribers: int = DEFAULT_MAX_SUBSCRIBERS,
    ) -> None:
        self._queue_size = max(1, int(queue_size))
        self._max_subscribers = max(1, int(max_subscribers))
        self._subs: dict[int, Subscription] = {}
        self._lock = threading.Lock()
        self._next_id = 1
        self._seq = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._published = 0
        self._dropped = 0

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """记录当前事件循环，供跨线程投递使用。"""
        self._loop = loop

    def subscribe(self) -> Subscription | None:
        """新建订阅；超过并发上限时返回 ``None``（调用方应回 503）。"""
        with self._lock:
            if len(self._subs) >= self._max_subscribers:
                return None
            sub = Subscription(id=self._next_id)
            sub.queue = asyncio.Queue(maxsize=self._queue_size)
            self._subs[sub.id] = sub
            self._next_id += 1
            return sub

    def unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            self._subs.pop(sub.id, None)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "subscribers": len(self._subs),
                "max_subscribers": self._max_subscribers,
                "published": self._published,
                "dropped": self._dropped,
            }

    # ------------------------------------------------------------------ #
    # 发布
    # ------------------------------------------------------------------ #
    def publish(self, event_type: str, data: dict[str, Any]) -> None:
        """发布一条事件。**绝不抛异常**。"""
        with self._lock:
            self._seq += 1
            self._published += 1
            event = {
                "seq": self._seq,
                "ts": datetime.now(timezone.utc).isoformat(),
                "type": str(event_type),
                "data": data,
            }
            subs = list(self._subs.values())
            loop = self._loop

        if not subs:
            return

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None

        for sub in subs:
            if running is not None and (loop is None or running is loop):
                self._offer(sub, event)
            elif loop is not None and not loop.is_closed():
                try:
                    loop.call_soon_threadsafe(self._offer, sub, event)
                except RuntimeError:
                    with self._lock:
                        self._dropped += 1
            else:
                # 没有可用事件循环：无法投递，如实计数（不静默丢失）。
                with self._lock:
                    self._dropped += 1

    def _offer(self, sub: Subscription, event: dict[str, Any]) -> None:
        """把事件放入订阅队列；队列满时丢弃最旧一条（保新弃旧）。"""
        try:
            sub.queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                sub.queue.get_nowait()  # 丢弃最旧
            except asyncio.QueueEmpty:  # pragma: no cover - 竞态兜底
                pass
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:  # pragma: no cover - 竞态兜底
                pass
            sub.dropped += 1
            with self._lock:
                self._dropped += 1
        except RuntimeError:  # pragma: no cover - 队列绑定到已关闭的循环
            with self._lock:
                self._dropped += 1
