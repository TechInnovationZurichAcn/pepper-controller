"""Reflex layer control."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from pepper_mcp.tools._common import ACTION, READ_ONLY, require, tool


def register(mcp, rt) -> None:  # type: ignore[no-untyped-def]
    @tool(mcp, READ_ONLY)
    def list_reflexes() -> dict[str, Any]:
        """Automatic reactions that run without you (attention lights, high_five, greet,
        touch_reaction, low_battery): enabled state, triggers, parameters, how often they fired."""
        return {"reflexes": require(rt.reflexes, "reflexes").list()}

    @tool(mcp, ACTION)
    def set_reflex(name: str, enabled: bool | None = None,
                   params: Annotated[dict[str, Any] | None, Field(description="override parameters, e.g. {\"phrase\": \"Hi!\"} for greet or {\"wait_s\": 5} for high_five")] = None) -> dict[str, Any]:
        """Enable/disable a reflex or change its parameters (e.g. make greet say a phrase, change
        the high-five catchphrase, switch LED moods off)."""
        return require(rt.reflexes, "reflexes").set(name, enabled, params)
