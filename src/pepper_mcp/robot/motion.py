"""Base, joints, posture and safety (ALMotion / ALRobotPosture).

Conventions used across the MCP surface: the LLM speaks in **degrees** and metres, the
robot in radians. Joint targets are clamped to conservative limits taken from the
teleop code that was validated on the robot.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Any

from pepper_mcp.robot.connection import RobotConnection, RobotError
from pepper_mcp.robot.events import EventBus

log = logging.getLogger(__name__)

# (min, max) in radians – safely inside the hardware limits of Pepper.
JOINT_LIMITS: dict[str, tuple[float, float]] = {
    "HeadYaw": (-2.0, 2.0),
    "HeadPitch": (-0.6, 0.5),          # negative = look up
    "LShoulderPitch": (-2.0, 2.0),     # negative = arm up, positive = down
    "LShoulderRoll": (0.05, 1.5),
    "LElbowYaw": (-2.0, 2.0),
    "LElbowRoll": (-1.5, -0.05),
    "LWristYaw": (-1.8, 1.8),
    "LHand": (0.0, 1.0),               # 1 = open
    "RShoulderPitch": (-2.0, 2.0),
    "RShoulderRoll": (-1.5, -0.05),
    "RElbowYaw": (-2.0, 2.0),
    "RElbowRoll": (0.05, 1.5),
    "RWristYaw": (-1.8, 1.8),
    "RHand": (0.0, 1.0),
    "HipRoll": (-0.45, 0.45),          # + = lean to the robot's left
    "HipPitch": (-0.9, 0.9),           # + = lean forward
    "KneePitch": (-0.45, 0.45),        # - = crouch
}

JOINT_DESCRIPTIONS: dict[str, str] = {
    "HeadYaw": "head left(+)/right(-)",
    "HeadPitch": "head up(-)/down(+)",
    "LShoulderPitch": "left arm up(-)/down(+)",
    "LShoulderRoll": "left arm out(+)/in",
    "LElbowYaw": "left elbow rotation",
    "LElbowRoll": "left elbow bend (more negative = more bent)",
    "LWristYaw": "left wrist rotation",
    "LHand": "left hand 1=open 0=closed",
    "RShoulderPitch": "right arm up(-)/down(+)",
    "RShoulderRoll": "right arm out(-)/in",
    "RElbowYaw": "right elbow rotation",
    "RElbowRoll": "right elbow bend (more positive = more bent)",
    "RWristYaw": "right wrist rotation",
    "RHand": "right hand 1=open 0=closed",
    "HipRoll": "lean left(+)/right(-)",
    "HipPitch": "lean forward(+)/back(-)",
    "KneePitch": "crouch(-)/rise(+)",
}

CHAINS = {"Head": ["HeadYaw", "HeadPitch"],
          "LArm": ["LShoulderPitch", "LShoulderRoll", "LElbowYaw", "LElbowRoll", "LWristYaw", "LHand"],
          "RArm": ["RShoulderPitch", "RShoulderRoll", "RElbowYaw", "RElbowRoll", "RWristYaw", "RHand"],
          "Leg": ["HipRoll", "HipPitch", "KneePitch"]}

POSTURES = ["StandInit", "Stand", "StandZero", "Crouch"]

# Named arm poses in degrees (joint -> deg) for `arm_pose`. "L" prefix; mirrored for right.
ARM_POSES_DEG: dict[str, dict[str, float]] = {
    "down": {"ShoulderPitch": 88, "ShoulderRoll": 8, "ElbowYaw": -70, "ElbowRoll": -25, "WristYaw": 0},
    "up": {"ShoulderPitch": -80, "ShoulderRoll": 12, "ElbowYaw": -70, "ElbowRoll": -20, "WristYaw": 0},
    "forward": {"ShoulderPitch": 10, "ShoulderRoll": 10, "ElbowYaw": -60, "ElbowRoll": -20, "WristYaw": 0},
    "out": {"ShoulderPitch": 20, "ShoulderRoll": 75, "ElbowYaw": -40, "ElbowRoll": -15, "WristYaw": 0},
    "hug": {"ShoulderPitch": 30, "ShoulderRoll": 20, "ElbowYaw": -80, "ElbowRoll": -70, "WristYaw": -30},
    "wave_ready": {"ShoulderPitch": -45, "ShoulderRoll": 25, "ElbowYaw": -50, "ElbowRoll": -80, "WristYaw": 0},
    "high_five": {"ShoulderPitch": -30, "ShoulderRoll": 15, "ElbowYaw": 10, "ElbowRoll": -45, "WristYaw": -60},
}

DEFAULT_ORTHO_SECURITY = 0.4
DEFAULT_TANGENT_SECURITY = 0.1


def deg(rad: float) -> float:
    return round(math.degrees(rad), 1)


def rad(degrees: float) -> float:
    return math.radians(degrees)


def clamp_joint(name: str, value_rad: float) -> float:
    lo, hi = JOINT_LIMITS[name]
    return max(lo, min(hi, value_rad))


def mirror_joint(left_name: str) -> str:
    return "R" + left_name[1:]


def arm_pose_angles(side: str, pose: str) -> dict[str, float]:
    """Return {joint: rad} for ``pose`` on ``side`` (left/right/both)."""
    if pose not in ARM_POSES_DEG:
        raise RobotError(f"unknown arm pose {pose!r}; choose from {sorted(ARM_POSES_DEG)}")
    spec = ARM_POSES_DEG[pose]
    out: dict[str, float] = {}
    sides = ["L", "R"] if side == "both" else ["L" if side == "left" else "R"]
    for s in sides:
        for joint, value in spec.items():
            name = s + joint
            v = value
            if s == "R" and joint in ("ShoulderRoll", "ElbowYaw", "ElbowRoll", "WristYaw"):
                v = -value
            out[name] = clamp_joint(name, rad(v))
    return out


class Motion:
    """Base + joints + posture with a velocity watchdog."""

    MAX_MOVE_DURATION_S = 5.0
    KEEPALIVE_S = 0.25

    def __init__(self, conn: RobotConnection, bus: EventBus):
        self.conn = conn
        self.bus = bus
        self._vel = (0.0, 0.0, 0.0)
        self._vel_until = 0.0
        self._vel_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.security = (DEFAULT_ORTHO_SECURITY, DEFAULT_TANGENT_SECURITY)
        self.owner: str | None = None  # who currently owns the base (follow/nav) – informational

    @property
    def motion(self) -> Any:
        return self.conn.service("ALMotion")

    @property
    def posture(self) -> Any:
        return self.conn.service("ALRobotPosture")

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._watchdog, name="motion-watchdog", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def wake_up(self) -> None:
        self.motion.wakeUp()

    def rest(self) -> None:
        self.stop_base()
        self.motion.rest()

    def is_awake(self) -> bool:
        try:
            return bool(self.motion.robotIsWakeUp())
        except Exception:
            return False

    def prepare(self, posture: str | None = "StandInit") -> None:
        """Wake up, stiffen the joints we drive, go to a neutral posture."""
        self.wake_up()
        for chain in ("Head", "LArm", "RArm"):
            try:
                self.motion.setStiffnesses(chain, 1.0)
            except Exception as exc:
                log.debug("stiffness %s: %s", chain, exc)
        try:
            self.motion.setStiffnesses(CHAINS["Leg"], 1.0)
        except Exception:
            pass
        if posture:
            try:
                self.posture.goToPosture(posture, 0.6)
            except Exception as exc:
                log.warning("goToPosture failed: %s", exc)

    # ------------------------------------------------------------- base
    def move(self, vx: float, vy: float, vtheta: float, duration_s: float) -> dict[str, Any]:
        vx, vy, vtheta = (max(-1.0, min(1.0, float(v))) for v in (vx, vy, vtheta))
        duration_s = max(0.0, min(self.MAX_MOVE_DURATION_S, float(duration_s)))
        with self._vel_lock:
            self._vel = (vx, vy, vtheta)
            self._vel_until = time.monotonic() + duration_s
        self.motion.moveToward(vx, vy, vtheta)
        self.start()
        return {"vx": vx, "vy": vy, "vtheta": vtheta, "duration_s": duration_s}

    def stop_base(self) -> None:
        with self._vel_lock:
            self._vel = (0.0, 0.0, 0.0)
            self._vel_until = 0.0
        try:
            self.motion.stopMove()
        except Exception as exc:
            log.debug("stopMove: %s", exc)

    def move_to_async(self, x: float, y: float, theta_rad: float) -> Any:
        return RobotConnection.call_async(self.motion, "moveTo", float(x), float(y), float(theta_rad))

    def get_pose(self) -> dict[str, float]:
        x, y, th = self.motion.getRobotPosition(True)
        return {"x_m": round(x, 3), "y_m": round(y, 3), "theta_deg": deg(th)}

    def _watchdog(self) -> None:
        last_send = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            with self._vel_lock:
                vel, until = self._vel, self._vel_until
            if vel != (0.0, 0.0, 0.0):
                if now >= until:
                    self.stop_base()
                elif now - last_send >= self.KEEPALIVE_S:
                    try:
                        self.motion.moveToward(*vel)
                    except Exception as exc:
                        log.debug("keepalive moveToward failed: %s", exc)
                    last_send = now
            self._stop.wait(0.05)

    # ------------------------------------------------------------- joints
    def get_angles(self, names: list[str] | None = None) -> dict[str, float]:
        names = names or list(JOINT_LIMITS)
        values = self.motion.getAngles(names, True)
        return dict(zip(names, values, strict=False))

    def set_angles(self, angles_rad: dict[str, float], speed: float = 0.3) -> dict[str, float]:
        names, values = [], []
        for name, value in angles_rad.items():
            if name not in JOINT_LIMITS:
                raise RobotError(f"unknown joint {name!r}; see list_joints")
            names.append(name)
            values.append(clamp_joint(name, float(value)))
        speed = max(0.05, min(1.0, float(speed)))
        self.motion.setAngles(names, values, speed)
        return dict(zip(names, values, strict=False))

    def interpolate_async(self, names: list[str], angle_lists: list[list[float]],
                          time_lists: list[list[float]], absolute: bool = True) -> Any:
        """``angleInterpolation`` as a future – the primitive for keyframe gestures."""
        return RobotConnection.call_async(self.motion, "angleInterpolation", names, angle_lists,
                                          time_lists, absolute)

    def look(self, yaw_deg: float, pitch_deg: float, speed: float = 0.3) -> dict[str, float]:
        return self.set_angles({"HeadYaw": rad(yaw_deg), "HeadPitch": rad(pitch_deg)}, speed)

    def set_hands(self, left_open: bool | None, right_open: bool | None, speed: float = 0.6) -> None:
        angles: dict[str, float] = {}
        if left_open is not None:
            angles["LHand"] = 1.0 if left_open else 0.0
        if right_open is not None:
            angles["RHand"] = 1.0 if right_open else 0.0
        if angles:
            self.set_angles(angles, speed)

    def go_to_posture(self, name: str, speed: float = 0.6) -> bool:
        return bool(self.posture.goToPosture(name, max(0.1, min(1.0, speed))))

    def go_to_posture_async(self, name: str, speed: float = 0.6) -> Any:
        return RobotConnection.call_async(self.posture, "goToPosture", name, max(0.1, min(1.0, speed)))

    def current_posture(self) -> str:
        try:
            return str(self.posture.getPostureFamily())
        except Exception:
            return "unknown"

    # ------------------------------------------------------------- safety
    def set_security_distances(self, ortho: float | None, tangent: float | None) -> dict[str, float]:
        o, t = self.security
        if ortho is not None:
            o = max(0.0, float(ortho))
        if tangent is not None:
            t = max(0.0, float(tangent))
        self.motion.setOrthogonalSecurityDistance(o)
        self.motion.setTangentialSecurityDistance(t)
        self.security = (o, t)
        return {"orthogonal_m": o, "tangential_m": t}

    def set_collision_protection(self, chain: str, enabled: bool) -> None:
        self.motion.setExternalCollisionProtectionEnabled(chain, bool(enabled))

    def get_safety(self) -> dict[str, Any]:
        out: dict[str, Any] = {"orthogonal_m": self.security[0], "tangential_m": self.security[1]}
        for chain in ("Arms", "Move", "All"):
            try:
                out[f"collision_protection_{chain.lower()}"] = bool(
                    self.motion.getExternalCollisionProtectionEnabled(chain))
            except Exception:
                pass
        return out

    def stop_all(self) -> None:
        """Emergency-ish stop: base, pending interpolations."""
        self.stop_base()
        try:
            self.motion.killAll()
        except Exception as exc:
            log.debug("killAll: %s", exc)

    def restore_defaults(self) -> None:
        try:
            self.set_security_distances(DEFAULT_ORTHO_SECURITY, DEFAULT_TANGENT_SECURITY)
        except Exception:
            pass
        self.stop_base()
