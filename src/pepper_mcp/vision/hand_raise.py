"""Raised-hand detection on camera frames (experimental, optional).

Pepper's own ``ALWavingDetection`` (3D sensor) is the primary trigger for the
``high_five`` reflex. This module adds a laptop-side detector for firmwares where that
extractor is missing or when a hand is simply *held up* without waving. It uses
MediaPipe pose landmarks when ``mediapipe`` is installed; otherwise it stays idle and
reports itself as unavailable.

A frame analyser is just ``analyse(frame) -> list[event dicts]``; plug in others the
same way (e.g. a face-recognition model) through :class:`FrameAnalyzerLoop`.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Protocol

from pepper_mcp.robot.camera import CameraStream, Frame
from pepper_mcp.robot.events import EventBus

log = logging.getLogger(__name__)


class FrameAnalyzer(Protocol):
    name: str

    def analyse(self, frame: Frame) -> list[dict[str, Any]]: ...


class HandRaiseDetector:
    name = "hand_raise"

    def __init__(self, cooldown_s: float = 3.0):
        self.cooldown_s = cooldown_s
        self._last_fire = 0.0
        self._pose = None
        self.available = False
        try:
            import mediapipe as mp

            self._mp = mp
            solutions = getattr(mp, "solutions", None)
            if solutions is not None and hasattr(solutions, "pose"):
                self._pose = solutions.pose.Pose(static_image_mode=False, model_complexity=0,
                                                 min_detection_confidence=0.5)
                self.available = True
            else:
                log.info("mediapipe present but the legacy pose solution is missing; hand-raise vision off")
        except Exception as exc:
            log.info("mediapipe not available (%s); hand-raise vision off", exc)

    def analyse(self, frame: Frame) -> list[dict[str, Any]]:
        if not self.available or frame.colorspace == 17:
            return []
        import numpy as np

        img = np.frombuffer(frame.data, dtype=np.uint8).reshape(frame.height, frame.width, 3)
        result = self._pose.process(img)
        lm = getattr(result, "pose_landmarks", None)
        if lm is None:
            return []
        pts = lm.landmark
        events = []
        now = time.time()
        # MediaPipe indices: 11/12 shoulders, 15/16 wrists (image y grows downward)
        for side, wrist_i, shoulder_i in (("left", 15, 11), ("right", 16, 12)):
            wrist, shoulder = pts[wrist_i], pts[shoulder_i]
            if wrist.visibility > 0.5 and shoulder.visibility > 0.5 and wrist.y < shoulder.y - 0.05:
                if now - self._last_fire >= self.cooldown_s:
                    self._last_fire = now
                    events.append({"type": "gesture.hand_raised", "data": {"side": side,
                                                                            "confidence": round(float(wrist.visibility), 2)}})
        return events


class FrameAnalyzerLoop:
    """Runs analysers on the newest frame of a camera at a modest rate."""

    def __init__(self, stream: CameraStream, bus: EventBus, analyzers: list[FrameAnalyzer], fps: float = 5.0):
        self.stream = stream
        self.bus = bus
        self.analyzers = analyzers
        self.period = 1.0 / fps
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_seq = -1

    def start(self) -> None:
        if self._thread or not self.analyzers:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="vision", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            frame = self.stream.latest()
            if frame is not None and frame.seq != self._last_seq:
                self._last_seq = frame.seq
                for analyzer in self.analyzers:
                    try:
                        for ev in analyzer.analyse(frame):
                            self.bus.publish(ev["type"], ev.get("data", {}), source=f"vision.{analyzer.name}")
                    except Exception as exc:
                        log.debug("analyzer %s failed: %s", analyzer.name, exc)
            self._stop.wait(self.period)
