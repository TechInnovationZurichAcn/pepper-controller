"""A new person shows up → small wave (gesture only by default; the LLM does the talking)."""

from __future__ import annotations

import time
from typing import Any

from pepper_mcp.reflexes.base import Reflex
from pepper_mcp.robot.events import Event


class GreetReflex(Reflex):
    name = "greet"
    description = ("when a person appears within range after nobody was around, wave at them; "
                   "optionally say a greeting phrase (set params.phrase)")
    triggers = ("person.appeared",)
    params: dict[str, Any] = {"phrase": "", "animation": "animations/Stand/Gestures/Hey_1",
                              "max_distance_m": 2.5, "quiet_period_s": 20.0}
    cooldown_s = 8.0

    def __init__(self, rt: Any):
        super().__init__(rt)
        self._last_person_seen = 0.0

    def react(self, event: Event) -> Any:
        rt = self.rt
        now = time.time()
        others = [p for p in (rt.perception.people() if rt.perception else [])
                  if p.get("person_id") != event.data.get("person_id")]
        quiet = (now - self._last_person_seen) > self.params["quiet_period_s"] and not others
        self._last_person_seen = now
        dist = event.data.get("distance_m")
        if dist is not None and dist > self.params["max_distance_m"]:
            return None
        if not quiet:
            return None
        rt.bus.publish("reflex.triggered", {"reflex": self.name, "trigger": event.type,
                                            "person_id": event.data.get("person_id")}, source="reflex")
        if self.params["phrase"]:
            rt.speech.say(self.params["phrase"], animated=False)
        if self.params["animation"]:
            _, fut = rt.animations.run_async(self.params["animation"])
            try:
                fut.wait(6000)
            except Exception:
                pass
        return {"person_id": event.data.get("person_id"), "greeted": True}
