"""Gemini Live bridge: tool mapping, echo gate, events and speaker streaming on the fake robot.

The Gemini session itself is replaced by a recorder; everything robot-side is real (fake NAOqi)."""

from __future__ import annotations

import argparse
import asyncio
import threading
import time
from typing import Any

import numpy as np
import pytest

pytest.importorskip("google.genai")

from pepper_mcp.gemini_live import (
    EXCLUDED_TOOLS,
    KEY_VAR,
    PERSONA_FILE,
    VOCABULARY_FILE,
    EchoGate,
    GeminiPepper,
    SpeechFilter,
    _parse,
    clean_transcript,
    format_event,
    load_api_key,
    load_knowledge,
    load_persona,
    load_vocabulary,
    makes_sound,
    supports_async_tools,
    system_instruction,
    tool_declarations,
    tool_response,
    transcription_languages,
)
from pepper_mcp.robot.audio_out import SpeakerStream
from pepper_mcp.robot.fake import FakeRobot
from tests.conftest import make_settings
from tests.test_server import connected


class RecordingSession:
    """Stands in for a Gemini Live session: records what the bridge sends."""

    def __init__(self) -> None:
        self.tool_responses: list[Any] = []
        self.realtime: list[dict[str, Any]] = []

    async def send_tool_response(self, function_responses: list[Any]) -> None:
        self.tool_responses.extend(function_responses)

    async def send_realtime_input(self, **kwargs: Any) -> None:
        self.realtime.append(kwargs)


class Call:
    def __init__(self, name: str, args: dict[str, Any], id_: str = "c1"):
        self.name, self.args, self.id = name, args, id_


def _bridge() -> GeminiPepper:
    args = argparse.Namespace(model="m", voice="Kore", persona="Pepper", no_video=True, no_transcripts=True)
    return GeminiPepper(make_settings(), args, api_key="k")


# ---------------------------------------------------------------- tools
async def test_declarations_cover_body_tools_and_hide_voice_and_blocking_ones() -> None:
    async with connected() as client:
        tools = (await client.list_tools()).tools
    decls = {d["name"]: d for d in tool_declarations(tools)}
    for name in ("move", "move_to", "turn", "play_gesture", "look_at_person", "set_leds", "follow_person",
                 "navigate_to", "tablet_show_text", "stop_motion", "camera_snapshot", "set_safety"):
        assert name in decls, name
    assert not EXCLUDED_TOOLS & set(decls)
    assert len(decls) == len(tools) - len(EXCLUDED_TOOLS & {t.name for t in tools})
    schema = decls["move"]["parameters_json_schema"]
    assert schema["properties"]["duration_s"]["maximum"] == 5  # server limits reach the model unchanged
    assert "parameters_json_schema" not in decls["stop_motion"]  # no-argument tools stay bare
    # `wait` would pause the model for a whole move; laptop file access is not for room voices.
    turn = decls["turn"]["parameters_json_schema"]
    assert "wait" not in turn["properties"] and turn["required"] == ["degrees"]
    assert "upload_audio" not in decls


async def test_tool_results_errors_and_images_become_function_responses() -> None:
    async with connected() as client:
        ok = tool_response(await client.call_tool("turn", {"degrees": 30}))
        bad = tool_response(await client.call_tool("set_leds", {"group": "nope", "color": "red"}))
        snap = tool_response(await client.call_tool("camera_snapshot", {}))
    assert ok["task_id"].startswith("turn-") and ok["state"] == "running"
    assert "error" in bad and "group" in bad["error"]
    assert snap["image"] == "sent to your video input"


async def test_sound_tool_never_leaves_the_mic_muted_after_its_task() -> None:
    # The fake gesture usually finishes before the tool call returns: the mute must not wait for
    # a task.finished that already happened (it would only lift after EchoGate.MAX_S).
    async with connected() as client:
        bridge = _bridge()
        bridge.loop = asyncio.get_running_loop()
        unsubscribe = client.rt.bus.subscribe("*", bridge._on_event)
        session = RecordingSession()
        try:
            await bridge._run_tools(session, client, client.rt, [Call("play_gesture", {"name": "nod"})])
            assert session.tool_responses[0].response["task_id"].startswith("gesture-")
            deadline = time.monotonic() + 3
            while bridge.gate.muted() and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            assert not bridge.gate.muted()
        finally:
            unsubscribe()


async def test_camera_snapshot_feeds_the_video_input() -> None:
    async with connected() as client:
        bridge = _bridge()
        session = RecordingSession()
        await bridge._run_tools(session, client, client.rt, [Call("camera_snapshot", {"camera": "top"})])
    video = [r["video"] for r in session.realtime if "video" in r]
    assert video and video[0].mime_type == "image/jpeg" and video[0].data[:2] == b"\xff\xd8"
    assert session.tool_responses[0].response["image"] == "sent to your video input"


