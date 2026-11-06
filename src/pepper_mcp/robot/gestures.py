"""Custom gestures: keyframe animations and procedural (time-driven) motions.

Two kinds, same interface (:class:`Gesture.play`):

* :class:`KeyframeGesture` – a list of ``(t_s, {joint: degrees})`` keyframes played with
  ``ALMotion.angleInterpolation``. Cheap to author; the LLM can add new ones at runtime
  through the ``define_gesture`` tool.
* :class:`ProceduralGesture` – a Python function ``step(t) -> {joint: rad}`` evaluated
  every ``tick_s`` (used for the 67 emote and the clown dance ported from the teleop code).

All gestures run on a background thread tracked by the task registry and respect a
``stop_event`` so ``stop_motion`` interrupts them.
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from pepper_mcp.robot.audio_out import ASSETS_DIR
from pepper_mcp.robot.connection import RobotError
from pepper_mcp.robot.motion import JOINT_LIMITS, Motion, arm_pose_angles, clamp_joint, rad
from pepper_mcp.robot.qianim import Timeline, parse_qianim, to_bezier

log = logging.getLogger(__name__)


@dataclass
class Keyframe:
    t_s: float
    joints: dict[str, float]  # degrees


@dataclass
class Gesture:
    name: str
    description: str
    duration_s: float
    tags: list[str] = field(default_factory=list)
    builtin: bool = True

    def play(self, ctx: GestureContext, stop_event: threading.Event, params: dict[str, Any]) -> Any:
        raise NotImplementedError

    @property
    def has_music(self) -> bool:
        """Plays music through the speakers (the voice agent keeps the mic muted meanwhile)."""
        return "music" in self.tags

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "duration_s": round(self.duration_s, 1), "tags": self.tags,
                "builtin": self.builtin, "kind": self.__class__.__name__}


@dataclass
class GestureContext:
    """What a gesture may touch. Kept small so gestures stay testable."""
    motion: Motion
    say: Callable[[str], Any] | None = None
    leds: Any = None
    tablet: Any = None
    audio: Any = None
    perception: Any = None
    bus: Any = None
    assets_url: Callable[[str], str | None] | None = None
    disable_arm_collision: bool = True


@contextmanager
def _arms_collision_off(ctx: GestureContext, needed: bool) -> Iterator[None]:
    """External arm collision protection off for the duration (if the gesture needs it and the
    context allows it); always switched back on, even when the motion raises."""
    off = needed and ctx.disable_arm_collision
    if off:
        try:
            ctx.motion.set_collision_protection("Arms", False)
        except Exception as exc:
            log.debug("collision protection off failed: %s", exc)
    try:
        yield
    finally:
        if off:
            try:
                ctx.motion.set_collision_protection("Arms", True)
            except Exception as exc:
                log.warning("could not re-enable arm collision protection: %s", exc)


def _wait_motion(ctx: GestureContext, fut: Any, stop_event: threading.Event, deadline: float,
                 on_tick: Callable[[], None] | None = None) -> bool:
    """Wait for an interpolation future; True when ``stop_event`` interrupted it."""
    while not fut.isFinished():
        if stop_event.is_set():
            try:
                fut.cancel()
            except Exception:
                pass
            ctx.motion.stop_all()
            return True
        if on_tick is not None:
            on_tick()
        if time.monotonic() > deadline:
            break
        time.sleep(0.02)
    return False


def _return_to_rest(ctx: GestureContext) -> None:
    try:
        ctx.motion.go_to_posture("StandInit", 0.5)
    except Exception as exc:
        log.debug("return to rest: %s", exc)


@dataclass
class KeyframeGesture(Gesture):
    keyframes: list[Keyframe] = field(default_factory=list)
    return_to_rest: bool = True
    speech: str | None = None  # optional phrase spoken at the start
    # External arm collision protection freezes an arm that rises toward someone standing in
    # front of the robot, so gestures made *for* that person stall halfway up.
    needs_arm_collision_off: bool = False

    def __post_init__(self) -> None:
        self.validate()
        self.duration_s = (self.keyframes[-1].t_s if self.keyframes else 0.0) + (1.5 if self.return_to_rest else 0.0)

    def validate(self) -> None:
        last = 0.0
        if not self.keyframes:
            raise RobotError("a gesture needs at least one keyframe")
        for kf in self.keyframes:
            if kf.t_s <= last and kf is not self.keyframes[0]:
                raise RobotError("keyframe times must increase")
            if kf.t_s <= 0:
                raise RobotError("keyframe times must be > 0 seconds")
            last = kf.t_s
            for joint in kf.joints:
                if joint not in JOINT_LIMITS:
                    raise RobotError(f"unknown joint {joint!r}")

    def play(self, ctx: GestureContext, stop_event: threading.Event, params: dict[str, Any]) -> Any:
        if self.speech and ctx.say:
            ctx.say(self.speech)
        names = sorted({j for kf in self.keyframes for j in kf.joints})
        current = ctx.motion.get_angles(names)
        angle_lists: list[list[float]] = []
        time_lists: list[list[float]] = []
        for name in names:
            angles, times = [], []
            prev = current[name]
            for kf in self.keyframes:
                if name in kf.joints:
                    prev = clamp_joint(name, rad(kf.joints[name]))
                angles.append(prev)
                times.append(kf.t_s)
            angle_lists.append(angles)
            time_lists.append(times)
        with _arms_collision_off(ctx, self.needs_arm_collision_off):
            fut = ctx.motion.interpolate_async(names, angle_lists, time_lists, True)
            if _wait_motion(ctx, fut, stop_event, time.monotonic() + self.keyframes[-1].t_s + 2.0):
                return {"interrupted": True}
            # Lower the arm while protection is still off, or it can freeze on the way down too.
            if self.return_to_rest and not stop_event.is_set():
                _return_to_rest(ctx)
            return {"interrupted": False}

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d["keyframes"] = [{"t_s": kf.t_s, "joints": kf.joints} for kf in self.keyframes]
        return d


@dataclass
class ProceduralGesture(Gesture):
    step: Callable[[float, dict[str, Any]], dict[str, float]] | None = None  # t -> {joint: rad}
    setup: Callable[[GestureContext, dict[str, Any]], None] | None = None
    teardown: Callable[[GestureContext, dict[str, Any]], None] | None = None
    tick_s: float = 0.05
    speed: float = 0.7
    needs_arm_collision_off: bool = True

    def play(self, ctx: GestureContext, stop_event: threading.Event, params: dict[str, Any]) -> Any:
        motion = ctx.motion
        duration = float(params.get("duration_s", self.duration_s))
        if self.needs_arm_collision_off and ctx.disable_arm_collision:
            try:
                motion.set_collision_protection("Arms", False)
            except Exception as exc:
                log.debug("collision protection off failed: %s", exc)
        try:
            if self.setup:
                self.setup(ctx, params)
            start = time.monotonic()
            while not stop_event.is_set():
                t = time.monotonic() - start
                if t >= duration:
                    break
                targets = self.step(t, params) if self.step else {}
                if targets:
                    try:
                        motion.set_angles(targets, self.speed)
                    except Exception as exc:
                        log.debug("procedural step failed: %s", exc)
                time.sleep(self.tick_s)
        finally:
            if self.teardown:
                try:
                    self.teardown(ctx, params)
                except Exception as exc:
                    log.debug("teardown: %s", exc)
            if self.needs_arm_collision_off and ctx.disable_arm_collision:
                try:
                    motion.set_collision_protection("Arms", True)
                except Exception:
                    pass
            if not stop_event.is_set():
                try:
                    motion.go_to_posture("StandInit", 0.5)
                except Exception:
                    pass
        return {"interrupted": stop_event.is_set()}


class HighFiveGesture(Gesture):
    """Raise the right hand, wait for a touch on it, celebrate (or lower it sadly)."""

    TOUCH_KEYS = ("HandRightBackTouched", "HandRightLeftTouched", "HandRightRightTouched")

    def __init__(self) -> None:
        super().__init__(name="high_five", description="raise the right hand for a high five, "
                         "celebrate when the hand is touched", duration_s=5.0,
                         tags=["social", "reaction"])

    def play(self, ctx: GestureContext, stop_event: threading.Event, params: dict[str, Any]) -> Any:
        motion = ctx.motion
        wait_s = float(params.get("wait_s", 3.5))
        side = params.get("side", "right")
        prefix = "R" if side == "right" else "L"
        angles = arm_pose_angles("right" if side == "right" else "left", "high_five")
        angles[f"{prefix}Hand"] = 1.0
        try:
            motion.set_collision_protection("Arms", False)
        except Exception:
            pass
        touched = False
        try:
            motion.set_angles(angles, 0.6)
            # Silent unless asked: NAOqi's voice would talk over Gemini's.
            if ctx.say and params.get("announce", True) and params.get("phrase"):
                ctx.say(params["phrase"])
            if ctx.leds is not None:
                try:
                    ctx.leds.set_mood("happy")
                except Exception:
                    pass
            deadline = time.monotonic() + wait_s
            keys = list(self.TOUCH_KEYS) if prefix == "R" else \
                ["HandLeftBackTouched", "HandLeftLeftTouched", "HandLeftRightTouched"]
            while time.monotonic() < deadline and not stop_event.is_set():
                if ctx.perception is not None and ctx.perception.any_touched(keys):
                    touched = True
                    break
                time.sleep(0.03)
            if touched:
                if ctx.say and params.get("success_phrase"):
                    ctx.say(params["success_phrase"])
                # quick pump: arm back then forward
                motion.set_angles({f"{prefix}ShoulderPitch": angles[f"{prefix}ShoulderPitch"] + 0.4}, 0.8)
                time.sleep(0.25)
                motion.set_angles({f"{prefix}ShoulderPitch": angles[f"{prefix}ShoulderPitch"] - 0.2}, 0.8)
                time.sleep(0.3)
            elif ctx.say and params.get("miss_phrase"):
                ctx.say(params["miss_phrase"])
        finally:
            try:
                motion.set_collision_protection("Arms", True)
            except Exception:
                pass
            if not stop_event.is_set():
                try:
                    motion.set_angles(arm_pose_angles("right" if side == "right" else "left", "down"), 0.4)
                    time.sleep(0.8)
                    motion.go_to_posture("StandInit", 0.5)
                except Exception:
                    pass
            if ctx.leds is not None:
                try:
                    ctx.leds.set_mood("neutral")
                except Exception:
                    pass
        return {"touched": touched, "interrupted": stop_event.is_set()}


# ---------------------------------------------------------------------------
# Choregraphe timelines (assets/choreography.json, imported by pepper_mcp.choregraphe_import).
# ---------------------------------------------------------------------------

CHOREOGRAPHY_MANIFEST = "choreography.json"
LEAD_IN_S = 0.8  # time to reach the first keys from wherever the robot stands
REST_S = 1.5


@dataclass
class TimelineGesture(Gesture):
    """A Choregraphe ``.qianim`` timeline played with ``angleInterpolationBezier``, optionally
    with music started ``music_offset_s`` after the timeline's origin (dances)."""
    file: str = ""  # relative to ASSETS_DIR
    music: str | None = None  # relative to ASSETS_DIR; may be missing (dance then runs silently)
    music_offset_s: float = 0.0
    return_to_rest: bool = True
    # Choreographies are made for an audience in front of the robot: same reason as `wave`.
    needs_arm_collision_off: bool = True
    _timeline: Timeline | None = field(default=None, init=False, repr=False, compare=False)

    @property
    def has_music(self) -> bool:
        return bool(self.music) and os.path.exists(os.path.join(ASSETS_DIR, self.music or ""))

    def timeline(self) -> Timeline:
        if self._timeline is None:
            with open(os.path.join(ASSETS_DIR, self.file), encoding="utf-8") as fh:
                self._timeline = parse_qianim(fh.read())
        return self._timeline

    def _load_music(self, ctx: GestureContext, params: dict[str, Any]) -> Any:
        """Upload (first time only) and preload the track so it starts on the beat."""
        if not self.has_music or ctx.audio is None or not params.get("music", True):
            return None
        path = ctx.audio.robot_asset_path(self.music)
        if not path:
            return None
        try:
            return ctx.audio.load(path)
        except Exception as exc:
            log.info("could not load %s on the robot: %s", path, exc)
            return None

    def play(self, ctx: GestureContext, stop_event: threading.Event, params: dict[str, Any]) -> Any:
        timeline = self.timeline()
        try:
            current = ctx.motion.get_angles(sorted(timeline.curves))
        except Exception as exc:
            log.debug("current angles unavailable: %s", exc)
            current = None
        plan = to_bezier(timeline, LEAD_IN_S, min_shift_s=max(0.0, -self.music_offset_s), current=current)
        track = self._load_music(ctx, params)
        music_at = plan.shift_s + self.music_offset_s
        playing = False
        volume: int | None = None
        if track is not None:
            try:
                volume = ctx.audio.get_volume()
                ctx.audio.set_volume(max(volume, 70))
            except Exception:
                volume = None
        try:
            with _arms_collision_off(ctx, self.needs_arm_collision_off):
                fut = ctx.motion.interpolate_bezier_async(plan.names, plan.times, plan.keys)
                start = time.monotonic()

                def start_music() -> None:
                    nonlocal playing
                    if track is not None and not playing and time.monotonic() - start >= music_at:
                        playing = True  # one attempt only: a failed start leaves the dance silent
                        try:
                            ctx.audio.play_loaded_async(track)
                        except Exception as exc:
                            log.info("music for %s did not start: %s", self.name, exc)

                interrupted = _wait_motion(ctx, fut, stop_event, start + plan.end_s + 2.0, start_music)
                if playing:
                    ctx.audio.stop()
                    playing = False
                if self.return_to_rest and not interrupted and not stop_event.is_set():
                    _return_to_rest(ctx)
                return {"interrupted": interrupted, "music": track is not None}
        finally:
            if playing:
                ctx.audio.stop()
            if volume is not None:
                try:
                    ctx.audio.set_volume(volume)  # the music level would otherwise stick to the voice
                except Exception as exc:
                    log.debug("volume restore: %s", exc)

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        if self.music:
            d["music"] = self.has_music
        return d


