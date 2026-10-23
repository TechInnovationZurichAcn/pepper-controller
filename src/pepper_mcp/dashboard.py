"""Control dashboard served by the preview side channel (``/``, ``/api/state``, ``/api/action/<name>``).

The actions mirror the MCP tools a human most often wants at hand (stop, reflexes, hearing,
perception, liveliness, gestures, head/base nudges, speech, volume, mood lights) and keep the
same safety limits: base moves are clamped to the 5 s watchdog and refused while follow/navigation
own the base. Every action is a whitelisted function, so the HTTP layer never reaches arbitrary
runtime attributes.
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from collections.abc import Callable
from typing import Any

from pepper_mcp.robot.awareness import ABILITIES, LIFE_STATES, PRESETS
from pepper_mcp.robot.leds import MOODS
from pepper_mcp.robot.perception import EXTRACTORS

log = logging.getLogger(__name__)

HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")

# Same table as tools/motion.py look_toward, restricted to what the d-pad shows.
LOOK_DIRECTIONS = {"left": (45, 0), "right": (-45, 0), "up": (0, -25), "down": (0, 25), "center": (0, 0)}
TRACK_TARGETS = ("face", "people", "sound")
TRACK_MODES = ("head", "body_rotation", "whole_body", "move")

# HTTP requests run on separate threads; starting/stopping the vision loop is check-then-act.
_vision_lock = threading.Lock()


class ActionError(ValueError):
    """Bad request from the dashboard (unknown action, missing or invalid argument)."""


def html() -> bytes:
    with open(HTML_PATH, "rb") as fh:
        return fh.read()


# ------------------------------------------------------------------ state
def _safe(label: str, fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception as exc:
        log.debug("dashboard state %s: %s", label, exc)
        return None


def state(rt: Any) -> dict[str, Any]:
    """Everything the dashboard shows, in one poll. A failing part is ``None``, never an error."""
    out: dict[str, Any] = {"ts": time.time(), "status": _safe("status", rt.status)}
    out["features"] = {
        "camera": rt.cameras is not None,
        "microphone": rt.mic is not None,
        "listener": rt.listener is not None,
        "perception": rt.perception is not None,
        "reflexes": rt.reflexes is not None,
        "tracker": rt.tracker is not None,
        "leds": rt.leds is not None,
        "audio_out": rt.audio_out is not None,
        "navigation": rt.navigator is not None,
    }
    out["reflexes"] = _safe("reflexes", rt.reflexes.list) if rt.reflexes else []
    out["perception"] = None
    if rt.perception is not None:
        active = dict(rt.perception.active)
        active["hand_raise_vision"] = rt.vision is not None
        out["perception"] = active
        out["touch"] = _safe("touch", rt.perception.touch_state)
    out["abilities"] = _safe("abilities", rt.awareness.abilities)
    out["cameras"] = _safe("cameras", rt.cameras.status) if rt.cameras else []
    out["volume"] = _safe("volume", rt.audio_out.get_volume) if rt.audio_out else None
    out["gestures"] = [{"name": g["name"], "description": g.get("description", "")}
                       for g in (_safe("gestures", rt.gestures.list) or [])]
    out["transcript"] = list(rt.listener.transcript)[-8:] if rt.listener else []
    out["options"] = {"liveliness": sorted(PRESETS), "life_states": list(LIFE_STATES),
                      "abilities": list(ABILITIES), "moods": sorted(MOODS), "extractors": sorted(EXTRACTORS),
                      "track_targets": list(TRACK_TARGETS), "track_modes": list(TRACK_MODES)}
    return out


# ------------------------------------------------------------------ argument helpers
def _bool(args: dict[str, Any], key: str) -> bool:
    value = args.get(key)
    if not isinstance(value, bool):
        raise ActionError(f"{key!r} must be true or false")
    return value


def _num(args: dict[str, Any], key: str, lo: float, hi: float, default: float | None = None) -> float:
    value = args.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ActionError(f"{key!r} must be a number")
    return max(lo, min(hi, float(value)))


def _choice(args: dict[str, Any], key: str, choices: Any) -> str:
    value = args.get(key)
    if value not in choices:
        raise ActionError(f"{key!r} must be one of {sorted(choices)}")
    return str(value)


def _need(obj: Any, what: str) -> Any:
    if obj is None:
        raise ActionError(f"{what} is not available (service missing or disabled by settings)")
    return obj


def _base_free(rt: Any) -> None:
    if rt.tracker and rt.tracker.following:
        raise ActionError("follow person is active and owns the base; turn it off first")
    if rt.navigator and rt.navigator.busy:
        raise ActionError("navigation owns the base; stop it first")


# ------------------------------------------------------------------ actions
def _reflex(rt: Any, a: dict[str, Any]) -> Any:
    reflexes = _need(rt.reflexes, "reflexes")
    name = a.get("name")
    if not isinstance(name, str):
        raise ActionError("'name' must be a reflex name")
    return reflexes.set(name, _bool(a, "enabled"))


def _listening(rt: Any, a: dict[str, Any]) -> Any:
    listener = _need(rt.listener, "listener (microphone + STT)")
    if "barge_in" in a:
        listener.barge_in = _bool(a, "barge_in")
    if "enabled" in a:
        listener.set_enabled(_bool(a, "enabled"))
    return listener.status()


def _perception(rt: Any, a: dict[str, Any]) -> Any:
    perception = _need(rt.perception, "perception")
    name = _choice(a, "name", set(EXTRACTORS) | {"hand_raise_vision"})
    enabled = _bool(a, "enabled")
    if name != "hand_raise_vision":
        active = perception.set_extractor(name, enabled)
        if enabled and not active:
            raise ActionError(f"{name} could not be enabled (NAOqi service missing on this robot)")
        return {name: active}
    with _vision_lock:
        if enabled and rt.vision is None:
            _need(rt.cameras, "camera")
            rt._start_vision()
        elif not enabled and rt.vision is not None:
            rt.vision.stop()
            rt.vision = None
        return {name: rt.vision is not None}


def _tracking(rt: Any, a: dict[str, Any]) -> Any:
    tracker = _need(rt.tracker, "tracker")
    if not _bool(a, "enabled"):
        return tracker.stop()
    return tracker.track(_choice(a, "target", TRACK_TARGETS), _choice(a, "mode", TRACK_MODES))


def _follow(rt: Any, a: dict[str, Any]) -> Any:
    tracker = _need(rt.tracker, "tracker")
    if not _bool(a, "enabled"):
        return tracker.stop()
    return tracker.follow(_num(a, "distance_m", 0.3, 2.0, 0.6))


def _gesture(rt: Any, a: dict[str, Any]) -> Any:
    name = a.get("name")
    if not isinstance(name, str):
        raise ActionError("'name' must be a gesture name")
    rt.gestures.get(name)
    task = rt.tasks.run("gesture", name, lambda: rt.gestures.play(name, {}), cancel=rt.gestures.stop_all)
    return task.to_dict()


def _move(rt: Any, a: dict[str, Any]) -> Any:
    _base_free(rt)
    return rt.motion.move(_num(a, "vx", -1, 1, 0.0), _num(a, "vy", -1, 1, 0.0),
                          _num(a, "vtheta", -1, 1, 0.0), _num(a, "duration_s", 0.1, 5.0, 1.0))


def _turn(rt: Any, a: dict[str, Any]) -> Any:
    _base_free(rt)
    degrees = _num(a, "degrees", -360, 360)
    task = rt.tasks.run("turn", f"turn {degrees}deg",
                        lambda: rt.motion.move_relative(0.0, 0.0, math.radians(degrees)),
                        cancel=rt.motion.stop_base)
    return task.to_dict()


def _look(rt: Any, a: dict[str, Any]) -> Any:
    yaw, pitch = LOOK_DIRECTIONS[_choice(a, "direction", LOOK_DIRECTIONS)]
    return {k: round(math.degrees(v), 1) for k, v in rt.motion.look(yaw, pitch, 0.3).items()}


def _say(rt: Any, a: dict[str, Any]) -> Any:
    text = a.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ActionError("'text' must be a non-empty string")
    return rt.speech.say(text.strip()[:500], animated=_bool(a, "animated") if "animated" in a else True).to_dict()


def _camera(rt: Any, a: dict[str, Any]) -> Any:
    cams = _need(rt.cameras, "camera")
    return cams.set_stream(_choice(a, "camera", ("top", "bottom", "depth")), _bool(a, "enabled"), None, None)


ACTIONS: dict[str, Callable[[Any, dict[str, Any]], Any]] = {
    "stop": lambda rt, a: rt.stop_everything(),
    "wake_up": lambda rt, a: (rt.motion.prepare("StandInit"), {"awake": rt.motion.is_awake()})[1],
    "rest": lambda rt, a: (rt.stop_everything(), rt.motion.rest(), {"awake": rt.motion.is_awake()})[2],
    "reflex": _reflex,
    "listening": _listening,
    "perception": _perception,
    "liveliness": lambda rt, a: rt.awareness.apply_preset(_choice(a, "preset", PRESETS)),
    "ability": lambda rt, a: {"applied": rt.awareness.set_ability(_choice(a, "name", ABILITIES),
                                                                  _bool(a, "enabled"))},
    "autonomous_life": lambda rt, a: {"state": rt.awareness.set_life_state(_choice(a, "state", LIFE_STATES))},
    "tracking": _tracking,
    "follow": _follow,
    "gesture": _gesture,
    "stop_animation": lambda rt, a: (rt.animations.stop(), {"gestures_stopped": rt.gestures.stop_all()})[1],
    "move": _move,
    "turn": _turn,
    "look": _look,
    "say": _say,
    "stop_speaking": lambda rt, a: {"dropped": rt.speech.stop_speaking()},
    "volume": lambda rt, a: {"volume": _need(rt.audio_out, "audio").set_volume(int(_num(a, "level", 0, 100)))},
    "mood": lambda rt, a: _need(rt.leds, "LEDs").set_mood(_choice(a, "mood", MOODS)),
    "camera": _camera,
}


def run_action(rt: Any, name: str, args: dict[str, Any]) -> Any:
    """Run one whitelisted action. Raises :class:`ActionError` for bad input and
    ``RobotError`` when the robot refuses."""
    fn = ACTIONS.get(name)
    if fn is None:
        raise ActionError(f"unknown action {name!r}")
    if not isinstance(args, dict):
        raise ActionError("body must be a JSON object")
    log.info("dashboard action %s %s", name, args)
    try:
        return fn(rt, args)
    except KeyError as exc:
        raise ActionError(str(exc).strip("'\"")) from exc