async def test_tool_exception_is_reported_to_gemini_not_raised() -> None:
    async with connected() as client:
        session = RecordingSession()
        await _bridge()._run_tools(session, client, client.rt, [Call("no_such_tool", {})])
    assert "error" in session.tool_responses[0].response


async def test_actions_run_in_the_background_and_queries_block() -> None:
    async with connected() as client:
        tools = (await client.list_tools()).tools
    decls = {d["name"]: d for d in tool_declarations(tools)}
    for name in ("play_gesture", "move", "turn", "set_leds", "look_at_person", "stop_motion", "tictactoe_start"):
        assert decls[name]["behavior"] == "NON_BLOCKING", name
    for name in ("get_people", "camera_snapshot", "list_animations", "analyze_face"):
        assert "behavior" not in decls[name], name  # the model needs the answer before it speaks
    assert not any("behavior" in d for d in tool_declarations(tools, non_blocking=False))
    assert supports_async_tools("gemini-3.8-live")
    assert not supports_async_tools("gemini-3.1-flash-live-preview")
    # The declarations must survive the SDK's config validation.
    bridge = _bridge()
    config = bridge._config(list(decls.values()), "sys")
    move = next(f for f in config.tools[0].function_declarations if f.name == "move")
    assert move.behavior.value == "NON_BLOCKING"
    assert config.output_audio_transcription is not None


async def test_background_tool_results_go_back_silent_and_failures_when_idle() -> None:
    from google.genai import types

    async with connected() as client:
        bridge = _bridge()
        bridge.background_tools = frozenset({"play_gesture", "set_leds"})
        session = RecordingSession()
        await bridge._run_tools(session, client, client.rt, [
            Call("play_gesture", {"name": "wave"}, "a"),
            Call("set_leds", {"group": "nope", "color": "red"}, "b"),
            Call("get_battery", {}, "c"),
        ])
    ok, bad, query = session.tool_responses
    assert ok.scheduling == types.FunctionResponseScheduling.SILENT and ok.response["scheduling"] == "SILENT"
    assert bad.scheduling == types.FunctionResponseScheduling.WHEN_IDLE and "error" in bad.response
    assert query.scheduling is None and "scheduling" not in query.response


async def test_gestures_never_mute_the_mic_so_pepper_hears_while_moving() -> None:
    assert not makes_sound("play_gesture", {"name": "wave"})
    assert not makes_sound("play_animation", {"name": "animations/Stand/Gestures/Hey_1"})
    assert makes_sound("play_gesture", {"name": "clown_dance"})  # music
    assert makes_sound("play_audio", {"url": "x"})

    class Slow:
        async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
            await asyncio.sleep(60)

    bridge = _bridge()
    for name, gesture, muted in (("play_gesture", "wave", False), ("play_gesture", "clown_dance", True)):
        task = asyncio.create_task(bridge._run_tools(RecordingSession(), Slow(), None, [Call(name, {"name": gesture})]))
        await asyncio.sleep(0.05)
        assert bridge.gate.muted() is muted, gesture
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        bridge.gate._quiet_at = 0.0


async def test_music_gestures_come_from_the_registry_and_mute_for_the_whole_dance() -> None:
    from types import SimpleNamespace

    async with connected() as client:
        music = client.rt.gestures.music_gestures()
        assert "clown_dance" in music and not {"wave", "nod", "hello_1"} & music
        assert makes_sound("play_gesture", {"name": "dance_x"}, frozenset({"dance_x"}))
        assert not makes_sound("play_gesture", {"name": "dance_x"})  # default before the session starts

    class Done:  # a 90 s dance task, already started
        async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
            text = '{"task_id": "t-dance", "gesture": {"name": "dance_x", "duration_s": 90.0}}'
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], is_error=False,
                                   structured_content=None)

    bridge = _bridge()
    bridge.music_gestures = frozenset({"dance_x"})
    await bridge._run_tools(RecordingSession(), Done(), None, [Call("play_gesture", {"name": "dance_x"})])
    # Regression: the gate's 30 s default cap unmuted the mic a minute before the song ended.
    assert bridge.gate._active["task:t-dance"] - time.monotonic() > 90.0


async def test_tool_called_before_any_words_gets_a_turn_to_answer() -> None:
    # "hello" → the model calls `wave` first and waits for the result before it speaks: a SILENT
    # result would leave the greeting unanswered.
    from google.genai import types

    async with connected() as client:
        bridge = _bridge()
        bridge.background_tools = frozenset({"play_gesture"})
        quiet, talking = RecordingSession(), RecordingSession()
        await bridge._run_tools(quiet, client, client.rt, [Call("play_gesture", {"name": "wave"})], turn=0)
        bridge._play([b"hi"])  # the model says hello while it waves
        await bridge._run_tools(talking, client, client.rt, [Call("play_gesture", {"name": "nod"})], turn=0)
        bridge._end_turn()
        late = RecordingSession()  # result after the turn already ended with words
        await bridge._run_tools(late, client, client.rt, [Call("play_gesture", {"name": "nod"})], turn=0)
    assert quiet.tool_responses[0].scheduling == types.FunctionResponseScheduling.WHEN_IDLE
    assert talking.tool_responses[0].scheduling == types.FunctionResponseScheduling.SILENT
    assert late.tool_responses[0].scheduling == types.FunctionResponseScheduling.SILENT


