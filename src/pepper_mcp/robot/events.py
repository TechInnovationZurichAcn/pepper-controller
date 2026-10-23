"""Event bus and ALMemory watcher – the real-time backbone.

Everything that happens on the robot (speech heard, person seen, touch, task done…)
becomes an :class:`Event` published on the :class:`EventBus`. Consumers:

* MCP tools ``wait_for_event`` / ``get_events`` (async waiters wake up immediately)
* reflexes (synchronous subscribers, run on a small thread pool)
* the preview server (SSE)

:class:`MemoryWatcher` turns ALMemory keys into events. It tries the native
``ALMemory.subscriber(key).signal.connect`` first and falls back to polling the key
when the firmware/qi build rejects the subscriber (``Invalid signature`` on this
robot's teleop code), so the rest of the system never has to care.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Any

log = logging.getLogger(__name__)

EventHandler = Callable[["Event"], None]


@dataclass
class Event:
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    seq: int = 0
    source: str = "robot"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["age_ms"] = int((time.time() - self.ts) * 1000)
        return d

    def matches(self, types: Iterable[str] | None) -> bool:
        if not types:
            return True
        for pattern in types:
            if pattern == self.type or pattern == "*":
                return True
            if pattern.endswith(".*") and self.type.startswith(pattern[:-1]):
                return True
            if pattern.endswith("*") and self.type.startswith(pattern[:-1]):
                return True
        return False


class EventBus:
    """Thread-safe publish/subscribe with history and asyncio long-polling."""

    def __init__(self, history: int = 500, workers: int = 4):
        self._history: deque[Event] = deque(maxlen=history)
        self._seq = itertools.count(1)
        self._lock = threading.Lock()
        self._subs: list[tuple[tuple[str, ...], EventHandler]] = []
        self._waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Future, tuple[str, ...]]] = []
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="events")
        self._sync_hooks: list[EventHandler] = []
        self.last_seq = 0

    # ------------------------------------------------------------- publishing
    def publish(self, type_: str, data: dict[str, Any] | None = None, source: str = "robot") -> Event:
        event = Event(type=type_, data=dict(data or {}), source=source)
        with self._lock:
            event.seq = next(self._seq)
            self.last_seq = event.seq
            self._history.append(event)
            subs = list(self._subs)
            waiters = list(self._waiters)
            hooks = list(self._sync_hooks)
        log.debug("event %s %s", event.type, event.data)
        for hook in hooks:  # cheap, in-line (e.g. SSE fan-out)
            try:
                hook(event)
            except Exception as exc:
                log.debug("sync hook failed: %s", exc)
        for types, handler in subs:
            if event.matches(types):
                self._pool.submit(self._run_handler, handler, event)
        for loop, fut, types in waiters:
            if event.matches(types):
                loop.call_soon_threadsafe(self._resolve, fut, event)
        return event

    @staticmethod
    def _resolve(fut: asyncio.Future, event: Event) -> None:
        if not fut.done():
            fut.set_result(event)

    @staticmethod
    def _run_handler(handler: EventHandler, event: Event) -> None:
        try:
            handler(event)
        except Exception as exc:
            log.warning("event handler %s failed on %s: %s", handler, event.type, exc)

    # ------------------------------------------------------------- subscribing
    def subscribe(self, types: Iterable[str] | str, handler: EventHandler) -> Callable[[], None]:
        """Register ``handler`` for event types (supports ``prefix.*``). Returns unsubscribe."""
        if isinstance(types, str):
            types = (types,)
        entry = (tuple(types), handler)
        with self._lock:
            self._subs.append(entry)

        def _unsub() -> None:
            with self._lock:
                if entry in self._subs:
                    self._subs.remove(entry)

        return _unsub

    def add_sync_hook(self, hook: EventHandler) -> None:
        with self._lock:
            self._sync_hooks.append(hook)

    # ------------------------------------------------------------- history
    def recent(self, since_seq: int = 0, types: Iterable[str] | None = None, limit: int = 50) -> list[Event]:
        types = tuple(types) if types else None
        with self._lock:
            items = [e for e in self._history if e.seq > since_seq and e.matches(types)]
        return items[-limit:]

    def last(self, type_: str) -> Event | None:
        with self._lock:
            for e in reversed(self._history):
                if e.type == type_:
                    return e
        return None

    # ------------------------------------------------------------- waiting
    async def wait(self, types: Iterable[str] | None = None, timeout_s: float = 30.0,
                   since_seq: int | None = None) -> list[Event]:
        """Return matching events newer than ``since_seq`` as soon as one exists."""
        types_t = tuple(types) if types else ()
        if since_seq is not None:
            backlog = self.recent(since_seq, types_t or None, limit=50)
            if backlog:
                return backlog
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        entry = (loop, fut, types_t)
        with self._lock:
            self._waiters.append(entry)
        try:
            first = await asyncio.wait_for(fut, timeout_s)
        except asyncio.TimeoutError:
            return []
        finally:
            with self._lock:
                if entry in self._waiters:
                    self._waiters.remove(entry)
        # Gather anything else that arrived in the same instant.
        extra = self.recent(first.seq, types_t or None, limit=20)
        return [first, *extra]

    def wait_sync(self, types: Iterable[str] | None, timeout_s: float) -> Event | None:
        """Blocking wait used by reflexes running on worker threads."""
        got: list[Event] = []
        done = threading.Event()

        def _h(e: Event) -> None:
            got.append(e)
            done.set()

        unsub = self.subscribe(types or ("*",), _h)
        try:
            done.wait(timeout_s)
        finally:
            unsub()
        return got[0] if got else None

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


# ---------------------------------------------------------------------------
# ALMemory watcher
# ---------------------------------------------------------------------------

ValueHandler = Callable[[str, Any], None]


class MemoryWatcher:
    """Deliver ALMemory key changes to handlers, by subscription or by polling."""

    def __init__(self, conn: Any, default_poll_s: float = 0.1):
        self.conn = conn
        self.default_poll_s = default_poll_s
        self._memory = None
        self._subscribers: dict[str, Any] = {}  # key -> (subscriber object, link id)
        self._polled: dict[str, dict[str, Any]] = {}
        self._handlers: dict[str, list[ValueHandler]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.native_supported: bool | None = None
        self.poll_count = 0

    @property
    def memory(self):
        if self._memory is None:
            self._memory = self.conn.service("ALMemory")
        return self._memory

    def start(self) -> None:
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._poll_loop, name="memory-poll", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        with self._lock:
            for key, (sub, link) in list(self._subscribers.items()):
                try:
                    sub.signal.disconnect(link)
                except Exception:
                    pass
            self._subscribers.clear()
            self._polled.clear()
            self._handlers.clear()

    # ------------------------------------------------------------------ API
    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self.memory.getData(key)
        except Exception:
            return default

    def set(self, key: str, value: Any) -> bool:
        try:
            self.memory.insertData(key, value)
        except Exception as exc:
            log.debug("ALMemory.insertData(%s) failed: %s", key, exc)
            return False
        # Record our own write so the poller does not miss a change back to the old value
        # that happens before its next read.
        with self._lock:
            spec = self._polled.get(key)
            if spec is not None:
                spec["last"] = value
        return True

    def watch(self, key: str, handler: ValueHandler, poll_s: float | None = None,
              edge_only: bool = True, force_poll: bool = False) -> None:
        """Call ``handler(key, value)`` whenever ``key`` changes.

        Native subscription is attempted once per process; if it fails we poll the key
        every ``poll_s`` seconds (``edge_only`` suppresses repeats of the same value).
        """
        with self._lock:
            self._handlers.setdefault(key, []).append(handler)
            if key in self._subscribers or key in self._polled:
                return
        if not force_poll and self.native_supported is not False:
            try:
                sub = self.memory.subscriber(key)
                link = sub.signal.connect(lambda value, k=key: self._dispatch(k, value))
                with self._lock:
                    self._subscribers[key] = (sub, link)
                if self.native_supported is None:
                    self.native_supported = True
                    log.info("ALMemory native subscriptions work on this robot")
                return
            except Exception as exc:
                if self.native_supported is None:
                    log.info("ALMemory.subscriber unavailable (%s) – polling keys instead", exc)
                self.native_supported = False
        with self._lock:
            self._polled[key] = {
                "period": poll_s or self.default_poll_s,
                "next": 0.0,
                "last": _UNSET,
                "edge_only": edge_only,
            }
        self.start()

    def unwatch(self, key: str) -> None:
        with self._lock:
            self._handlers.pop(key, None)
            self._polled.pop(key, None)
            sub = self._subscribers.pop(key, None)
        if sub:
            try:
                sub[0].signal.disconnect(sub[1])
            except Exception:
                pass

    def watched_keys(self) -> dict[str, str]:
        with self._lock:
            out = {k: "subscribed" for k in self._subscribers}
            out.update({k: "polled" for k in self._polled})
        return out

    # ------------------------------------------------------------------ internals
    def _dispatch(self, key: str, value: Any) -> None:
        with self._lock:
            handlers = list(self._handlers.get(key, ()))
        for h in handlers:
            try:
                h(key, value)
            except Exception as exc:
                log.warning("memory handler for %s failed: %s", key, exc)

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            now = time.monotonic()
            with self._lock:
                due = [(k, spec) for k, spec in self._polled.items() if spec["next"] <= now]
                sleep_for = min([spec["next"] - now for spec in self._polled.values() if spec["next"] > now]
                                or [self.default_poll_s])
            for key, spec in due:
                spec["next"] = now + spec["period"]
                try:
                    value = self.memory.getData(key)
                except Exception:
                    continue  # key does not exist (yet) on this firmware
                self.poll_count += 1
                if spec["edge_only"] and _same(spec["last"], value):
                    continue
                spec["last"] = value
                self._dispatch(key, value)
            self._stop.wait(max(0.005, min(sleep_for, 0.25)))


_UNSET = object()


def _same(a: Any, b: Any) -> bool:
    if a is _UNSET:
        return False
    try:
        return a == b
    except Exception:
        return False
