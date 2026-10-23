from __future__ import annotations

import math
import time

import pytest

from pepper_mcp.robot.audio_in import decode_pcm, pcm_to_wav
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
    wav = pcm_to_wav(np.zeros(16, dtype="<i2"), 16000)
    assert wav[:4] == b"RIFF" and b"WAVE" in wav


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
