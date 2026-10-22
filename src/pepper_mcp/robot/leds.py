"""LEDs (ALLeds) including mood presets used by the reflex layer."""

from __future__ import annotations

import logging
import threading
from typing import Any

from pepper_mcp.robot.connection import RobotConnection, RobotError

log = logging.getLogger(__name__)

GROUPS = {"eyes": "FaceLeds", "chest": "ChestLeds", "ears": "EarLeds", "shoulders": "ShoulderLeds",
          "all": "AllLeds", "left_eye": "LeftFaceLeds", "right_eye": "RightFaceLeds"}

COLORS = {
    "white": (1, 1, 1), "red": (1, 0, 0), "green": (0, 1, 0), "blue": (0, 0, 1), "yellow": (1, 1, 0),
    "magenta": (1, 0, 1), "cyan": (0, 1, 1), "orange": (1, 0.45, 0), "purple": (0.5, 0, 1),
    "pink": (1, 0.3, 0.6), "off": (0, 0, 0), "black": (0, 0, 0), "warm": (1, 0.8, 0.5),
}

MOODS: dict[str, tuple[str, float]] = {
    "neutral": ("white", 0.3), "listening": ("cyan", 0.2), "thinking": ("purple", 0.3),
    "speaking": ("white", 0.2), "happy": ("green", 0.15), "alert": ("orange", 0.1),
    "sad": ("blue", 0.6), "error": ("red", 0.1),
}


def parse_color(color: str) -> tuple[float, float, float]:
    color = color.strip().lower()
    if color in COLORS:
        return COLORS[color]
    if color.startswith("#") and len(color) == 7:
        return tuple(int(color[i:i + 2], 16) / 255.0 for i in (1, 3, 5))  # type: ignore[return-value]
    raise RobotError(f"unknown color {color!r}; use a name ({', '.join(sorted(COLORS))}) or #rrggbb")


def rgb_int(rgb: tuple[float, float, float]) -> int:
    r, g, b = (int(max(0.0, min(1.0, c)) * 255) for c in rgb)
    return (r << 16) | (g << 8) | b


class Leds:
    def __init__(self, conn: RobotConnection):
        self.conn = conn
        self.mood = "neutral"
        self._lock = threading.Lock()

    @property
    def leds(self) -> Any:
        return self.conn.service("ALLeds")

    @staticmethod
    def group(name: str) -> str:
        if name in GROUPS:
            return GROUPS[name]
        if name in GROUPS.values():
            return name
        raise RobotError(f"unknown LED group {name!r}; choose from {sorted(GROUPS)}")

    def set(self, group: str, color: str, fade_s: float = 0.2) -> dict[str, Any]:
        rgb = parse_color(color)
        self.leds.fadeRGB(self.group(group), rgb_int(rgb), float(max(0.0, fade_s)))
        return {"group": group, "color": color, "rgb": [round(c, 2) for c in rgb]}

    def off(self, group: str = "all") -> None:
        self.leds.off(self.group(group))

    def reset(self, group: str = "all") -> None:
        self.leds.reset(self.group(group))
        self.mood = "neutral"

    def set_intensity(self, group: str, intensity: float) -> None:
        self.leds.setIntensity(self.group(group), float(max(0.0, min(1.0, intensity))))

    def rotate_eyes(self, color: str, duration_s: float = 2.0, speed_s: float = 0.5) -> None:
        rgb = rgb_int(parse_color(color))
        try:
            self.leds.rotateEyes(rgb, float(speed_s), float(duration_s))
        except Exception as exc:
            log.debug("rotateEyes: %s", exc)
            self.set("eyes", color, 0.2)

    def set_mood(self, mood: str) -> dict[str, Any]:
        if mood not in MOODS:
            raise RobotError(f"unknown mood {mood!r}; choose from {sorted(MOODS)}")
        color, fade = MOODS[mood]
        with self._lock:
            self.mood = mood
            if mood == "neutral":
                try:
                    self.leds.reset("FaceLeds")
                    return {"mood": mood}
                except Exception:
                    pass
            self.leds.fadeRGB("FaceLeds", rgb_int(COLORS[color]), fade)
        return {"mood": mood, "color": color}

    def groups(self) -> dict[str, str]:
        return dict(GROUPS)
