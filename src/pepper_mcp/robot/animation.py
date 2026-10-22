"""Stock Pepper animations (ALAnimationPlayer) with live discovery.

NAOqi ships a library of animations under ``animations/Stand/...``. The exact set
varies per firmware, so :meth:`Animations.list` asks ``ALBehaviorManager`` for what is
installed and only falls back to the curated list below (known from the 2.5 docs) when
discovery fails. Tags such as ``hello`` or ``thinking`` are resolved by ALAnimationPlayer.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from pepper_mcp.robot.connection import RobotConnection, RobotError

log = logging.getLogger(__name__)

# Curated subset of the Pepper 2.5 default library (gesture name -> meaning).
CURATED: dict[str, str] = {
    "animations/Stand/Gestures/Hey_1": "friendly wave",
    "animations/Stand/Gestures/Hey_3": "big wave",
    "animations/Stand/Gestures/Hey_4": "small wave",
    "animations/Stand/Gestures/Hey_6": "wave with both hands",
    "animations/Stand/Gestures/BowShort_1": "short bow",
    "animations/Stand/Gestures/BowShort_3": "bow",
    "animations/Stand/Gestures/Yes_1": "nod yes",
    "animations/Stand/Gestures/Yes_2": "nod yes (bigger)",
    "animations/Stand/Gestures/No_1": "shake head no",
    "animations/Stand/Gestures/No_3": "no with hand",
    "animations/Stand/Gestures/Thinking_1": "thinking pose",
    "animations/Stand/Gestures/Thinking_3": "thinking, hand on chin",
    "animations/Stand/Gestures/Explain_1": "explaining with hands",
    "animations/Stand/Gestures/Explain_2": "explaining",
    "animations/Stand/Gestures/Enthusiastic_4": "enthusiastic",
    "animations/Stand/Gestures/Enthusiastic_5": "very enthusiastic",
    "animations/Stand/Gestures/Excited_1": "excited",
    "animations/Stand/Gestures/Me_1": "pointing at self",
    "animations/Stand/Gestures/You_1": "pointing at you",
    "animations/Stand/Gestures/Please_1": "please gesture",
    "animations/Stand/Gestures/IDontKnow_1": "shrug, I don't know",
    "animations/Stand/Gestures/CalmDown_1": "calm down",
    "animations/Stand/Gestures/ShowSky_1": "pointing to the sky",
    "animations/Stand/Gestures/ShowTablet_1": "showing the tablet",
    "animations/Stand/Gestures/Give_1": "giving / offering",
    "animations/Stand/Gestures/Kisses_1": "blowing a kiss",
    "animations/Stand/Gestures/Salute_1": "salute",
    "animations/Stand/Gestures/Stretch_1": "stretch",
    "animations/Stand/Gestures/Yum_1": "yummy",
    "animations/Stand/Gestures/Wings_1": "flap wings",
    "animations/Stand/Emotions/Positive/Happy_4": "happy",
    "animations/Stand/Emotions/Positive/Excited_1": "excited",
    "animations/Stand/Emotions/Positive/Enthusiastic_1": "enthusiastic",
    "animations/Stand/Emotions/Positive/Laugh_1": "laugh",
    "animations/Stand/Emotions/Positive/Proud_1": "proud",
    "animations/Stand/Emotions/Positive/Winner_1": "winner",
    "animations/Stand/Emotions/Positive/Shy_1": "shy",
    "animations/Stand/Emotions/Positive/Peaceful_1": "peaceful",
    "animations/Stand/Emotions/Positive/Interested_1": "interested",
    "animations/Stand/Emotions/Negative/Sad_1": "sad",
    "animations/Stand/Emotions/Negative/Bored_1": "bored",
    "animations/Stand/Emotions/Negative/Fear_1": "afraid",
    "animations/Stand/Emotions/Negative/Angry_1": "angry",
    "animations/Stand/Emotions/Negative/Surprise_1": "surprised",
    "animations/Stand/Emotions/Negative/Disappointed_1": "disappointed",
    "animations/Stand/Emotions/Neutral/Hello_1": "hello",
    "animations/Stand/Emotions/Neutral/AskForAttention_1": "asking for attention",
    "animations/Stand/Emotions/Neutral/Embarrassed_1": "embarrassed",
    "animations/Stand/Emotions/Neutral/Confused_1": "confused",
    "animations/Stand/Emotions/Neutral/Puzzled_1": "puzzled",
    "animations/Stand/Emotions/Neutral/Determined_1": "determined",
    "animations/Stand/Waiting/ShowMuscles_1": "show muscles",
    "animations/Stand/Waiting/TakePicture_1": "take a picture",
    "animations/Stand/Waiting/Robot_1": "robot dance",
    "animations/Stand/Waiting/FunnyDancer_1": "funny dance",
    "animations/Stand/Waiting/Fitness_1": "fitness",
    "animations/Stand/Waiting/Think_1": "thinking while waiting",
    "animations/Stand/Waiting/LoveYou_1": "love you",
    "animations/Stand/Waiting/Zombie_1": "zombie walk",
    "animations/Stand/Waiting/Helicopter_1": "helicopter",
    "animations/Stand/Waiting/Bandmaster_1": "conducting an orchestra",
    "animations/Stand/Waiting/Binoculars_1": "looking through binoculars",
    "animations/Stand/Waiting/KnockEye_1": "knocking on eye",
    "animations/Stand/BodyTalk/Listening/Listening_1": "listening body language",
    "animations/Stand/BodyTalk/Thinking/ThinkingLoop_1": "thinking loop",
}

# Tags understood by ALAnimationPlayer.runTag / ^startTag in ALAnimatedSpeech.
TAGS = sorted({"hello", "hey", "hi", "bow", "yes", "no", "thinking", "think", "explain", "me",
               "you", "please", "enthusiastic", "excited", "happy", "sad", "angry", "bored",
               "surprised", "proud", "shy", "laugh", "calm", "sky", "tablet", "give", "kiss",
               "salute", "stretch", "yum", "wings", "confused", "embarrassed", "winner", "dance",
               "muscle", "picture", "love", "zombie", "fear", "disappointed", "interested",
               "peaceful", "attention", "determined", "puzzled", "helicopter", "binoculars",
               "fitness", "robot", "funny", "showtablet", "idontknow", "shrug", "listening"})


class Animations:
    def __init__(self, conn: RobotConnection):
        self.conn = conn
        self._installed: list[str] | None = None
        self._lock = threading.Lock()

    @property
    def player(self) -> Any:
        return self.conn.service("ALAnimationPlayer")

    def installed(self, refresh: bool = False) -> list[str]:
        with self._lock:
            if self._installed is not None and not refresh:
                return self._installed
            names: list[str] = []
            try:
                bm = self.conn.service("ALBehaviorManager")
                names = [b for b in bm.getInstalledBehaviors() if "animations/" in b]
            except Exception as exc:
                log.debug("behaviour discovery failed: %s", exc)
            # Pepper always stands: keep "animations/Stand/..." entries, drop Sit/SitOnPod variants.
            names = sorted({n.split("/.")[0] for n in names if n.startswith("animations/Stand/")})
            self._installed = names or sorted(CURATED)
            self.discovered = bool(names)
            return self._installed

    def list(self, filter_: str | None = None, category: str | None = None) -> list[dict[str, str]]:
        out = []
        for name in self.installed():
            if category and f"/{category}/" not in name and not name.startswith(f"animations/Stand/{category}"):
                continue
            if filter_ and filter_.lower() not in name.lower():
                continue
            out.append({"name": name, "meaning": CURATED.get(name, "")})
        return out

    def resolve(self, name: str) -> str:
        """Accept a full path, a bare animation name (Hey_1) or a tag."""
        name = name.strip()
        if name.startswith("animations/"):
            return name
        lowered = name.lower()
        for path in self.installed():
            if path.rsplit("/", 1)[-1].lower() == lowered:
                return path
        if lowered in TAGS:
            return f"tag:{lowered}"
        for path, meaning in CURATED.items():
            if lowered in meaning:
                return path
        raise RobotError(f"unknown animation {name!r}; use list_animations or a tag like 'hello'")

    def run_async(self, name: str) -> tuple[str, Any]:
        resolved = self.resolve(name)
        if resolved.startswith("tag:"):
            return resolved, RobotConnection.call_async(self.player, "runTag", resolved[4:])
        return resolved, RobotConnection.call_async(self.player, "run", resolved)

    def stop(self) -> None:
        try:
            self.player.stopAll()
        except Exception as exc:
            log.debug("stopAll animations: %s", exc)
        try:
            bm = self.conn.service("ALBehaviorManager")
            for name in bm.getRunningBehaviors():
                if "animations/" in name:
                    bm.stopBehavior(name)
        except Exception:
            pass
