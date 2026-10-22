"""ALTracker: follow a person (Move mode), track a face/people/sound with the head or
body, look at / point at 3D targets.

Port of the follow-me logic validated on the robot: register the *actual* person IDs
first, then (re)apply Move mode + relative position, then ``track`` – registering a
target resets the tracker to head-only.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Any

from pepper_mcp.robot.connection import RobotConnection, RobotError
from pepper_mcp.robot.events import EventBus, MemoryWatcher

log = logging.getLogger(__name__)

MODES = {"head": "Head", "body_rotation": "BodyRotation", "whole_body": "WholeBody", "move": "Move"}
TARGETS = {"face": "Face", "people": "People", "sound": "Sound", "red_ball": "RedBall"}
FRAMES = {"torso": 0, "world": 1, "robot": 2}


class Tracker:
    FOLLOW_TOL = 0.1
    LOOP_S = 0.4

    def __init__(self, conn: RobotConnection, bus: EventBus, memory: MemoryWatcher):
        self.conn = conn
        self.bus = bus
        self.memory = memory
        self.active_target: str | None = None
        self.active_mode: str | None = None
        self.following = False
        self.follow_distance = 0.6
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._known: set[int] = set()
        self._tracking = False

    @property
    def tracker(self) -> Any:
        return self.conn.service("ALTracker")

    # ------------------------------------------------------------- generic tracking
    def track(self, target: str, mode: str = "head") -> dict[str, Any]:
        if target not in TARGETS:
            raise RobotError(f"target must be one of {sorted(TARGETS)}")
        if mode not in MODES:
            raise RobotError(f"mode must be one of {sorted(MODES)}")
        self.stop()
        tracker = self.tracker
        name = TARGETS[target]
        if name == "Face":
            tracker.registerTarget(name, 0.15)
        elif name == "People":
            ids = [int(i) for i in (self.memory.get("PeoplePerception/PeopleList") or [])]
            tracker.registerTarget(name, ids if ids else [])
        elif name == "Sound":
            tracker.registerTarget(name, [0.8, 0.3])  # [distance, confidence]
        else:
            tracker.registerTarget(name, 0.06)
        tracker.setMode(MODES[mode])
        tracker.track(name)
        self.active_target, self.active_mode = target, mode
        return self.status()

    def follow(self, distance_m: float = 0.6) -> dict[str, Any]:
        self.stop()
        self.follow_distance = max(0.3, min(2.0, float(distance_m)))
        people = self.conn.try_service("ALPeoplePerception")
        if people is not None:
            try:
                people.subscribe("pepper_mcp_follow")
                people.setFastModeEnabled(True)
            except Exception:
                pass
        try:
            self.tracker.setMaximumDistanceDetection(3.0)
        except Exception:
            pass
        self._known = set()
        self._tracking = False
        self._stop.clear()
        self._thread = threading.Thread(target=self._follow_loop, name="follow", daemon=True)
        self._thread.start()
        self.following = True
        self.active_target, self.active_mode = "people", "move"
        self.bus.publish("tracking.changed", self.status(), source="tracker")
        return self.status()

    def _follow_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._follow_update()
            except Exception as exc:
                log.debug("follow update: %s", exc)
            self._stop.wait(self.LOOP_S)

    def _follow_update(self) -> None:
        ids = [int(i) for i in (self.memory.get("PeoplePerception/PeopleList") or [])]
        if not ids:
            return
        if set(ids) != self._known:
            self._known = set(ids)
            try:
                self.tracker.registerTarget("People", ids)
            except Exception:
                self.tracker.registerTarget("People", ids[0])
            self._tracking = False
        if not self._tracking:
            self.tracker.setMode("Move")
            self.tracker.setRelativePosition([-self.follow_distance, 0.0, 0.0,
                                              self.FOLLOW_TOL, self.FOLLOW_TOL, self.FOLLOW_TOL])
            self.tracker.track("People")
            self._tracking = True

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        was_active = self.following or self.active_target is not None
        try:
            tracker = self.tracker
            tracker.stopTracker()
            tracker.unregisterAllTargets()
            try:
                tracker.setEffector("None")
            except Exception:
                pass
        except Exception as exc:
            log.debug("tracker stop: %s", exc)
        people = self.conn.try_service("ALPeoplePerception")
        if people is not None and self.following:
            try:
                people.unsubscribe("pepper_mcp_follow")
            except Exception:
                pass
        self.following = False
        self.active_target = self.active_mode = None
        if was_active:
            self.bus.publish("tracking.changed", self.status(), source="tracker")
        return self.status()

    def status(self) -> dict[str, Any]:
        out = {"following": self.following, "target": self.active_target, "mode": self.active_mode,
               "follow_distance_m": self.follow_distance}
        try:
            out["robot_tracking"] = bool(self.tracker.isActive())
        except Exception:
            pass
        return out

    # ------------------------------------------------------------- look / point
    def look_at(self, x: float, y: float, z: float, frame: str = "robot", speed: float = 0.5,
                whole_body: bool = False) -> None:
        self.tracker.lookAt([float(x), float(y), float(z)], FRAMES.get(frame, 2), float(speed), bool(whole_body))

    def point_at(self, x: float, y: float, z: float, effector: str = "RArm", frame: str = "robot",
                 speed: float = 0.5) -> None:
        self.tracker.pointAt(effector, [float(x), float(y), float(z)], FRAMES.get(frame, 2), float(speed))

    def look_at_person(self, position: list[float] | None, bearing_deg: float | None = None) -> bool:
        if position and len(position) >= 3:
            self.look_at(position[0], position[1], position[2] or 1.2, "robot", 0.6)
            return True
        if bearing_deg is not None:
            d = 1.5
            a = math.radians(bearing_deg)
            self.look_at(d * math.cos(a), d * math.sin(a), 1.2, "robot", 0.6)
            return True
        return False

    def look_at_sound(self, azimuth_deg: float, elevation_deg: float = 0.0, distance: float = 1.5) -> None:
        a, e = math.radians(azimuth_deg), math.radians(elevation_deg)
        self.look_at(distance * math.cos(a) * math.cos(e), distance * math.sin(a) * math.cos(e),
                     1.1 + distance * math.sin(e), "robot", 0.7)
        time.sleep(0)  # yield
