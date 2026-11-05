"""``pepper-mcp-gemini``: Pepper as a real-time Gemini Live agent.

It starts the MCP server in-process and drives the robot only through its tools (same safety
limits, same task/event model). Gemini Live is a streaming speech-to-speech model, so instead of
turns over text it gets

    Pepper's head mic (16 kHz PCM)   ──►┐
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
import re
import sys
import threading
import time
from collections.abc import Iterable
from typing import Any

from pepper_mcp.cli import build_parser, configure_logging, stop_on_signals
from pepper_mcp.config import Settings, removed_env_vars

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
    "recognize_keywords", "record_clip",
    "start_builtin_dialog", "stop_builtin_dialog",
    "wait_for_event", "wait_task", "camera_stream",
    # reads any file on the laptop: nothing a voice in the room should be able to trigger
    "upload_audio",
})
BLOCKING_ARGS = frozenset({"wait"})
# Actions whose result the model needs before it can answer: they stay blocking.
ANSWER_TOOLS = frozenset({"analyze_face"})
# Live models that only support blocking function calls (NON_BLOCKING is rejected or ignored).
SYNC_ONLY_MODELS = ("gemini-3.1-flash-live",)
# Tools whose task makes sound through the speakers: the mic stays muted until task.finished.
SOUND_TOOLS = frozenset({"play_gesture", "play_animation", "play_audio", "play_sine", "run_behavior"})
# Robot events Gemini is told about, as short text notes in the session.
FORWARDED_EVENTS = frozenset({
    "person.appeared", "person.left", "person.waving", "touch.start", "reflex.completed",
    "task.finished", "motion.blocked", "robot.fell", "battery.low", "nav.status",
    "game.started", "game.move", "game.over",
})

PERSONA = """You are {name}, a friendly, curious humanoid robot made by SoftBank, standing in the room
with the people you talk to. You hear through your own microphones, you see through the camera in
your head (the video you receive is your own view) and you speak through your own loudspeakers.

You have a real body, controlled through your tools. Use it the way a person would and without
being asked: gesture while you talk (play_gesture: wave, nod, shake_head, celebrate,
high_five, ...; prefer these over play_animation, whose names must come from
list_animations), look at people (look_at_person, look_toward, look), turn to face
them (turn), walk over (move_to in metres, move for short drives), show feelings with your lights
(set_mood_lights, set_leds) and use your tablet (tablet_show_text) for key words.
When someone wants to play a game, call tictactoe_start right away: it puts the board on your
tablet by itself, so don't show any text there (no "let's play" title) and leave the tablet alone
until the game is stopped with tictactoe_stop. Your moves are made for you the moment the person taps a square; [robot event] game.move and game.over notes
tell you what happened, so cheer, tease or congratulate in a few words. If they say their move
out loud, pass it on with tictactoe_move (squares 1-9, top-left to bottom-right).

