"""``pepper-mcp-gemini``: Pepper as a real-time Gemini Live agent.

Like ``pepper-mcp-agent`` it starts the MCP server in-process and drives the robot only through
its tools (same safety limits, same task/event model). The difference is the loop: Gemini Live is
a streaming speech-to-speech model, so instead of turns over text it gets

    Pepper's front mic (16 kHz PCM)  ──►┐
    Pepper's head camera (JPEG, 1 fps) ─►  Gemini Live session  ──► Pepper's loudspeakers (24 kHz PCM)
    robot events (touch, people, ...) ─►┘          │
                                                   └── function calls ──► MCP tools (move, gestures, ...)

Everything is the robot's own hardware; nothing on the laptop is used. Pepper's mic sits next
to its speakers and there is no echo cancellation, so the mic is muted while Pepper makes sound
(Gemini's voice, NAOqi speech from reflexes, gestures with sound effects).

Requires ``pip install 'pepper-mcp[gemini]'`` and a Gemini API key in ``GEMINI_LIVE_KEY``
(environment or a ``.env`` file in the working directory).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import queue
import sys
import threading
import time
from typing import Any

from pepper_mcp.cli import build_parser, configure_logging
from pepper_mcp.config import Settings

log = logging.getLogger("pepper_mcp.gemini_live")

MODEL = "gemini-3.8-live"
KEY_VAR = "GEMINI_LIVE_KEY"
MIC_RATE = 16_000  # Live API input: 16-bit PCM, 16 kHz, mono
VOICE_RATE = 24_000  # Live API output: 16-bit PCM, 24 kHz, mono
VIDEO_PERIOD_S = 1.0  # Live API takes at most 1 frame per second
ECHO_TAIL_S = 0.4  # room reverb after Pepper stops making sound
TOOL_TIMEOUT_S = 20.0  # a tool call pauses the model; never let one hang the conversation

# Tools that would fight the Live session: Gemini is the voice and the ears, and every function
# call blocks the model until it returns, so nothing that waits for the world may be exposed.
EXCLUDED_TOOLS = frozenset({
    "say", "stop_speaking", "speech_status", "set_voice", "list_languages",
    "listen", "set_listening", "get_transcript", "recognize_keywords", "record_clip",
    "start_builtin_dialog", "stop_builtin_dialog",
    "wait_for_event", "wait_task", "camera_stream",
    # reads any file on the laptop: nothing a voice in the room should be able to trigger
    "upload_audio",
})
BLOCKING_ARGS = frozenset({"wait"})
# Tools whose task makes sound through the speakers: the mic stays muted until task.finished.
SOUND_TOOLS = frozenset({"play_gesture", "play_animation", "play_audio", "play_sine", "run_behavior"})
# Robot events Gemini is told about, as short text notes in the session.
FORWARDED_EVENTS = frozenset({
    "person.appeared", "person.left", "person.waving", "touch.start", "reflex.completed",
    "task.finished", "motion.blocked", "robot.fell", "battery.low", "nav.status",
})

PERSONA = """You are {name}, a friendly, curious humanoid robot made by SoftBank, standing in the room
with the people you talk to. You hear through your own microphones, you see through the camera in
your head (the video you receive is your own view) and you speak through your own loudspeakers.

You have a real body, controlled through your tools. Use it the way a person would and without
being asked: gesture while you talk (play_gesture: wave, nod, shake_head, celebrate,
thinking_pose, high_five, ...; prefer these over play_animation, whose names must come from
list_animations), look at people (look_at_person, look_toward, look), turn to face
them (turn), walk over (move_to in metres, move for short drives), show feelings with your lights
(set_mood_lights, set_leds) and use your tablet (tablet_show_text) for key words.
Never describe a tool call ("I'll set my mood to happy") - just do it and talk naturally.

