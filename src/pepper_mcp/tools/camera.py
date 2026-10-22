"""Seeing."""

from __future__ import annotations

import base64
from typing import Annotated, Any, Literal

from mcp import types
from pydantic import Field

from pepper_mcp.tools._common import ACTION, READ_ONLY, require, text_block, tool


def register(mcp, rt) -> None:  # type: ignore[no-untyped-def]
    @tool(mcp, READ_ONLY)
    def camera_snapshot(camera: Literal["top", "bottom", "depth"] = "top",
                        max_width: Annotated[int, Field(ge=80, le=1280)] = 640,
                        quality: Annotated[int, Field(ge=30, le=95)] = 80) -> types.CallToolResult:
        """Return the newest frame from a camera as a JPEG image (top = forehead camera looking
        ahead, bottom = looks down at hands/floor, depth = 3D sensor rendered as grey, near = bright).
        Never blocks on the robot – frames are grabbed continuously."""
        cams = require(rt.cameras, "camera")
        data, frame = cams.snapshot(camera, max_width, quality)
        return types.CallToolResult(content=[
            types.ImageContent(type="image", data=base64.b64encode(data).decode(), mimeType="image/jpeg"),
            text_block({"camera": camera, "width": frame.width, "height": frame.height,
                        "frame_age_ms": frame.age_ms, "bytes": len(data)}),
        ])

    @tool(mcp, ACTION)
    def camera_stream(camera: Literal["top", "bottom", "depth"] = "top", enabled: bool = True,
                      fps: Annotated[int | None, Field(ge=1, le=30)] = None,
                      resolution: Literal["qqvga", "qvga", "vga"] | None = None) -> dict[str, Any]:
        """Start/stop background grabbing for a camera and tune fps/resolution (qvga 320x240 is the
        sweet spot for latency)."""
        return require(rt.cameras, "camera").set_stream(camera, enabled, fps, resolution)

    @tool(mcp, READ_ONLY)
    def camera_status() -> dict[str, Any]:
        """Per camera: running, achieved fps, last frame age, errors."""
        return {"cameras": require(rt.cameras, "camera").status()}
