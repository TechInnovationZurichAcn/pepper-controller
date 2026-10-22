"""Text-to-speech queue (ALAnimatedSpeech / ALTextToSpeech).

``say`` returns immediately; a single worker drains the queue so that sentences the LLM
streams out play back-to-back with gestures. Publishes ``tts.started`` /
``tts.finished`` events – the listener uses them to mute the microphone while Pepper is
talking (echo gating).
"""

from __future__ import annotations

import itertools
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from pepper_mcp.robot.connection import RobotConnection, RobotError
from pepper_mcp.robot.events import EventBus

log = logging.getLogger(__name__)

BODY_LANGUAGE_MODES = ("contextual", "random", "disabled")


@dataclass
class Utterance:
    id: str
    text: str
    animated: bool = True
    body_language: str = "contextual"
    language: str | None = None
    speed: int | None = None
    pitch: int | None = None
    volume: int | None = None
    queued_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    done: threading.Event = field(default_factory=threading.Event, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "utterance_id": self.id,
            "text": self.text,
            "animated": self.animated,
            "state": "error" if self.error else "done" if self.finished_at else
            "speaking" if self.started_at else "queued",
            "error": self.error,
        }


class Speech:
    def __init__(self, conn: RobotConnection, bus: EventBus, default_language: str = "English"):
        self.conn = conn
        self.bus = bus
        self.default_language = default_language
        self._queue: queue.Queue[Utterance | None] = queue.Queue()
        self._ids = itertools.count(1)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._current: Utterance | None = None
        self._lock = threading.Lock()
        self._history: list[Utterance] = []
        self.speaking = False
        self.last_spoke_at = 0.0
        self.animated_available: bool | None = None

    # ------------------------------------------------------------- services
    @property
    def tts(self) -> Any:
        return self.conn.service("ALTextToSpeech")

    @property
    def animated(self) -> Any | None:
        svc = self.conn.try_service("ALAnimatedSpeech")
        self.animated_available = svc is not None
        return svc

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._worker, name="speech-tts", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._queue.put(None)
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    # ------------------------------------------------------------- API
    def say(self, text: str, animated: bool = True, body_language: str = "contextual",
            language: str | None = None, speed: int | None = None, pitch: int | None = None,
            volume: int | None = None, wait: bool = False, timeout_s: float = 60.0) -> Utterance:
        text = (text or "").strip()
        if not text:
            raise RobotError("nothing to say")
        if body_language not in BODY_LANGUAGE_MODES:
            raise RobotError(f"body_language must be one of {BODY_LANGUAGE_MODES}")
        utt = Utterance(id=f"utt-{next(self._ids)}", text=text, animated=animated,
                        body_language=body_language, language=language, speed=speed, pitch=pitch,
                        volume=volume)
        with self._lock:
            self._history.append(utt)
            self._history = self._history[-100:]
        self.start()
        self._queue.put(utt)
        if wait:
            if not utt.done.wait(timeout_s):
                raise RobotError("timed out waiting for speech to finish")
        return utt

    def stop_speaking(self) -> int:
        """Flush the queue and interrupt the current utterance. Returns #dropped."""
        dropped = 0
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is not None:
                item.error = "cancelled"
                item.finished_at = time.time()
                item.done.set()
                dropped += 1
        try:
            self.tts.stopAll()
        except Exception as exc:
            log.debug("stopAll: %s", exc)
        return dropped

    def status(self) -> dict[str, Any]:
        cur = self._current
        return {
            "speaking": self.speaking,
            "queued": self._queue.qsize(),
            "current": cur.to_dict() if cur else None,
            "animated_speech_available": self.animated_available,
        }

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            return [u.to_dict() | {"at": u.started_at or u.queued_at} for u in self._history[-limit:]]

    def set_voice(self, language: str | None = None, speed: int | None = None,
                  pitch: int | None = None, voice: str | None = None) -> dict[str, Any]:
        tts = self.tts
        if language:
            tts.setLanguage(language)
            self.default_language = language
        if voice:
            tts.setVoice(voice)
        if speed is not None:
            tts.setParameter("speed", float(max(50, min(400, speed))))
        if pitch is not None:
            tts.setParameter("pitchShift", float(max(50, min(200, pitch))) / 100.0)
        return self.voice_info()

    def voice_info(self) -> dict[str, Any]:
        tts = self.tts
        info: dict[str, Any] = {}
        for key, fn in (("language", "getLanguage"), ("voice", "getVoice"),
                        ("volume", "getVolume")):
            try:
                info[key] = getattr(tts, fn)()
            except Exception:
                pass
        for param in ("speed", "pitchShift"):
            try:
                info[param] = tts.getParameter(param)
            except Exception:
                pass
        return info

    def languages(self) -> dict[str, Any]:
        tts = self.tts
        out: dict[str, Any] = {}
        for key, fn in (("languages", "getAvailableLanguages"), ("voices", "getAvailableVoices"),
                        ("supported", "getSupportedLanguages")):
            try:
                out[key] = list(getattr(tts, fn)())
            except Exception:
                pass
        return out

    # ------------------------------------------------------------- worker
    def _worker(self) -> None:
        while not self._stop.is_set():
            utt = self._queue.get()
            if utt is None:
                continue
            self._speak(utt)

    def _speak(self, utt: Utterance) -> None:
        self._current = utt
        utt.started_at = time.time()
        self.speaking = True
        self.bus.publish("tts.started", {"utterance_id": utt.id, "text": utt.text}, source="speech")
        try:
            self._apply_params(utt)
            animated = self.animated if utt.animated else None
            if animated is not None:
                try:
                    future = RobotConnection.call_async(
                        animated, "say", utt.text, {"bodyLanguageMode": utt.body_language})
                    RobotConnection.future_result(future, timeout_s=120)
                except Exception as exc:
                    log.warning("ALAnimatedSpeech failed (%s); falling back to plain TTS", exc)
                    self.tts.say(utt.text)
            else:
                future = RobotConnection.call_async(self.tts, "say", utt.text)
                RobotConnection.future_result(future, timeout_s=120)
        except Exception as exc:
            utt.error = str(exc)
            log.warning("say failed: %s", exc)
        finally:
            utt.finished_at = time.time()
            self.last_spoke_at = utt.finished_at
            self.speaking = False
            self._current = None
            utt.done.set()
            self.bus.publish("tts.finished", {"utterance_id": utt.id, "text": utt.text,
                                              "error": utt.error,
                                              "duration_s": round(utt.finished_at - utt.started_at, 2)},
                             source="speech")

    def _apply_params(self, utt: Utterance) -> None:
        tts = self.tts
        if utt.language:
            try:
                tts.setLanguage(utt.language)
            except Exception as exc:
                log.debug("setLanguage: %s", exc)
        if utt.speed is not None:
            try:
                tts.setParameter("speed", float(max(50, min(400, utt.speed))))
            except Exception:
                pass
        if utt.pitch is not None:
            try:
                tts.setParameter("pitchShift", float(max(50, min(200, utt.pitch))) / 100.0)
            except Exception:
                pass
        if utt.volume is not None:
            try:
                tts.setVolume(max(0.0, min(1.0, utt.volume / 100.0)))
            except Exception:
                pass
