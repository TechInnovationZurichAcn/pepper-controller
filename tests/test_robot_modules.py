from __future__ import annotations

import math
import threading
import time

import pytest

from pepper_mcp.robot import audio_in
from pepper_mcp.robot.audio_in import MicrophoneStream, decode_pcm, pcm_to_wav
from pepper_mcp.robot.camera import encode_jpeg
from pepper_mcp.robot.connection import RobotError
from pepper_mcp.robot.events import EventBus
from pepper_mcp.robot.fake import FakeConnection, FakeRobot
from pepper_mcp.robot.gestures import GestureContext, GestureRegistry, KeyframeGesture, builtin_gestures
from pepper_mcp.robot.leds import parse_color, rgb_int
from pepper_mcp.robot.motion import JOINT_LIMITS, Motion, arm_pose_angles, clamp_joint
from pepper_mcp.robot.speech import Speech


def test_decode_pcm_accepts_bytes_and_latin1_str() -> None:
    import numpy as np

    pcm = np.array([1, -2, 300, -400], dtype="<i2").tobytes()
    assert decode_pcm(pcm, 1, 4).tolist() == [1, -2, 300, -400]
    assert decode_pcm(pcm.decode("latin-1"), 1, 4).tolist() == [1, -2, 300, -400]
    # interleaved 4 channels -> channel 0
    inter = np.array([10, 11, 12, 13, 20, 21, 22, 23], dtype="<i2").tobytes()
    assert decode_pcm(inter, 4, 2).tolist() == [10, 20]
    assert decode_pcm(inter, 4, 2, index=2).tolist() == [12, 22]
    wav = pcm_to_wav(np.zeros(16, dtype="<i2"), 16000)
    assert wav[:4] == b"RIFF" and b"WAVE" in wav


@pytest.fixture
def fast_mic_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audio_in, "PROBE_PERIOD_S", 0.0)


def _mic(robot: FakeRobot, rate: int = 16000, mic: str = "auto") -> MicrophoneStream:
    stream = MicrophoneStream(FakeConnection(robot), rate, mic)
    stream.start()
    stream.stop()
    return stream


def test_mic_auto_keeps_front_when_every_mic_works(fast_mic_probe: None) -> None:
    robot = FakeRobot()
    stream = _mic(robot)
    assert stream.mic == "front"
    assert robot.services["ALAudioDevice"].prefs[1:3] == (16000, 3)
    assert not robot.services["ALAudioDevice"].energy_on  # meters switched off again


def test_mic_auto_skips_dead_front_mic(fast_mic_probe: None) -> None:
    # Real robot: the front mic sat at the ADC floor (~72) while the others heard fans (~300-450),
    # so the stream carried silence and Gemini never heard anyone.
    robot = FakeRobot()
    robot.services["ALAudioDevice"].mic_energy["front"] = 72.0
    stream = _mic(robot)
    assert stream.mic == "left"
    assert robot.services["ALAudioDevice"].prefs[1:3] == (16000, 1)
    assert stream.status()["mic_energy"]["front"] == 72.0


def test_mic_auto_at_48k_picks_the_live_interleaved_channel(fast_mic_probe: None) -> None:
    import numpy as np

    robot = FakeRobot()
    robot.services["ALAudioDevice"].mic_energy.update(front=70.0, left=60.0)
    stream = _mic(robot, rate=48000)
    assert stream.mic == "right"
    assert robot.services["ALAudioDevice"].prefs[1:3] == (48000, 0)
    got: list[list[int]] = []
    stream.add_consumer(lambda samples, rate, ts: got.append(samples.tolist()))
    inter = np.array([10, 11, 12, 13, 20, 21, 22, 23], dtype="<i2").tobytes()
    stream._on_buffer(4, 2, 0, inter)
    assert got == [[11, 21]]


def test_mic_explicit_choice_skips_the_probe() -> None:
    robot = FakeRobot()
    stream = _mic(robot, mic="rear")
    assert stream.mic == "rear"
    assert robot.services["ALAudioDevice"].prefs[1:3] == (16000, 4)
    assert stream.mic_energy == {}


