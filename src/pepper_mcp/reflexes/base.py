from __future__ import annotations

import logging
import threading
import time
from typing import Any

from pepper_mcp.robot.events import Event, EventBus

log = logging.getLogger(__name__)


class Reflex:
    """Base class. Subclasses set ``name``, ``description``, ``triggers`` and ``params``
    and implement :meth:`react`. ``on_event`` handles enable/cooldown bookkeeping."""

    name = "reflex"
    description = ""
    triggers: tuple[str, ...] = ()
    params: dict[str, Any] = {}
    cooldown_s = 0.0
    enabled_by_default = True

    def __init__(self, rt: Any):
        self.rt = rt
        self.enabled = self.enabled_by_default
        self.params = dict(self.params)
        self.count = 0
        self.last_fired = 0.0
        self._busy = threading.Lock()

    def on_event(self, event: Event) -> None:
        if not self.enabled:
            return
        now = time.time()
        if self.cooldown_s and now - self.last_fired < self.cooldown_s:
            return
        if not self._busy.acquire(blocking=False):
            return
        try:
            self.last_fired = now
            result = self.react(event)
            if result is not None:
                self.count += 1
                self.rt.bus.publish("reflex.completed", {"reflex": self.name, "trigger": event.type,
                                                         "result": result}, source="reflex")
        except Exception as exc:
            log.warning("reflex %s failed: %s", self.name, exc)
        finally:
            self._busy.release()

    def react(self, event: Event) -> Any:
        raise NotImplementedError

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "enabled": self.enabled, "description": self.description,
                "triggers": list(self.triggers), "params": self.params, "cooldown_s": self.cooldown_s,
                "fired": self.count}


class ReflexManager:
    def __init__(self, bus: EventBus):
        self.bus = bus
        self._reflexes: dict[str, Reflex] = {}
        self._unsubs: list[Any] = []

    def register(self, reflex: Reflex) -> None:
        self._reflexes[reflex.name] = reflex
        self._unsubs.append(self.bus.subscribe(reflex.triggers or ("*",), reflex.on_event))

    def start(self, disabled: set[str] | None = None) -> None:
        for r in self._reflexes.values():
            if disabled and r.name in disabled:
                r.enabled = False
            try:
                r.start()
            except Exception as exc:
                log.warning("reflex %s start failed: %s", r.name, exc)

    def stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for r in self._reflexes.values():
            try:
                r.stop()
            except Exception:
                pass

    def get(self, name: str) -> Reflex:
        if name not in self._reflexes:
            raise KeyError(f"unknown reflex {name!r}; available: {sorted(self._reflexes)}")
        return self._reflexes[name]

    def set(self, name: str, enabled: bool | None = None, params: dict[str, Any] | None = None) -> dict[str, Any]:
        r = self.get(name)
        if enabled is not None:
            r.enabled = bool(enabled)
        if params:
            unknown = set(params) - set(r.params)
            if unknown:
                raise KeyError(f"unknown params for {name}: {sorted(unknown)}; valid: {sorted(r.params)}")
            r.params.update(params)
        return r.to_dict()

    def list(self) -> list[dict[str, Any]]:
        return [r.to_dict() for r in self._reflexes.values()]

    def active_names(self) -> list[str]:
        return [r.name for r in self._reflexes.values() if r.enabled]
