"""An in-memory stand-in for NAOqi.

Used by the test-suite and by ``pepper-mcp --fake`` so the whole MCP surface can be
exercised (and demoed to an LLM) without a robot. Services record every call, return
plausible values and support the ``_async=True`` convention. The fake ALMemory can be
scripted from tests (``fake.memory_set(key, value)``) and simulates a person, touch and
speech so reflexes and events can be tested end-to-end.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from pepper_mcp.robot.connection import RobotConnection, ServiceUnavailable, _ThreadFuture

log = logging.getLogger(__name__)


class FakeFuture(_ThreadFuture):
    pass


class FakeService:
    """Records calls; ``returns`` maps method name -> value or callable(*args)."""

    def __init__(self, name: str, returns: dict[str, Any] | None = None, latency_s: float = 0.0):
        self.name = name
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.returns = dict(returns or {})
        self.latency_s = latency_s
        self._lock = threading.Lock()

    def __getattr__(self, method: str) -> Callable[..., Any]:
        if method.startswith("__"):
            raise AttributeError(method)

        def _call(*args: Any, _async: bool = False, **kwargs: Any) -> Any:
            if _async:
                return FakeFuture(lambda: _call(*args, **kwargs), ())
            with self._lock:
                self.calls.append((method, args))
            if self.latency_s:
                time.sleep(self.latency_s)
            value = self.returns.get(method)
            if callable(value):
                return value(*args)
            return value

        return _call

    def count(self, method: str) -> int:
        with self._lock:
            return sum(1 for m, _ in self.calls if m == method)

    def last(self, method: str) -> tuple[Any, ...] | None:
        with self._lock:
            for m, args in reversed(self.calls):
                if m == method:
                    return args
        return None


class FakeMemory(FakeService):
    def __init__(self, subscriber_works: bool = False):
        super().__init__("ALMemory")
        self.data: dict[str, Any] = {
            "PeoplePerception/PeopleList": [],
            "Device/SubDeviceList/Battery/Charge/Sensor/Value": 0.87,
            "Device/SubDeviceList/Platform/Front/Sonar/Sensor/Value": 1.4,
            "Device/SubDeviceList/Platform/Back/Sonar/Sensor/Value": 2.1,
        }
        self.subscriber_works = subscriber_works
        self._subs: dict[str, FakeSignal] = {}

    def getData(self, key: str) -> Any:
        if key not in self.data:
            raise RuntimeError(f"ALMemory::getData key not found: {key}")
        return self.data[key]

    def insertData(self, key: str, value: Any) -> None:
        self.data[key] = value
        sig = self._subs.get(key)
        if sig is not None:
            sig.emit(value)

    def raiseEvent(self, key: str, value: Any) -> None:
        self.insertData(key, value)

    def subscriber(self, key: str) -> Any:
        if not self.subscriber_works:
            raise RuntimeError("Invalid signature")
        sig = self._subs.setdefault(key, FakeSignal())
        return _Sub(sig)


class FakeSignal:
    def __init__(self) -> None:
        self._cbs: dict[int, Callable[[Any], None]] = {}
        self._n = 0

    def connect(self, cb: Callable[[Any], None]) -> int:
        self._n += 1
        self._cbs[self._n] = cb
        return self._n

    def disconnect(self, link: int) -> None:
        self._cbs.pop(link, None)

    def emit(self, value: Any) -> None:
        for cb in list(self._cbs.values()):
            cb(value)


class _Sub:
    def __init__(self, signal: FakeSignal):
        self.signal = signal


class FakeVideo(FakeService):
    """Produces a moving gradient so snapshots differ frame to frame."""

    def __init__(self) -> None:
        super().__init__("ALVideoDevice")
        self._handles: dict[str, tuple[int, int, int]] = {}
        self._n = 0

    def subscribeCamera(self, name: str, cam: int, res: int, cs: int, fps: int) -> str:
        # like NAOqi: "<name>_<n>" with the first free n, and at most 8 handles per name
        for n in range(8):
            handle = f"{name}_{n}"
            if handle not in self._handles:
                self._handles[handle] = (cam, res, cs)
                return handle
        return ""

    def getSubscribers(self) -> list[str]:
        return list(self._handles)

    def unsubscribe(self, handle: str) -> bool:
        self._handles.pop(handle, None)
        return True

    def setFrameRate(self, handle: str, fps: int) -> bool:
        return True

    def releaseImage(self, handle: str) -> bool:
        return True

    def getImageRemote(self, handle: str) -> list[Any] | None:
        if handle not in self._handles:
            return None
        import numpy as np

        cam, res, cs = self._handles[handle]
        w, h = {0: (160, 120), 1: (320, 240), 2: (640, 480), 3: (1280, 960)}[res]
        self._n += 1
        time.sleep(0.02)
        if cs == 17:  # depth, 16-bit little endian millimetres
            depth = (500 + np.arange(w) * 3000 // w).astype("<u2")
            data = np.tile(depth, (h, 1)).tobytes()
        else:
            shift = self._n % 256
            xs = ((np.arange(w) * 255 // w + shift) % 256).astype(np.uint8)
            ys = ((np.arange(h) * 255 // h) % 256).astype(np.uint8)
            img = np.zeros((h, w, 3), dtype=np.uint8)
            img[:, :, 0] = xs[None, :]
            img[:, :, 1] = ys[:, None]
            img[:, :, 2] = 128
            data = img.tobytes()
        return [w, h, 3 if cs != 17 else 2, cs, 0, 0, data, cam]


class FakeAudioDevice(FakeService):
    """Calls the registered module's processRemote with synthetic PCM when subscribed."""

    def __init__(self, robot: FakeRobot):
        super().__init__("ALAudioDevice", returns={"getOutputVolume": 55})
        self.robot = robot
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.volume = 55
        self.prefs: tuple[Any, ...] | None = None

    def setOutputVolume(self, level: int) -> None:
        self.volume = level

    def getOutputVolume(self) -> int:
        return self.volume

    def setClientPreferences(self, *args: Any) -> None:
        self.prefs = args

    def subscribe(self, name: str) -> None:
        module = self.robot.registered.get(name)
        if module is None:
            raise RuntimeError(f"no module named {name} registered")
        self._stop.clear()
        rate = int(self.prefs[1]) if self.prefs else 16000

        def _pump() -> None:
            import numpy as np

            n = 2730 if rate == 16000 else 8192
            t = 0.0
            while not self._stop.is_set():
                samples = self.robot.audio_source(t, n, rate)
                buf = np.asarray(samples, dtype="<i2").tobytes()
                try:
                    module.processRemote(1, n, [int(time.time()), 0], buf)
                except Exception as exc:
                    log.debug("fake processRemote: %s", exc)
                t += n / rate
                self._stop.wait(n / rate)

        self._thread = threading.Thread(target=_pump, name="fake-audio", daemon=True)
        self._thread.start()

    def unsubscribe(self, name: str) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None


