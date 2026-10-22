"""Sounds through Pepper's speakers."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from pepper_mcp.tools._common import ACTION, require, task_result, tool


def register(mcp, rt) -> None:  # type: ignore[no-untyped-def]
    @tool(mcp, ACTION)
    def play_audio(source: Annotated[str, Field(description="path on the robot (/home/nao/x.mp3) or an http(s) URL of an mp3/wav/stream")],
                   volume: Annotated[float | None, Field(ge=0, le=1)] = None, loop: bool = False,
                   wait: bool = False) -> dict[str, Any]:
        """Play a sound or music. URLs are streamed by the robot itself (playWebStream)."""
        audio = require(rt.audio_out, "audio")
        fut = audio.play_async(source, volume, loop)
        return task_result(rt.tasks.from_future("audio", source, fut), wait, 600)

    @tool(mcp, ACTION)
    def play_sine(frequency_hz: Annotated[int, Field(ge=50, le=5000)] = 880,
                  duration_s: Annotated[float, Field(gt=0, le=5)] = 0.3,
                  volume: Annotated[int, Field(ge=0, le=100)] = 40) -> dict[str, Any]:
        """Beep (useful as an attention or confirmation sound)."""
        require(rt.audio_out, "audio").play_sine(frequency_hz, duration_s, volume)
        return {"played": True}

    @tool(mcp, ACTION)
    def stop_audio() -> dict[str, Any]:
        """Stop all sounds/music (not speech)."""
        require(rt.audio_out, "audio").stop()
        return {"stopped": True}

    @tool(mcp, ACTION)
    def upload_audio(local_path: Annotated[str, Field(description="file on the machine running this server")]) -> dict[str, Any]:
        """Copy a sound file onto the robot (scp with the nao password) so play_audio can play it offline."""
        remote = require(rt.audio_out, "audio").upload(local_path)
        return {"robot_path": remote}