@dataclass
class GestureGroup(Gesture):
    """Plays a random member (never the same one twice in a row); ``params.variant`` picks one."""
    members: list[Gesture] = field(default_factory=list)
    _last: str | None = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.members:
            raise RobotError(f"gesture group {self.name!r} has no members")
        self.duration_s = max(m.duration_s for m in self.members)

    @property
    def has_music(self) -> bool:
        return any(m.has_music for m in self.members)

    def pick(self, params: dict[str, Any]) -> Gesture:
        by_name = {m.name: m for m in self.members}
        wanted = params.get("variant")
        if wanted is not None:
            if wanted not in by_name:
                raise RobotError(f"{self.name!r} has no variant {wanted!r}; variants: {', '.join(by_name)}")
            return by_name[wanted]
        choices = [m for m in self.members if m.name != self._last] or self.members
        return random.choice(choices)

    def play(self, ctx: GestureContext, stop_event: threading.Event, params: dict[str, Any]) -> Any:
        gesture = self.pick(params)
        self._last = gesture.name
        result = gesture.play(ctx, stop_event, {k: v for k, v in params.items() if k != "variant"})
        return {"variant": gesture.name, **(result if isinstance(result, dict) else {"result": result})}

    def to_dict(self) -> dict[str, Any]:
        return super().to_dict() | {"variants": [m.name for m in self.members]}