class FakeSession:
    def __init__(self, robot: FakeRobot):
        self.robot = robot

    def service(self, name: str) -> Any:
        svc = self.robot.services.get(name)
        if svc is None:
            raise RuntimeError(f"Cannot find service '{name}' in index")
        return svc

    def services(self) -> list[dict[str, Any]]:
        return [{"name": n} for n in sorted(self.robot.services)]

    def registerService(self, name: str, obj: Any) -> int:
        self.robot.registered[name] = obj
        return len(self.robot.registered)

    def unregisterService(self, service_id: int) -> None:
        pass

    def isConnected(self) -> bool:
        return True

    def endpoints(self) -> list[str]:
        return ["tcp://127.0.0.1:0"]


class FakeRobot:
    """Holds the fake services; also the test harness entry point."""

    def __init__(self, subscriber_works: bool = False, missing: set[str] | None = None):
        self.registered: dict[str, Any] = {}
        self.memory = FakeMemory(subscriber_works)
        self.spoken: list[str] = []
        self._audio_fn: Callable[[float, int, int], Any] | None = None
        animations = [f"animations/Stand/Gestures/{n}" for n in ("Hey_1", "Hey_3", "Yes_1", "No_1", "Explain_1",
                                                                  "BowShort_1", "Thinking_1")]
        animations += ["animations/Stand/Emotions/Positive/Happy_4", "animations/Stand/Waiting/Robot_1",
                       "followme/behavior_1"]
        tts = FakeService("ALTextToSpeech", returns={"getLanguage": "English", "getVolume": 0.8,
                                                      "getAvailableLanguages": ["English", "French"],
                                                      "getParameter": 100.0, "getVoice": "naoenu"},
                          latency_s=0.05)
        tts.returns["say"] = lambda text, *a: self.spoken.append(text)
        animated = FakeService("ALAnimatedSpeech", latency_s=0.05)
        animated.returns["say"] = lambda text, *a: self.spoken.append(text)
        self.services: dict[str, Any] = {
            "ALMemory": self.memory,
            "ALMotion": FakeService("ALMotion", returns={
                "getAngles": lambda names, use_sensors: [0.0] * len(names),
                "getRobotPosition": lambda *_: [0.1, 0.0, 0.05], "robotIsWakeUp": True,
                "getExternalCollisionProtectionEnabled": True}),
            "ALRobotPosture": FakeService("ALRobotPosture", returns={"goToPosture": True,
                                                                     "getPostureFamily": "Standing"}),
            "ALTextToSpeech": tts,
            "ALAnimatedSpeech": animated,
            "ALAnimationPlayer": FakeService("ALAnimationPlayer", latency_s=0.05),
            "ALBehaviorManager": FakeService("ALBehaviorManager", returns={
                "getInstalledBehaviors": animations, "getRunningBehaviors": [], "getDefaultBehaviors": [],
                "isBehaviorInstalled": lambda n: n in animations}),
            "ALLeds": FakeService("ALLeds"),
            "ALTabletService": FakeService("ALTabletService", returns={"showWebview": True, "getWifiStatus": "CONNECTED"}),
            "ALVideoDevice": FakeVideo(),
            "ALAudioDevice": FakeAudioDevice(self),
            "ALAudioPlayer": FakeService("ALAudioPlayer"),
            "ALTracker": FakeService("ALTracker", returns={"isActive": False}),
            "ALPeoplePerception": FakeService("ALPeoplePerception"),
            "ALWavingDetection": FakeService("ALWavingDetection"),
            "ALGazeAnalysis": FakeService("ALGazeAnalysis"),
            "ALEngagementZones": FakeService("ALEngagementZones"),
            "ALFaceDetection": FakeService("ALFaceDetection"),
            "ALSoundLocalization": FakeService("ALSoundLocalization"),
            "ALFaceCharacteristics": FakeService("ALFaceCharacteristics", returns={"analyzeFaceCharacteristics": True}),
            "ALAutonomousLife": FakeService("ALAutonomousLife", returns={"getState": "solitary",
                                                                         "getAutonomousAbilityEnabled": True}),
            "ALBasicAwareness": FakeService("ALBasicAwareness", returns={"isEnabled": False,
                                                                         "getEngagementMode": "Unengaged",
                                                                         "getTrackingMode": "Head",
                                                                         "engagePerson": True}),
            "ALBackgroundMovement": FakeService("ALBackgroundMovement"),
            "ALListeningMovement": FakeService("ALListeningMovement"),
            "ALSpeakingMovement": FakeService("ALSpeakingMovement"),
            "ALAutonomousBlinking": FakeService("ALAutonomousBlinking"),
            "ALSystem": FakeService("ALSystem", returns={"robotName": "FakePepper", "systemVersion": "2.5.10.7",
                                                        "timezone": "Europe/Zurich"}),
            "ALBattery": FakeService("ALBattery", returns={"getBatteryCharge": 87}),
            "ALNavigation": FakeService("ALNavigation", returns={"navigateTo": True, "saveExploration": "/home/nao/.local/share/Explorer/map.explo",
                                                                "getRobotPositionInMap": [[0.5, 0.2, 0.1], [0.1]],
                                                                "navigateToInMap": 0}),
            "ALSpeechRecognition": FakeService("ALSpeechRecognition"),
            "ALDialog": FakeService("ALDialog", returns={"loadTopicContent": "fake_topic"}),
        }
        for name in missing or set():
            self.services.pop(name, None)
        self.session = FakeSession(self)

    # ------------------------------------------------------------- scripting helpers
    def memory_set(self, key: str, value: Any) -> None:
        self.memory.insertData(key, value)

    def add_person(self, pid: int = 1, distance: float = 1.2, waving: bool = False, looking: bool = True) -> None:
        self.memory_set(f"PeoplePerception/Person/{pid}/Distance", distance)
        self.memory_set(f"PeoplePerception/Person/{pid}/PositionInRobotFrame", [distance, 0.1, 1.2])
        self.memory_set(f"PeoplePerception/Person/{pid}/IsFaceDetected", True)
        self.memory_set(f"PeoplePerception/Person/{pid}/IsWaving", waving)
        self.memory_set(f"PeoplePerception/Person/{pid}/IsLookingAtRobot", looking)
        self.memory_set(f"PeoplePerception/Person/{pid}/AgeProperties", [31.0, 0.6])
        self.memory_set(f"PeoplePerception/Person/{pid}/GenderProperties", [0.7, 0.6])
        self.memory_set(f"PeoplePerception/Person/{pid}/SmileProperties", [0.8, 0.7])
        self.memory_set(f"PeoplePerception/Person/{pid}/ExpressionProperties", [0.1, 0.7, 0.1, 0.05, 0.05])
        ids = list(self.memory.data.get("PeoplePerception/PeopleList", []))
        if pid not in ids:
            ids.append(pid)
        self.memory_set("PeoplePerception/PeopleList", ids)

    def remove_person(self, pid: int = 1) -> None:
        ids = [i for i in self.memory.data.get("PeoplePerception/PeopleList", []) if i != pid]
        self.memory_set("PeoplePerception/PeopleList", ids)

    def touch(self, key: str, on: bool = True) -> None:
        self.memory_set(key, 1.0 if on else 0.0)

    def set_audio_source(self, fn: Callable[[float, int, int], Any] | None) -> None:
        self._audio_fn = fn

    def audio_source(self, t: float, n: int, rate: int) -> Any:
        import numpy as np

        if self._audio_fn is not None:
            return self._audio_fn(t, n, rate)
        return np.zeros(n, dtype=np.int16)


class FakeConnection(RobotConnection):
    """RobotConnection bound to a FakeRobot (no qi import)."""

    def __init__(self, robot: FakeRobot | None = None):
        super().__init__("fake", 0, "")
        self.robot = robot or FakeRobot()
        self._session = self.robot.session
        self.connected_at = time.time()

    def connect(self, timeout_s: float = 10.0) -> None:
        self._session = self.robot.session
        self.connected_at = time.time()

    def close(self) -> None:
        self._services.clear()

    def service(self, name: str) -> Any:
        if name not in self.robot.services:
            raise ServiceUnavailable(f"{name} is not available on this robot")
        return self.robot.services[name]

    def register_service(self, name: str, obj: Any) -> int:
        return self.robot.session.registerService(name, obj)

    def unregister_service(self, name: str) -> None:
        self.robot.registered.pop(name, None)
