"""Nobody around for a while → small idle fidgets (looking around, playing with a hand…).

Off by default. Runs its own timer thread: every ``interval_s`` while the robot is awake, sees
nobody, runs no task or gesture and nothing happened for ``idle_after_s``, it plays the
``idle`` gesture group (Choregraphe "solitaries"). It reports nothing to the event bus, so the
voice agent is not told about every fidget.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from pepper_mcp.reflexes.base import Reflex
from pepper_mcp.robot.events import Event

log = logging.getLogger(__name__)


class IdleReflex(Reflex):
    name = "idle"
    description = ("nobody around and nothing happening → a small idle animation (look around, play "
                   "with a hand…) every params.interval_s seconds")
    # Anything that means someone is interacting resets the idle clock.
    triggers = ("person.appeared", "person.engaged", "speech.started", "tts.started", "touch.start",
                "tablet.touched", "game.started")
    # The animation library asks for 15-30 s between two of these.
    params: dict[str, Any] = {"gesture": "idle", "idle_after_s": 30.0, "interval_s": 30.0}
    enabled_by_default = False
    CHECK_S = 1.0

    def __init__(self, rt: Any):
        super().__init__(rt)
        self._last_activity = time.monotonic()
        self._last_fidget = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def react(self, event: Event) -> Any:
        self._last_activity = time.monotonic()
        return None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._last_activity = time.monotonic()
        self._thread = threading.Thread(target=self._loop, name="idle-reflex", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def _loop(self) -> None:
        was_enabled = self.enabled
        while not self._stop.wait(self.CHECK_S):
            if not self.enabled:
                was_enabled = False
                continue
            if not was_enabled:  # events were not tracked while off: start the clock now
                was_enabled = True
                self._last_activity = time.monotonic()
            try:
                self.tick()
            except Exception as exc:
                log.warning("idle reflex failed: %s", exc)

    def is_idle(self, now: float) -> bool:
        rt = self.rt
        if now - self._last_activity < float(self.params["idle_after_s"]):
            return False
        if now - self._last_fidget < float(self.params["interval_s"]):
            return False
        if rt.gestures.active() or rt.tasks.running():
            return False
        if rt.perception is not None and rt.perception.people():
            return False
        return bool(rt.motion.is_awake())

    def tick(self) -> bool:
        """Play one idle animation if the robot is idle; True when it did."""
        now = time.monotonic()
        if not self.is_idle(now):
            return False
        self._last_fidget = now
        # preempt=False: never cut off a gesture that Gemini started a moment ago
        if self.rt.gestures.play(self.params["gesture"], preempt=False) is None:
            return False
        self.count += 1
        self.last_fired = time.time()
        self._last_fidget = time.monotonic()  # count the interval from the end of the animation
        return True