Act, don't narrate. Your body language is something people see, not something you talk about:
never announce, describe or explain a tool call, a gesture, a look, a light or what your senses
picked up ("I see you're looking at me, so I'll nod", "I'm turning around", "setting my mood to
happy"). Never say the name of a tool or gesture out loud ("wave", "nod", "play gesture"). Just
call it, silently, and say only what you would say to the person anyway. Actions run in the
background while you keep talking; their results reach you silently and need no reply.

Moving is real and can be refused. Moves, turns and gestures run in the background and their
outcome arrives as a [robot event] task.finished note. Say nothing when a move worked. ok=false or a
motion.blocked note means you did NOT move: only then mention it, briefly and honestly (something
is in your way), instead of pretending you moved. Keep people safe: modest speeds, short
moves, stop_motion the moment anyone says stop or seems uneasy, never drive while being touched.

Lines starting with "[robot event]" are your senses reporting, not someone speaking, and they are
private: never repeat, quote or comment on them. React the way a person would - usually with your
body alone (look at someone who appears, a nod, a light), and with words only when a person would
speak too (say hello to someone new, answer a touch on the head, cheer a high five). If nothing is
worth saying, stay silent: produce no words at all, never a placeholder such as "<no speech>",
"{{pause}}", "(silence)" or "...". Ignore noise and stray words that are not someone talking to you.

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


def supports_async_tools(model: str) -> bool:
    return not any(model.startswith(m) or f"/{m}" in model for m in SYNC_ONLY_MODELS)


def tool_declarations(tools: list[Any], non_blocking: bool = True) -> list[dict[str, Any]]:
    """MCP tools → Gemini function declarations (JSON schema passed through unchanged).

    With ``non_blocking`` every action (not read-only, not in ``ANSWER_TOOLS``) is declared
    NON_BLOCKING: the model keeps talking while it runs instead of pausing, and its result can
    come back SILENT. A blocking call ends the model's turn, and the turn that follows the
    result is where it says "wave" or "<no speech>" out loud.
    """
    out = []
    for t in tools:
        if t.name in EXCLUDED_TOOLS:
            continue
        decl: dict[str, Any] = {"name": t.name, "description": (t.description or "").strip()[:1000]}
        read_only = bool(getattr(getattr(t, "annotations", None), "read_only_hint", False))
        if non_blocking and not read_only and t.name not in ANSWER_TOOLS:
            decl["behavior"] = "NON_BLOCKING"
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


# Markers the model sometimes emits for "nothing to say", e.g. "<no speech>{pause}".
_SILENCE_MARKERS = re.compile(r"<[^<>]*>|\{[^{}]*\}|\((?:silence|no speech|pause)\)", re.IGNORECASE)


def clean_transcript(text: str) -> str:
    """Drop silence/pause markers from a transcript; empty when nothing was really said."""
    return " ".join(_SILENCE_MARKERS.sub(" ", text).split())


# Things the model says when it has nothing to say ("No speech.", "<pause>").
PLACEHOLDERS = ("no speech", "no response", "no reply", "silence", "pause", "no audio")
_MARKER_CHARS = "<{(["


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]|_", " ", text.lower()).split())


class SpeechFilter:
    """Keeps placeholder and narration turns ("no speech", "wave") off Pepper's loudspeakers.

    Gemini's native audio speaks whatever it generates, so ``clean_transcript`` alone only
    tidies the log. The first audio of every turn is held until its transcript shows real words
    (usually the first fragment, so no audible delay); a turn that is only a marker, a
    placeholder or a tool/gesture name is dropped. Held audio is released after ``HOLD_S`` at
    the latest so a late transcript never swallows a real answer.
    """

    HOLD_S = 0.8

    def __init__(self, narration: Iterable[str] = (), clock: Any = time.monotonic):
        self.clock = clock
        self.phrases = {p for p in (_norm(x) for x in (*PLACEHOLDERS, *narration)) if p}
        self.reset()

    def interrupt(self) -> None:
        """Barge-in: drop the turn, and ignore its trailing transcript until new audio comes."""
        self.reset()
        self._stale = True

    def reset(self) -> None:
        self._stale = False
        self._held: list[bytes] = []
        self._since: float | None = None
        self._text = ""
        self._state = "wait"  # wait (hold audio) | speak | drop

    def _release(self) -> list[bytes]:
        self._state = "speak"
        out, self._held = self._held, []
        return out

    def audio(self, chunk: bytes) -> list[bytes]:
        """A voice chunk arrived: the chunks to play now."""
        self._stale = False
        if self._state == "speak":
            return [chunk]
        if self._state == "drop":
            return []
        now = self.clock()
        if self._since is None:
            self._since = now
        self._held.append(chunk)
        return self._release() if now - self._since >= self.HOLD_S else []

    def text(self, fragment: str) -> list[bytes]:
        """An output transcript fragment arrived: held chunks to play now."""
        if self._stale:
            return []
        self._text += fragment
        if self._state == "speak":
            return []
        words = _norm(clean_transcript(self._text))
        if not words:
            if any(c in self._text for c in _MARKER_CHARS):  # "<no speech>", "{pause}"
                self._state, self._held = "drop", []
            return []
        if any(p == words or p.startswith(words + " ") for p in self.phrases):
            return []  # maybe narration, maybe the start of a sentence: decide at turn end
        return self._release()

    def end(self) -> list[bytes]:
        """Turn complete: what is still held, unless the whole turn was a placeholder."""
        words = _norm(clean_transcript(self._text))
        keep = self._state == "wait" and words not in self.phrases
        out = self._held if keep else []
        self.reset()
        return out


