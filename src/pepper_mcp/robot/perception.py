"""People, gaze, waving, engagement, touch, sonar, battery, sound → events.

All data comes from ALMemory keys written by NAOqi's extractors. Extractors only run
while something is subscribed to them, so :meth:`Perception.start` subscribes to each one
that exists and :class:`MemoryWatcher` delivers changes (subscribe or poll).
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any

from pepper_mcp.robot.connection import RobotConnection
from pepper_mcp.robot.events import EventBus, MemoryWatcher

log = logging.getLogger(__name__)

SUB_NAME = "pepper_mcp"

TOUCH_KEYS = {
    "FrontTactilTouched": "head_front", "MiddleTactilTouched": "head_middle", "RearTactilTouched": "head_rear",
    "HandLeftBackTouched": "left_hand_back", "HandLeftLeftTouched": "left_hand_left",
    "HandLeftRightTouched": "left_hand_right",
    "HandRightBackTouched": "right_hand_back", "HandRightLeftTouched": "right_hand_left",
    "HandRightRightTouched": "right_hand_right",
    "LeftBumperPressed": "left_bumper", "RightBumperPressed": "right_bumper", "BackBumperPressed": "back_bumper",
}

EXPRESSIONS = ["neutral", "happy", "surprised", "angry", "sad"]

EXTRACTORS = {
    # name -> (service, memory key subscription needed?)
    "people": "ALPeoplePerception",
    "waving": "ALWavingDetection",
    "gaze": "ALGazeAnalysis",
    "engagement": "ALEngagementZones",
    "face_detection": "ALFaceDetection",
    "sound_localization": "ALSoundLocalization",
    "face_characteristics": "ALFaceCharacteristics",
}

SONAR_FRONT = "Device/SubDeviceList/Platform/Front/Sonar/Sensor/Value"
SONAR_BACK = "Device/SubDeviceList/Platform/Back/Sonar/Sensor/Value"
BATTERY = "Device/SubDeviceList/Battery/Charge/Sensor/Value"


class Perception:
    def __init__(self, conn: RobotConnection, bus: EventBus, memory: MemoryWatcher):
        self.conn = conn
        self.bus = bus
        self.memory = memory
        self.active: dict[str, bool] = {}
        self._people: dict[int, dict[str, Any]] = {}
        self._touched: dict[str, bool] = {}
        self._last_sound: dict[str, Any] | None = None
        self._started = False
        self._waving_state: dict[int, bool] = {}
        self._gaze_state: dict[int, bool] = {}

    # ------------------------------------------------------------- lifecycle
    def start(self, extractors: dict[str, bool] | None = None) -> dict[str, bool]:
        wanted = {name: True for name in EXTRACTORS}
        if extractors:
            wanted.update(extractors)
        for name, on in wanted.items():
            self.set_extractor(name, on)
        if not self._started:
            self._started = True
            self.memory.watch("PeoplePerception/PeopleList", self._on_people_list, poll_s=0.2)
            self.memory.watch("PeoplePerception/PeopleDetected", lambda k, v: None, poll_s=0.5)
            for key in TOUCH_KEYS:
                self.memory.watch(key, self._on_touch, poll_s=0.1)
            self.memory.watch("ALSoundLocalization/SoundLocated", self._on_sound, poll_s=0.15, edge_only=True)
            self.memory.watch("EngagementZones/PersonApproached", self._on_engaged, poll_s=0.3)
            self.memory.watch("EngagementZones/PersonEnteredZone1", self._on_zone1, poll_s=0.3)
            self.memory.watch("robotIsFalling", self._on_fall, poll_s=0.2)
            self.memory.watch("robotHasFallen", self._on_fall, poll_s=0.2)
            self.memory.watch("ALMotion/RobotIsPushed", self._on_pushed, poll_s=0.3)
            self.memory.watch(BATTERY, self._on_battery, poll_s=5.0)
            self.memory.watch("HotJointDetected", self._on_hot_joint, poll_s=2.0)
            self.memory.watch("ALTabletService/onTouchDown", self._on_tablet_touch, poll_s=0.2)
            self.memory.watch("WavingDetection/PersonWaving", self._on_waving_event, poll_s=0.15)
        return dict(self.active)

    def stop(self) -> None:
        for name in list(self.active):
            self.set_extractor(name, False)

    def set_extractor(self, name: str, enabled: bool) -> bool:
        service_name = EXTRACTORS.get(name)
        if service_name is None:
            return False
        svc = self.conn.try_service(service_name)
        if svc is None:
            self.active[name] = False
            return False
        try:
            if enabled:
                svc.subscribe(SUB_NAME)
                if name == "people":
                    try:
                        svc.setFastModeEnabled(True)
                    except Exception:
                        pass
                if name == "sound_localization":
                    try:
                        svc.setParameter("Sensitivity", 0.85)
                    except Exception:
                        pass
            else:
                svc.unsubscribe(SUB_NAME)
            self.active[name] = enabled
        except Exception as exc:
            log.info("extractor %s %s failed: %s", name, "on" if enabled else "off", exc)
            self.active[name] = False
        return self.active[name]

    # ------------------------------------------------------------- people
    def _person_key(self, pid: int, suffix: str) -> Any:
        return self.memory.get(f"PeoplePerception/Person/{pid}/{suffix}")

    def _on_people_list(self, key: str, value: Any) -> None:
        try:
            ids = {int(i) for i in (value or [])}
        except Exception:
            ids = set()
        known = set(self._people)
        for pid in sorted(ids - known):
            info = self.person_info(pid)
            self._people[pid] = info
            self.bus.publish("person.appeared", info, source="perception")
        for pid in known - ids:
            info = self._people.pop(pid, {"person_id": pid})
            self._waving_state.pop(pid, None)
            self._gaze_state.pop(pid, None)
            self.bus.publish("person.left", {"person_id": pid, "last_distance_m": info.get("distance_m")},
                             source="perception")
        # refresh waving / gaze on present people (cheap memory reads)
        for pid in ids:
            waving = self._person_key(pid, "IsWaving")
            if waving is not None:
                waving = bool(waving)
                if waving and not self._waving_state.get(pid, False):
                    side = "center"
                    for s in ("Left", "Right", "Center"):
                        if self._person_key(pid, f"IsWaving{s}"):
                            side = s.lower()
                    self.bus.publish("person.waving", {"person_id": pid, "side": side}, source="perception")
                self._waving_state[pid] = waving
            looking = self._person_key(pid, "IsLookingAtRobot")
            if looking is not None:
                looking = bool(looking)
                if looking != self._gaze_state.get(pid):
                    self._gaze_state[pid] = looking
                    self.bus.publish("person.looking_at_robot", {"person_id": pid, "looking": looking},
                                     source="perception")

    def _on_waving_event(self, key: str, value: Any) -> None:
        # Event payload (2.5): [timestamp, [[confidence, type, position, person_id], ...]] – be tolerant.
        pid = None
        try:
            clusters = value[1] if isinstance(value, (list, tuple)) and len(value) > 1 else []
            if clusters and isinstance(clusters[0], (list, tuple)) and len(clusters[0]) >= 4:
                pid = int(clusters[0][3])
        except Exception:
            pid = None
        if pid is not None and self._waving_state.get(pid):
            return  # already reported via the IsWaving key
        self.bus.publish("person.waving", {"person_id": pid, "side": "unknown"}, source="perception")

    def person_info(self, pid: int) -> dict[str, Any]:
        info: dict[str, Any] = {"person_id": pid}
        dist = self._person_key(pid, "Distance")
        if dist is not None:
            info["distance_m"] = round(float(dist), 2)
        pos = self._person_key(pid, "PositionInRobotFrame")
        if pos and len(pos) >= 2:
            info["position_m"] = [round(float(p), 2) for p in pos[:3]]
            info["bearing_deg"] = round(math.degrees(math.atan2(float(pos[1]), float(pos[0]))), 1)
        angles = self._person_key(pid, "AnglesYawPitch")
        if angles and len(angles) >= 2 and "bearing_deg" not in info:
            info["bearing_deg"] = round(math.degrees(float(angles[0])), 1)
            info["elevation_deg"] = round(math.degrees(float(angles[1])), 1)
        for key, name in (("IsFaceDetected", "face_detected"), ("IsWaving", "waving"),
                          ("IsLookingAtRobot", "looking_at_robot"), ("IsVisible", "visible"),
                          ("PresentSince", "present_since_s")):
            val = self._person_key(pid, key)
            if val is not None:
                info[name] = bool(val) if name != "present_since_s" else round(float(val), 1)
        zone = self._person_key(pid, "EngagementZone")
        if zone is not None:
            info["engagement_zone"] = int(zone)
        info.update(self.face_properties(pid))
        return info

    def face_properties(self, pid: int) -> dict[str, Any]:
        out: dict[str, Any] = {}
        age = self._person_key(pid, "AgeProperties")
        if age:
            out["age"] = int(round(age[0]))
            out["age_confidence"] = round(float(age[1]), 2) if len(age) > 1 else None
        gender = self._person_key(pid, "GenderProperties")
        if gender:
            out["gender"] = "male" if gender[0] >= 0.5 else "female"
        smile = self._person_key(pid, "SmileProperties")
        if smile:
            out["smiling"] = bool(smile[0] > 0.5)
            out["smile_degree"] = round(float(smile[0]), 2)
        expr = self._person_key(pid, "ExpressionProperties")
        if expr:
            out["expression"] = EXPRESSIONS[list(expr).index(max(expr))]
        return out

    def people(self) -> list[dict[str, Any]]:
        ids = self.memory.get("PeoplePerception/PeopleList") or []
        out = []
        for pid in ids:
            try:
                out.append(self.person_info(int(pid)))
            except Exception as exc:
                log.debug("person %s: %s", pid, exc)
        out.sort(key=lambda p: p.get("distance_m", 99))
        return out

    def nearest_person(self) -> dict[str, Any] | None:
        people = self.people()
        return people[0] if people else None

    def analyze_face(self, pid: int | None = None, timeout_s: float = 2.0) -> dict[str, Any]:
        if pid is None:
            p = self.nearest_person()
            if p is None:
                return {"error": "nobody in front of me"}
            pid = p["person_id"]
        svc = self.conn.try_service("ALFaceCharacteristics")
        if svc is None:
            return {"person_id": pid, "error": "ALFaceCharacteristics unavailable"}
        ok = False
        try:
            ok = bool(svc.analyzeFaceCharacteristics(pid))
        except Exception as exc:
            log.debug("analyzeFaceCharacteristics: %s", exc)
        deadline = time.monotonic() + timeout_s
        props = self.face_properties(pid)
        while not props and time.monotonic() < deadline:
            time.sleep(0.1)
            props = self.face_properties(pid)
        return {"person_id": pid, "analyzed": ok, **props}

    # ------------------------------------------------------------- touch
    def _on_touch(self, key: str, value: Any) -> None:
        try:
            touched = float(value) >= 0.5
        except (TypeError, ValueError):
            return
        prev = self._touched.get(key, False)
        self._touched[key] = touched
        if touched != prev:
            self.bus.publish("touch.start" if touched else "touch.end",
                             {"sensor": TOUCH_KEYS.get(key, key), "key": key}, source="perception")

    def touch_state(self) -> dict[str, bool]:
        state = {}
        for key, name in TOUCH_KEYS.items():
            val = self.memory.get(key)
            if val is None:
                continue
            try:
                state[name] = float(val) >= 0.5
            except (TypeError, ValueError):
                pass
        return state

    def any_touched(self, keys: list[str] | tuple[str, ...]) -> bool:
        for key in keys:
            val = self.memory.get(key)
            try:
                if val is not None and float(val) >= 0.5:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    # ------------------------------------------------------------- sound
    def _on_sound(self, key: str, value: Any) -> None:
        # [[sec, usec], [azimuth, elevation, confidence, energy], [head pos 6D], [head pos 6D]]
        try:
            az, el, conf, energy = (float(v) for v in value[1][:4])
        except Exception:
            return
        info = {"azimuth_deg": round(math.degrees(az), 1), "elevation_deg": round(math.degrees(el), 1),
                "confidence": round(conf, 2), "energy": round(energy, 2), "ts": time.time()}
        self._last_sound = info
        self.bus.publish("sound.located", info, source="perception")

    def last_sound(self) -> dict[str, Any] | None:
        if self._last_sound is None:
            return None
        return {**self._last_sound, "age_s": round(time.time() - self._last_sound["ts"], 1)}

    # ------------------------------------------------------------- misc events
    def _on_engaged(self, key: str, value: Any) -> None:
        try:
            pid = int(value)
        except Exception:
            pid = None
        self.bus.publish("person.engaged", {"person_id": pid, "zone": 1, "event": "approached"}, source="perception")

    def _on_zone1(self, key: str, value: Any) -> None:
        try:
            pid = int(value)
        except Exception:
            pid = None
        self.bus.publish("person.engaged", {"person_id": pid, "zone": 1, "event": "entered_zone"},
                         source="perception")

    def _on_fall(self, key: str, value: Any) -> None:
        if value:
            self.bus.publish("robot.fell", {"key": key}, source="perception")

    def _on_pushed(self, key: str, value: Any) -> None:
        if value:
            self.bus.publish("robot.pushed", {"value": value}, source="perception")

    def _on_battery(self, key: str, value: Any) -> None:
        try:
            pct = int(round(float(value) * 100))
        except Exception:
            return
        if pct <= 15:
            self.bus.publish("battery.low", {"percent": pct}, source="perception")

    def _on_hot_joint(self, key: str, value: Any) -> None:
        if value:
            self.bus.publish("robot.hot_joint", {"joints": value}, source="perception")

    def _on_tablet_touch(self, key: str, value: Any) -> None:
        try:
            x, y = float(value[0]), float(value[1])
        except Exception:
            x = y = None
        self.bus.publish("tablet.touched", {"x": x, "y": y}, source="perception")

    # ------------------------------------------------------------- sensors
    def sonar(self) -> dict[str, float | None]:
        def _f(key: str) -> float | None:
            v = self.memory.get(key)
            try:
                return round(float(v), 2) if v is not None else None
            except (TypeError, ValueError):
                return None
        return {"front_m": _f(SONAR_FRONT), "back_m": _f(SONAR_BACK)}

    def battery(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        val = self.memory.get(BATTERY)
        if val is not None:
            try:
                out["percent"] = int(round(float(val) * 100))
            except (TypeError, ValueError):
                pass
        bat = self.conn.try_service("ALBattery")
        if bat is not None:
            try:
                out["percent"] = int(bat.getBatteryCharge())
            except Exception:
                pass
        charging = self.memory.get("Device/SubDeviceList/Battery/Current/Sensor/Value")
        if charging is not None:
            try:
                out["charging"] = float(charging) > 0
            except (TypeError, ValueError):
                pass
        return out
