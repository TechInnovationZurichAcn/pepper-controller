"""On-robot speech recognition (ALSpeechRecognition word spotting) and QiChat dialogs.

These are *in addition* to the laptop STT pipeline: `recognize_keywords` is a fast,
offline way to get a yes/no or a menu choice without transcribing free speech, and
``ALDialog`` runs a scripted conversation entirely on the robot. The two share the
robot's recogniser, so only one may be active at a time; both also conflict with
nothing on the laptop side (our mic stream is a separate ALAudioDevice client).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from pepper_mcp.robot.connection import RobotConnection, RobotError
from pepper_mcp.robot.events import EventBus, MemoryWatcher

log = logging.getLogger(__name__)

SUB = "pepper_mcp_asr"


class OnRobotASR:
    def __init__(self, conn: RobotConnection, bus: EventBus, memory: MemoryWatcher, language: str = "English"):
        self.conn = conn
        self.bus = bus
        self.memory = memory
        self.language = language
        self._lock = threading.Lock()
        self.dialog_topic: str | None = None

    @property
    def asr(self) -> Any:
        return self.conn.service("ALSpeechRecognition")

    def recognize(self, words: list[str], timeout_s: float = 8.0, word_spotting: bool = True,
                  threshold: float = 0.35) -> dict[str, Any]:
        words = [w.strip().lower() for w in words if w.strip()]
        if not words:
            raise RobotError("give at least one word")
        if self.dialog_topic:
            raise RobotError("stop the builtin dialog first (shared recognizer)")
        with self._lock:
            asr = self.asr
            try:
                asr.setLanguage(self.language)
            except Exception:
                pass
            try:
                asr.setVocabulary(words, word_spotting)
            except Exception:
                asr.pause(True)
                asr.setVocabulary(words, word_spotting)
                asr.pause(False)
            try:
                self.memory.memory.insertData("WordRecognized", ["", 0.0])
            except Exception:
                pass
            asr.subscribe(SUB)
            started = time.monotonic()
            try:
                while time.monotonic() - started < timeout_s:
                    data = self.memory.get("WordRecognized")
                    if data and len(data) >= 2 and data[0]:
                        word, conf = str(data[0]).strip().lower(), float(data[1])
                        if conf >= threshold:
                            word = word.replace("<...>", "").strip()
                            result = {"word": word, "confidence": round(conf, 2),
                                      "latency_s": round(time.monotonic() - started, 2)}
                            self.bus.publish("speech.keyword", result, source="asr")
                            return result
                    time.sleep(0.05)
                return {"word": None, "timeout": True}
            finally:
                try:
                    asr.unsubscribe(SUB)
                except Exception:
                    pass

    # ------------------------------------------------------------- QiChat
    @property
    def dialog(self) -> Any:
        return self.conn.service("ALDialog")

    def start_dialog(self, topic_content: str) -> dict[str, Any]:
        if self.dialog_topic:
            self.stop_dialog()
        dialog = self.dialog
        try:
            dialog.setLanguage(self.language)
        except Exception:
            pass
        topic = dialog.loadTopicContent(topic_content)
        dialog.activateTopic(topic)
        dialog.subscribe("pepper_mcp_dialog")
        self.dialog_topic = topic
        return {"topic": topic, "active": True}

    def stop_dialog(self) -> dict[str, Any]:
        if not self.dialog_topic:
            return {"active": False}
        dialog = self.dialog
        for fn, args in (("unsubscribe", ("pepper_mcp_dialog",)), ("deactivateTopic", (self.dialog_topic,)),
                         ("unloadTopic", (self.dialog_topic,))):
            try:
                getattr(dialog, fn)(*args)
            except Exception as exc:
                log.debug("dialog %s: %s", fn, exc)
        self.dialog_topic = None
        return {"active": False}