def test_mic_probe_failure_keeps_front(fast_mic_probe: None) -> None:
    robot = FakeRobot()
    dev = robot.services["ALAudioDevice"]

    def broken() -> None:
        raise RuntimeError("no energy meter")

    dev.enableEnergyComputation = broken  # type: ignore[method-assign]
    stream = _mic(robot)
    assert stream.mic == "front" and dev.prefs[1:3] == (16000, 3)


def test_mic_all_zero_energies_keep_front(fast_mic_probe: None) -> None:
    robot = FakeRobot()
    robot.services["ALAudioDevice"].mic_energy.update(front=0.0, left=0.0, right=0.0, rear=0.0)
    assert _mic(robot).mic == "front"


def test_mic_failed_switch_leaves_stream_stopped(fast_mic_probe: None) -> None:
    robot = FakeRobot()
    dev = robot.services["ALAudioDevice"]
    dev.mic_energy["front"] = 72.0
    real_subscribe, calls = dev.subscribe, []

    def flaky_subscribe(name: str) -> None:
        calls.append(name)
        if len(calls) > 1:
            raise RuntimeError("subscribe failed")
        real_subscribe(name)

    dev.subscribe = flaky_subscribe  # type: ignore[method-assign]
    stream = MicrophoneStream(FakeConnection(robot), 16000)
    with pytest.raises(RuntimeError, match="subscribe failed"):
        stream.start()
    assert not stream.running and stream.mic == "front"
    assert dev._stop.is_set()  # nothing left subscribed on the robot


def test_mic_failed_switch_falls_back_to_the_previous_mic(fast_mic_probe: None) -> None:
    # Only the switch fails: keep hearing through the probed mic rather than go deaf.
    robot = FakeRobot()
    dev = robot.services["ALAudioDevice"]
    real_subscribe, calls = dev.subscribe, []

    def switch_fails(name: str) -> None:
        calls.append(name)
        if len(calls) == 2:
            raise RuntimeError("subscribe failed")
        real_subscribe(name)

    dev.mic_energy["front"] = 72.0
    dev.subscribe = switch_fails  # type: ignore[method-assign]
    stream = MicrophoneStream(FakeConnection(robot), 16000)
    stream.start()
    try:
        assert stream.running and stream.mic == "front" and dev.prefs[1:3] == (16000, 3)
        assert not dev._stop.is_set()
    finally:
        stream.close()


def test_mic_interrupt_during_probe_unsubscribes(fast_mic_probe: None) -> None:
    # Ctrl+C / SIGTERM during the probe: running is still False, so stop() could not undo it.
    robot = FakeRobot()
    dev = robot.services["ALAudioDevice"]

    def interrupted() -> float:
        raise KeyboardInterrupt

    dev.getLeftMicEnergy = interrupted  # type: ignore[method-assign]
    stream = MicrophoneStream(FakeConnection(robot), 16000)
    with pytest.raises(KeyboardInterrupt):
        stream.start()
    assert not stream.running and dev._stop.is_set()


def test_mic_auto_not_fooled_by_someone_talking_next_to_one_mic(fast_mic_probe: None) -> None:
    robot = FakeRobot()
    robot.services["ALAudioDevice"].mic_energy.update(left=3000.0)  # loud voice at the left side
    assert _mic(robot).mic == "front"


def test_mic_rejects_unknown_choice() -> None:
    from pepper_mcp.config import Settings

    with pytest.raises(ValueError, match="mic must be"):
        MicrophoneStream(FakeConnection(FakeRobot()), 16000, "top")
    with pytest.raises(ValueError, match="mic must be"):
        Settings(fake=True, mic="top").validate()


