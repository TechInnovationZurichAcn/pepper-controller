"""Moving the base, head, arms and joints."""

from __future__ import annotations

import math
from typing import Annotated, Any, Literal

from pydantic import Field

from pepper_mcp.robot.motion import (
    ARM_POSES_DEG,
    JOINT_DESCRIPTIONS,
    JOINT_LIMITS,
    POSTURES,
    arm_pose_angles,
    deg,
    rad,
)
from pepper_mcp.tools._common import ACTION, MOVES_ROBOT, READ_ONLY, require, task_result, tool

DIRECTIONS = {"left": (45, 0), "right": (-45, 0), "slightly_left": (20, 0), "slightly_right": (-20, 0),
              "up": (0, -25), "down": (0, 25), "center": (0, 0), "forward": (0, 0), "up_left": (35, -20),
              "up_right": (-35, -20), "down_left": (30, 20), "down_right": (-30, 20)}


def register(mcp, rt) -> None:  # type: ignore[no-untyped-def]
    def _check_base_free() -> None:
        if rt.tracker and rt.tracker.following:
            raise ValueError("follow_person is active and owns the base; disable it first")
        if rt.navigator and rt.navigator.busy:
            raise ValueError("navigation owns the base; stop_navigation first")

    @tool(mcp, MOVES_ROBOT)
    def move(vx: Annotated[float, Field(ge=-1, le=1, description="forward(+)/back(-) fraction of max speed")] = 0.0,
             vy: Annotated[float, Field(ge=-1, le=1, description="left(+)/right(-) strafe")] = 0.0,
             vtheta: Annotated[float, Field(ge=-1, le=1, description="rotate left(+)/right(-)")] = 0.0,
             duration_s: Annotated[float, Field(gt=0, le=5, description="auto-stop after this many seconds")] = 1.0
             ) -> dict[str, Any]:
        """Drive the wheeled base at a velocity for a short time (max 5 s, auto-stops). Max speed is
        ~0.35 m/s. Use small values near people. For precise displacements use move_to. If NAOqi
        blocks the move (obstacle in the safety distance) a motion.blocked event is published."""
        _check_base_free()
        return rt.motion.move(vx, vy, vtheta, duration_s)

    @tool(mcp, MOVES_ROBOT)
    def move_to(x_m: Annotated[float, Field(ge=-3, le=3, description="metres forward(+)/back(-)")] = 0.0,
                y_m: Annotated[float, Field(ge=-3, le=3, description="metres left(+)/right(-)")] = 0.0,
                theta_deg: Annotated[float, Field(ge=-360, le=360, description="final rotation, left positive")] = 0.0,
                wait: bool = False) -> dict[str, Any]:
        """Move a relative distance using odometry (ALMotion.moveTo). Background task unless wait.
        The result (or task.finished) says how far the robot really moved; ok=false with the reason
        when NAOqi stopped it early (usually an obstacle within the safety distance)."""
        _check_base_free()
        task = rt.tasks.run("move_to", f"moveTo({x_m}, {y_m}, {theta_deg}deg)",
                            lambda: rt.motion.move_relative(x_m, y_m, rad(theta_deg)), cancel=rt.motion.stop_base)
        return task_result(task, wait, 60)

    @tool(mcp, MOVES_ROBOT)
    def turn(degrees: Annotated[float, Field(ge=-360, le=360, description="left positive")], wait: bool = False) -> dict[str, Any]:
        """Turn in place by an angle (left positive), e.g. 180 to turn around. Reports the angle
        actually turned; ok=false with the reason if NAOqi stopped it early."""
        _check_base_free()
        task = rt.tasks.run("turn", f"turn {degrees}deg",
                            lambda: rt.motion.move_relative(0.0, 0.0, rad(degrees)), cancel=rt.motion.stop_base)
        return task_result(task, wait, 30)

    @tool(mcp, ACTION)
    def go_to_posture(name: Literal["StandInit", "Stand", "StandZero", "Crouch"] = "StandInit",
                      speed: Annotated[float, Field(gt=0, le=1)] = 0.6, wait: bool = False) -> dict[str, Any]:
        """Go to a predefined whole-body posture. StandInit is the neutral ready pose."""
        if name not in POSTURES:
            raise ValueError(f"posture must be one of {POSTURES}")
        fut = rt.motion.go_to_posture_async(name, speed)
        return task_result(rt.tasks.from_future("posture", name, fut), wait, 20)

    @tool(mcp, ACTION)
    def look(yaw_deg: Annotated[float, Field(ge=-115, le=115, description="left positive")],
             pitch_deg: Annotated[float, Field(ge=-35, le=28, description="up negative, down positive")] = 0.0,
             speed: Annotated[float, Field(gt=0, le=1)] = 0.3) -> dict[str, Any]:
        """Turn the head to absolute angles."""
        angles = rt.motion.look(yaw_deg, pitch_deg, speed)
        return {k: deg(v) for k, v in angles.items()}

    @tool(mcp, ACTION)
    def look_toward(direction: Literal["left", "right", "slightly_left", "slightly_right", "up", "down", "center",
                                       "up_left", "up_right", "down_left", "down_right"],
                    speed: Annotated[float, Field(gt=0, le=1)] = 0.3) -> dict[str, Any]:
        """Turn the head in a named direction."""
        yaw, pitch = DIRECTIONS[direction]
        return {k: deg(v) for k, v in rt.motion.look(yaw, pitch, speed).items()}

    @tool(mcp, ACTION)
    def look_at_person(person_id: int | None = None) -> dict[str, Any]:
        """Look at a perceived person (nearest one if no id). Uses ALTracker.lookAt."""
        perception = require(rt.perception, "perception")
        tracker = require(rt.tracker, "tracker")
        info = perception.person_info(person_id) if person_id is not None else perception.nearest_person()
        if not info or ("position_m" not in info and "bearing_deg" not in info):
            raise ValueError("I don't see anyone to look at")
        ok = tracker.look_at_person(info.get("position_m"), info.get("bearing_deg"))
        return {"looked": ok, "person": info}

    @tool(mcp, ACTION)
    def look_at_point(x: float, y: float, z: float = 1.2, frame: Literal["robot", "world", "torso"] = "robot",
                      speed: Annotated[float, Field(gt=0, le=1)] = 0.5, whole_body: bool = False) -> dict[str, Any]:
        """Look at a 3D point (metres; robot frame: x forward, y left, z up)."""
        require(rt.tracker, "tracker").look_at(x, y, z, frame, speed, whole_body)
        return {"target": [x, y, z], "frame": frame}

    @tool(mcp, ACTION)
    def point_at(x: float, y: float, z: float = 1.0, effector: Literal["RArm", "LArm", "Arms"] = "RArm",
                 frame: Literal["robot", "world", "torso"] = "robot",
                 speed: Annotated[float, Field(gt=0, le=1)] = 0.5) -> dict[str, Any]:
        """Point an arm at a 3D point."""
        require(rt.tracker, "tracker").point_at(x, y, z, effector, frame, speed)
        return {"target": [x, y, z], "effector": effector}

    @tool(mcp, READ_ONLY)
    def get_joint_angles(names: list[str] | None = None) -> dict[str, float]:
        """Current joint angles in degrees (all controllable joints when names is omitted)."""
        return {k: deg(v) for k, v in rt.motion.get_angles(names).items()}

    @tool(mcp, ACTION)
    def set_joint_angles(angles: Annotated[dict[str, float], Field(description="{joint_name: degrees}")],
                         speed: Annotated[float, Field(gt=0, le=1, description="fraction of max joint speed")] = 0.3
                         ) -> dict[str, float]:
        """Set joints to absolute angles (degrees), clamped to safe limits. See list_joints."""
        result = rt.motion.set_angles({k: rad(v) for k, v in angles.items()}, speed)
        return {k: deg(v) for k, v in result.items()}

    @tool(mcp, READ_ONLY)
    def list_joints() -> dict[str, Any]:
        """Controllable joints with safe limits (degrees) and meaning."""
        return {"joints": [{"name": n, "min_deg": deg(lo), "max_deg": deg(hi), "meaning": JOINT_DESCRIPTIONS.get(n, "")}
                for n, (lo, hi) in JOINT_LIMITS.items()]}

    @tool(mcp, ACTION)
    def arm_pose(side: Literal["left", "right", "both"],
                 pose: Literal["down", "up", "forward", "out", "hug", "wave_ready", "high_five"],
                 speed: Annotated[float, Field(gt=0, le=1)] = 0.4) -> dict[str, float]:
        """Put an arm (or both) into a named pose."""
        if pose not in ARM_POSES_DEG:
            raise ValueError(f"pose must be one of {sorted(ARM_POSES_DEG)}")
        result = rt.motion.set_angles(arm_pose_angles(side, pose), speed)
        return {k: deg(v) for k, v in result.items()}

    @tool(mcp, ACTION)
    def set_hands(left_open: bool | None = None, right_open: bool | None = None) -> dict[str, Any]:
        """Open (true) or close (false) the hands. Omit a side to leave it unchanged."""
        rt.motion.set_hands(left_open, right_open)
        return {"left_open": left_open, "right_open": right_open}

    @tool(mcp, READ_ONLY)
    def get_pose() -> dict[str, float]:
        """Odometry pose of the base since start (x, y in metres, theta in degrees)."""
        return rt.motion.get_pose()

    @tool(mcp, ACTION)
    def set_safety(orthogonal_m: Annotated[float | None, Field(ge=0, le=1, description="front safety distance in m")] = None,
                   tangential_m: Annotated[float | None, Field(ge=0, le=1, description="side safety distance in m")] = None,
                   arm_collision_protection: bool | None = None,
                   move_collision_protection: bool | None = None) -> dict[str, Any]:
        """Adjust NAOqi safety. Base moves stop when anything is closer than the security distances
        (server default 0.15 front / 0.05 side, NAOqi default 0.4 / 0.1): lower them if moves keep
        getting blocked, raise them in crowded spaces. Collision protection stops arms/base when
        something is detected in the way."""
        if orthogonal_m is not None or tangential_m is not None:
            rt.motion.set_security_distances(orthogonal_m, tangential_m)
        if arm_collision_protection is not None:
            rt.motion.set_collision_protection("Arms", arm_collision_protection)
        if move_collision_protection is not None:
            rt.motion.set_collision_protection("Move", move_collision_protection)
        return rt.motion.get_safety()

    @tool(mcp, ACTION)
    def set_body_lean(hip_pitch_deg: Annotated[float, Field(ge=-50, le=50, description="forward positive")] = 0.0,
                      hip_roll_deg: Annotated[float, Field(ge=-25, le=25, description="lean left positive")] = 0.0,
                      knee_pitch_deg: Annotated[float, Field(ge=-25, le=25, description="crouch negative")] = 0.0,
                      speed: Annotated[float, Field(gt=0, le=1)] = 0.2) -> dict[str, float]:
        """Lean / crouch with the hip and knee (expressive body posture)."""
        result = rt.motion.set_angles({"HipPitch": rad(hip_pitch_deg), "HipRoll": rad(hip_roll_deg),
                                       "KneePitch": rad(knee_pitch_deg)}, speed)
        return {k: deg(v) for k, v in result.items()}

    _ = math  # keep import for future use
