from __future__ import annotations

from typing import Any

from pepper_mcp.reflexes.base import Reflex
from pepper_mcp.robot.events import Event


class LowBatteryReflex(Reflex):
    name = "low_battery"
    description = "battery under 15 % → chest LED turns orange and Pepper says it needs charging"
    triggers = ("battery.low",)
    params: dict[str, Any] = {"phrase": "My battery is getting low, I should charge soon."}
    cooldown_s = 600.0

    def react(self, event: Event) -> Any:
        rt = self.rt
        if rt.leds:
            try:
                rt.leds.set("chest", "orange", 0.5)
            except Exception:
                pass
        if self.params["phrase"]:
            rt.speech.say(self.params["phrase"], animated=False)
        return {"percent": event.data.get("percent")}