def library_gestures(manifest_path: str | None = None) -> list[Gesture]:
    """Animations, dances and their random-variant groups from ``assets/choreography.json``."""
    path = manifest_path or os.path.join(ASSETS_DIR, CHOREOGRAPHY_MANIFEST)
    try:
        with open(path, encoding="utf-8") as fh:
            manifest = json.load(fh)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        log.warning("choreography manifest %s unreadable: %s", path, exc)
        return []
    out: dict[str, Gesture] = {}
    for a in manifest.get("animations", []):
        rest = bool(a.get("return_to_rest", True))
        out[a["name"]] = TimelineGesture(
            name=a["name"], description=a["description"], tags=list(a.get("tags", [])),
            duration_s=float(a["duration_s"]) + LEAD_IN_S + (REST_S if rest else 0.0),
            file=a["file"], return_to_rest=rest)
    for d in manifest.get("dances", []):
        gesture = TimelineGesture(
            name=d["name"], description=d["description"], tags=["dance", "fun"],
            duration_s=float(d["duration_s"]) + LEAD_IN_S + max(0.0, -float(d.get("music_offset_s", 0))) + REST_S,
            file=d["file"], music=d.get("music"), music_offset_s=float(d.get("music_offset_s", 0)))
        if gesture.has_music:
            gesture.tags.append("music")
        out[d["name"]] = gesture
    for g in manifest.get("groups", []):
        members = [out[m] for m in g.get("members", []) if m in out]
        if members:
            out[g["name"]] = GestureGroup(name=g["name"], description=g["description"], duration_s=0,
                                          tags=["group", *g.get("tags", [])], members=members)
    return list(out.values())


