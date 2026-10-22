"""Hearing: laptop STT pipeline + on-robot keyword recognition + QiChat."""

from __future__ import annotations

import base64
from typing import Annotated, Any

from mcp import types
from pydantic import Field

from pepper_mcp.robot.audio_in import pcm_to_wav
from pepper_mcp.tools._common import ACTION, READ_ONLY, require, text_block, tool


def register(mcp, rt) -> None:  # type: ignore[no-untyped-def]
    @tool(mcp, READ_ONLY)
    async def listen(timeout_s: Annotated[float, Field(gt=0, le=300)] = 20.0) -> dict[str, Any]:
        """Wait for the next thing a person says and return its transcript (sugar for
        wait_for_event(["speech.final"])). Returns {timeout: true} if nobody spoke."""
        if rt.listener is None or not rt.listener.enabled:
            return {"error": "hearing is disabled (no microphone/STT); use recognize_keywords or enable with set_listening"}
        events = await rt.bus.wait(["speech.final"], timeout_s)
        if not events:
            return {"timeout": True, "waited_s": timeout_s}
        return events[-1].data | {"seq": events[-1].seq}

    @tool(mcp, ACTION)
    def set_listening(enabled: bool, barge_in: bool | None = None,
                      end_silence_ms: Annotated[int | None, Field(ge=150, le=2000)] = None) -> dict[str, Any]:
        """Turn free-speech hearing on/off; barge_in lets a person interrupt Pepper mid-sentence;
        end_silence_ms is how long a pause ends an utterance (lower = snappier, more cut-offs)."""
        listener = require(rt.listener, "listener (microphone + STT)")
        if barge_in is not None:
            listener.barge_in = barge_in
        if end_silence_ms is not None:
            listener.end_silence_ms = end_silence_ms
        listener.set_enabled(enabled)
        return listener.status()

    @tool(mcp, READ_ONLY)
    def get_transcript(limit: Annotated[int, Field(ge=1, le=200)] = 20) -> dict[str, Any]:
        """Recent utterances heard from people, newest last."""
        heard = list(rt.listener.transcript)[-limit:] if rt.listener else []
        return {"count": len(heard), "utterances": heard, "hearing_enabled": bool(rt.listener and rt.listener.enabled)}

    @tool(mcp, READ_ONLY)
    def mic_level() -> dict[str, Any]:
        """Microphone stream status: RMS level, callback rate, whether speech is detected now."""
        mic = require(rt.mic, "microphone")
        out = mic.status()
        if rt.listener:
            out["listener"] = rt.listener.status()
        return out

    @tool(mcp, READ_ONLY)
    def record_clip(seconds: Annotated[float, Field(gt=0, le=10)] = 3.0) -> types.CallToolResult:
        """Record a short clip from Pepper's front microphone and return it as WAV audio
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
        Faster and works without STT; blocks until a word is heard or the timeout passes."""
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