def test_joint_clamping_and_arm_poses() -> None:
    assert clamp_joint("HeadPitch", 5.0) == JOINT_LIMITS["HeadPitch"][1]
    both = arm_pose_angles("both", "up")
    assert "LShoulderPitch" in both and "RShoulderPitch" in both
    assert math.isclose(both["LShoulderRoll"], -both["RShoulderRoll"])  # mirrored
    with pytest.raises(RobotError):
        arm_pose_angles("left", "nope")


def test_motion_velocity_watchdog_stops_base() -> None:
    robot = FakeRobot()
    motion = Motion(FakeConnection(robot), EventBus())
    motion.move(0.5, 0.0, 0.0, duration_s=0.15)
    alm = robot.services["ALMotion"]
    assert alm.last("moveToward") == (0.5, 0.0, 0.0)
    time.sleep(0.5)
    assert alm.count("stopMove") >= 1
    motion.stop()
    with pytest.raises(RobotError):
        motion.set_angles({"NotAJoint": 0.0})


def test_speech_queue_publishes_events_and_gates() -> None:
    robot = FakeRobot()
    bus = EventBus()
    speech = Speech(FakeConnection(robot), bus)
    u1 = speech.say("Hello.")
    u2 = speech.say("How are you?", wait=True, timeout_s=5)
    assert u1.done.wait(2) and u2.finished_at is not None
    assert robot.spoken == ["Hello.", "How are you?"]
    types = [e.type for e in bus.recent(0)]
    assert types == ["tts.started", "tts.finished", "tts.started", "tts.finished"]
    assert speech.speaking is False and speech.last_spoke_at > 0
    speech.stop()


def test_gesture_registry_validation_and_play() -> None:
    robot = FakeRobot()
    motion = Motion(FakeConnection(robot), EventBus())
    reg = GestureRegistry(lambda: GestureContext(motion=motion))
    assert {"high_five", "wave", "sixty_seven", "clown_dance"} <= set(reg.names())
    with pytest.raises(RobotError):
        reg.define("bad", "x", [{"t_s": 0.5, "joints": {"Nope": 1}}])
    with pytest.raises(RobotError):
        reg.define("bad", "x", [{"t_s": 1.0, "joints": {"HeadYaw": 1}}, {"t_s": 0.5, "joints": {"HeadYaw": 0}}])
    g = reg.define("look_left_quick", "turn head left", [{"t_s": 0.3, "joints": {"HeadYaw": 400}}], return_to_rest=False)
    assert isinstance(g, KeyframeGesture)
    result = reg.play("look_left_quick")
    assert result == {"interrupted": False}
    names, angles, times, _ = robot.services["ALMotion"].last("angleInterpolation")
    assert names == ["HeadYaw"] and angles[0][0] == pytest.approx(JOINT_LIMITS["HeadYaw"][1])  # clamped
    assert reg.remove("look_left_quick") and not reg.remove("wave")
    for gesture in builtin_gestures():
        assert gesture.duration_s >= 0 and gesture.description


def test_wave_raises_arm_high_with_arm_collision_protection_off() -> None:
    # Regression: with a person in front, ALMotion's arm collision protection froze the wave
    # at shoulder height; the arm must also go well above the shoulder (pitch < -60 deg).
    robot = FakeRobot()
    motion = Motion(FakeConnection(robot), EventBus())
    reg = GestureRegistry(lambda: GestureContext(motion=motion))
    almotion = robot.services["ALMotion"]
    assert reg.play("wave") == {"interrupted": False}
    toggles = [args for m, args in almotion.calls if m == "setExternalCollisionProtectionEnabled"]
    assert toggles == [("Arms", False), ("Arms", True)]
    order = [m for m, _ in almotion.calls
             if m in ("setExternalCollisionProtectionEnabled", "angleInterpolation")]
    assert order == ["setExternalCollisionProtectionEnabled", "angleInterpolation",
                     "setExternalCollisionProtectionEnabled"]
    names, angles, _, _ = almotion.last("angleInterpolation")
    pitch = angles[names.index("RShoulderPitch")]
    assert min(pitch) < math.radians(-60)
    elbow = angles[names.index("RElbowRoll")]
    assert max(elbow) - min(elbow) > math.radians(30)  # the forearm actually swings


