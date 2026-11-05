"""Hearing: microphone status and clips, on-robot keyword recognition and QiChat.

Free speech is heard by the voice agent (Gemini Live), which gets the mic stream directly."""

from __future__ import annotations

import base64
from typing import Annotated, Any

from mcp import types
from pydantic import Field

from pepper_mcp.robot.audio_in import pcm_to_wav
from pepper_mcp.tools._common import ACTION, READ_ONLY, require, text_block, tool


def register(mcp, rt) -> None:  # type: ignore[no-untyped-def]
    @tool(mcp, READ_ONLY)
    def mic_level() -> dict[str, Any]:
        """Microphone stream status: which head mic is used, per-mic energies, RMS level and
        callback rate."""
        return require(rt.mic, "microphone").status()

    @tool(mcp, READ_ONLY)
    def record_clip(seconds: Annotated[float, Field(gt=0, le=10)] = 3.0) -> types.CallToolResult:
        """Record a short clip from Pepper's head microphone and return it as WAV audio
        (16 kHz mono) so you can listen to it yourself."""
        mic = require(rt.mic, "microphone")
        samples = mic.record(seconds)
        wav = pcm_to_wav(samples, mic.rate)
        return types.CallToolResult(content=[
            types.AudioContent(type="audio", data=base64.b64encode(wav).decode(), mimeType="audio/wav"),
            text_block({"seconds": seconds, "samples": int(samples.size), "rate": mic.rate}),
        ])

    @tool(mcp, ACTION)
    def recognize_keywords(words: Annotated[list[str], Field(min_length=1, max_length=50)],
                           timeout_s: Annotated[float, Field(gt=0, le=60)] = 8.0,
                           word_spotting: bool = True) -> dict[str, Any]:
        """Offline on-robot recognition of a small fixed vocabulary (yes/no, menu choices, names).
        Runs on the robot; blocks until a word is heard or the timeout passes."""
        return require(rt.asr, "on-robot speech recognition").recognize(words, timeout_s, word_spotting)

    @tool(mcp, ACTION)
    def start_builtin_dialog(qichat_topic: Annotated[str, Field(description="QiChat topic text: 'topic: ~name()\\nlanguage: enu\\nu:(hello) Hi there!'")]) -> dict[str, Any]:
        """Run a scripted QiChat dialog entirely on the robot (no LLM in the loop). Shares the
        recogniser with recognize_keywords."""
        return require(rt.asr, "ALDialog").start_dialog(qichat_topic)

    @tool(mcp, ACTION)
    def stop_builtin_dialog() -> dict[str, Any]:
        """Stop the QiChat dialog started with start_builtin_dialog."""
        return require(rt.asr, "ALDialog").stop_dialog()
