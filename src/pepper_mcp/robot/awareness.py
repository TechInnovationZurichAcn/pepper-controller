"""Autonomous life, basic awareness and the "liveliness" presets.

NAOqi's own autonomous behaviours (looking at people, breathing, blinking, nodding
while listening) make Pepper feel alive for free – but they also fight explicit
head/arm commands. The presets let the LLM pick a trade-off with one call:

* ``off``     – everything disabled; the LLM owns every joint (teleop-like)
* ``calm``    – blinking + breathing + speaking gestures, no autonomous head tracking
* ``lively``  – also BasicAwareness (looks at people/sounds, engages) + listening nods
"""

from __future__ import annotations

import logging
from typing import Any

from pepper_mcp.robot.connection import RobotConnection, RobotError

log = logging.getLogger(__name__)

ABILITIES = ("BasicAwareness", "BackgroundMovement", "ListeningMovement", "SpeakingMovement",
             "AutonomousBlinking")
LIFE_STATES = ("disabled", "solitary", "interactive", "safeguard")
ENGAGEMENT_MODES = ("Unengaged", "SemiEngaged", "FullyEngaged")
TRACKING_MODES = ("Head", "BodyRotation", "WholeBody", "MoveContextually")

PRESETS: dict[str, dict[str, Any]] = {
    "off": {"BasicAwareness": False, "BackgroundMovement": False, "ListeningMovement": False,
            "SpeakingMovement": False, "AutonomousBlinking": False},
    "calm": {"BasicAwareness": False, "BackgroundMovement": True, "ListeningMovement": False,
             "SpeakingMovement": True, "AutonomousBlinking": True},
    "lively": {"BasicAwareness": True, "BackgroundMovement": True, "ListeningMovement": True,
               "SpeakingMovement": True, "AutonomousBlinking": True,
               "engagement_mode": "SemiEngaged", "tracking_mode": "Head"},
}


class Awareness:
    def __init__(self, conn: RobotConnection):
        self.conn = conn
        self.initial_life_state: str | None = None
        self.preset = "unknown"

    @property
    def life(self) -> Any:
        return self.conn.service("ALAutonomousLife")

    # ------------------------------------------------------------- autonomous life
    def life_state(self) -> str:
        try:
            return str(self.life.getState())
        except Exception:
            return "unknown"

    def set_life_state(self, state: str) -> str:
        if state not in LIFE_STATES:
            raise RobotError(f"state must be one of {LIFE_STATES}")
        if self.initial_life_state is None:
            self.initial_life_state = self.life_state()
        if self.life_state() != state:
            self.life.setState(state)
        return self.life_state()

    def disable_life(self) -> None:
        """Take Pepper out of autonomous life so our commands are not fought (teleop logic)."""
        try:
            self.set_life_state("disabled")
        except Exception as exc:
            log.warning("could not disable autonomous life: %s", exc)

    def restore_life(self) -> None:
        if self.initial_life_state and self.initial_life_state not in ("unknown", "disabled"):
            try:
                self.life.setState(self.initial_life_state)
            except Exception as exc:
                log.debug("restore life: %s", exc)

    # ------------------------------------------------------------- abilities
    def set_ability(self, name: str, enabled: bool) -> bool:
        if name not in ABILITIES:
            raise RobotError(f"ability must be one of {ABILITIES}")
        ok = False
        try:
            self.life.setAutonomousAbilityEnabled(name, bool(enabled))
            ok = True
        except Exception as exc:
            log.debug("setAutonomousAbilityEnabled %s: %s", name, exc)
        service = {"BasicAwareness": "ALBasicAwareness", "BackgroundMovement": "ALBackgroundMovement",
                   "ListeningMovement": "ALListeningMovement", "SpeakingMovement": "ALSpeakingMovement",
                   "AutonomousBlinking": "ALAutonomousBlinking"}[name]
        svc = self.conn.try_service(service)
        if svc is not None:
            try:
                svc.setEnabled(bool(enabled))
                ok = True
            except Exception:
                if name == "BasicAwareness":
                    try:
                        (svc.startAwareness if enabled else svc.stopAwareness)()
                        ok = True
                    except Exception as exc:
                        log.debug("basic awareness toggle: %s", exc)
        return ok

    def abilities(self) -> dict[str, bool | None]:
        out: dict[str, bool | None] = {}
        for name in ABILITIES:
            try:
                out[name] = bool(self.life.getAutonomousAbilityEnabled(name))
            except Exception:
                out[name] = None
        return out

    # ------------------------------------------------------------- basic awareness details
    def configure_awareness(self, engagement_mode: str | None = None, tracking_mode: str | None = None,
                            stimuli: dict[str, bool] | None = None) -> dict[str, Any]:
        ba = self.conn.try_service("ALBasicAwareness")
        if ba is None:
            raise RobotError("ALBasicAwareness unavailable")
        if engagement_mode:
            if engagement_mode not in ENGAGEMENT_MODES:
                raise RobotError(f"engagement_mode must be one of {ENGAGEMENT_MODES}")
            ba.setEngagementMode(engagement_mode)
        if tracking_mode:
            if tracking_mode not in TRACKING_MODES:
                raise RobotError(f"tracking_mode must be one of {TRACKING_MODES}")
            ba.setTrackingMode(tracking_mode)
        for stim, on in (stimuli or {}).items():
            try:
                ba.setStimulusDetectionEnabled(stim, bool(on))
            except Exception as exc:
                log.debug("stimulus %s: %s", stim, exc)
        return self.awareness_status()

    def awareness_status(self) -> dict[str, Any]:
        ba = self.conn.try_service("ALBasicAwareness")
        if ba is None:
            return {"available": False}
        out: dict[str, Any] = {"available": True}
        for key, fn in (("enabled", "isEnabled"), ("engagement_mode", "getEngagementMode"),
                        ("tracking_mode", "getTrackingMode")):
            try:
                out[key] = getattr(ba, fn)()
            except Exception:
                pass
        return out

    def engage_person(self, person_id: int) -> bool:
        ba = self.conn.try_service("ALBasicAwareness")
        if ba is None:
            return False
        try:
            return bool(ba.engagePerson(int(person_id)))
        except Exception:
            return False

    # ------------------------------------------------------------- presets
    def apply_preset(self, preset: str, overrides: dict[str, bool] | None = None) -> dict[str, Any]:
        if preset not in PRESETS:
            raise RobotError(f"preset must be one of {sorted(PRESETS)}")
        spec = dict(PRESETS[preset])
        spec.update(overrides or {})
        applied: dict[str, Any] = {}
        for name in ABILITIES:
            if name in spec:
                applied[name] = self.set_ability(name, bool(spec[name]))
        if spec.get("BasicAwareness"):
            try:
                self.configure_awareness(spec.get("engagement_mode"), spec.get("tracking_mode"),
                                         {"People": True, "Sound": True, "Movement": True, "Touch": True})
            except RobotError as exc:
                applied["basic_awareness_error"] = str(exc)
        self.preset = preset
        return {"preset": preset, "applied": applied, "abilities": self.abilities()}
