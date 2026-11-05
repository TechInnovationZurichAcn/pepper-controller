"""Following people and tracking targets with ALTracker."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from pepper_mcp.robot.tracker import DEFAULT_FOLLOW_M, MIN_FOLLOW_M
from pepper_mcp.tools._common import ACTION, MOVES_ROBOT, READ_ONLY, require, tool


def register(mcp, rt) -> None:  # type: ignore[no-untyped-def]
    @tool(mcp, MOVES_ROBOT)
    def follow_person(enabled: bool = True,
                      distance_m: Annotated[float, Field(ge=MIN_FOLLOW_M, le=2.0, description="metres to keep from the person; the default is the closest that works")] = DEFAULT_FOLLOW_M) -> dict[str, Any]:
        """Follow the person in front: Pepper drives after them, keeping `distance_m` (ALTracker Move
        mode), until follow_person(enabled=false), stop_motion, or a move/move_to/turn command.
        If the person is out of sight for a few seconds a follow.lost event arrives (follow.found
        when someone is seen again)."""
        tracker = require(rt.tracker, "tracker")
        if not enabled:
            return tracker.stop()
        if rt.navigator and rt.navigator.busy:
            raise ValueError("navigation owns the base; stop_navigation first")
        if not rt.motion.is_awake():
            rt.motion.wake_up()
        rt.motion.check_base_ready()
        return tracker.follow(distance_m)

    @tool(mcp, ACTION)
    def track(target: Literal["face", "people", "sound"] = "face",
              mode: Literal["head", "body_rotation", "whole_body", "move"] = "head",
              enabled: bool = True) -> dict[str, Any]:
        """Keep the head (or body) on a target: a face, people or the loudest sound. Use mode=head
        for conversations so Pepper keeps eye contact."""
        tracker = require(rt.tracker, "tracker")
        return tracker.track(target, mode) if enabled else tracker.stop()

    @tool(mcp, READ_ONLY)
    def tracking_status() -> dict[str, Any]:
        """Current tracking/following state."""
        return require(rt.tracker, "tracker").status()
