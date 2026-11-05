"""Somebody raises/waves a hand → Pepper offers a high five right away."""

from __future__ import annotations

from typing import Any

from pepper_mcp.reflexes.base import Reflex
from pepper_mcp.robot.events import Event


class HighFiveReflex(Reflex):
    name = "high_five"
    description = ("when a person waves or raises a hand (ALWavingDetection on Pepper's own sensors), raise the "
                   "right hand, wait for the hand touch and celebrate; silent by default so Gemini does the "
                   "talking (set params.phrase / success_phrase for NAOqi speech)")
    triggers = ("person.waving",)
    params: dict[str, Any] = {"phrase": "", "success_phrase": "", "miss_phrase": "",
                              "wait_s": 3.5, "max_distance_m": 1.6, "announce": True}
    cooldown_s = 6.0

    def react(self, event: Event) -> Any:
        rt = self.rt
        pid = event.data.get("person_id")
        if pid is not None and rt.perception is not None:
            info = rt.perception.person_info(int(pid))
            dist = info.get("distance_m")
            if dist is not None and dist > self.params["max_distance_m"]:
                return None  # too far for a high five – the LLM may still react verbally
        rt.bus.publish("reflex.triggered", {"reflex": self.name, "trigger": event.type, "person_id": pid},
                       source="reflex")
        params = {"phrase": self.params["phrase"], "success_phrase": self.params["success_phrase"],
                  "miss_phrase": self.params["miss_phrase"] or None, "wait_s": self.params["wait_s"],
                  "announce": self.params["announce"]}
        result = rt.gestures.play("high_five", params)
        return {"person_id": pid, **(result or {})}