# ---------------------------------------------------------------- speech filter
def _turn(f: SpeechFilter, *steps: str | bytes) -> list[bytes]:
    played: list[bytes] = []
    for step in steps:
        played += f.audio(step) if isinstance(step, bytes) else f.text(step)
    return played + f.end()


def test_placeholder_turns_never_reach_the_speakers() -> None:
    f = SpeechFilter(["play_gesture", "wave", "shake_head"])
    assert _turn(f, b"a", "<no speech>", b"b", "{pause}", b"c") == []
    assert _turn(f, b"a", "No ", b"b", "speech.") == []
    assert _turn(f, b"a", "Wave", b"b") == []  # narrating the gesture it is about to play
    assert _turn(f, b"a", "play gesture", b"b") == []
    assert _turn(f, b"a", "Shake", b"b", " head.") == []


def test_real_speech_plays_at_once_and_whole() -> None:
    f = SpeechFilter(["wave"])
    assert f.audio(b"a") == []  # held until the transcript arrives
    assert f.text("Hello there!") == [b"a"]
    assert f.audio(b"b") == [b"b"]
    assert f.end() == []
    assert _turn(f, b"a", "Wave", b"b", " to me again!", b"c") == [b"a", b"b", b"c"]
    assert _turn(f, b"a", b"b") == [b"a", b"b"]  # no transcript at all: never swallow the answer
    assert _turn(f, b"a", "<no speech>", b"b", " Oh, hi!", b"c") == [b"c"]


def test_late_transcript_only_delays_speech_briefly() -> None:
    now = [100.0]
    f = SpeechFilter(clock=lambda: now[0])
    assert f.audio(b"a") == []
    now[0] += SpeechFilter.HOLD_S / 2
    assert f.audio(b"b") == []
    now[0] += SpeechFilter.HOLD_S
    assert f.audio(b"c") == [b"a", b"b", b"c"]
    assert f.text("<no speech>") == [] and f.audio(b"d") == [b"d"]  # too late to take back


def test_interrupted_turns_trailing_transcript_does_not_leak_into_the_next_turn() -> None:
    f = SpeechFilter(["wave"])
    f.audio(b"old")
    f.interrupt()
    assert f.text("Sure, let me") == []  # the interrupted turn's tail
    assert _turn(f, b"a", "Wave", b"b") == []  # the next turn is still filtered


async def test_cancelled_sound_tool_does_not_leave_the_mic_muted() -> None:
    class Hanging:
        async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
            await asyncio.sleep(60)

    bridge = _bridge()
    task = asyncio.create_task(bridge._run_tools(RecordingSession(), Hanging(), None, [Call("play_audio", {"url": "x"})]))
    await asyncio.sleep(0.05)
    assert bridge.gate.muted()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    bridge.gate._quiet_at = 0.0  # skip the reverb tail
    assert not bridge.gate.muted()


async def test_receive_drops_placeholder_turns_and_plays_real_ones() -> None:
    def msg(text: str | None = None, audio: bytes | None = None, done: bool = False) -> Any:
        part = type("P", (), {"inline_data": type("D", (), {"data": audio})()})()
        sc = type("SC", (), {
            "interrupted": False, "input_transcription": None, "turn_complete": done,
            "output_transcription": type("T", (), {"text": text})() if text else None,
            "model_turn": type("MT", (), {"parts": [part]})() if audio else None,
        })()
        return type("M", (), {"session_resumption_update": None, "go_away": None, "tool_call": None,
                              "server_content": sc})()

    class Session(RecordingSession):
        turns = [[msg(audio=b"x"), msg(text="<no speech>"), msg(audio=b"y"), msg(done=True)],
                 [msg(audio=b"h"), msg(text="Hi!"), msg(audio=b"i"), msg(done=True)]]

        async def receive(self):  # type: ignore[no-untyped-def]
            for m in self.turns.pop(0) if self.turns else []:
                yield m

    bridge = _bridge()
    task = asyncio.create_task(bridge._receive(Session(), None, None))
    await asyncio.sleep(0.2)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    played = []
    while not bridge.voice_q.empty():
        played.append(bridge.voice_q.get_nowait())
    assert played == [b"h", b"i"]


# ---------------------------------------------------------------- events
def test_events_worth_hearing_about_are_forwarded() -> None:
    assert format_event("touch.start", {"part": "head"}).startswith("[robot event] touch.start")
    blocked = format_event("motion.blocked", {"reason": "Safety", "raw": ["Safety", 1, [0.1]]})
    assert blocked and "raw" not in blocked
    assert format_event("task.finished", {"kind": "turn", "ok": False, "error": "obstacle"})
    assert format_event("task.finished", {"kind": "gesture", "ok": True}) is None
    assert format_event("task.finished", {"kind": "gesture", "ok": False, "error": "x"})
    # A successful move needs no words; a note would only make Gemini fill the turn with "no speech".
    assert format_event("task.finished", {"kind": "turn", "ok": True}) is None
    assert format_event("sound.located", {"azimuth_deg": 4}) is None
    assert format_event("speech.final", {"text": "hi"}) is None  # Gemini hears for itself


