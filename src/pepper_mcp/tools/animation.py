"""Stock animations and custom gestures."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from pepper_mcp.robot.animation import TAGS
from pepper_mcp.tools._common import ACTION, READ_ONLY, task_result, tool


def register(mcp, rt) -> None:  # type: ignore[no-untyped-def]
    @tool(mcp, ACTION)
    def play_animation(name: Annotated[str, Field(description="full path (animations/Stand/Gestures/Hey_1), bare name (Hey_1) or a tag (hello, yes, no, thinking, happy, bow…)")],
                       wait: bool = False) -> dict[str, Any]:
        """Play one of Pepper's built-in animations (a few seconds). Runs in the background unless
        wait=true, so call it right before/while you `say` something."""
        resolved, fut = rt.animations.run_async(name)
        return task_result(rt.tasks.from_future("animation", resolved, fut), wait, 30) | {"animation": resolved}

    @tool(mcp, READ_ONLY)
    def list_animations(filter: Annotated[str | None, Field(description="substring, e.g. 'Hey' or 'Emotions'")] = None,
                        category: Annotated[str | None, Field(description="Gestures | Emotions | Waiting | BodyTalk")] = None,
                        limit: Annotated[int, Field(ge=1, le=500)] = 80) -> dict[str, Any]:
        """Installed animations (discovered from the robot) with a short meaning where known."""
        items = rt.animations.list(filter, category)
        return {"count": len(items), "animations": items[:limit], "tags": TAGS}

    @tool(mcp, ACTION)
    def play_gesture(name: Annotated[str, Field(description="see list_gestures: high_five, wave, celebrate, nod, shake_head, hug_open, point_forward, thinking_pose, bow, arms_down, sixty_seven, clown_dance or a user-defined one")],
                     wait: bool = False,
                     params: Annotated[dict[str, Any] | None, Field(description="gesture parameters, e.g. {\"duration_s\": 10} for dances, {\"phrase\": \"Up top!\"} for high_five")] = None) -> dict[str, Any]:
        """Play a custom gesture from this server's registry (keyframe or procedural). Only one
        gesture runs at a time; a new one stops the previous."""
        gesture = rt.gestures.get(name)
        task = rt.tasks.run("gesture", name, lambda: rt.gestures.play(name, params or {}),
                            cancel=lambda: rt.gestures.stop_all())
        return task_result(task, wait, gesture.duration_s + 10) | {"gesture": gesture.to_dict()}

    @tool(mcp, READ_ONLY)
    def list_gestures() -> dict[str, Any]:
        """Available custom gestures with description, duration and (for keyframe ones) the keyframes."""
        items = rt.gestures.list()
        return {"count": len(items), "gestures": items}

    @tool(mcp, ACTION)
    def define_gesture(name: Annotated[str, Field(min_length=2, max_length=40)],
                       description: str,
                       keyframes: Annotated[list[dict[str, Any]], Field(
                           description="[{\"t_s\": 0.8, \"joints\": {\"RShoulderPitch\": -60, \"RElbowRoll\": 40}}, ...] times increasing, angles in degrees (see list_joints)")],
                       return_to_rest: bool = True, speech: str | None = None) -> dict[str, Any]:
        """Create a new keyframe gesture at runtime; it is immediately available in play_gesture.
        Angles are clamped to safe joint limits."""
        g = rt.gestures.define(name, description, keyframes, return_to_rest, speech)
        return g.to_dict()

    @tool(mcp, ACTION)
    def remove_gesture(name: str) -> dict[str, Any]:
        """Remove a user-defined gesture (built-ins cannot be removed)."""
        return {"removed": rt.gestures.remove(name)}

    @tool(mcp, ACTION)
    def stop_animation() -> dict[str, Any]:
        """Stop running animations and gestures (keeps the base/tracking as they are)."""
        n = rt.gestures.stop_all()
        rt.animations.stop()
        return {"gestures_stopped": n}