def test_keyframe_gesture_reenables_collision_protection_when_motion_fails() -> None:
    robot = FakeRobot()
    motion = Motion(FakeConnection(robot), EventBus())

    def _fail(*_args: object) -> None:
        raise RobotError("ALMotion refused")

    motion.interpolate_async = _fail  # type: ignore[method-assign]
    gesture = next(g for g in builtin_gestures() if g.name == "wave")
    with pytest.raises(RobotError):
        gesture.play(GestureContext(motion=motion), threading.Event(), {})
    assert robot.services["ALMotion"].last("setExternalCollisionProtectionEnabled") == ("Arms", True)


def test_arms_down_sets_every_arm_joint_and_untwists_before_lowering() -> None:
    # Regression: arms_down left ElbowYaw/WristYaw where the previous gesture put them, so one
    # forearm hung into the hip on the way down and collision avoidance stopped that arm.
    robot = FakeRobot()
    motion = Motion(FakeConnection(robot), EventBus())
    reg = GestureRegistry(lambda: GestureContext(motion=motion))
    almotion = robot.services["ALMotion"]
    assert reg.play("arms_down") == {"interrupted": False}
    toggles = [args for m, args in almotion.calls if m == "setExternalCollisionProtectionEnabled"]
    assert toggles == [("Arms", False), ("Arms", True)]
    names, angles, _, _ = almotion.last("angleInterpolation")
    final = {n: a[-1] for n, a in zip(names, angles, strict=True)}
    for joint, value in arm_pose_angles("both", "down").items():  # the validated StandInit-like pose
        assert final[joint] == pytest.approx(value, abs=math.radians(3)), joint
    for side in ("L", "R"):
        pitch = angles[names.index(f"{side}ShoulderPitch")]
        assert pitch[0] == pytest.approx(0.0)  # held at the current angle while untwisting
        assert angles[names.index(f"{side}ElbowYaw")][0] == pytest.approx(final[f"{side}ElbowYaw"])  # untwisted first


def test_thinking_pose_gesture_removed() -> None:
    assert "thinking_pose" not in {g.name for g in builtin_gestures()}


def test_leds_colors() -> None:
    assert parse_color("red") == (1, 0, 0)
    assert parse_color("#00ff00") == (0.0, 1.0, 0.0)
    assert rgb_int((1, 0, 0)) == 0xFF0000
    with pytest.raises(RobotError):
        parse_color("ultraviolet")


def test_encode_jpeg_rgb_and_depth() -> None:
    import numpy as np

    from pepper_mcp.robot.camera import COLOR_DEPTH, COLOR_RGB, Frame

    rgb = Frame("top", 8, 4, bytes(8 * 4 * 3), COLOR_RGB, time.time(), 1)
    assert encode_jpeg(rgb, 8, 80)[:2] == b"\xff\xd8"
    depth = Frame("depth", 8, 4, np.full(32, 1000, dtype="<u2").tobytes(), COLOR_DEPTH, time.time(), 1)
    assert encode_jpeg(depth, 4, 80)[:2] == b"\xff\xd8"
    with pytest.raises(RobotError):
        encode_jpeg(Frame("top", 8, 4, b"123", COLOR_RGB, time.time(), 1), 8, 80)


def test_waving_key_reports_repeat_waves_and_ignores_stale_value() -> None:
    from pepper_mcp.robot.perception import WAVING_KEY
    from pepper_mcp.runtime import Runtime
    from tests.conftest import make_settings

    robot = FakeRobot()
    # NAOqi 2.5.10 leaves the last waver's id in the key across sessions
    robot.memory_set(WAVING_KEY, 7)
    rt = Runtime(make_settings(reflexes_enabled=False), FakeConnection(robot))
    rt.start()
    try:
        time.sleep(0.4)
        assert rt.bus.recent(types=["person.waving"]) == []

        def wave_and_count(expected: int) -> None:
            robot.memory_set(WAVING_KEY, 7)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and len(rt.bus.recent(types=["person.waving"])) < expected:
                time.sleep(0.05)
            assert len(rt.bus.recent(types=["person.waving"])) == expected

        wave_and_count(1)
        # the same person waving again writes the same id: it must still be reported
        wave_and_count(2)
        assert rt.bus.recent(types=["person.waving"])[-1].data["person_id"] == 7
    finally:
        rt.stop()


