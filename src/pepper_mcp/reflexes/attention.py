"""Attention: eye colour follows the conversation state and the head turns toward the
person who is talking to Pepper.

Sound localisation fires for every door, chair and keyboard, so a sound alone never moves
the head. The head only turns for an utterance the listener confirmed as speech: as soon
as a voice starts, Pepper turns to a visible person standing in the direction it came from;
if nobody is visible there it waits for the transcript (``speech.final``) before turning
toward the voice, so noise that the listener rejects (too short, empty transcript) never
turns the head."""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from typing import Any

from pepper_mcp.reflexes.base import Reflex
from pepper_mcp.robot.events import Event

log = logging.getLogger(__name__)

#: an utterance whose end event never arrived is forgotten after this long
UTTERANCE_TIMEOUT_S = 20.0
#: speech.final / speech.aborted carry the started_at of their speech.started
SAME_UTTERANCE_S = 0.05


def _angle_diff(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


class AttentionReflex(Reflex):
    name = "attention"
    description = ("eyes turn cyan while a person speaks, purple while the LLM is thinking (between a "
                   "speech.final and the next say), white while Pepper talks; head turns toward the person "
                   "talking (only for confirmed speech, never for plain noises)")
    triggers = ("speech.started", "speech.final", "speech.aborted", "tts.started", "tts.finished",
                "sound.located", "person.appeared")
    params: dict[str, Any] = {
        "led_moods": True,
        "look_at_sound": True,          # turn toward people who talk to Pepper
        "look_at_new_person": True,     # glance at someone who shows up while nobody is talking
        "sound_confidence": 0.3,        # ignore localisations below this confidence
        "min_sound_energy": 0.0,
        "person_match_deg": 35.0,       # a voice within this angle of a visible person is that person
        "speaker_height_m": 1.5,        # where to look when the voice has no visible person
        "speaker_distance_m": 1.5,
        "new_person_quiet_s": 6.0,      # no glancing at newcomers this soon after a conversation turn
        "turn_body": True,              # rotate the base toward a speaker who is off to the side
        "turn_body_min_deg": 45.0,      # closer to the front than this, only the head turns
    }

    def __init__(self, rt: Any):
        super().__init__(rt)
        self._state = threading.Lock()
        self._sounds: deque[dict[str, Any]] = deque(maxlen=64)
        # The utterance the listener is currently capturing. STT runs behind the VAD, so the
        # next speech.started can arrive before the previous utterance's speech.final.
        self._utterance_started: float | None = None
        self._last_ended_start = 0.0  # started_at of the newest utterance that already ended
        self._aimed = False
        self._aimed_person: Any = None
        self._last_talk = 0.0
        self._look_lock = threading.Lock()
        self._look_target: tuple[Any, ...] | None = None
        self._look_wake = threading.Event()
        self._look_stop = threading.Event()
        self._look_thread: threading.Thread | None = None

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        with self._look_lock:
            if self._look_thread is not None and self._look_thread.is_alive():
                return
            self._look_stop.clear()
            self._look_thread = threading.Thread(target=self._look_loop, name="attention-look", daemon=True)
            self._look_thread.start()

    def stop(self) -> None:
        with self._look_lock:
            self._look_stop.set()
            self._look_target = None
            thread = self._look_thread
        self._look_wake.set()
        if thread:
            thread.join(timeout=1.0)

    def on_event(self, event: Event) -> None:
        # Unlike the base class this never drops an event because another one is being handled:
        # a lost speech.final or speech.aborted would leave the utterance state wrong.
        if not self.enabled:
            return
        try:
            with self._state:
                result = self.react(event)
        except Exception as exc:
            log.warning("reflex %s failed: %s", self.name, exc)
            return
        if result is not None:
            self.count += 1
            self.last_fired = time.time()
            self.rt.bus.publish("reflex.completed", {"reflex": self.name, "trigger": event.type, "result": result},
                                source="reflex")

    # ------------------------------------------------------------- reactions
    def react(self, event: Event) -> Any:
        rt = self.rt
        leds = rt.leds if self.params["led_moods"] else None
        t, d = event.type, event.data
        if t == "sound.located":
            return self._on_sound(d)
        if t == "speech.started":
            at = float(d.get("at") or time.time())
            if at <= self._last_ended_start + SAME_UTTERANCE_S:
                return None  # handled after its own end event
            if leds:
                leds.set_mood("listening")
            self._utterance_started = at
            self._aimed, self._aimed_person = False, None
            return self._aim(at, None, confirmed=False)
        if t == "speech.final":
            if leds:
                leds.set_mood("thinking")
            start = float(d.get("started_at") or time.time())
            current = self._ends_current(start)
            result = None
            if current or self._utterance_started is None:
                result = self._aim(start, d.get("ended_at"), confirmed=True)
            if current:
                self._utterance_started = None
            self._last_ended_start = max(self._last_ended_start, start)
            self._last_talk = time.time()
            return result
        if t == "speech.aborted":
            # noise the listener rejected: forget it without moving
            start = d.get("started_at")
            if start is None or self._ends_current(float(start)):
                self._utterance_started = None
            if start is not None:
                self._last_ended_start = max(self._last_ended_start, float(start))
            return None
        if t == "tts.started":
            if leds:
                leds.set_mood("speaking")
            return None
        if t == "tts.finished":
            if leds and not rt.speech.speaking:
                leds.set_mood("neutral")
            return None
        if t == "person.appeared":
            return self._on_new_person(d)
        return None

    def _ends_current(self, started_at: float) -> bool:
        return self._utterance_started is not None and abs(started_at - self._utterance_started) <= SAME_UTTERANCE_S

    def _open_utterance(self) -> float | None:
        start = self._utterance_started
        if start is not None and time.time() - start > UTTERANCE_TIMEOUT_S:
            self._utterance_started = start = None
        return start

    def _on_sound(self, d: dict[str, Any]) -> Any:
        if self.rt.speech.speaking:
            return None  # that sound is probably us
        if d.get("confidence", 0) < self.params["sound_confidence"] or d.get("energy", 0) < self.params["min_sound_energy"]:
            return None
        self._sounds.append(d)
        start = self._open_utterance()
        if start is not None and not self._aimed:
            return self._aim(start, None, confirmed=False)  # the voice's direction just became known
        return None

    def _head_free(self) -> bool:
        tracker = self.rt.tracker
        if not self.params["look_at_sound"] or tracker is None:
            return False
        return not (tracker.following or tracker.active_target)  # tracker owns the head

    def _utterance_sound(self, start: float, end: float | None) -> dict[str, Any] | None:
        # localisation can arrive slightly before the VAD declares speech
        window = [s for s in self._sounds
                  if s.get("ts", 0) >= start - 0.5 and (end is None or s.get("ts", 0) <= float(end) + 0.5)]
        if not window:
            return None
        return max(window, key=lambda s: s.get("confidence", 0) * max(s.get("energy", 0), 0.01))

    def _person_toward(self, azimuth_deg: float) -> dict[str, Any] | None:
        perception = self.rt.perception
        if perception is None:
            return None
        best, best_diff = None, self.params["person_match_deg"]
        for p in perception.people():
            bearing = p.get("bearing_deg")
            if bearing is None:
                continue
            diff = _angle_diff(float(bearing), azimuth_deg)
            if diff <= best_diff:
                best, best_diff = p, diff
        return best

    def _aim(self, start: float, end: float | None, confirmed: bool) -> Any:
        if not self._head_free():
            return None
        sound = self._utterance_sound(start, end)
        if sound is None:
            return None
        azimuth = float(sound["azimuth_deg"])
        person = self._person_toward(azimuth)
        if person is not None:
            pid = person.get("person_id")
            if self._aimed and pid == self._aimed_person:
                return None
            self._aimed, self._aimed_person = True, pid
            self._request_look("person", person.get("position_m"), person.get("bearing_deg"))
            return {"looked_at_person": pid, "voice_azimuth_deg": round(azimuth, 1)}
        if not confirmed or self._aimed:
            return None  # wait for the transcript before turning toward a voice nobody is seen at
        self._request_look("voice", azimuth)
        return {"looked_at_voice_azimuth_deg": round(azimuth, 1)}

    def _on_new_person(self, d: dict[str, Any]) -> Any:
        if not self.params["look_at_new_person"] or not self._head_free():
            return None
        if self._open_utterance() is not None or time.time() - self._last_talk < self.params["new_person_quiet_s"]:
            return None  # stay with whoever is talking
        self._request_look("person", d.get("position_m"), d.get("bearing_deg"))
        return {"looked_at_person": d.get("person_id")}

    # ------------------------------------------------------------- head moves
    def _request_look(self, *target: Any) -> None:
        # Latest request wins; lookAt blocks until the head arrives, so it runs off the event path.
        with self._look_lock:
            if self._look_stop.is_set():
                return  # stopped: a late event must not restart the worker
            self._look_target = target
        self._look_wake.set()

    def _look_loop(self) -> None:
        while True:
            self._look_wake.wait()
            self._look_wake.clear()
            with self._look_lock:
                if self._look_stop.is_set():
                    return
                target, self._look_target = self._look_target, None
            if target is None:
                continue
            try:
                self._do_look(target)
            except Exception as exc:
                log.warning("attention could not turn the head (%s): %s", target[0], exc)

    def _do_look(self, target: tuple[Any, ...]) -> None:
        tracker = self.rt.tracker
        if tracker is None:
            return
        if target[0] == "person":
            position, bearing = target[1], target[2]
            height = position[2] if position and len(position) >= 3 and position[2] else self.params["speaker_height_m"]
            distance = math.hypot(position[0], position[1]) if position and len(position) >= 2 else None
        else:
            position, bearing = None, float(target[1])
            height, distance = self.params["speaker_height_m"], None
        distance = distance or self.params["speaker_distance_m"]
        if bearing is not None and self._turn_body(float(bearing)):
            # the speaker is now straight ahead
            tracker.look_at(distance, 0.0, height, "robot", 0.6)
            return
        if target[0] == "person":
            tracker.look_at_person(position, bearing)
            return
        a = math.radians(float(bearing))
        tracker.look_at(distance * math.cos(a), distance * math.sin(a), height, "robot", 0.6)

    def _turn_body(self, bearing_deg: float) -> bool:
        """Rotate in place toward ``bearing_deg`` if it is too far to the side for a natural head turn."""
        if not self.params["turn_body"] or abs(bearing_deg) < self.params["turn_body_min_deg"]:
            return False
        motion = self.rt.motion
        try:
            if motion.motion.moveIsActive():
                return False  # someone else (LLM move, navigation) is driving the base
        except Exception as exc:
            log.debug("moveIsActive: %s", exc)
        try:
            motion.move_relative(0.0, 0.0, math.radians(bearing_deg), wait_for_base=False)
        except Exception as exc:
            # blocked (usually something inside the safety distance): at least turn the head
            log.info("attention body turn toward %.0f deg failed: %s", bearing_deg, exc)
            return False
        return True