def test_body_reflexes_are_not_reported_so_gemini_does_not_narrate_them() -> None:
    for reflex in ("attention", "greet", "touch_reaction", "low_battery"):
        assert format_event("reflex.completed", {"reflex": reflex, "result": {"looked": True}}) is None
    high_five = format_event("reflex.completed", {"reflex": "high_five", "result": {"touched": True}})
    assert high_five and "touched" in high_five


def test_conversation_is_recorded_for_the_dashboard_even_without_printing(runtime) -> None:  # type: ignore[no-untyped-def]
    bridge = _bridge()  # no_transcripts=True: nothing printed, the dashboard still gets it
    bridge.rt = runtime
    bridge._show("heard", "hello Pepper")
    bridge._show("said", "Hi there!")
    bridge._show("tool", "move(...)")
    assert [(t["role"], t["text"]) for t in runtime.transcript] == [("user", "hello Pepper"), ("robot", "Hi there!")]


def test_silence_markers_are_not_shown_as_speech() -> None:
    assert clean_transcript("<no speech>{pause}") == ""
    assert clean_transcript(" (silence) ") == ""
    assert clean_transcript("Hello there!{pause} Nice to see you.") == "Hello there! Nice to see you."


def test_persona_file_is_in_the_repo_root() -> None:
    assert PERSONA_FILE.name == "persona.txt"
    assert (PERSONA_FILE.parent / "pyproject.toml").exists()


def test_persona_forbids_narrating_actions_and_events() -> None:
    persona = load_persona("Pepper", "English")
    assert "Handle, statt zu erzählen" in persona
    assert "Sag nie den Namen eines Tools" in persona


def test_persona_fills_language_and_keeps_literal_braces() -> None:
    system = load_persona("Pepper", "German")
    assert system.endswith("standardmässig auf Deutsch.")
    assert '"{pause}"' in system
    assert "{language}" not in system


def test_persona_is_read_from_the_given_file(tmp_path) -> None:
    path = tmp_path / "persona.txt"
    path.write_text("  You are {name}; speak {language}. Never say {pause}.\n", encoding="utf-8")
    assert load_persona("Robo", "French", path) == "You are Robo; speak French. Never say {pause}."


def test_missing_persona_file_stops_with_a_clear_error(tmp_path) -> None:
    with pytest.raises(SystemExit, match="persona file"):
        load_persona("Pepper", "English", tmp_path / "nope.txt")


def test_knowledge_is_appended_after_the_persona(tmp_path) -> None:
    persona, knowledge = tmp_path / "persona.txt", tmp_path / "knowledge.txt"
    persona.write_text("You are {name}.\n", encoding="utf-8")
    knowledge.write_text("\nThe workshop starts at 10:30.\n", encoding="utf-8")
    assert system_instruction("Pepper", "German", persona, knowledge) == "You are Pepper.\n\nThe workshop starts at 10:30."
    # optional: without the file the persona is the whole instruction
    assert system_instruction("Pepper", "German", persona, tmp_path / "none.txt") == "You are Pepper."
    assert load_knowledge(tmp_path / "none.txt") == ""


def test_shipped_knowledge_covers_the_zukunftstag_slides() -> None:
    system = system_instruction("Pepper", "German")
    assert system.startswith(load_persona("Pepper", "German"))
    for fact in ("CLEAR", "Large Language Model", "814'000", "Applikationsentwickler", "11:25-11:40 Quiz"):
        assert fact in system, fact


async def test_robot_events_reach_the_event_queue() -> None:
    async with connected() as client:
        bridge = _bridge()
        bridge.loop = asyncio.get_running_loop()
        unsubscribe = client.rt.bus.subscribe("*", bridge._on_event)
        try:
            client.rt.bus.publish("touch.start", {"part": "head"})
            note = await asyncio.wait_for(bridge.events_q.get(), 2)
        finally:
            unsubscribe()
    assert "touch.start" in note and "head" in note


# ---------------------------------------------------------------- echo gate
def test_echo_gate_tracks_pepper_speech_but_not_reflex_movement() -> None:
    gate = EchoGate(tail_s=0.05)
    gate.on_event("reflex.triggered", {"reflex": "greet"})
    assert not gate.muted()  # a reflex only moves the body; its speech arrives as tts.*
    gate.on_event("tts.started", {"utterance_id": "u1"})
    gate.on_event("tts.started", {"utterance_id": "u2"})
    assert gate.muted()
    gate.on_event("tts.finished", {"utterance_id": "u1"})
    assert gate.muted()  # u2 still speaking
    gate.on_event("tts.finished", {"utterance_id": "u2"})
    assert gate.muted()  # reverb tail
    time.sleep(0.08)
    assert not gate.muted()