Moving is real and can be refused. Moves, turns and gestures run in the background: say what you
are doing while it happens ("turning around"), not that it is done. The outcome arrives as a
[robot event] task.finished note; ok=false or a motion.blocked note means you did NOT move - say so
honestly (something is in your way) instead of claiming you moved. Keep people safe: modest speeds, short
moves, stop_motion the moment anyone says stop or seems uneasy, never drive while being touched.

Lines starting with "[robot event]" are your senses reporting, not someone speaking. React when
it fits (greet a person who appears, respond to a touch, celebrate a successful high five) and
stay quiet otherwise. Ignore noise and stray words that are not someone talking to you.

Speak naturally in one to three short sentences. Reply in the language the person speaks, by
default {language}."""


# ---------------------------------------------------------------- helpers (pure, unit-tested)
def load_api_key(env_file: str = ".env") -> str | None:
    """``GEMINI_LIVE_KEY`` from ``env_file`` (wins) or the environment."""
    if os.path.exists(env_file):
        with open(env_file, encoding="utf-8") as fh:
            for line in fh:
                name, sep, value = line.strip().partition("=")
                if sep and name.strip().removeprefix("export ").strip() == KEY_VAR:
                    value = value.strip().strip("'\"")
                    if value:
                        return value
    return os.environ.get(KEY_VAR) or None


def tool_declarations(tools: list[Any]) -> list[dict[str, Any]]:
    """MCP tools → Gemini function declarations (JSON schema passed through unchanged)."""
    out = []
    for t in tools:
        if t.name in EXCLUDED_TOOLS:
            continue
        decl: dict[str, Any] = {"name": t.name, "description": (t.description or "").strip()[:1000]}
        schema = dict(t.input_schema or {})
        # A function call pauses the model until it returns: `wait=true` on a 5 s turn would
        # freeze the conversation. Tasks run in the background and report via task.finished.
        props = {k: v for k, v in (schema.get("properties") or {}).items() if k not in BLOCKING_ARGS}
        if props:
            schema["properties"] = props
            if "required" in schema:
                schema["required"] = [r for r in schema["required"] if r in props]
            decl["parameters_json_schema"] = schema
        out.append(decl)
    return out


def tool_response(result: Any) -> dict[str, Any]:
    """MCP CallToolResult → JSON object for a Gemini FunctionResponse."""
    texts = [c.text for c in result.content if getattr(c, "type", "") == "text"]
    images = sum(1 for c in result.content if getattr(c, "type", "") == "image")
    text = "\n".join(texts).strip()
    if getattr(result, "is_error", False):
        return {"error": text or "tool failed"}
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        payload: dict[str, Any] = dict(structured)
    else:
        try:
            parsed = json.loads(text) if text else {}
            payload = parsed if isinstance(parsed, dict) else {"result": parsed}
        except json.JSONDecodeError:
            payload = {"result": text[:4000]}
    if images:
        payload["image"] = "sent to your video input"
    return payload or {"ok": True}


def format_event(event_type: str, data: dict[str, Any]) -> str | None:
    """One robot event → a short note for Gemini, or None when it is not worth mentioning."""
    if event_type not in FORWARDED_EVENTS:
        return None
    if event_type == "task.finished" and data.get("kind") in ("gesture", "animation", "posture", "audio"):
        if data.get("ok") is not False:  # finished body language is noise; failures are not
            return None
    if event_type == "person.left" and (data.get("present_for_s") or 99) < 3:
        return None
    keep = {k: v for k, v in data.items() if k not in ("raw", "ts", "position_m") and v is not None}
    return f"[robot event] {event_type} {json.dumps(keep, default=str)[:400]}"


class EchoGate:
    """Decides when Pepper's mic must be muted because Pepper itself is making sound.

    Sources are keyed (``voice``, ``tts:<id>``, ``task:<id>``, ``reflex:<name>``) and each one
    expires on its own so a missed "finished" event can never leave the robot deaf. A task can
    finish before its tool call returns, so finished task ids are remembered and a late start for
    one is ignored.
    """

    MAX_S = 30.0
    REMEMBER_S = 60.0

    def __init__(self, tail_s: float = ECHO_TAIL_S):
        self.tail_s = tail_s
        self._lock = threading.Lock()
        self._active: dict[str, float] = {}
        self._finished: dict[str, float] = {}
        self._quiet_at = 0.0

    def start(self, key: str, max_s: float | None = None) -> None:
        now = time.monotonic()
        with self._lock:
            if now - self._finished.get(key, -self.REMEMBER_S) < self.REMEMBER_S:
                return
            self._active[key] = now + (max_s or self.MAX_S)

    def finish(self, key: str, extra_s: float = 0.0) -> None:
        now = time.monotonic()
        with self._lock:
            # Task ids are unique and race with their tool call; voice/reflex keys are reused.
            if key.startswith("task:"):
                self._finished = {k: t for k, t in self._finished.items() if now - t < self.REMEMBER_S}
                self._finished[key] = now
            if self._active.pop(key, None) is not None:
                self._quiet_at = max(self._quiet_at, now + extra_s + self.tail_s)

    def muted(self) -> bool:
        now = time.monotonic()
        with self._lock:
            self._active = {k: t for k, t in self._active.items() if t > now}
            return bool(self._active) or now < self._quiet_at

    def on_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Feed robot events: NAOqi speech and reflexes make sound without a tool call."""
        if event_type == "tts.started":
            self.start(f"tts:{data.get('utterance_id')}")
        elif event_type == "tts.finished":
            self.finish(f"tts:{data.get('utterance_id')}")
        elif event_type == "task.finished":
            self.finish(f"task:{data.get('task_id')}")
        elif event_type == "reflex.triggered":
            self.start(f"reflex:{data.get('reflex')}", max_s=15.0)
        elif event_type == "reflex.completed":
            self.finish(f"reflex:{data.get('reflex')}")