# ---------------------------------------------------------------------------
# Procedural emotes ported from the teleop code (validated on the robot).
# ---------------------------------------------------------------------------

_67_STATIC = {"LShoulderRoll": 0.15, "LElbowYaw": -1.0, "LElbowRoll": -1.0, "LWristYaw": -1.2, "LHand": 1.0,
              "RShoulderRoll": -0.15, "RElbowYaw": 1.0, "RElbowRoll": 1.0, "RWristYaw": 1.2, "RHand": 1.0}
_67_MID, _67_AMP, _67_HZ = 0.5, 0.4, 1.3


def _67_setup(ctx: GestureContext, params: dict[str, Any]) -> None:
    ctx.motion.set_angles(_67_STATIC, 0.3)
    if ctx.tablet is not None and ctx.tablet.holder is None and ctx.assets_url is not None:
        url = ctx.assets_url("67.html")
        if url:
            threading.Thread(target=lambda: ctx.tablet.show_url(url), daemon=True).start()
    # NAOqi chanting "six seven" would talk over Gemini; Gemini says it itself.
    if ctx.say is not None and params.get("speak", False):
        duration = float(params.get("duration_s", 6.0))

        def _loop() -> None:
            end = time.monotonic() + duration
            while time.monotonic() < end:
                try:
                    ctx.say("six seven")
                except Exception:
                    return
                time.sleep(0.9)

        threading.Thread(target=_loop, daemon=True).start()


