"""Touch → immediate acknowledgement (LED flash + small head move) so the person knows
Pepper felt it, before the LLM decides what to say."""

from __future__ import annotations

import time
from typing import Any

from pepper_mcp.reflexes.base import Reflex
from pepper_mcp.robot.events import Event
from pepper_mcp.robot.motion import rad


class TouchReflex(Reflex):
    name = "touch_reaction"
    description = ("head touched → eyes flash green and Pepper tilts its head; hand touched → hand "
                   "closes gently for a moment; bumper → nothing (driving)")
    triggers = ("touch.start",)
    params: dict[str, Any] = {"head_phrase": "", "hand_phrase": "", "led": True}
    cooldown_s = 1.5

    def react(self, event: Event) -> Any:
        rt = self.rt
        sensor = event.data.get("sensor", "")
        if "bumper" in sensor:
            return None
        if self.params["led"] and rt.leds:
            rt.leds.set_mood("happy")
        if sensor.startswith("head"):
            try:
                cur = rt.motion.get_angles(["HeadYaw", "HeadPitch"])
                rt.motion.set_angles({"HeadYaw": cur["HeadYaw"] + rad(12), "HeadPitch": cur["HeadPitch"] + rad(8)}, 0.3)
                time.sleep(0.5)
                rt.motion.set_angles(cur, 0.2)
            except Exception:
                pass
            if self.params["head_phrase"]:
                rt.speech.say(self.params["head_phrase"], animated=False)
        elif "hand" in sensor:
            side = "L" if sensor.startswith("left") else "R"
            try:
                rt.motion.set_angles({f"{side}Hand": 0.3}, 0.5)
                time.sleep(0.6)
                rt.motion.set_angles({f"{side}Hand": 1.0}, 0.4)
            except Exception:
                pass
            if self.params["hand_phrase"]:
                rt.speech.say(self.params["hand_phrase"], animated=False)
        if self.params["led"] and rt.leds:
            time.sleep(0.4)
            if not rt.speech.speaking:
                rt.leds.set_mood("neutral")
        return {"sensor": sensor}
