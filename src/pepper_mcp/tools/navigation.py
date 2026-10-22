"""Obstacle-avoiding navigation, mapping and remembered spots (ALNavigation)."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from pepper_mcp.tools._common import ACTION, MOVES_ROBOT, READ_ONLY, require, task_result, tool


def register(mcp, rt) -> None:  # type: ignore[no-untyped-def]
    def nav():  # type: ignore[no-untyped-def]
        return require(rt.navigator, "navigation (ALNavigation)")

    @tool(mcp, MOVES_ROBOT)
    def navigate_to(x_m: Annotated[float, Field(ge=-5, le=5, description="forward(+)/back(-) in the robot frame")],
                    y_m: Annotated[float, Field(ge=-5, le=5, description="left(+)/right(-)")] = 0.0,
                    wait: bool = False) -> dict[str, Any]:
        """Go to a point relative to the robot while avoiding obstacles (no map needed)."""
        n = nav()
        task = rt.tasks.run("navigate", f"navigate_to({x_m}, {y_m})", lambda: n.navigate_to(x_m, y_m), cancel=n.stop)
        return task_result(task, wait, 120)

    @tool(mcp, MOVES_ROBOT)
    def explore_area(radius_m: Annotated[float, Field(ge=1, le=15)] = 5.0) -> dict[str, Any]:
        """Autonomously explore and map the surroundings (takes minutes; keep the area clear). Saves
        the map and starts localization. Progress arrives as nav.status events."""
        n = nav()
        task = rt.tasks.run("explore", f"explore {radius_m} m", lambda: n.explore(radius_m), cancel=n.stop)
        return task_result(task, False)

    @tool(mcp, ACTION)
    def stop_navigation() -> dict[str, Any]:
        """Abort exploration / navigation and release the base."""
        n = nav()
        n.stop()
        return n.status()

    @tool(mcp, READ_ONLY)
    def navigation_status() -> dict[str, Any]:
        """Busy flag, status text, current map, localization, spots."""
        return nav().status()

    @tool(mcp, ACTION)
    def remember_spot(name: str | None = None) -> dict[str, Any]:
        """Save the current position in the current map under a name (requires localization)."""
        return nav().remember_spot(name)

    @tool(mcp, ACTION)
    def forget_spot(name: Annotated[str | None, Field(description="omit to forget all spots of the current map")] = None) -> dict[str, Any]:
        """Forget one or all remembered spots of the current map."""
        return {"spots": nav().forget_spot(name)}

    @tool(mcp, MOVES_ROBOT)
    def go_to_spot(name: str, wait: bool = False) -> dict[str, Any]:
        """Navigate back to a remembered spot, avoiding obstacles."""
        n = nav()
        task = rt.tasks.run("go_to_spot", name, lambda: n.go_to_spot(name), cancel=n.stop)
        return task_result(task, wait, 300)

    @tool(mcp, READ_ONLY)
    def list_spots() -> dict[str, Any]:
        """Remembered spots in the current map."""
        n = nav()
        return {"map": n.current, "spots": n.status()["spots"]}

    @tool(mcp, READ_ONLY)
    def list_maps() -> dict[str, Any]:
        """Known maps (one per room/area) and which one is current."""
        n = nav()
        return {"current": n.current, "maps": {name: {"explored": bool(rec.get("map_path")),
                                                     "spots": sorted(rec.get("spots", {}))}
                                              for name, rec in n.maps.items()}}

    @tool(mcp, ACTION)
    def new_map(name: str | None = None) -> dict[str, Any]:
        """Start a fresh empty map (the next explore_area fills it)."""
        return {"current": nav().new_map(name)}

    @tool(mcp, ACTION)
    def switch_map(name: str) -> dict[str, Any]:
        """Make another map current and relocalize in it (place Pepper at that map's start point first)."""
        return nav().switch_map(name)

    @tool(mcp, ACTION)
    def load_map() -> dict[str, Any]:
        """Reload the current map and start localization at its origin."""
        n = nav()
        return {"localized": n.load_map(), **n.status()}
