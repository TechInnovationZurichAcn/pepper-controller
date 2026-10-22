"""Registry for long-running robot actions.

A tool that starts something that takes seconds (animation, ``moveTo``, navigation,
behaviour) returns a ``task_id`` immediately. The task runs on its own thread or qi
future and publishes ``task.finished`` when it is done, so the LLM can keep talking and
either poll ``wait_task`` or react to the event.
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pepper_mcp.robot.events import EventBus

log = logging.getLogger(__name__)


@dataclass
class Task:
    id: str
    kind: str
    description: str
    started: float = field(default_factory=time.time)
    finished: float | None = None
    ok: bool | None = None
    error: str | None = None
    result: Any = None
    cancel: Callable[[], None] | None = None
    _done: threading.Event = field(default_factory=threading.Event, repr=False)

    @property
    def state(self) -> str:
        if self.finished is None:
            return "running"
        return "done" if self.ok else "failed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.id,
            "kind": self.kind,
            "description": self.description,
            "state": self.state,
            "ok": self.ok,
            "error": self.error,
            "result": self.result if _json_safe(self.result) else str(self.result),
            "elapsed_s": round((self.finished or time.time()) - self.started, 3),
        }

    def wait(self, timeout_s: float | None) -> bool:
        return self._done.wait(timeout_s)


def _json_safe(value: Any) -> bool:
    return value is None or isinstance(value, (bool, int, float, str, list, dict))


class TaskRegistry:
    def __init__(self, bus: EventBus, keep: int = 100):
        self.bus = bus
        self._tasks: dict[str, Task] = {}
        self._order: list[str] = []
        self._keep = keep
        self._lock = threading.Lock()
        self._ids = itertools.count(1)

    def _new(self, kind: str, description: str) -> Task:
        task = Task(id=f"{kind}-{next(self._ids)}", kind=kind, description=description)
        with self._lock:
            self._tasks[task.id] = task
            self._order.append(task.id)
            while len(self._order) > self._keep:
                old = self._order.pop(0)
                self._tasks.pop(old, None)
        return task

    def run(self, kind: str, description: str, fn: Callable[[], Any],
            cancel: Callable[[], None] | None = None) -> Task:
        """Run ``fn`` on a daemon thread as a tracked task."""
        task = self._new(kind, description)
        task.cancel = cancel

        def _body() -> None:
            try:
                task.result = fn()
                task.ok = True
            except Exception as exc:
                task.ok = False
                task.error = str(exc)
                log.warning("task %s failed: %s", task.id, exc)
            self._finish(task)

        threading.Thread(target=_body, name=f"task-{task.id}", daemon=True).start()
        return task

    def from_future(self, kind: str, description: str, future: Any) -> Task:
        """Track a qi future (``_async=True`` call)."""
        task = self._new(kind, description)

        def _cancel() -> None:
            try:
                future.cancel()
            except Exception:
                pass

        task.cancel = _cancel

        def _cb(fut: Any) -> None:
            try:
                if fut.hasError():
                    task.ok, task.error = False, str(fut.error())
                else:
                    task.result, task.ok = fut.value(), True
            except Exception as exc:
                task.ok, task.error = False, str(exc)
            self._finish(task)

        future.addCallback(_cb)
        return task

    def _finish(self, task: Task) -> None:
        task.finished = time.time()
        task._done.set()
        self.bus.publish("task.finished", {"task_id": task.id, "kind": task.kind, "ok": task.ok,
                                           "error": task.error, "description": task.description},
                         source="tasks")

    def get(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def running(self) -> list[Task]:
        with self._lock:
            return [t for t in self._tasks.values() if t.finished is None]

    def all(self, limit: int = 20) -> list[Task]:
        with self._lock:
            ids = self._order[-limit:]
            return [self._tasks[i] for i in ids if i in self._tasks]

    def cancel(self, task_id: str) -> bool:
        task = self.get(task_id)
        if task is None or task.finished is not None or task.cancel is None:
            return False
        task.cancel()
        return True

    def cancel_all(self) -> int:
        n = 0
        for t in self.running():
            if self.cancel(t.id):
                n += 1
        return n
