"""Following people and tracking targets with ALTracker."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from pepper_mcp.tools._common import ACTION, MOVES_ROBOT, READ_ONLY, require, tool


def register(mcp, rt) -> None:  # type: ignore[no-untyped-def]
    @tool(mcp, MOVES_ROBOT)
    def follow_person(enabled: bool = True,
                      distance_m: Annotated[float, Field(ge=0.3, le=2.0)] = 0.6) -> dict[str, Any]:
        """Follow the person in front: Pepper drives to keep `distance_m` from them (ALTracker Move
        mode). While active the base and head belong to the tracker – `move` is refused."""
        tracker = require(rt.tracker, "tracker")
        return tracker.follow(distance_m) if enabled else tracker.stop()

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
