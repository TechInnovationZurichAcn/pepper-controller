"""Wires every subsystem together and owns the start/stop order.

The :class:`Runtime` is what tool modules receive (``register(mcp, rt)``). Any attribute
may be ``None`` when the matching NAOqi service is missing on the robot or the feature
was disabled in :class:`pepper_mcp.config.Settings`; tools check and report that.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any
from urllib.parse import urlparse

from pepper_mcp import __version__
from pepper_mcp.config import Settings
from pepper_mcp.games.tictactoe import TicTacToe
from pepper_mcp.robot.animation import Animations
from pepper_mcp.robot.asr import OnRobotASR
from pepper_mcp.robot.audio_in import MicrophoneStream
from pepper_mcp.robot.audio_out import ASSETS_DIR, AudioOut
from pepper_mcp.robot.awareness import Awareness
from pepper_mcp.robot.camera import Cameras
from pepper_mcp.robot.connection import RobotConnection
from pepper_mcp.robot.events import EventBus, MemoryWatcher
from pepper_mcp.robot.gestures import GestureContext, GestureRegistry
from pepper_mcp.robot.leds import Leds
from pepper_mcp.robot.motion import HATCH_OPEN, Motion
from pepper_mcp.robot.navigation import Navigator
from pepper_mcp.robot.perception import Perception
from pepper_mcp.robot.speech import Speech
from pepper_mcp.robot.system import System
from pepper_mcp.robot.tablet import Tablet
from pepper_mcp.robot.tablet_relay import TabletRelay
from pepper_mcp.robot.tasks import TaskRegistry
from pepper_mcp.robot.tracker import Tracker
from pepper_mcp.tablet_home import TabletHome

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
        self.mic: MicrophoneStream | None = MicrophoneStream(conn, settings.audio_rate, settings.mic)
        #: the voice agent that owns the mic stream, e.g. "Gemini Live (<model>)"; set by the bridge
        self.voice_agent: str | None = None
        #: muted from the dashboard: the voice agent gets silence instead of mic audio
        self.mic_muted = False
        #: the conversation as the voice agent reports it: {"role": "user"|"robot", "text", "at"}
        self.transcript: deque[dict[str, Any]] = deque(maxlen=200)
        self.reflexes = None
        self.preview = None
        #: forwards the head's tablet-side address to the preview server (see robot/tablet_relay.py)
        self.tablet_relay: TabletRelay | None = None
        self.gestures = GestureRegistry(self._gesture_context)
        self.tictactoe = TicTacToe(self.bus)
        #: gesture buttons pinned to the tablet from the dashboard (its home screen)
        self.tablet_home = TabletHome(self)
        self.bus.add_sync_hook(self.tablet_home.on_event)
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
        # Not cached: the relay can die or come back, and the URL is cheap to rebuild.
        base = self.tablet_base_url()
        if base:
            return f"{base}/assets/{filename}"
        with self._lock:
            if filename in self._asset_urls:
                return self._asset_urls[filename]
        url = None
        if self.audio_out is not None and not self.settings.fake:
            url = self.audio_out.deploy_web_asset(filename)  # scp once, then the cached path
        with self._lock:
            self._asset_urls[filename] = url
        return url

    def tablet_base_url(self) -> str | None:
        """Where Pepper's tablet reaches the preview server, or ``None`` when it cannot.

        The tablet is not on the LAN: it goes through the relay on the head, or straight to the
        laptop only when its own Wi-Fi is connected. The preview server's LAN URL alone shows a
        blank page on the tablet.
        """
        if self.preview is None or not self.preview.base_url or self.tablet is None:
            return None
        if self.tablet_relay is not None:
            url = self.tablet_relay.url()  # never blocks; a dead relay restarts in the background
            if url:
                return url
        return self.preview.base_url if self.tablet.wifi_connected() else None

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
        self._optional("push recovery off", self.motion.disable_push_recovery)
        self.motion.start()
        if self.motion.hatch_open():
            self._warn(f"{HATCH_OPEN}: Pepper can't drive, turn or follow")
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

        if s.reflexes_enabled:
            self._optional("reflexes", self._start_reflexes)

        if s.preview_port:
            self._optional("preview server", self._start_preview)

        for name, obj in (("leds", self.leds), ("tablet", self.tablet)):
            if obj is not None and not self.conn.has_service({"leds": "ALLeds", "tablet": "ALTabletService"}[name]):
                setattr(self, name, None)
        if self.preview is not None and self.tablet is not None and not s.fake:
            self._optional("tablet relay", self._start_tablet_relay)
        if self.navigator is not None and not self.navigator.available():
            self.navigator = None
        if self.tracker is not None and not self.conn.has_service("ALTracker"):
            self.tracker = None

        self.started_at = time.time()
        self.bus.publish("server.started", {"version": __version__, "robot": info,
                                            "startup_s": round(time.monotonic() - t0, 2)}, source="server")

    def _start_reflexes(self) -> None:
        from pepper_mcp.reflexes import ReflexManager, builtin_reflexes

        self.reflexes = ReflexManager(self.bus)
        for reflex in builtin_reflexes(self):
            self.reflexes.register(reflex)
        off = {n.strip() for n in self.settings.reflexes_off.split(",") if n.strip()}
        self.reflexes.start(off)

    def _start_preview(self) -> None:
        from pepper_mcp.preview import PreviewServer

        self.preview = PreviewServer(self, self.settings.preview_host, self.settings.preview_port)
        self.preview.start()

    def _start_tablet_relay(self) -> None:
        try:
            listen_ip = str(self.tablet.tablet.robotIp() or "") or "198.18.0.1"
        except Exception:
            listen_ip = "198.18.0.1"
        relay = TabletRelay(self.settings.ip or "", self.settings.ssh_user, self.settings.ssh_password,
                            listen_ip, self.preview.port, urlparse(self.preview.base_url).hostname or "",
                            self.preview.port)
        self.tablet_relay = relay
        relay.ensure()

    def stop(self) -> None:
        log.info("shutting down")
        for label, fn in (
            ("reflexes", lambda: self.reflexes and self.reflexes.stop()),
            ("mic", lambda: self.mic and self.mic.close()),
            ("cameras", lambda: self.cameras and self.cameras.stop()),
            ("tracker", lambda: self.tracker and self.tracker.stop()),
            ("navigator", lambda: self.navigator and self.navigator.stop()),
            ("gestures", self.gestures.stop_all),
            ("games", self.tictactoe.shutdown),
            ("tablet home", self.tablet_home.shutdown),
            ("tasks", self.tasks.cancel_all),
            ("speech", lambda: (self.speech.stop_speaking(), self.speech.stop())),
            ("motion", lambda: (self.motion.restore_defaults(), self.motion.stop())),
            ("perception", lambda: self.perception and self.perception.stop()),
            ("memory", self.memory.stop),
            ("autonomous life", self.awareness.restore_life),
            ("tablet relay", lambda: self.tablet_relay and self.tablet_relay.stop()),
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
            "charging_hatch_open": self.motion.hatch_open(),
            "autonomous_life": self.awareness.life_state(),
            "liveliness_preset": self.awareness.preset,
            "speech": self.speech.status(),
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
                "voice_agent": self.voice_agent,
                "mic_muted": self.mic_muted,
                "animated_speech": self.conn.has_service("ALAnimatedSpeech"),
                "perception": self.perception.active if self.perception else {},
                "memory_events": self.memory.native_supported,
                "tablet": self.tablet is not None,
                "leds": self.leds is not None,
                "navigation": self.navigator is not None,
                "tracker": self.tracker is not None,
                "reflexes": self.reflexes.active_names() if self.reflexes else [],
                "preview_url": self.preview.base_url if self.preview else None,
                "tablet_url": self.tablet_relay.base_url if self.tablet_relay and self.tablet_relay.alive() else None,
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
