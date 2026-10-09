"""事件总线测试（阶段 4）：订阅、上限、丢弃计数、绝不抛异常。"""

from __future__ import annotations

import asyncio

import pytest

from app.events import EventBus


@pytest.mark.asyncio
async def test_publish_reaches_subscriber():
    bus = EventBus()
    sub = bus.subscribe()
    assert sub is not None
    bus.publish("audit", {"action": "demo"})
    event = await asyncio.wait_for(sub.queue.get(), timeout=1)
    assert event["type"] == "audit"
    assert event["data"] == {"action": "demo"}
    assert event["seq"] == 1
    assert event["ts"]


@pytest.mark.asyncio
async def test_sequence_increments_and_subscribers_isolated():
    bus = EventBus()
    a = bus.subscribe()
    b = bus.subscribe()
    bus.publish("x", {"n": 1})
    bus.publish("x", {"n": 2})
    ea = [await a.queue.get(), await a.queue.get()]
    eb = [await b.queue.get(), await b.queue.get()]
    assert [e["data"]["n"] for e in ea] == [1, 2]
    assert [e["data"]["n"] for e in eb] == [1, 2]
    assert [e["seq"] for e in ea] == [1, 2]


def test_unsubscribe_removes_subscriber():
    bus = EventBus()
    sub = bus.subscribe()
    assert bus.subscriber_count == 1
    bus.unsubscribe(sub)
    assert bus.subscriber_count == 0


def test_subscriber_limit_enforced():
    bus = EventBus(max_subscribers=2)
    assert bus.subscribe() is not None
    assert bus.subscribe() is not None
    assert bus.subscribe() is None, "超过上限必须拒绝，而不是无限接受"
    assert bus.stats()["max_subscribers"] == 2


@pytest.mark.asyncio
async def test_slow_subscriber_drops_oldest_not_crash():
    bus = EventBus(queue_size=2)
    sub = bus.subscribe()
    for n in range(5):
        bus.publish("x", {"n": n})
    # 队列保留最新的 2 条
    got = []
    while not sub.queue.empty():
        got.append((await sub.queue.get())["data"]["n"])
    assert got == [3, 4]
    assert bus.stats()["dropped"] >= 3


def test_publish_never_raises_without_loop_or_subscribers():
    bus = EventBus()
    bus.publish("x", {"n": 1})  # 无订阅者
    sub = bus.subscribe()
    assert sub is not None
    # 无运行中的事件循环，也没有绑定 loop：必须静默计数，不得抛异常
    bus.publish("x", {"n": 2})
    assert bus.stats()["dropped"] >= 1
    assert bus.stats()["published"] == 2


def test_publish_never_raises_on_bad_payload():
    """data 里有不可 JSON 序列化的对象时，publish 也必须不抛异常。"""
    bus = EventBus()
    bus.subscribe()
    bus.publish("x", {"obj": object()})
    assert bus.stats()["published"] == 1
