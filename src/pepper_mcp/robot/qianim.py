"""Choregraphe ``.qianim`` timelines → ``ALMotion.angleInterpolationBezier`` arguments.

A ``.qianim`` file holds one ``ActuatorCurve`` per joint: keys at a frame (``fps`` per curve)
with a value in degrees (hands: 0..1), optionally with Bezier tangents whose
``abscissaParam`` is in frames and ``ordinateParam`` in the curve's unit. Keys without a
tangent get Choregraphe's ``bezier_auto`` handles: a third of the neighbouring interval long,
flat at local extrema, otherwise sloped like the line between the two neighbours.

Every key value and every handle is clamped to :data:`motion.JOINT_LIMITS`. A Bezier segment
stays inside the hull of its control points, so the played curve never leaves the limits.
"""

from __future__ import annotations

import logging
import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from pepper_mcp.robot.connection import RobotError
from pepper_mcp.robot.motion import JOINT_LIMITS

log = logging.getLogger(__name__)

BEZIER = 3  # NAOqi interpolation type in angleInterpolationBezier handles
RATIO_UNITS = frozenset({"dimensionless", "1"})  # hands are 0..1, everything else degrees


@dataclass(frozen=True)
class Key:
    t: float  # seconds from the start of the timeline
    value: float  # radians (hands: 0..1)
    left: tuple[float, float] | None = None  # (dt <= 0 s, d_value) handle, None = auto
    right: tuple[float, float] | None = None  # (dt >= 0 s, d_value)


@dataclass(frozen=True)
class Timeline:
    curves: dict[str, list[Key]]

    @property
    def end_s(self) -> float:
        return max((keys[-1].t for keys in self.curves.values() if keys), default=0.0)

    @property
    def first_s(self) -> float:
        return min((keys[0].t for keys in self.curves.values() if keys), default=0.0)


@dataclass(frozen=True)
class BezierPlan:
    names: list[str]
    times: list[list[float]]
    keys: list[list[list[float | list[float]]]]
    shift_s: float  # how far the timeline was delayed so the first keys are not instantaneous
    end_s: float  # last key time after the shift


def _strip_ns(root: ET.Element) -> ET.Element:
    for el in root.iter():
        el.tag = el.tag.rsplit("}", 1)[-1]
        for attr in [a for a in el.attrib if a.startswith("{")]:
            el.attrib[attr.rsplit("}", 1)[-1]] = el.attrib.pop(attr)
    return root


def _to_native(unit: str | None, value: float) -> float:
    return value if (unit or "") in RATIO_UNITS else math.radians(value)


def parse_qianim(text: str) -> Timeline:
    """Parse the text of a ``.qianim`` file. Muted curves and unknown joints are skipped."""
    try:
        root = _strip_ns(ET.fromstring(text))
    except ET.ParseError as exc:
        raise RobotError(f"not a valid .qianim file: {exc}") from exc
    default_fps = float(root.get("fps") or 25)
    curves: dict[str, list[Key]] = {}
    for curve in root.iter("ActuatorCurve"):
        joint = curve.get("actuator") or ""
        if curve.get("mute", "0").lower() in ("1", "true"):
            continue
        if joint not in JOINT_LIMITS:
            log.debug("qianim: skipping unknown actuator %s", joint)
            continue
        fps = float(curve.get("fps") or default_fps)
        unit = curve.get("unit")
        keys: list[Key] = []
        for key in curve.iter("Key"):
            handles: dict[str, tuple[float, float]] = {}
            for tangent in key.iter("Tangent"):
                side = tangent.get("side")
                if side in ("left", "right"):
                    handles[side] = (float(tangent.get("abscissaParam", 0)) / fps,
                                     _to_native(unit, float(tangent.get("ordinateParam", 0))))
            keys.append(Key(t=float(key.get("frame", 0)) / fps, value=_to_native(unit, float(key.get("value", 0))),
                            left=handles.get("left"), right=handles.get("right")))
        keys = dedupe_keys(keys)
        if keys:
            curves[joint] = keys
    if not curves:
        raise RobotError("the .qianim file has no playable curves")
    return Timeline(curves)