def _67_step(t: float, params: dict[str, Any]) -> dict[str, float]:
    offset = _67_AMP * math.sin(2 * math.pi * _67_HZ * t)
    return {"LShoulderPitch": _67_MID - offset, "RShoulderPitch": _67_MID + offset}


def _67_teardown(ctx: GestureContext, params: dict[str, Any]) -> None:
    if ctx.tablet is not None and ctx.tablet.holder is None:  # never wipe a game board
        threading.Thread(target=ctx.tablet.hide, daemon=True).start()


def _clown_setup(ctx: GestureContext, params: dict[str, Any]) -> None:
    if ctx.audio is not None and params.get("music", True):
        path = params.get("music_path") or ctx.audio.robot_asset_path("clown-circus-music.mp3")
        if path:
            try:
                ctx.audio.set_volume(max(ctx.audio.get_volume(), 70))
            except Exception:
                pass
            ctx.audio.play(path, loop=True)


def _clown_step(t: float, params: dict[str, Any]) -> dict[str, float]:
    beat = math.sin(2 * math.pi * 2.0 * t)
    arm = math.sin(2 * math.pi * 1.0 * t)
    head = math.sin(2 * math.pi * 1.0 * t)
    return {
        "HeadYaw": 0.5 * head, "HeadPitch": -0.2 + 0.15 * beat,
        "LShoulderPitch": 0.6 - 0.7 * arm, "RShoulderPitch": 0.6 + 0.7 * arm,
        "LShoulderRoll": 0.4 + 0.3 * abs(arm), "RShoulderRoll": -0.4 - 0.3 * abs(arm),
        "LElbowRoll": -1.0 - 0.3 * beat, "RElbowRoll": 1.0 + 0.3 * beat,
        "HipRoll": 0.25 * beat, "KneePitch": 0.09 * (1.0 + beat),
        "LHand": 0.5 + 0.5 * beat, "RHand": 0.5 - 0.5 * beat,
    }


def _clown_teardown(ctx: GestureContext, params: dict[str, Any]) -> None:
    if ctx.audio is not None:
        ctx.audio.stop()


def _kf(t: float, **joints_deg: float) -> Keyframe:
    return Keyframe(t_s=t, joints=joints_deg)


