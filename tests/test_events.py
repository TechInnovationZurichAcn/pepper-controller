from __future__ import annotations

import asyncio
import threading
import time

import pytest

from pepper_mcp.robot.events import Event, EventBus, MemoryWatcher
from pepper_mcp.robot.fake import FakeConnection, FakeRobot


def test_event_matching() -> None:
    e = Event(type="person.appeared")
    assert e.matches(None)
    assert e.matches(["person.*"])
    assert e.matches(["person.appeared", "touch.start"])
    assert not e.matches(["touch.*"])


def test_publish_history_and_subscribers() -> None:
    bus = EventBus()
    got: list[Event] = []
    done = threading.Event()
    bus.subscribe("touch.*", lambda e: (got.append(e), done.set()))
    bus.publish("speech.final", {"text": "hi"})
    bus.publish("touch.start", {"sensor": "head_front"})
    assert done.wait(1.0)
    assert [e.type for e in got] == ["touch.start"]
    assert [e.type for e in bus.recent(0)] == ["speech.final", "touch.start"]
    assert bus.recent(1)[0].type == "touch.start"
    assert bus.last("speech.final").data["text"] == "hi"
    bus.shutdown()


async def test_async_wait_wakes_immediately() -> None:
    bus = EventBus()

    async def publish_later() -> None:
        await asyncio.sleep(0.05)
        bus.publish("speech.final", {"text": "hello"})

    t0 = time.monotonic()
    task = asyncio.get_running_loop().create_task(publish_later())
    events = await bus.wait(["speech.final"], timeout_s=2.0)
    await task
    assert events and events[0].data["text"] == "hello"
    assert time.monotonic() - t0 < 0.5
    assert await bus.wait(["never"], timeout_s=0.05) == []
    bus.shutdown()


async def test_wait_returns_backlog_since_seq() -> None:
    bus = EventBus()
    bus.publish("a", {})
    second = bus.publish("b", {})
    events = await bus.wait(None, timeout_s=0.1, since_seq=1)
    assert [e.seq for e in events] == [second.seq]
    bus.shutdown()


@pytest.mark.parametrize("native", [False, True])
def test_memory_watcher_subscribe_or_poll(native: bool) -> None:
    robot = FakeRobot(subscriber_works=native)
    watcher = MemoryWatcher(FakeConnection(robot), default_poll_s=0.02)
    seen: list[tuple[str, object]] = []
    watcher.watch("FrontTactilTouched", lambda k, v: seen.append((k, v)))
    robot.memory_set("FrontTactilTouched", 1.0)
    deadline = time.monotonic() + 1.0
    while not seen and time.monotonic() < deadline:
        time.sleep(0.01)
    assert seen == [("FrontTactilTouched", 1.0)]
    assert watcher.native_supported is native
    assert watcher.watched_keys()["FrontTactilTouched"] == ("subscribed" if native else "polled")
    # edge detection: same value does not fire twice when polling
    robot.memory_set("FrontTactilTouched", 1.0)
    time.sleep(0.1)
    assert len(seen) == (2 if native else 1)
    watcher.stop()
