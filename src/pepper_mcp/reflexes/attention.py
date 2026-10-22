"""Attention: eye colour follows the conversation state and the head turns toward
whoever is speaking (sound localisation) or a person who just appeared."""

from __future__ import annotations

import logging
from typing import Any

from pepper_mcp.reflexes.base import Reflex
from pepper_mcp.robot.events import Event

log = logging.getLogger(__name__)


class AttentionReflex(Reflex):
    name = "attention"
    description = ("eyes turn cyan while a person speaks, purple while the LLM is thinking (between a "
                   "speech.final and the next say), white while Pepper talks; head turns toward speakers")
    triggers = ("speech.started", "speech.final", "tts.started", "tts.finished", "sound.located", "person.appeared")
    params: dict[str, Any] = {"led_moods": True, "look_at_sound": True, "look_at_new_person": True,
                              "min_sound_energy": 0.0, "sound_confidence": 0.3}

    def react(self, event: Event) -> Any:
        rt = self.rt
        leds = rt.leds
        if event.type == "speech.started":
            if self.params["led_moods"] and leds:
                leds.set_mood("listening")
            return None
        if event.type == "speech.final":
            if self.params["led_moods"] and leds:
                leds.set_mood("thinking")
            return None
        if event.type == "tts.started":
            if self.params["led_moods"] and leds:
                leds.set_mood("speaking")
            return None
        if event.type == "tts.finished":
            if self.params["led_moods"] and leds and not rt.speech.speaking:
                leds.set_mood("neutral")
            return None
        if event.type == "sound.located" and self.params["look_at_sound"] and rt.tracker:
            if rt.tracker.following or rt.tracker.active_target:
                return None  # tracker owns the head
            d = event.data
            if d.get("confidence", 0) < self.params["sound_confidence"] or d.get("energy", 0) < self.params["min_sound_energy"]:
                return None
            if rt.speech.speaking:
                return None  # that sound is probably us
            try:
                rt.tracker.look_at_sound(d["azimuth_deg"], d.get("elevation_deg", 0.0))
                return {"looked_at_azimuth_deg": d["azimuth_deg"]}
            except Exception as exc:
                log.debug("look at sound: %s", exc)
            return None
        if event.type == "person.appeared" and self.params["look_at_new_person"] and rt.tracker:
            if rt.tracker.following or rt.tracker.active_target:
                return None
            d = event.data
            try:
                if rt.tracker.look_at_person(d.get("position_m"), d.get("bearing_deg")):
                    return {"looked_at_person": d.get("person_id")}
            except Exception as exc:
                log.debug("look at person: %s", exc)
        return None