def test_echo_gate_ignores_a_task_that_finished_before_its_tool_call_returned() -> None:
    gate = EchoGate(tail_s=0.0)
    gate.on_event("task.finished", {"task_id": "gesture-9"})
    gate.start("task:gesture-9")
    assert not gate.muted()
    gate.finish("voice")
    gate.start("voice")  # Gemini's voice repeats under the same key and must still mute
    assert gate.muted()


def test_echo_gate_never_stays_muted_on_a_missed_finish() -> None:
    gate = EchoGate(tail_s=0.0)
    gate.start("task:lost", max_s=0.05)
    assert gate.muted()
    time.sleep(0.08)
    assert not gate.muted()


def test_muted_mic_sends_silence_so_turn_detection_keeps_its_timing() -> None:
    bridge = _bridge()
    loop = asyncio.new_event_loop()
    try:
        bridge.loop = loop
        speech = np.full(320, 1234, np.int16)
        bridge.gate.start("voice")
        bridge._on_mic(speech, 16000, 0.0)
        bridge.gate.finish("voice")
        bridge.gate._quiet_at = 0.0
        bridge._on_mic(speech, 16000, 0.0)
        bridge._on_mic(speech, 48000, 0.0)  # wrong rate never reaches Gemini
        loop.run_until_complete(asyncio.sleep(0))
        muted, live = bridge.mic_q.get_nowait(), bridge.mic_q.get_nowait()
        assert bridge.mic_q.empty()
        assert muted == bytes(640)  # same duration, no sound
        assert live == speech.tobytes()
    finally:
        loop.close()


def test_dashboard_mute_sends_silence_until_unmuted(runtime) -> None:  # type: ignore[no-untyped-def]
    bridge = _bridge()
    bridge.rt = runtime
    loop = asyncio.new_event_loop()
    try:
        bridge.loop = loop
        speech = np.full(320, 1234, np.int16)
        runtime.mic_muted = True
        bridge._on_mic(speech, 16000, 0.0)
        runtime.mic_muted = False
        bridge._on_mic(speech, 16000, 0.0)
        loop.run_until_complete(asyncio.sleep(0))
        assert bridge.mic_q.get_nowait() == bytes(640)
        assert bridge.mic_q.get_nowait() == speech.tobytes()
    finally:
        loop.close()


def test_odd_event_payloads_do_not_raise() -> None:
    assert format_event("person.left", {"present_for_s": None}) is not None
    bridge = _bridge()
    bridge._on_event(type("E", (), {"type": "person.left", "data": {"present_for_s": "x"}})())  # logged, not raised


class FailingThenGoAwayClient:
    """Gemini client stand-in: first connect fails, second session sends go_away at once."""

    def __init__(self, stop: asyncio.Event) -> None:
        self.connects = 0
        self.stop = stop
        outer = self

        class _Session(RecordingSession):
            async def receive(self):  # type: ignore[no-untyped-def]
                yield type("M", (), {"session_resumption_update": None, "tool_call": None, "server_content": None,
                                     "go_away": type("G", (), {"time_left": "0s"})()})()

        class _Live:
            def connect(self, model: str, config: Any):  # type: ignore[no-untyped-def]
                outer.connects += 1
                if outer.connects == 1:
                    raise ConnectionError("network down")
                if outer.connects >= 3:
                    outer.stop.set()
                return _Ctx()

        class _Ctx:
            async def __aenter__(self):  # type: ignore[no-untyped-def]
                return _Session()

            async def __aexit__(self, *exc: Any) -> None:
                return None

        self.aio = type("A", (), {"live": _Live()})()


async def test_session_failures_and_go_away_reconnect_instead_of_exiting() -> None:
    async with connected() as client:
        bridge = _bridge()
        bridge.loop = asyncio.get_running_loop()
        bridge.handle = "stale"
        stop = asyncio.Event()
        fake = FailingThenGoAwayClient(stop)
        speaker = client.rt.audio_out.speaker_stream(24000)

        keep_alive = asyncio.create_task(
            bridge._keep_alive(lambda: bridge._session(fake, client, client.rt, speaker, [], "sys")))
        await asyncio.wait_for(stop.wait(), 5)  # connect 1 fails, 2 gets go_away, 3 reached
        keep_alive.cancel()
        await asyncio.gather(keep_alive, return_exceptions=True)
    assert fake.connects >= 3
    assert bridge.handle is None  # the immediate failure dropped the stale resumption handle
    assert not bridge.gate.muted()