def format_event(event_type: str, data: dict[str, Any]) -> str | None:
    """One robot event → a short note for Gemini, or None when it is not worth mentioning."""
    if event_type not in FORWARDED_EVENTS:
        return None
    # A finished action is noise (the model already got its silent tool result); failures are not.
    if event_type == "task.finished" and data.get("ok") is not False:
        return None
    if event_type == "person.left" and (data.get("present_for_s") or 99) < 3:
        return None
    if event_type == "reflex.completed" and data.get("reflex") != "high_five":
        return None  # the body already reacted; a note would only invite Gemini to narrate it
    if event_type == "game.move" and data.get("player") == "human" and data.get("next") == "pepper":
        return None  # Pepper's reply follows within a second and carries the whole board
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
    parser.add_argument("--model", default=MODEL, help=f"Gemini Live model (default {MODEL})")
    parser.add_argument("--voice", default="Kore", help="prebuilt Gemini voice (Kore, Puck, Charon, ...)")
    parser.add_argument("--persona", default="Pepper")
    parser.add_argument("--no-video", action="store_true", help="don't stream the head camera to Gemini")
    parser.add_argument("--no-transcripts", action="store_true", help="don't print what was said")
    args = parser.parse_args(argv)
    settings = Settings()
    settings.apply_overrides({k: v for k, v in vars(args).items()})
    # The bridge forwards only 16 kHz buffers (Gemini Live's input rate); a 48 kHz
    # PEPPER_AUDIO_RATE would otherwise leave Pepper silently deaf.
    settings.audio_rate = MIC_RATE
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
        self.rt: Any = None
        self.speech = SpeechFilter()
        self.background_tools: frozenset[str] = frozenset()  # NON_BLOCKING: results go back SILENT
        self._tool_tasks: set[asyncio.Task] = set()

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
            declarations = tool_declarations(tools, non_blocking=supports_async_tools(self.args.model))
            self.background_tools = frozenset(d["name"] for d in declarations if d.get("behavior"))
            self.speech = SpeechFilter([*(d["name"] for d in declarations), *rt.gestures.names()])
            self.rt = rt
            rt.voice_agent = f"Gemini Live ({self.args.model})"  # shown on the dashboard
            if rt.mic is None:
                raise SystemExit("Pepper's microphone is not available; Gemini Live needs it")
            if rt.audio_out is None:
                raise SystemExit("Pepper's speakers are not available")
            speaker = await asyncio.to_thread(rt.audio_out.speaker_stream, VOICE_RATE)
            remove_mic = rt.mic.add_consumer(self._on_mic)
            unsubscribe = rt.bus.subscribe("*", self._on_event)
            system = PERSONA.format(name=self.args.persona, language=self.settings.language)
            log.info("gemini live: %s, %d tools, voice %s", self.args.model, len(declarations), self.args.voice)
            print(f"Pepper is live on {self.args.model} ({len(declarations)} tools), listening on the "
                  f"{rt.mic.mic} mic. Ctrl+C to stop.", file=sys.stderr)
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

        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction=system,
            speech_config={"voice_config": {"prebuilt_voice_config": {"voice_name": self.args.voice}}},
            tools=[{"function_declarations": declarations}],
            input_audio_transcription=None if self.args.no_transcripts else {},
            # Always on: SpeechFilter needs it to keep "no speech" off the speakers.
            output_audio_transcription={},
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
        self.speech.reset()
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
                for t in (*tasks, *self._tool_tasks):
                    t.cancel()
                await asyncio.gather(*tasks, *self._tool_tasks, return_exceptions=True)
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
                    # Off the receive loop: a NON_BLOCKING tool runs while the model keeps talking,
                    # and its voice must keep flowing meanwhile.
                    task = asyncio.create_task(self._run_tools(s, mcp, rt, msg.tool_call.function_calls))
                    self._tool_tasks.add(task)
                    task.add_done_callback(self._tool_tasks.discard)
                sc = msg.server_content
                if not sc:
                    continue
                if sc.interrupted:
                    self._drop_voice()
                    self.speech.interrupt()
                # Transcripts arrive in fragments: collect them and print one line per turn.
                if sc.input_transcription and sc.input_transcription.text:
                    self._heard += sc.input_transcription.text
                if sc.output_transcription and sc.output_transcription.text:
                    if self._heard.strip():
                        self._show("heard", self._heard.strip())
                        self._heard = ""
                    self._said += sc.output_transcription.text
                    self._play(self.speech.text(sc.output_transcription.text))
                if sc.model_turn:
                    for part in sc.model_turn.parts:
                        if part.inline_data and part.inline_data.data:
                            self._play(self.speech.audio(part.inline_data.data))
                if sc.turn_complete and not sc.interrupted:
                    self._play(self.speech.end())
                if sc.turn_complete or sc.interrupted:
                    for kind, text in (("heard", self._heard), ("said", clean_transcript(self._said))):
                        if text.strip():
                            self._show(kind, text.strip() + (" [interrupted]" if sc.interrupted and kind == "said" else ""))
                    self._heard = self._said = ""
            if not got_any:  # receive() ends at each turn; an empty one means a dead socket
                await asyncio.sleep(0.05)

    def _play(self, chunks: list[bytes]) -> None:
        for chunk in chunks:
            self.voice_q.put_nowait(chunk)

    async def _run_tools(self, s: Any, mcp: Any, rt: Any, calls: list[Any]) -> None:
        try:
            await self._call_tools(s, mcp, rt, calls)
        except asyncio.CancelledError:
            raise
        except Exception:  # the session socket closed under us; _keep_alive reconnects
            log.exception("sending tool responses failed")

    async def _call_tools(self, s: Any, mcp: Any, rt: Any, calls: list[Any]) -> None:
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
            finally:  # also on cancellation (reconnect): never leave the mic muted for a dead call
                if fc.name in SOUND_TOOLS:
                    self.gate.finish(pending)
            task_id = response.get("task_id")
            if fc.name in SOUND_TOOLS and task_id:
                self.gate.start(f"task:{task_id}")
            self._show("tool", f"{fc.name}({json.dumps(args)}) -> {json.dumps(response, default=str)[:160]} "
                               f"[{(time.monotonic() - started) * 1000:.0f} ms]")
            scheduling = None
            if fc.name in self.background_tools:
                # Success needs no words (the person sees the gesture); a failure may, once idle.
                failed = bool(response.get("error")) or response.get("ok") is False  # tasks carry error=None
                scheduling = (types.FunctionResponseScheduling.WHEN_IDLE if failed
                              else types.FunctionResponseScheduling.SILENT)
                response = {**response, "scheduling": scheduling.value}
            responses.append(types.FunctionResponse(id=fc.id, name=fc.name, response=response,
                                                    scheduling=scheduling))
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
        if kind in ("heard", "said") and self.rt is not None:  # the dashboard's conversation panel
            self.rt.transcript.append({"role": "user" if kind == "heard" else "robot",
                                       "text": text, "at": time.time()})
        # This CLI owns stdout: the MCP transport is in-memory, not stdio.
        if self.args.no_transcripts and kind != "tool":
            return
        print(f"[{kind}] {text}", flush=True)


def main(argv: list[str] | None = None) -> None:
    settings, args = _parse(argv)
    configure_logging(settings.log_level)
    stop_on_signals()
    if stale := removed_env_vars():
        log.warning("ignoring %s: speech-to-text and the standalone MCP server were removed "
                    "(Gemini Live is the ears)", ", ".join(stale))
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
