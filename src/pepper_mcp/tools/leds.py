"""Lights."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from pepper_mcp.robot.leds import COLORS, MOODS
from pepper_mcp.tools._common import ACTION, READ_ONLY, require, tool


def register(mcp, rt) -> None:  # type: ignore[no-untyped-def]
    @tool(mcp, ACTION)
    def set_leds(group: Literal["eyes", "chest", "ears", "shoulders", "all", "left_eye", "right_eye"],
                 color: Annotated[str, Field(description=f"name ({', '.join(sorted(COLORS))}) or #rrggbb")],
                 fade_s: Annotated[float, Field(ge=0, le=5)] = 0.3) -> dict[str, Any]:
        """Set an LED group to a colour (ears are blue-only: intensity follows brightness)."""
        return require(rt.leds, "LEDs").set(group, color, fade_s)

    @tool(mcp, ACTION)
    def leds_off(group: Literal["eyes", "chest", "ears", "shoulders", "all"] = "all") -> dict[str, Any]:
        """Switch an LED group off."""
        require(rt.leds, "LEDs").off(group)
        return {"group": group, "off": True}

    @tool(mcp, ACTION)
    def leds_reset(group: Literal["eyes", "chest", "ears", "shoulders", "all"] = "all") -> dict[str, Any]:
        """Restore the default LED colours."""
        require(rt.leds, "LEDs").reset(group)
        return {"group": group, "reset": True}

    @tool(mcp, ACTION)
    def eyes_rotate(color: str = "cyan", duration_s: Annotated[float, Field(gt=0, le=30)] = 2.0,
                    speed_s: Annotated[float, Field(gt=0, le=5)] = 0.5) -> dict[str, Any]:
        """Spin a colour around the eyes (thinking / loading effect)."""
        require(rt.leds, "LEDs").rotate_eyes(color, duration_s, speed_s)
        return {"color": color, "duration_s": duration_s}

    @tool(mcp, ACTION)
    def set_mood_lights(mood: Literal["neutral", "listening", "thinking", "speaking", "happy", "alert", "sad", "error"]) -> dict[str, Any]:
        """Eye colour preset expressing a mood (the attention reflex also drives these)."""
        return require(rt.leds, "LEDs").set_mood(mood)

    @tool(mcp, READ_ONLY)
    def list_led_groups() -> dict[str, Any]:
        """LED groups, colour names and moods."""
        return {"groups": require(rt.leds, "LEDs").groups(), "colors": sorted(COLORS), "moods": sorted(MOODS)}