def builtin_gestures() -> list[Gesture]:
    # Every arm joint is set (matching the "down" arm_pose / StandInit): a forearm left twisted
    # by an earlier gesture hangs into the hip or tablet and collision avoidance stops that arm.
    untwist = {"LElbowYaw": -70, "RElbowYaw": 70, "LWristYaw": 0, "RWristYaw": 0,
               "LElbowRoll": -25, "RElbowRoll": 25}
    down = {"LShoulderPitch": 88, "RShoulderPitch": 88, "LShoulderRoll": 8, "RShoulderRoll": -8, **untwist}
    return [
        HighFiveGesture(),
        # Raise into the (robot-validated) high-five pose, tilted out to the side, then swing the
        # forearm side to side in the frontal plane: ElbowYaw ~0 makes ElbowRoll bend the hand
        # toward/away from the head instead of toward the person.
        KeyframeGesture(
            name="wave", description="raise the right hand high and wave hello", duration_s=0,
            tags=["social", "greeting"], needs_arm_collision_off=True,
            keyframes=[
                _kf(1.0, RShoulderPitch=-75, RShoulderRoll=-30, RElbowYaw=-10, RElbowRoll=30, RWristYaw=60,
                    RHand=1),
                _kf(1.35, RShoulderRoll=-38, RElbowRoll=8),
                _kf(1.7, RShoulderRoll=-24, RElbowRoll=55),
                _kf(2.05, RShoulderRoll=-38, RElbowRoll=8),
                _kf(2.4, RShoulderRoll=-24, RElbowRoll=55),
                _kf(2.75, RShoulderRoll=-38, RElbowRoll=8),
                _kf(3.1, RShoulderRoll=-30, RElbowRoll=30),
            ]),
        KeyframeGesture(
            name="celebrate", description="both arms up in the air, happy", duration_s=0, tags=["emotion"],
            keyframes=[
                _kf(0.7, LShoulderPitch=-100, RShoulderPitch=-100, LShoulderRoll=30, RShoulderRoll=-30,
                    LElbowRoll=-25, RElbowRoll=25, LHand=1, RHand=1, HeadPitch=-15),
                _kf(1.1, LShoulderRoll=15, RShoulderRoll=-15),
                _kf(1.5, LShoulderRoll=35, RShoulderRoll=-35),
                _kf(2.0, LShoulderRoll=20, RShoulderRoll=-20),
            ]),
        KeyframeGesture(
            name="nod", description="nod the head (yes)", duration_s=0, tags=["head"], return_to_rest=False,
            keyframes=[_kf(0.3, HeadPitch=15), _kf(0.6, HeadPitch=-5), _kf(0.9, HeadPitch=15), _kf(1.2, HeadPitch=0)]),
        KeyframeGesture(
            name="shake_head", description="shake the head (no)", duration_s=0, tags=["head"], return_to_rest=False,
            keyframes=[_kf(0.3, HeadYaw=25), _kf(0.7, HeadYaw=-25), _kf(1.1, HeadYaw=20), _kf(1.4, HeadYaw=0)]),
        KeyframeGesture(
            name="hug_open", description="open both arms wide for a hug", duration_s=0, tags=["social"],
            keyframes=[_kf(1.0, LShoulderPitch=20, RShoulderPitch=20, LShoulderRoll=60, RShoulderRoll=-60,
                           LElbowRoll=-20, RElbowRoll=20, LHand=1, RHand=1, HeadPitch=-5),
                       _kf(3.0, LShoulderRoll=60, RShoulderRoll=-60)]),
        KeyframeGesture(
            name="point_forward", description="point straight ahead with the right arm", duration_s=0,
            tags=["deictic"],
            keyframes=[_kf(0.8, RShoulderPitch=5, RShoulderRoll=-10, RElbowYaw=70, RElbowRoll=15, RWristYaw=0, RHand=0),
                       _kf(2.5, RShoulderPitch=5)]),
        KeyframeGesture(
            name="bow", description="bow forward politely", duration_s=0, tags=["social"],
            keyframes=[_kf(1.0, HipPitch=-20, HeadPitch=20, **down), _kf(2.2, HipPitch=-20), _kf(3.2, HipPitch=0, HeadPitch=0)]),
        KeyframeGesture(
            name="arms_down", description="return both arms to a relaxed position", duration_s=0,
            tags=["utility"], return_to_rest=False, needs_arm_collision_off=True,
            # Untwist the forearms and open the shoulders slightly first (pitch held where it
            # is), then lower: both arms clear the hips on the way down.
            keyframes=[_kf(0.6, LShoulderRoll=12, RShoulderRoll=-12, **untwist), _kf(1.6, **down)]),
        ProceduralGesture(
            name="sixty_seven", description="the 67 meme: palms up, arms rocking like scales, GIF "
            "on the tablet (say 'six seven' yourself)", duration_s=6.0, tags=["emote", "fun"],
            step=_67_step, setup=_67_setup, teardown=_67_teardown, speed=0.8),
        ProceduralGesture(
            name="clown_dance", description="whole-body jig to looped circus music", duration_s=20.0,
            tags=["emote", "fun", "dance", "music"], step=_clown_step, setup=_clown_setup, teardown=_clown_teardown),
    ]