def dedupe_keys(keys: list[Key]) -> list[Key]:
    """Sort by time and keep the last key of any group closer than 1 ms (times must increase)."""
    out: list[Key] = []
    for key in sorted(keys, key=lambda k: k.t):
        if out and key.t - out[-1].t < 1e-3:
            out[-1] = key
        else:
            out.append(key)
    return out


def _auto_slope(keys: list[Key], i: int) -> float:
    if i == 0 or i == len(keys) - 1:
        return 0.0
    prev, cur, nxt = keys[i - 1], keys[i], keys[i + 1]
    if (cur.value - prev.value) * (nxt.value - cur.value) <= 0:  # local extremum or plateau
        return 0.0
    return (nxt.value - prev.value) / (nxt.t - prev.t)


def _handle(given: tuple[float, float] | None, room_s: float, slope: float, sign: int) -> tuple[float, float]:
    """A handle at most half the interval long, so the two handles between neighbouring keys
    never cross (time must keep increasing along the curve)."""
    if given is None:
        dt = sign * room_s / 3.0
        return dt, slope * dt
    dt, dv = given
    if abs(dt) > room_s / 2:
        scale = room_s / 2 / abs(dt) if dt else 0.0
        dt, dv = dt * scale, dv * scale
    return dt, dv


HOLD_GLIDE_S = 1.0


def to_bezier(timeline: Timeline, lead_in_s: float = 0.8, min_shift_s: float = 0.0,
              current: dict[str, float] | None = None) -> BezierPlan:
    """Shift the timeline so the earliest key is at least ``lead_in_s`` away (the robot gets
    there from wherever it stands) and build clamped ``angleInterpolationBezier`` arguments.

    ALMotion glides a joint from its current angle to its first key over the whole time before
    that key. A joint a dance only uses from minute one on would creep for a minute; with
    ``current`` angles it holds still and glides over the last ``HOLD_GLIDE_S`` instead, as
    the joint does in Choregraphe."""
    shift = max(0.0, lead_in_s - timeline.first_s, min_shift_s)
    names: list[str] = []
    times: list[list[float]] = []
    all_keys: list[list[list[float | list[float]]]] = []
    for joint in sorted(timeline.curves):
        lo, hi = JOINT_LIMITS[joint]
        keys = [Key(k.t + shift, k.value, k.left, k.right) for k in timeline.curves[joint]]
        if current is not None and joint in current and keys[0].t > lead_in_s + HOLD_GLIDE_S:
            keys.insert(0, Key(keys[0].t - HOLD_GLIDE_S, current[joint]))
        joint_times: list[float] = []
        joint_keys: list[list[float | list[float]]] = []
        for i, key in enumerate(keys):
            value = max(lo, min(hi, key.value))
            slope = _auto_slope(keys, i)
            room_left = key.t - (keys[i - 1].t if i else 0.0)
            room_right = keys[i + 1].t - key.t if i < len(keys) - 1 else 0.0
            ldt, ldv = _handle(key.left, room_left, slope, -1)
            rdt, rdv = _handle(key.right, room_right, slope, 1)
            # keep the control points inside the limits too
            ldv = max(lo, min(hi, value + ldv)) - value
            rdv = max(lo, min(hi, value + rdv)) - value
            joint_times.append(round(key.t, 4))
            joint_keys.append([value, [BEZIER, ldt, ldv], [BEZIER, rdt, rdv]])
        names.append(joint)
        times.append(joint_times)
        all_keys.append(joint_keys)
    return BezierPlan(names=names, times=times, keys=all_keys, shift_s=shift,
                      end_s=max((t[-1] for t in times), default=0.0))
