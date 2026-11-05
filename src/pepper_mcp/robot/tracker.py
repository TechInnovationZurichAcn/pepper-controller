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


# Closest follow distance that still lets the base drive: the base reaches ~0.2 m ahead of the
# robot origin and NAOqi stops forward moves with anything inside the 0.15 m front safety
# distance, while ALTracker measures to the person's head, ~0.1 m behind their toes.
MIN_FOLLOW_M = 0.5
DEFAULT_FOLLOW_M = MIN_FOLLOW_M


class Tracker:
    FOLLOW_TOL = 0.1
    LOOP_S = 0.4
    LOST_S = 3.0  # nobody perceived for this long while following → follow.lost

    def __init__(self, conn: RobotConnection, bus: EventBus, memory: MemoryWatcher):
        self.conn = conn
        self.bus = bus
        self.memory = memory
        self.active_target: str | None = None
        self.active_mode: str | None = None
        self.following = False
        self.follow_distance = DEFAULT_FOLLOW_M
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._known: set[int] = set()
        self._tracking = False
        self._awareness_paused = False
        self._unseen_since: float | None = None
        self._lost_reported = False
        self._loop_errors = 0

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
        self._pause_awareness()
        tracker = self.tracker
        name = TARGETS[target]
        try:
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
        except Exception:
            self._resume_awareness()  # nothing is tracking: give the head back
            raise
        self.active_target, self.active_mode = target, mode
        return self.status()

    def follow(self, distance_m: float = DEFAULT_FOLLOW_M) -> dict[str, Any]:
        self.stop()
        self._pause_awareness()
        self.follow_distance = max(MIN_FOLLOW_M, min(2.0, float(distance_m)))
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
        self._unseen_since = None
        self._lost_reported = False
        self._loop_errors = 0
        self._stop = threading.Event()  # per run: a stale loop can never be revived by clear()
        self._thread = threading.Thread(target=self._follow_loop, args=(self._stop,), name="follow", daemon=True)
        self._thread.start()
        self.following = True
        self.active_target, self.active_mode = "people", "move"
        self.bus.publish("tracking.changed", self.status(), source="tracker")
        out = self.status()
        if not self._people():
            out["hint"] = ("nobody is visible yet: following starts as soon as someone stands in front of "
                           "Pepper (within ~3 m)")
        return out

    def _people(self) -> list[int]:
        return [int(i) for i in (self.memory.get("PeoplePerception/PeopleList") or [])]

    def _follow_loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self._follow_update(stop)
                self._loop_errors = 0
            except Exception as exc:
                self._loop_errors += 1
                # the first failure in a row is worth seeing; a stuck loop would spam the log
                (log.warning if self._loop_errors == 1 else log.debug)("follow update failed: %s", exc)
            stop.wait(self.LOOP_S)

    def _follow_update(self, stop: threading.Event) -> None:
        ids = self._people()
        if not ids:
            self._note_unseen()
            return
        if self._lost_reported:
            self.bus.publish("follow.found", {"people": ids}, source="tracker")
        self._unseen_since = None
        self._lost_reported = False
        if self._tracking and not self._tracker_active():
            # something else (an animation, awareness, a posture change) stopped ALTracker
            log.info("follow: tracker stopped, re-arming")
            self._tracking = False
        if stop.is_set():  # stop() ran during the slow calls above: never re-engage Move mode
            return
        if set(ids) != self._known:
            self._known = set(ids)
            try:
                self.tracker.registerTarget("People", ids)
            except Exception:
                self.tracker.registerTarget("People", ids[0])
            self._tracking = False
        if not self._tracking and not stop.is_set():
            self.tracker.setMode("Move")
            self.tracker.setRelativePosition([-self.follow_distance, 0.0, 0.0,
                                              self.FOLLOW_TOL, self.FOLLOW_TOL, self.FOLLOW_TOL])
            self.tracker.track("People")
            self._tracking = True

    def _tracker_active(self) -> bool:
        try:
            return bool(self.tracker.isActive())
        except Exception:
            return True  # can't tell: don't re-register every loop

    def _note_unseen(self) -> None:
        now = time.monotonic()
        if self._unseen_since is None:
            self._unseen_since = now
        elif not self._lost_reported and now - self._unseen_since >= self.LOST_S:
            self._lost_reported = True
            self.bus.publish("follow.lost", {"unseen_s": round(now - self._unseen_since, 1)}, source="tracker")

    # ------------------------------------------------------------- basic awareness
    # ALBasicAwareness drives the head through ALTracker as well: left running it re-targets the
    # tracker to whoever it engages and the follow/track target is dropped.
    def _pause_awareness(self) -> None:
        ba = self.conn.try_service("ALBasicAwareness")
        if ba is None:
            return
        try:
            if ba.isAwarenessRunning():
                ba.pauseAwareness()
                self._awareness_paused = True
        except Exception as exc:
            log.warning("could not pause basic awareness, it may fight the tracker: %s", exc)

    def _resume_awareness(self) -> None:
        if not self._awareness_paused:
            return
        self._awareness_paused = False
        ba = self.conn.try_service("ALBasicAwareness")
        if ba is None:
            return
        try:
            ba.resumeAwareness()
        except Exception as exc:
            log.warning("could not resume basic awareness: %s", exc)

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            if self._thread.is_alive():
                log.warning("follow loop still in a NAOqi call; it will exit without re-arming the tracker")
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
        self._resume_awareness()
        if was_active:
            self.bus.publish("tracking.changed", self.status(), source="tracker")
        return self.status()

    def status(self) -> dict[str, Any]:
        out = {"following": self.following, "target": self.active_target, "mode": self.active_mode,
               "follow_distance_m": self.follow_distance}
        if self.following:
            out["person_lost"] = self._lost_reported
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
