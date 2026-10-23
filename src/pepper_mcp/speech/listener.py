"""Turns the microphone stream into ``speech.*`` events.

Pipeline (all on background threads, never on the MCP event loop):

    PCM buffers (≈170 ms) → 20 ms frames → VAD → utterance segmenter
      → echo gate (muted while Pepper speaks + tail) → STT worker → speech.final

Events:
* ``speech.started`` – a person started talking (lets reflexes look at them / LEDs)
* ``speech.final``   – transcript with timing (``listen`` tool returns this)
* ``speech.aborted`` – too short / empty transcription
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from typing import Any

import numpy as np

from pepper_mcp.robot.audio_in import MicrophoneStream
from pepper_mcp.robot.events import EventBus
from pepper_mcp.speech.stt import SpeechToText, Transcript
from pepper_mcp.speech.vad import FRAME_MS, VAD

log = logging.getLogger(__name__)


class Listener:
    def __init__(self, mic: MicrophoneStream, vad: VAD, stt: SpeechToText, bus: EventBus,
                 speech_state: Any = None, end_silence_ms: int = 450, min_speech_ms: int = 250,
                 max_utterance_s: float = 15.0, pre_roll_ms: int = 300, echo_tail_ms: int = 350,
                 barge_in: bool = False):
        self.mic = mic
        self.vad = vad
        self.stt = stt
        self.bus = bus
        self.speech_state = speech_state  # object with .speaking / .last_spoke_at / .stop_speaking()
        self.end_silence_ms = end_silence_ms
        self.min_speech_ms = min_speech_ms
        self.max_utterance_s = max_utterance_s
        self.pre_roll_ms = pre_roll_ms
        self.echo_tail_ms = echo_tail_ms
        self.barge_in = barge_in
        self.enabled = False
        self._remove_consumer = None
        self._frame_len = 0
        self._pending = np.zeros(0, dtype=np.int16)
        self._pre_roll: deque[np.ndarray] = deque()
        self._utterance: list[np.ndarray] = []
        self._in_speech = False
        self._speech_ms = 0
        self._silence_ms = 0
        self._utt_started_at = 0.0
        self._stt_queue: queue.Queue[tuple[np.ndarray, int, float, float] | None] = queue.Queue()
        self._stt_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.transcript: deque[dict[str, Any]] = deque(maxlen=200)
        self.stats = {"utterances": 0, "aborted": 0, "muted_frames": 0, "last_stt_latency_ms": None,
                      "vad": getattr(vad, "name", "?"), "stt": getattr(stt, "name", "?")}
        self.speech_detected = False
        self._lock = threading.Lock()

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self.enabled:
            return
        self._stop.clear()
        self._stt_thread = threading.Thread(target=self._stt_worker, name="stt", daemon=True)
        self._stt_thread.start()
        self._frame_len = int(self.mic.rate * FRAME_MS / 1000)
        self._remove_consumer = self.mic.add_consumer(self._on_audio)
        self.enabled = True
        threading.Thread(target=self._warm_up, name="stt-warmup", daemon=True).start()

    def _warm_up(self) -> None:
        try:
            self.stt.warm_up()
        except Exception as exc:
            log.warning("STT warm-up failed: %s", exc)

    def stop(self) -> None:
        self.enabled = False
        if self._remove_consumer:
            self._remove_consumer()
            self._remove_consumer = None
        self._stop.set()
        self._stt_queue.put(None)
        if self._stt_thread:
            self._stt_thread.join(timeout=2.0)
            self._stt_thread = None
        self._reset_segment()

    def set_enabled(self, enabled: bool) -> None:
        if enabled:
            self.start()
        else:
            self.stop()

    # ------------------------------------------------------------- gating
    def _muted(self) -> bool:
        st = self.speech_state
        if st is None:
            return False
        if getattr(st, "speaking", False):
            return True
        last = getattr(st, "last_spoke_at", 0.0) or 0.0
        return (time.time() - last) * 1000 < self.echo_tail_ms

    # ------------------------------------------------------------- audio path
    def _on_audio(self, samples: np.ndarray, rate: int, ts: float) -> None:
        if not self.enabled:
            return
        with self._lock:
            data = np.concatenate([self._pending, samples]) if self._pending.size else samples
            n_frames = data.size // self._frame_len
            frames = data[: n_frames * self._frame_len].reshape(n_frames, self._frame_len)
            self._pending = data[n_frames * self._frame_len:]
            for frame in frames:
                self._process_frame(frame, rate)

    def _process_frame(self, frame: np.ndarray, rate: int) -> None:
        muted = self._muted()
        if muted and not self.barge_in:
            self.stats["muted_frames"] += 1
            if self._in_speech:
                self._finish_segment(rate)  # robot started talking: close what we had
            self.speech_detected = False
            return
        try:
            speech = self.vad.is_speech(frame, rate)
        except Exception:
            speech = False
        if muted and speech and self.barge_in and self.speech_state is not None:
            # a person talks over Pepper: stop the robot and keep listening
            try:
                self.speech_state.stop_speaking()
                self.bus.publish("speech.barge_in", {}, source="listener")
            except Exception:
                pass
        self.speech_detected = speech
        if not self._in_speech:
            self._pre_roll.append(frame)
            while len(self._pre_roll) * FRAME_MS > self.pre_roll_ms:
                self._pre_roll.popleft()
            if speech:
                self._in_speech = True
                self._speech_ms = FRAME_MS
                self._silence_ms = 0
                self._utt_started_at = time.time() - self.pre_roll_ms / 1000
                self._utterance = list(self._pre_roll)
                self._pre_roll.clear()
                self.bus.publish("speech.started", {"at": self._utt_started_at}, source="listener")
            return
        self._utterance.append(frame)
        if speech:
            self._speech_ms += FRAME_MS
            self._silence_ms = 0
        else:
            self._silence_ms += FRAME_MS
        total_s = len(self._utterance) * FRAME_MS / 1000
        if self._silence_ms >= self.end_silence_ms or total_s >= self.max_utterance_s:
            self._finish_segment(rate)

    def _finish_segment(self, rate: int) -> None:
        frames, speech_ms, started = self._utterance, self._speech_ms, self._utt_started_at
        self._reset_segment()
        if speech_ms < self.min_speech_ms or not frames:
            self.stats["aborted"] += 1
            self.bus.publish("speech.aborted", {"reason": "too_short", "speech_ms": speech_ms,
                                                     "started_at": started}, source="listener")
            return
        audio = np.concatenate(frames)
        self._stt_queue.put((audio, rate, started, time.time()))

    def _reset_segment(self) -> None:
        self._in_speech = False
        self._utterance = []
        self._speech_ms = 0
        self._silence_ms = 0
        self._pre_roll.clear()

    # ------------------------------------------------------------- STT worker
    def _stt_worker(self) -> None:
        while not self._stop.is_set():
            item = self._stt_queue.get()
            if item is None:
                continue
            audio, rate, started, ended = item
            # Drop stale backlog: only the most recent utterance matters for a live chat.
            while not self._stt_queue.empty():
                try:
                    nxt = self._stt_queue.get_nowait()
                except queue.Empty:
                    break
                if nxt is None:
                    self._stt_queue.put(None)
                    break
                audio, rate, started, ended = nxt
            t0 = time.monotonic()
            try:
                result: Transcript = self.stt.transcribe(audio, rate)
            except Exception as exc:
                log.warning("STT failed: %s", exc)
                self.bus.publish("speech.aborted", {"reason": f"stt_error: {exc}", "started_at": started}, source="listener")
                continue
            latency = time.monotonic() - t0
            self.stats["last_stt_latency_ms"] = int(latency * 1000)
            text = result.text.strip()
            if not text or _looks_like_noise(text):
                self.stats["aborted"] += 1
                self.bus.publish("speech.aborted", {"reason": "empty_transcript", "started_at": started}, source="listener")
                continue
            self.stats["utterances"] += 1
            payload = {"text": text, "language": result.language, "confidence": result.confidence,
                       "started_at": started, "ended_at": ended, "duration_s": round(ended - started, 2),
                       "stt_latency_ms": int(latency * 1000), "backend": result.backend}
            self.transcript.append({"role": "user", "at": ended, **payload})
            self.bus.publish("speech.final", payload, source="listener")

    # ------------------------------------------------------------- status
    def status(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "in_speech": self._in_speech, "speech_detected": self.speech_detected,
                "muted_by_tts": self._muted(), "barge_in": self.barge_in,
                "end_silence_ms": self.end_silence_ms, "min_speech_ms": self.min_speech_ms, **self.stats}


_NOISE = {"you", "thank you.", "thanks for watching!", "bye.", ".", "uh", "um", "hmm"}


def _looks_like_noise(text: str) -> bool:
    t = text.strip().lower()
    return t in _NOISE or len(t) < 2