class GestureRegistry:
    def __init__(self, ctx_factory: Callable[[], GestureContext]):
        self._gestures: dict[str, Gesture] = {g.name: g for g in builtin_gestures()}
        for g in library_gestures():
            if g.name in self._gestures:
                log.warning("library gesture %s shadows a built-in; skipped", g.name)
                continue
            self._gestures[g.name] = g
        self._ctx_factory = ctx_factory
        # name -> (stop request, finished); a gesture keeps its entry until its finally has run
        self._active: dict[str, tuple[threading.Event, threading.Event]] = {}
        self._lock = threading.Lock()

    def names(self) -> list[str]:
        return sorted(self._gestures)

    def list(self, tag: str | None = None) -> list[dict[str, Any]]:
        return [self._gestures[n].to_dict() for n in self.names() if tag is None or tag in self._gestures[n].tags]

    def music_gestures(self) -> frozenset[str]:
        """Gestures that play music, so the voice agent mutes the mic while they run."""
        return frozenset(n for n, g in self._gestures.items() if g.has_music)

    def get(self, name: str) -> Gesture:
        g = self._gestures.get(name)
        if g is None:
            raise RobotError(f"unknown gesture {name!r}; available: {', '.join(self.names())}")
        return g

    def define(self, name: str, description: str, keyframes: list[dict[str, Any]],
               return_to_rest: bool = True, speech: str | None = None, tags: list[str] | None = None) -> Gesture:
        kfs = [Keyframe(t_s=float(k["t_s"]), joints={str(j): float(v) for j, v in dict(k.get("joints", {})).items()})
               for k in keyframes]
        g = KeyframeGesture(name=name.strip().lower().replace(" ", "_"), description=description,
                            duration_s=0, keyframes=kfs, return_to_rest=return_to_rest, speech=speech,
                            tags=tags or ["user"], builtin=False)
        with self._lock:
            self._gestures[g.name] = g
        return g

    def remove(self, name: str) -> bool:
        with self._lock:
            g = self._gestures.get(name)
            if g is None or g.builtin:
                return False
            del self._gestures[name]
            return True

    PREEMPT_WAIT_S = 3.0

    def play(self, name: str, params: dict[str, Any] | None = None, preempt: bool = True) -> Any:
        """Blocking play (call from a task thread). Only one gesture at a time: a new one stops
        the running one and waits for its cleanup (collision protection back on, its music
        stopped) so that cleanup cannot undo the new gesture's setup. ``preempt=False`` plays
        only when nothing else runs and returns None otherwise (background reflexes)."""
        gesture = self.get(name)
        entry = (threading.Event(), threading.Event())
        with self._lock:
            if not preempt and self._active:
                return None
            previous = list(self._active.values())
            for stop, _ in previous:
                stop.set()
            self._active[name] = entry
        stop_event, done = entry
        try:
            for _, finished in previous:
                if not finished.wait(self.PREEMPT_WAIT_S):
                    log.warning("previous gesture did not finish within %.0f s", self.PREEMPT_WAIT_S)
            if stop_event.is_set():  # stopped (or preempted) while waiting
                return {"interrupted": True}
            return gesture.play(self._ctx_factory(), stop_event, params or {})
        finally:
            done.set()
            with self._lock:
                if self._active.get(name) is entry:
                    del self._active[name]

    def stop_all(self) -> int:
        with self._lock:
            n = len(self._active)
            for stop, _ in self._active.values():
                stop.set()
        return n

    def active(self) -> list[str]:
        with self._lock:
            return list(self._active)
