"""The chest tablet."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from pepper_mcp.tools._common import ACTION, READ_ONLY, require, tool


def register(mcp, rt) -> None:  # type: ignore[no-untyped-def]
    @tool(mcp, ACTION)
    def tablet_show_text(text: Annotated[str, Field(max_length=2000)], background: str = "#0b1020",
                         color: str = "#eaf2ff", font_px: Annotated[int, Field(ge=16, le=200)] = 64) -> dict[str, Any]:
        """Show big centred text (or emoji) on the tablet. Great for key words, names, choices."""
        require(rt.tablet, "tablet").show_text(text, background, color, font_px)
        return {"shown": text}

    @tool(mcp, ACTION)
    def tablet_show_html(html: Annotated[str, Field(max_length=60000)]) -> dict[str, Any]:
        """Render a self-contained HTML page on the tablet (inline CSS/JS only, no external fetches
        unless the tablet has internet)."""
        require(rt.tablet, "tablet").show_html(html)
        return {"shown": "html", "bytes": len(html)}

    @tool(mcp, ACTION)
    def tablet_show_url(url: str) -> dict[str, Any]:
        """Open a web page on the tablet (the tablet must be able to reach the URL)."""
        require(rt.tablet, "tablet").show_url(url)
        return {"shown": url}

    @tool(mcp, ACTION)
    def tablet_show_image(url: str) -> dict[str, Any]:
        """Show an image from a URL on the tablet."""
        require(rt.tablet, "tablet").show_image(url)
        return {"shown": url}

    @tool(mcp, ACTION)
    def tablet_hide() -> dict[str, Any]:
        """Return the tablet to its idle screen."""
        require(rt.tablet, "tablet").hide()
        return {"hidden": True}

    @tool(mcp, ACTION)
    def tablet_execute_js(script: Annotated[str, Field(max_length=10000)]) -> dict[str, Any]:
        """Run JavaScript inside the page currently shown on the tablet."""
        require(rt.tablet, "tablet").execute_js(script)
        return {"executed": True}

    @tool(mcp, ACTION)
    def tablet_play_video(url: str) -> dict[str, Any]:
        """Play an mp4/mov video from a URL on the tablet."""
        require(rt.tablet, "tablet").play_video(url)
        return {"playing": url}

    @tool(mcp, ACTION)
    def tablet_stop_video() -> dict[str, Any]:
        """Stop the tablet video."""
        require(rt.tablet, "tablet").stop_video()
        return {"stopped": True}

    @tool(mcp, ACTION)
    def tablet_set_brightness(level: Annotated[float, Field(ge=0, le=1)]) -> dict[str, Any]:
        """Tablet screen brightness 0-1."""
        require(rt.tablet, "tablet").set_brightness(level)
        return {"brightness": level}

    @tool(mcp, ACTION)
    def tablet_open_settings() -> dict[str, Any]:
        """Open Pepper's on-tablet Settings menu (Wi-Fi, volume, language) for a human to use."""
        require(rt.tablet, "tablet").open_settings()
        return {"opened": True}

    @tool(mcp, READ_ONLY)
    def tablet_status() -> dict[str, Any]:
        """Tablet availability, what is shown, Wi-Fi status."""
        return require(rt.tablet, "tablet").status()
