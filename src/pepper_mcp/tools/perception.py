"""Who and what is around the robot."""

from __future__ import annotations

from typing import Any

from pepper_mcp.tools._common import ACTION, READ_ONLY, require, tool


def register(mcp, rt) -> None:  # type: ignore[no-untyped-def]
    @tool(mcp, READ_ONLY)
    def get_people() -> dict[str, Any]:
        """People Pepper currently perceives: id, distance, bearing (deg, left positive), whether a
        face is seen, waving, looking at the robot, engagement zone and face properties if analysed."""
        p = require(rt.perception, "perception")
        people = p.people()
        return {"count": len(people), "people": people}

    @tool(mcp, ACTION)
    def analyze_face(person_id: int | None = None) -> dict[str, Any]:
        """Estimate age, gender, smile and expression of a person (nearest if no id). Takes ~1 s."""
        return require(rt.perception, "perception").analyze_face(person_id)

    @tool(mcp, READ_ONLY)
    def get_touch_state() -> dict[str, bool]:
        """Which tactile sensors (head front/middle/rear, hand backs, bumpers) are touched right now."""
        return require(rt.perception, "perception").touch_state()

    @tool(mcp, READ_ONLY)
    def get_sonar() -> dict[str, float | None]:
        """Front/back sonar distance in metres (obstacle or person in front of the base)."""
        return require(rt.perception, "perception").sonar()

    @tool(mcp, READ_ONLY)
    def get_sound_direction() -> dict[str, Any]:
        """Direction of the last loud sound (azimuth deg, left positive) and how long ago."""
        last = require(rt.perception, "perception").last_sound()
        return last or {"none": True}

    @tool(mcp, ACTION)
    def set_perception(people: bool | None = None, waving: bool | None = None, gaze: bool | None = None,
                       engagement: bool | None = None, sound_localization: bool | None = None,
                       face_detection: bool | None = None, face_characteristics: bool | None = None,
                       hand_raise_vision: bool | None = None) -> dict[str, Any]:
        """Enable/disable NAOqi perception extractors (each costs some robot CPU) and the optional
        laptop hand-raise detector."""
        p = require(rt.perception, "perception")
        for name, value in {"people": people, "waving": waving, "gaze": gaze, "engagement": engagement,
                            "sound_localization": sound_localization, "face_detection": face_detection,
                            "face_characteristics": face_characteristics}.items():
            if value is not None:
                p.set_extractor(name, value)
        out = dict(p.active)
        if hand_raise_vision is not None:
            if hand_raise_vision and rt.vision is None:
                rt._start_vision()
            elif not hand_raise_vision and rt.vision is not None:
                rt.vision.stop()
                rt.vision = None
        out["hand_raise_vision"] = rt.vision is not None
        return out