# ---------------------------------------------------------------- CLI
def _parse(argv: list[str] | None) -> tuple[Settings, argparse.Namespace]:
    parser = build_parser()
    parser.prog = "pepper-mcp-gemini"
    parser.add_argument("--model", default=MODEL, help=f"Gemini Live model (default {MODEL})")
    parser.add_argument("--voice", default="Kore", help="prebuilt Gemini voice (Kore, Puck, Charon, ...)")
    parser.add_argument("--persona", default="Pepper")
    parser.add_argument("--no-video", action="store_true", help="don't stream the head camera to Gemini")
    parser.add_argument("--no-transcripts", action="store_true", help="don't print what was said")
    args = parser.parse_args(argv)
    settings = Settings()
    settings.apply_overrides({k: v for k, v in vars(args).items()})
    # Gemini is the ears: the server's own STT would only burn GPU and publish duplicate speech.
    if args.stt_backend is None:
        settings.stt_backend = "none"
    settings.transport = "stdio"
    settings.validate()
    return settings, args


# ---------------------------------------------------------------- the live loop
class GeminiPepper:
    def __init__(self, settings: Settings, args: argparse.Namespace, api_key: str):
        self.settings = settings
        self.args = args
        self.api_key = api_key
        self.gate = EchoGate()
        self.handle: str | None = None  # session resumption
        self.loop: asyncio.AbstractEventLoop | None = None
        self.mic_q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=200)
        self.events_q: asyncio.Queue[str] = asyncio.Queue(maxsize=50)
        # Plain queue + daemon thread: a speaker write stuck in a NAOqi call on a closed
        # connection must not keep the process alive at exit (asyncio.to_thread workers would).
        self.voice_q: queue.Queue[bytes] = queue.Queue()
        self._heard = ""
        self._said = ""
        self.speaker: Any = None

    # ------------------------------------------------------------ robot callbacks (NAOqi threads)
    def _on_mic(self, samples: Any, rate: int, _ts: float) -> None:
        if rate != MIC_RATE or self.loop is None:
            return
        # While muted send silence rather than nothing: Gemini's turn detection runs on the
        # stream's timing, and a gap would glue speech before and after the mute together.
        data = bytes(len(samples) * 2) if self.gate.muted() else samples.astype("<i2").tobytes()
        try:
            self.loop.call_soon_threadsafe(self._put_nowait, self.mic_q, data)
        except RuntimeError:  # loop closed during shutdown
            pass

    def _on_event(self, event: Any) -> None:
        try:
            self.gate.on_event(event.type, event.data or {})
            note = format_event(event.type, event.data or {})
            if note and self.loop is not None:
                self.loop.call_soon_threadsafe(self._put_nowait, self.events_q, note)
        except Exception:  # runs on the bus worker: never let one odd event break it
            log.exception("gemini event forwarding failed for %s", getattr(event, "type", "?"))

    @staticmethod
    def _put_nowait(q: asyncio.Queue, item: Any) -> None:
        if q.full():  # drop the oldest: fresh audio/events matter more than complete history
            q.get_nowait()
        q.put_nowait(item)

    # ------------------------------------------------------------ main
    async def run(self) -> None:
        from google import genai
        from mcp.client import Client

        from pepper_mcp.server import create_server

        self.loop = asyncio.get_running_loop()
        server = create_server(self.settings)
        rt = server.runtime  # type: ignore[attr-defined]
        client = genai.Client(api_key=self.api_key, http_options={"api_version": "v1alpha"})

        async with Client(server) as mcp:
            tools = (await mcp.list_tools()).tools
            declarations = tool_declarations(tools)
            if rt.mic is None:
                raise SystemExit("Pepper's microphone is not available; Gemini Live needs it")
            if rt.audio_out is None:
                raise SystemExit("Pepper's speakers are not available")
            speaker = await asyncio.to_thread(rt.audio_out.speaker_stream, VOICE_RATE)
            remove_mic = rt.mic.add_consumer(self._on_mic)
            unsubscribe = rt.bus.subscribe("*", self._on_event)
            system = PERSONA.format(name=self.args.persona, language=self.settings.language)
            log.info("gemini live: %s, %d tools, voice %s", self.args.model, len(declarations), self.args.voice)
            print(f"Pepper is live on {self.args.model} ({len(declarations)} tools). Ctrl+C to stop.",
                  file=sys.stderr)
            self.speaker = speaker
            threading.Thread(target=self._play_forever, args=(speaker,), name="gemini-voice", daemon=True).start()
            try:
                await self._keep_alive(lambda: self._session(client, mcp, rt, speaker, declarations, system))
            finally:
                unsubscribe()
                remove_mic()
                self._drop_voice()  # stop feeding the speaker before the server closes the robot

    async def _keep_alive(self, session: Any, max_backoff_s: float = 10.0) -> None:
        """Run sessions forever: reconnect (resuming the conversation) on go_away and on any failure."""
        backoff = 0.5
        while True:
            started = time.monotonic()
            try:
                await session()
                backoff = 0.5
                continue
            except Exception as exc:
                if time.monotonic() - started > 10:
                    backoff = 0.5
                log.warning("gemini session failed (%s: %s); reconnecting in %.1fs", type(exc).__name__, exc, backoff)
                if time.monotonic() - started < 2:
                    self.handle = None  # a stale resumption handle can make connect fail
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff_s)

    def _config(self, declarations: list[dict[str, Any]], system: str) -> Any:
        from google.genai import types

        transcribe = None if self.args.no_transcripts else {}
        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction=system,
            speech_config={"voice_config": {"prebuilt_voice_config": {"voice_name": self.args.voice}}},
            tools=[{"function_declarations": declarations}],
            input_audio_transcription=transcribe,
            output_audio_transcription=transcribe,
            # Pepper hears its whole surroundings: let the model ignore what is not meant for it.
            proactivity={"proactive_audio": True},
            realtime_input_config={"automatic_activity_detection": {
                "silence_duration_ms": 600, "prefix_padding_ms": 100}},
            # Audio+video sessions end after ~2 min without compression.
            context_window_compression=types.ContextWindowCompressionConfig(sliding_window=types.SlidingWindow()),
            session_resumption=types.SessionResumptionConfig(handle=self.handle),
            media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,
        )

    async def _session(self, client: Any, mcp: Any, rt: Any, speaker: Any,
                       declarations: list[dict[str, Any]], system: str) -> None:
        for q in (self.mic_q, self.events_q):  # backlog from the old session is stale
            while not q.empty():
                q.get_nowait()
        self._drop_voice()
        self._heard = self._said = ""
        async with client.aio.live.connect(model=self.args.model, config=self._config(declarations, system)) as s:
            tasks = [asyncio.create_task(c) for c in (
                self._send_mic(s), self._send_events(s), self._receive(s, mcp, rt))]
            if not self.args.no_video and rt.cameras is not None:
                tasks.append(asyncio.create_task(self._send_video(s, rt)))
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    if t.exception():
                        raise t.exception()  # type: ignore[misc]
            finally:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                speaker.cancel()
                self.gate.finish("voice")

    async def _send_mic(self, s: Any) -> None:
        from google.genai import types

        mime = f"audio/pcm;rate={MIC_RATE}"
        while True:
            data = await self.mic_q.get()
            await s.send_realtime_input(audio=types.Blob(data=data, mime_type=mime))

    async def _send_video(self, s: Any, rt: Any, camera: str = "top") -> None:
        from google.genai import types

        while True:
            try:
                jpeg, _frame = await asyncio.to_thread(rt.cameras.snapshot, camera, 640)
                await s.send_realtime_input(video=types.Blob(data=jpeg, mime_type="image/jpeg"))
            except Exception as exc:
                log.debug("camera frame skipped: %s", exc)
            await asyncio.sleep(VIDEO_PERIOD_S)

    async def _send_events(self, s: Any) -> None:
        while True:
            note = await self.events_q.get()
            # Don't talk over Pepper: a text note mid-sentence would cut the answer off.
            while self.gate.muted():
                await asyncio.sleep(0.1)
            notes = [note]
            while not self.events_q.empty():
                notes.append(self.events_q.get_nowait())
            await s.send_realtime_input(text="\n".join(notes))

    def _play_forever(self, speaker: Any) -> None:
        """Daemon thread: Gemini's voice → Pepper's loudspeakers, muting the mic meanwhile."""
        while True:
            chunk = self.voice_q.get()
            self.gate.start("voice")
            try:
                speaker.write(chunk)
            except Exception as exc:  # one bad chunk must not end the conversation
                log.warning("voice chunk not played: %s", exc)
            if self.voice_q.empty():
                self.gate.finish("voice", extra_s=speaker.remaining())

    def _drop_voice(self) -> None:
        while True:
            try:
                self.voice_q.get_nowait()
            except queue.Empty:
                break
        if self.speaker is not None:
            self.speaker.cancel()

    async def _receive(self, s: Any, mcp: Any, rt: Any) -> None:
        while True:
            got_any = False
            async for msg in s.receive():
                got_any = True
                upd = msg.session_resumption_update
                if upd and upd.resumable and upd.new_handle:
                    self.handle = upd.new_handle
                if msg.go_away is not None:
                    log.info("gemini session ending in %s, reconnecting", msg.go_away.time_left)
                    return
                if msg.tool_call:
                    await self._run_tools(s, mcp, rt, msg.tool_call.function_calls)
                sc = msg.server_content
                if not sc:
                    continue
                if sc.interrupted:
                    self._drop_voice()
                # Transcripts arrive in fragments: collect them and print one line per turn.
                if sc.input_transcription and sc.input_transcription.text:
                    self._heard += sc.input_transcription.text
                if sc.output_transcription and sc.output_transcription.text:
                    if self._heard.strip():
                        self._show("heard", self._heard.strip())
                        self._heard = ""
                    self._said += sc.output_transcription.text
                if sc.model_turn:
                    for part in sc.model_turn.parts:
                        if part.inline_data and part.inline_data.data:
                            self.voice_q.put_nowait(part.inline_data.data)
                if sc.turn_complete or sc.interrupted:
                    for kind, text in (("heard", self._heard), ("said", self._said)):
                        if text.strip():
                            self._show(kind, text.strip() + (" [interrupted]" if sc.interrupted and kind == "said" else ""))
                    self._heard = self._said = ""
            if not got_any:  # receive() ends at each turn; an empty one means a dead socket
                await asyncio.sleep(0.05)

    async def _run_tools(self, s: Any, mcp: Any, rt: Any, calls: list[Any]) -> None:
        from google.genai import types

        responses = []
        for fc in calls:
            args = dict(fc.args or {})
            started = time.monotonic()
            # Mute before the sound starts; the task id (and its task.finished) only exists after.
            pending = f"call:{fc.id}"
            if fc.name in SOUND_TOOLS:
                self.gate.start(pending, max_s=10.0)
            try:
                if fc.name == "camera_snapshot":
                    response = await self._snapshot_to_video(s, rt, args)
                else:
                    result = await asyncio.wait_for(mcp.call_tool(fc.name, args), TOOL_TIMEOUT_S)
                    response = tool_response(result)
            except Exception as exc:  # report to Gemini, keep the session alive
                response = {"error": f"{type(exc).__name__}: {exc}"}
            task_id = response.get("task_id")
            if fc.name in SOUND_TOOLS:
                if task_id:
                    self.gate.start(f"task:{task_id}")
                self.gate.finish(pending)
            self._show("tool", f"{fc.name}({json.dumps(args)}) -> {json.dumps(response, default=str)[:160]} "
                               f"[{(time.monotonic() - started) * 1000:.0f} ms]")
            responses.append(types.FunctionResponse(id=fc.id, name=fc.name, response=response))
        await s.send_tool_response(function_responses=responses)

    async def _snapshot_to_video(self, s: Any, rt: Any, args: dict[str, Any]) -> dict[str, Any]:
        """Gemini already watches the head camera; a snapshot just pushes a fresh frame now."""
        from google.genai import types

        if rt.cameras is None:
            return {"error": "camera disabled"}
        camera = str(args.get("camera", "top"))
        jpeg, frame = await asyncio.to_thread(rt.cameras.snapshot, camera, 640)
        await s.send_realtime_input(video=types.Blob(data=jpeg, mime_type="image/jpeg"))
        return {"camera": camera, "image": "sent to your video input", "width": getattr(frame, "width", None)}

    def _show(self, kind: str, text: str) -> None:
        # This CLI owns stdout: the MCP transport is in-memory, not stdio.
        if self.args.no_transcripts and kind != "tool":
            return
        print(f"[{kind}] {text}", flush=True)


def main(argv: list[str] | None = None) -> None:
    settings, args = _parse(argv)
    configure_logging(settings.log_level, "stdio")
    key = load_api_key()
    if not key:
        raise SystemExit(f"{KEY_VAR} is not set: put `{KEY_VAR}=<key>` in .env or export it")
    code = 0
    try:
        asyncio.run(GeminiPepper(settings, args, key).run())
    except KeyboardInterrupt:
        print("bye", file=sys.stderr)
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        code = exc.code if isinstance(exc.code, int) else 1
    except BaseException:
        log.exception("pepper-mcp-gemini crashed")
        code = 1
    # By now the server has stopped and released the robot (safety distances restored), but
    # libqi's native teardown can hang the interpreter exit forever (reproduced with a bare
    # Runtime start/stop and no Python threads left), so skip it.
    logging.shutdown()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


if __name__ == "__main__":
    main()
