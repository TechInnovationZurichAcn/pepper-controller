"""Wires every subsystem together and owns the start/stop order.

The :class:`Runtime` is what tool modules receive (``register(mcp, rt)``). Any attribute
may be ``None`` when the matching NAOqi service is missing on the robot or the feature
was disabled in :class:`pepper_mcp.config.Settings`; tools check and report that.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

from pepper_mcp import __version__
from pepper_mcp.config import Settings
from pepper_mcp.robot.animation import Animations
from pepper_mcp.robot.asr import OnRobotASR
from pepper_mcp.robot.audio_in import MicrophoneStream
from pepper_mcp.robot.audio_out import ASSETS_DIR, AudioOut
from pepper_mcp.robot.awareness import Awareness
from pepper_mcp.robot.camera import Cameras
from pepper_mcp.robot.connection import RobotConnection, RobotError
from pepper_mcp.robot.events import EventBus, MemoryWatcher
from pepper_mcp.robot.gestures import GestureContext, GestureRegistry
from pepper_mcp.robot.leds import Leds
from pepper_mcp.robot.motion import Motion
from pepper_mcp.robot.navigation import Navigator
from pepper_mcp.robot.perception import Perception
from pepper_mcp.robot.speech import Speech
from pepper_mcp.robot.system import System
from pepper_mcp.robot.tablet import Tablet
from pepper_mcp.robot.tasks import TaskRegistry
from pepper_mcp.robot.tracker import Tracker

log = logging.getLogger(__name__)


class Runtime:
    def __init__(self, settings: Settings, conn: RobotConnection | None = None):
        self.settings = settings
        self.started_at: float | None = None
        self.bus = EventBus()
        self.tasks = TaskRegistry(self.bus)
        if conn is None:
            if settings.fake:
                from pepper_mcp.robot.fake import FakeConnection

                conn = FakeConnection()
            else:
                conn = RobotConnection(settings.ip or "", settings.port, settings.listen_url)
        self.conn = conn
        self.memory = MemoryWatcher(conn, settings.memory_poll_s)
        self.motion = Motion(conn, self.bus, self.memory)
        self.speech = Speech(conn, self.bus, settings.language)
        self.animations = Animations(conn)
        self.system = System(conn)
        self.awareness = Awareness(conn)
        self.leds: Leds | None = Leds(conn)
        self.tablet: Tablet | None = Tablet(conn)
        self.audio_out: AudioOut | None = AudioOut(conn, settings.ssh_user, settings.ssh_password)
        self.perception: Perception | None = Perception(conn, self.bus, self.memory)
        self.tracker: Tracker | None = Tracker(conn, self.bus, self.memory)
        self.navigator: Navigator | None = Navigator(conn, self.bus, settings.map_store)
        self.asr: OnRobotASR | None = OnRobotASR(conn, self.bus, self.memory, settings.language)
        self.cameras: Cameras | None = Cameras(conn, settings.camera_resolution, settings.camera_fps,
                                               settings.jpeg_quality)
        self.mic: MicrophoneStream | None = MicrophoneStream(conn, settings.audio_rate)
        self.listener = None
        self.reflexes = None
        self.vision = None
        self.preview = None
        self.gestures = GestureRegistry(self._gesture_context)
        self._asset_urls: dict[str, str | None] = {}
        self._lock = threading.Lock()
        self.warnings: list[str] = []

    # ------------------------------------------------------------------ wiring helpers
    def _gesture_context(self) -> GestureContext:
        return GestureContext(motion=self.motion, say=lambda t: self.speech.say(t, animated=False),
                              leds=self.leds, tablet=self.tablet if self.tablet and self.tablet.available() else None,
                              audio=self.audio_out, perception=self.perception, bus=self.bus,
                              assets_url=self.assets_url)

    def assets_url(self, filename: str) -> str | None:
        """URL the robot's tablet can load for a bundled asset (preview server or robot web root)."""
        with self._lock:
            if filename in self._asset_urls:
                return self._asset_urls[filename]
        url = None
        if self.preview is not None and self.preview.base_url:
            url = f"{self.preview.base_url}/assets/{filename}"
        elif self.audio_out is not None and not self.settings.fake:
            url = self.audio_out.deploy_web_asset(filename)
        with self._lock:
            self._asset_urls[filename] = url
        return url

    def _warn(self, msg: str) -> None:
        log.warning(msg)
        self.warnings.append(msg)

    def _optional(self, label: str, fn: Any) -> Any:
        try:
            return fn()
        except Exception as exc:
            self._warn(f"{label} disabled: {exc}")
            return None

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        s = self.settings
        t0 = time.monotonic()
        self.conn.connect()
        self.memory.start()
        info = self.system.info()
        log.info("robot: %s", info)

        if s.autonomous_life == "disable":
            self._optional("autonomous life", self.awareness.disable_life)
        if s.wake_up:
            self._optional("wake up / posture", lambda: self.motion.prepare("StandInit"))
        self._optional("base safety distances",
                       lambda: self.motion.set_security_distances(s.safety_front_m, s.safety_side_m))
        self.motion.start()
        self._optional("liveliness preset", lambda: self.awareness.apply_preset(s.liveliness))
        self.speech.start()
        self._optional("TTS language", lambda: self.speech.set_voice(language=s.language))

        if s.perception_enabled and self.perception is not None:
            self._optional("perception", self.perception.start)
        else:
            self.perception = None

        if s.camera_enabled and self.cameras is not None:
            self._optional("camera", lambda: self.cameras.get("top").start())
        else:
            self.cameras = None

        if s.audio_enabled and self.mic is not None:
            if self._optional("microphone", self.mic.start) is None and not self.mic.running:
                self.mic = None
        else:
            self.mic = None

        if self.mic is not None and s.listening and s.stt_backend != "none":
            self._optional("listener", self._start_listener)
        elif s.stt_backend == "none":
            log.info("hearing disabled (stt backend 'none'); recognize_keywords still works on the robot")

        if s.reflexes_enabled:
            self._optional("reflexes", self._start_reflexes)

        if s.hand_raise_vision and self.cameras is not None:
            self._optional("hand-raise vision", self._start_vision)

        if s.preview_port:
            self._optional("preview server", self._start_preview)

        for name, obj in (("leds", self.leds), ("tablet", self.tablet)):
            if obj is not None and not self.conn.has_service({"leds": "ALLeds", "tablet": "ALTabletService"}[name]):
                setattr(self, name, None)
        if self.navigator is not None and not self.navigator.available():
            self.navigator = None
        if self.tracker is not None and not self.conn.has_service("ALTracker"):
            self.tracker = None

        self.started_at = time.time()
        self.bus.publish("server.started", {"version": __version__, "robot": info,
                                            "startup_s": round(time.monotonic() - t0, 2)}, source="server")

    def _start_listener(self) -> None:
        from pepper_mcp.speech.listener import Listener
        from pepper_mcp.speech.stt import make_stt
        from pepper_mcp.speech.vad import make_vad

        s = self.settings
        stt = make_stt(s.stt_backend, s.stt_model, s.stt_device, s.stt_compute_type,
                       s.stt_language or None, s.stt_url, s.stt_api_key,
                       download_root=os.path.join(os.getcwd(), ".models") if s.stt_backend == "faster-whisper" else None)
        self.listener = Listener(self.mic, make_vad(s.vad_aggressiveness), stt, self.bus, self.speech,
                                 end_silence_ms=s.vad_end_silence_ms, min_speech_ms=s.vad_min_speech_ms,
                                 max_utterance_s=s.vad_max_utterance_s, echo_tail_ms=s.tts_echo_tail_ms,
                                 barge_in=s.barge_in)
        self.listener.start()

    def _start_reflexes(self) -> None:
        from pepper_mcp.reflexes import ReflexManager, builtin_reflexes

        self.reflexes = ReflexManager(self.bus)
        for reflex in builtin_reflexes(self):
            self.reflexes.register(reflex)
        off = {n.strip() for n in self.settings.reflexes_off.split(",") if n.strip()}
        self.reflexes.start(off)

    def _start_vision(self) -> None:
        from pepper_mcp.vision.hand_raise import FrameAnalyzerLoop, HandRaiseDetector

        detector = HandRaiseDetector()
        if not detector.available:
            raise RobotError("mediapipe not installed (pip install 'pepper-mcp[vision]')")
        self.vision = FrameAnalyzerLoop(self.cameras.get("top"), self.bus, [detector])
        self.vision.start()

    def _start_preview(self) -> None:
        from pepper_mcp.preview import PreviewServer

        self.preview = PreviewServer(self, self.settings.preview_host, self.settings.preview_port)
        self.preview.start()

    def stop(self) -> None:
        log.info("shutting down")
        for label, fn in (
            ("reflexes", lambda: self.reflexes and self.reflexes.stop()),
            ("vision", lambda: self.vision and self.vision.stop()),
            ("listener", lambda: self.listener and self.listener.stop()),
            ("mic", lambda: self.mic and self.mic.close()),
            ("cameras", lambda: self.cameras and self.cameras.stop()),
            ("tracker", lambda: self.tracker and self.tracker.stop()),
            ("navigator", lambda: self.navigator and self.navigator.stop()),
            ("gestures", self.gestures.stop_all),
            ("tasks", self.tasks.cancel_all),
            ("speech", lambda: (self.speech.stop_speaking(), self.speech.stop())),
            ("motion", lambda: (self.motion.restore_defaults(), self.motion.stop())),
            ("perception", lambda: self.perception and self.perception.stop()),
            ("memory", self.memory.stop),
            ("autonomous life", self.awareness.restore_life),
            ("preview", lambda: self.preview and self.preview.stop()),
            ("events", self.bus.shutdown),
            ("connection", self.conn.close),
        ):
            try:
                fn()
            except Exception as exc:
                log.debug("stop %s: %s", label, exc)

    # ------------------------------------------------------------------ status
    def status(self) -> dict[str, Any]:
        people = []
        if self.perception is not None:
            try:
                people = self.perception.people()
            except Exception:
                people = []
        out: dict[str, Any] = {
            "connected": self.conn.is_connected(),
            "fake_robot": self.settings.fake,
            "robot": self.system.info(),
            "uptime_s": round(time.time() - self.started_at, 1) if self.started_at else None,
            "awake": self.motion.is_awake(),
            "posture": self.motion.current_posture(),
            "autonomous_life": self.awareness.life_state(),
            "liveliness_preset": self.awareness.preset,
            "speech": self.speech.status(),
            "listening": self.listener.status() if self.listener else {"enabled": False},
            "people_count": len(people),
            "people": people[:5],
            "tracking": self.tracker.status() if self.tracker else None,
            "navigation": self.navigator.status() if self.navigator else None,
            "reflexes_active": self.reflexes.active_names() if self.reflexes else [],
            "running_tasks": [t.to_dict() for t in self.tasks.running()],
            "last_event_seq": self.bus.last_seq,
            "warnings": self.warnings[-5:],
        }
        if self.perception is not None:
            out["battery"] = self.perception.battery()
            out["sonar"] = self.perception.sonar()
        return out

    def capabilities(self) -> dict[str, Any]:
        try:
            services = self.conn.service_names()
        except Exception:
            services = []
        return {
            "version": __version__,
            "robot": self.system.info(),
            "services": services,
            "features": {
                "camera": self.cameras is not None,
                "microphone": self.mic is not None and self.mic.running,
                "hearing": self.listener is not None and self.listener.enabled,
                "stt_backend": self.listener.stats.get("stt") if self.listener else None,
                "vad": self.listener.stats.get("vad") if self.listener else None,
                "animated_speech": self.conn.has_service("ALAnimatedSpeech"),
                "perception": self.perception.active if self.perception else {},
                "memory_events": self.memory.native_supported,
                "tablet": self.tablet is not None,
                "leds": self.leds is not None,
                "navigation": self.navigator is not None,
                "tracker": self.tracker is not None,
                "reflexes": self.reflexes.active_names() if self.reflexes else [],
                "hand_raise_vision": self.vision is not None,
                "preview_url": self.preview.base_url if self.preview else None,
                "gestures": self.gestures.names(),
            },
            "settings": self.settings.summary(),
            "assets_dir": ASSETS_DIR,
        }

    # ------------------------------------------------------------------ cross-cutting actions
    def stop_everything(self) -> dict[str, Any]:
        """Stop base, animations, gestures, tracking, navigation, speech. Safe to call anytime."""
        report: dict[str, Any] = {}
        report["gestures_stopped"] = self.gestures.stop_all()
        report["tasks_cancelled"] = self.tasks.cancel_all()
        for label, fn in (("base", self.motion.stop_all), ("animations", self.animations.stop),
                          ("tracker", lambda: self.tracker and self.tracker.stop()),
                          ("navigation", lambda: self.navigator and self.navigator.stop()),
                          ("speech", self.speech.stop_speaking),
                          ("audio", lambda: self.audio_out and self.audio_out.stop())):
            try:
                fn()
                report[label] = "stopped"
            except Exception as exc:
                report[label] = f"error: {exc}"
        self.bus.publish("robot.stopped", report, source="server")
        return report
