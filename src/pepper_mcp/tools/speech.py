"""Talking."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from pepper_mcp.tools._common import ACTION, READ_ONLY, tool


def register(mcp, rt) -> None:  # type: ignore[no-untyped-def]
    @tool(mcp, ACTION)
    def say(text: Annotated[str, Field(description="what to say; keep it to one or two short sentences per call")],
            animated: Annotated[bool, Field(description="gesture while speaking (ALAnimatedSpeech)")] = True,
            wait: Annotated[bool, Field(description="block until the sentence has been spoken")] = False,
            body_language: Literal["contextual", "random", "disabled"] = "contextual",
            language: str | None = None,
            speed: Annotated[int | None, Field(ge=50, le=400, description="speech rate %, 100 = normal")] = None,
            pitch: Annotated[int | None, Field(ge=50, le=200, description="pitch %, 100 = normal")] = None,
            volume: Annotated[int | None, Field(ge=0, le=100)] = None) -> dict[str, Any]:
        """Speak out loud. Returns immediately (queued, in order) unless wait=true. Call it as soon as
        you know your first sentence and keep adding sentences – they play back-to-back. Inline
        animation tags work too: "^start(animations/Stand/Gestures/Hey_1) Hello!" or "^startTag(hello)"."""
        utt = rt.speech.say(text, animated=animated, body_language=body_language, language=language,
                            speed=speed, pitch=pitch, volume=volume, wait=wait)
        return utt.to_dict() | {"queued_ahead": rt.speech.status()["queued"]}

    @tool(mcp, ACTION)
    def stop_speaking() -> dict[str, Any]:
        """Interrupt the current sentence and drop everything queued."""
        return {"dropped": rt.speech.stop_speaking()}

    @tool(mcp, READ_ONLY)
    def speech_status() -> dict[str, Any]:
        """Is Pepper talking, how much is queued, voice settings."""
        return rt.speech.status() | {"voice": rt.speech.voice_info()}

    @tool(mcp, ACTION)
    def set_voice(language: str | None = None,
                  speed: Annotated[int | None, Field(ge=50, le=400)] = None,
                  pitch: Annotated[int | None, Field(ge=50, le=200)] = None,
                  voice: str | None = None) -> dict[str, Any]:
        """Change the default TTS language (e.g. English, French, German), rate, pitch or voice."""
        return rt.speech.set_voice(language, speed, pitch, voice)

    @tool(mcp, READ_ONLY)
    def list_languages() -> dict[str, Any]:
        """Installed TTS languages and voices."""
        return rt.speech.languages()