# ---------------------------------------------------------------- API key
def test_api_key_from_dotenv_wins_over_environment(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    env = tmp_path / ".env"
    monkeypatch.setenv(KEY_VAR, "from-shell")
    assert load_api_key(str(env)) == "from-shell"
    env.write_text(f'OTHER=1\nexport {KEY_VAR}="from-file"\n')
    assert load_api_key(str(env)) == "from-file"
    env.write_text(f"{KEY_VAR}=\n")
    assert load_api_key(str(env)) == "from-shell"
    monkeypatch.delenv(KEY_VAR)
    assert load_api_key(str(env)) is None


# ---------------------------------------------------------------- CLI
def test_mic_always_streams_at_geminis_input_rate(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # 48 kHz buffers would be dropped by the bridge: Pepper would be silently deaf to Gemini.
    monkeypatch.setenv("PEPPER_AUDIO_RATE", "48000")
    settings, _ = _parse(["--fake"])
    assert settings.audio_rate == 16000


# ---------------------------------------------------------------- speaker stream
def _device() -> Any:
    return FakeRobot().services["ALAudioDevice"]


def test_speaker_stream_upsamples_to_stereo_output_rate() -> None:
    dev = _device()
    stream = SpeakerStream(dev, 24000)
    pcm = (np.sin(np.arange(2400) / 7) * 8000).astype(np.int16)
    stream.write(pcm.tobytes())
    out = np.frombuffer(bytes(dev.output), np.int16)
    assert len(out) == 2400 * 2 * 2  # x2 rate, x2 channels
    assert np.array_equal(out[0::2], out[1::2])  # both channels carry the voice
    assert np.array_equal(out[0::2][1::2], pcm)  # original samples land on every other frame
    frames = [args[0] for name, args in dev.calls if name == "sendRemoteBufferToOutput"]
    assert frames and max(frames) <= SpeakerStream.MAX_FRAMES


def test_speaker_stream_chunks_join_without_clicks() -> None:
    pcm = (np.sin(np.arange(3000) / 9) * 9000).astype(np.int16)
    whole, chunked = SpeakerStream(_device(), 24000), SpeakerStream(_device(), 24000)
    a = whole.convert(pcm.tobytes())
    b = np.concatenate([chunked.convert(pcm[i:i + 333].tobytes()) for i in range(0, len(pcm), 333)])
    assert np.array_equal(a, b)


def test_speaker_stream_handles_empty_and_odd_length_chunks() -> None:
    dev = _device()
    stream = SpeakerStream(dev, 24000)
    stream.write(b"")
    stream.write(b"\x01\x02\x03")  # 1.5 samples: the stray byte is dropped, no crash
    assert len(dev.output) == 1 * 2 * 2 * 2  # one sample -> 2 frames x 2 channels x 2 bytes


def test_speaker_stream_cancel_stops_a_long_chunk() -> None:
    import threading

    dev = _device()
    stream = SpeakerStream(dev, 24000)
    writer = threading.Thread(target=stream.write, args=(np.zeros(24000 * 3, np.int16).tobytes(),))
    writer.start()
    time.sleep(0.3)
    stream.cancel()
    writer.join(2)
    assert not writer.is_alive()
    sent_s = len(dev.output) / 4 / stream.rate
    assert sent_s < 1.0  # far less than the 3 s chunk


def test_speaker_stream_paces_itself_close_to_real_time() -> None:
    stream = SpeakerStream(_device(), 24000)
    one_second = np.zeros(24000, np.int16).tobytes()
    started = time.monotonic()
    stream.write(one_second)
    elapsed = time.monotonic() - started
    # Never more than LEAD_S ahead of the robot: writing 1 s of audio takes ~1 s - LEAD_S.
    assert 1.0 - SpeakerStream.LEAD_S - 0.15 < elapsed < 1.0
    assert 0 < stream.remaining() <= SpeakerStream.LEAD_S + 0.11


def test_speaker_stream_drops_audio_from_before_a_cancel() -> None:
    dev = _device()
    stream = SpeakerStream(dev, 24000)
    generation = stream.generation
    stream.cancel()  # barge-in while the voice thread was still gathering this audio
    stream.write(bytes(4800), generation)
    assert not dev.output


def _robot_gaps(dev: Any, rate: int, slow_call: int = -1, slow_s: float = 0.0) -> list[float]:
    """Wrap the fake device's output and return the silences the robot would play between blocks.
    A block reaches the robot when its call returns; call number ``slow_call`` takes ``slow_s``."""
    gaps: list[float] = []
    play_end = [0.0]
    calls = [0]
    send = dev.sendRemoteBufferToOutput

    def timed(frames: int, buffer: bytes) -> bool:
        if calls[0] == slow_call:
            time.sleep(slow_s)
        calls[0] += 1
        arrived = time.monotonic()
        if play_end[0] and arrived > play_end[0]:
            gaps.append(arrived - play_end[0])
        play_end[0] = max(play_end[0], arrived) + frames / rate
        return send(frames, buffer)

    dev.sendRemoteBufferToOutput = timed
    return gaps


def test_gemini_voice_survives_a_slow_robot_call_mid_reply(runtime) -> None:  # type: ignore[no-untyped-def]
    # A Wi-Fi hiccup on one speaker call must not be heard: the robot needs enough audio queued
    # to cover it. With a 150 ms lead the voice broke off mid-sentence and then resumed.
    bridge = _bridge()
    bridge.rt = runtime
    speaker = runtime.audio_out.speaker_stream(24_000)
    gaps = _robot_gaps(speaker.device, speaker.rate, slow_call=8, slow_s=0.3)
    import threading
    threading.Thread(target=bridge._play_forever, args=(speaker,), daemon=True).start()
    for _ in range(15):  # 1.5 s reply, delivered faster than real time
        bridge.voice_q.put(bytes(4800))
    time.sleep(2.0)
    assert len(speaker.device.output) == 15 * 4800 * 2 * 2
    assert gaps == []


def test_gemini_voice_prebuffers_a_trickling_reply_start(runtime) -> None:  # type: ignore[no-untyped-def]
    # The first chunks of a reply trickle in: starting on the first one alone ran the robot dry.
    bridge = _bridge()
    bridge.rt = runtime
    speaker = runtime.audio_out.speaker_stream(24_000)
    gaps = _robot_gaps(speaker.device, speaker.rate)
    import threading
    threading.Thread(target=bridge._play_forever, args=(speaker,), daemon=True).start()
    chunk = bytes(2400)  # 50 ms, delivered at real-time pace with jitter
    for delay in (0.0, 0.06, 0.06, 0.07, 0.05, 0.06, 0.04, 0.05, 0.05, 0.05):
        time.sleep(delay)
        bridge.voice_q.put(chunk)
    time.sleep(0.8)
    assert gaps == []
    assert len(speaker.device.output) == 10 * 2400 * 2 * 2  # nothing lost


def test_settings_of_removed_features_are_reported(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from pepper_mcp.config import removed_env_vars

    monkeypatch.delenv("PEPPER_STT", raising=False)
    monkeypatch.delenv("PEPPER_MCP_TRANSPORT", raising=False)
    assert "PEPPER_STT" not in removed_env_vars()
    monkeypatch.setenv("PEPPER_STT", "faster-whisper")
    monkeypatch.setenv("PEPPER_MCP_TRANSPORT", "stdio")
    assert {"PEPPER_STT", "PEPPER_MCP_TRANSPORT"} <= set(removed_env_vars())


# ---------------------------------------------------------------- speech events for reflexes
class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _tracker(lag_s: float = 0.8, quiet_s: float = 1.5) -> tuple[Any, list[tuple[str, dict[str, Any]]], _Clock]:
    from pepper_mcp.gemini_live import UtteranceTracker

    events: list[tuple[str, dict[str, Any]]] = []
    clock = _Clock()
    tracker = UtteranceTracker(lambda t, d: events.append((t, d)), lag_s=lag_s, quiet_s=quiet_s, clock=clock)
    return tracker, events, clock


def test_gemini_transcription_becomes_the_listeners_speech_events() -> None:
    # The attention reflex turns toward whoever talks on speech.started / speech.final.
    tracker, events, clock = _tracker()
    tracker.heard(" Hey,")
    clock.now += 0.4
    tracker.heard(" Pepper.")
    clock.now += 0.3
    tracker.end()  # Gemini starts answering: the person is done
    assert [t for t, _ in events] == ["speech.started", "speech.final"]
    started, final = events[0][1], events[1][1]
    assert started["at"] == pytest.approx(1000.0 - 0.8)  # transcripts lag the voice
    assert final["started_at"] == started["at"]  # the reflex pairs them by started_at
    assert final["ended_at"] == pytest.approx(1000.4)
    assert final["text"] == "Hey, Pepper."
    tracker.end()
    assert len(events) == 2  # nothing open: no duplicate final


def test_utterance_closes_after_a_quiet_gap_when_gemini_does_not_answer() -> None:
    tracker, events, clock = _tracker(quiet_s=1.5)
    tracker.heard("hm, what was that")
    clock.now += 1.0
    tracker.poll()
    assert [t for t, _ in events] == ["speech.started"]
    clock.now += 0.6
    tracker.poll()
    assert [t for t, _ in events] == ["speech.started", "speech.final"]


def test_silence_marker_transcript_is_aborted_not_final() -> None:
    tracker, events, _clock = _tracker()
    tracker.heard("<noise>")
    tracker.end()
    assert [t for t, _ in events] == ["speech.started", "speech.aborted"]
    assert events[1][1]["started_at"] == events[0][1]["at"]


def test_gemini_voice_publishes_tts_events_once_per_reply(runtime) -> None:  # type: ignore[no-untyped-def]
    # Network gaps between chunks of one reply must not flicker the "speaking" eyes.
    bridge = _bridge()
    bridge.rt = runtime
    seen: list[str] = []
    runtime.bus.subscribe("tts.*", lambda e: seen.append(e.type))
    speaker = runtime.audio_out.speaker_stream(24_000)
    import threading
    threading.Thread(target=bridge._play_forever, args=(speaker,), daemon=True).start()
    chunk = bytes(4800)  # 0.1 s at 24 kHz
    bridge.voice_q.put(chunk)
    time.sleep(0.05)
    bridge.voice_q.put(chunk)  # arrives while the first is still audible
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and "tts.finished" not in seen:
        time.sleep(0.02)
    time.sleep(0.2)
    assert seen == ["tts.started", "tts.finished"]


# ---------------------------------------------------------------- hearing accuracy
def test_transcription_hints_default_language_first_with_english_fallback() -> None:
    assert transcription_languages("German") == ["de-DE", "en-US"]
    assert transcription_languages("german") == ["de-DE", "en-US"]
    assert transcription_languages("English") == ["en-US"]
    assert transcription_languages("Klingon") == []  # unknown: keep auto-detection


def test_vocabulary_skips_comments_blanks_and_duplicates(tmp_path) -> None:
    path = tmp_path / "vocabulary.txt"
    path.write_text("# comment\n\n  Pepper \nTic Tac Toe\nPepper\n", encoding="utf-8")
    assert load_vocabulary(path) == ["Pepper", "Tic Tac Toe"]
    assert load_vocabulary(tmp_path / "missing.txt") == []


def test_repo_vocabulary_file_loads() -> None:
    assert VOCABULARY_FILE.parent == PERSONA_FILE.parent
    assert "Pepper" in load_vocabulary()


def test_live_config_hints_language_and_vocabulary_and_keeps_word_onsets() -> None:
    args = argparse.Namespace(model="m", voice="Kore", persona="Pepper", no_video=True, no_transcripts=False)
    bridge = GeminiPepper(make_settings(language="German"), args, api_key="k")
    config = bridge._config([], "sys")
    tr = config.input_audio_transcription
    assert tr.language_codes == ["de-DE", "en-US"]
    assert "Pepper" in tr.custom_vocabulary
    # No output language is pinned: Pepper answers in whatever language it is spoken to.
    assert config.speech_config.language_code is None
    vad = config.realtime_input_config.automatic_activity_detection
    assert vad.prefix_padding_ms >= 300 and vad.silence_duration_ms >= 800


def test_german_is_the_default_language(monkeypatch) -> None:
    from pepper_mcp.config import Settings

    monkeypatch.delenv("PEPPER_LANGUAGE", raising=False)
    assert Settings().language == "German"


async def test_a_dance_is_never_interrupted_by_listening_talking_or_tools(runtime) -> None:  # type: ignore[no-untyped-def]
    bridge = _bridge()
    bridge.rt = runtime
    bridge.loop = asyncio.get_running_loop()
    runtime.gestures.dancing = lambda: "dance_relax"  # type: ignore[method-assign]

    speech = np.full(320, 1234, np.int16)
    bridge._on_mic(speech, 16000, 0.0)
    await asyncio.sleep(0)
    assert bridge.mic_q.get_nowait() == bytes(640)  # Gemini hears nothing to answer

    class Never:
        async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
            raise AssertionError("no tool may run during a dance")

    session = RecordingSession()
    await bridge._run_tools(session, Never(), runtime, [Call("say", {"text": "hi"})])
    assert "dancing" in session.tool_responses[0].response["error"]

    class Speaker:
        generation, cancelled, written = 0, 0, []

        def remaining(self) -> float:
            return 0.0

        def cancel(self) -> None:
            self.cancelled += 1

        def write(self, chunk: bytes, generation: int) -> None:
            self.written.append(chunk)

    speaker = Speaker()
    threading.Thread(target=bridge._play_forever, args=(speaker,), daemon=True).start()
    bridge._play([b"\x01\x00" * 160])
    await asyncio.sleep(0.2)
    assert speaker.written == [] and speaker.cancelled == 1  # Gemini's voice is dropped


async def test_stop_tools_still_work_during_a_dance(runtime) -> None:  # type: ignore[no-untyped-def]
    from types import SimpleNamespace

    bridge = _bridge()
    bridge.rt = runtime
    runtime.gestures.dancing = lambda: "dance_relax"  # type: ignore[method-assign]
    called: list[str] = []

    class Recording:
        async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
            called.append(name)
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="{}")], is_error=False,
                                   structured_content=None)

    await bridge._run_tools(RecordingSession(), Recording(), runtime, [Call("stop_motion", {})])
    assert called == ["stop_motion"]


def test_dashboard_line_is_the_last_startup_line() -> None:
    from types import SimpleNamespace

    from pepper_mcp.gemini_live import dashboard_line

    lan = SimpleNamespace(port=8765, host="0.0.0.0", base_url="http://192.168.178.87:8765")
    assert dashboard_line(SimpleNamespace(preview=lan)) == (
        "Dashboard: http://localhost:8765/  (other devices: http://192.168.178.87:8765/)")
    local = SimpleNamespace(port=8765, host="127.0.0.1", base_url="http://192.168.178.87:8765")
    assert dashboard_line(SimpleNamespace(preview=local)) == "Dashboard: http://localhost:8765/"
    assert "--preview-port" in dashboard_line(SimpleNamespace(preview=None))