def test_camera_releases_handles_leaked_by_killed_runs() -> None:
    from pepper_mcp.robot.camera import CameraStream

    robot = FakeRobot()
    conn = FakeConnection(robot)
    conn.connect()
    video = conn.service("ALVideoDevice")
    for _ in range(8):  # eight runs killed before they could unsubscribe
        video.subscribeCamera("pepper_mcp_top", 0, 1, 11, 10)
    stream = CameraStream(conn, "top")
    stream.start()
    try:
        assert video.getSubscribers() == ["pepper_mcp_top_0"]
    finally:
        stream.stop()
    assert video.getSubscribers() == []


def test_move_relative_reports_distance_and_safety_block() -> None:
    from pepper_mcp.robot.motion import Motion

    robot = FakeRobot()
    conn = FakeConnection(robot)
    conn.connect()
    motion = Motion(conn, EventBus())
    result = motion.move_relative(0.5, 0.0, math.radians(90))
    assert result["moved"] == {"x_m": 0.5, "y_m": 0.0, "theta_deg": 90.0}
    # NAOqi aborts a move with an obstacle in the safety bubble but moveTo returns normally
    robot.block_moves = True
    with pytest.raises(RobotError, match="Safety"):
        motion.move_relative(1.0, 0.0, 0.0)


def test_move_relative_flags_a_move_that_silently_did_nothing() -> None:
    from pepper_mcp.robot.motion import Motion

    robot = FakeRobot()
    conn = FakeConnection(robot)
    conn.connect()
    motion = Motion(conn, EventBus())
    motion.MOVE_FAILED_GRACE_S = 0.0
    robot.services["ALMotion"].returns["moveTo"] = lambda *a: None  # refused without MoveFailed
    with pytest.raises(RobotError, match="did not complete"):
        motion.move_relative(0.0, 0.0, math.radians(-30))


def test_move_relative_serialises_moves_and_reports_interruptions() -> None:
    import threading as _threading

    from pepper_mcp.robot.motion import Motion

    robot = FakeRobot()
    conn = FakeConnection(robot)
    conn.connect()
    motion = Motion(conn, EventBus())
    motion.MOVE_FAILED_GRACE_S = 0.0
    release = _threading.Event()

    def slow_move(*_: float) -> None:
        release.wait(2)
        robot.memory_set("ALMotion/MoveFailed", ["Internal stop", 1, [0.0, 0.0, 0.0]])

    robot.services["ALMotion"].returns["moveTo"] = slow_move
    results: list = []
    worker = _threading.Thread(target=lambda: results.append(motion.move_relative(1.0, 0.0, 0.0)))
    worker.start()
    time.sleep(0.1)
    # a second move must not interleave with the running one
    with pytest.raises(RobotError, match="busy"):
        motion.move_relative(0.0, 0.0, 1.0, wait_for_base=False)
    release.set()
    worker.join(2)
    # stop_motion during a move is not a failure
    assert results and results[0]["interrupted"] is True


def test_greet_reflex_plays_wave_gesture(runtime) -> None:  # type: ignore[no-untyped-def]
    from pepper_mcp.robot.events import Event

    played: list[str] = []
    runtime.gestures.play = lambda name, params=None: played.append(name)
    runtime.perception.people = lambda: []
    result = runtime.reflexes.get("greet").react(Event("person.appeared", {"person_id": 7, "distance_m": 1.2}))
    assert result == {"person_id": 7, "greeted": True}
    assert played == ["wave"]
