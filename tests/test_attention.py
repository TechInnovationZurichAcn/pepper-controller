from __future__ import annotations

import time
from typing import Any

import pytest

from pepper_mcp.robot.events import Event
from pepper_mcp.robot.fake import FakeConnection, FakeRobot
from pepper_mcp.robot.perception import Perception
from pepper_mcp.runtime import Runtime
from tests.conftest import make_settings


@pytest.fixture
def attention():  # type: ignore[no-untyped-def]
    rt = Runtime(make_settings(), FakeConnection(FakeRobot()))
    rt.start()
    looks: list[tuple[Any, ...]] = []
    rt.tracker.look_at_person = lambda pos, bearing=None: looks.append(("person", bearing)) or True
    rt.tracker.look_at = lambda x, y, z, frame="robot", speed=0.5, whole_body=False: looks.append(("point", x, y))
    people: list[dict[str, Any]] = []
    rt.perception.people = lambda: list(people)
    reflex = rt.reflexes.get("attention")
    try:
        yield reflex, looks, people
    finally:
        rt.stop()


def _sound(azimuth: float, confidence: float = 0.6) -> Event:
    return Event("sound.located", {"azimuth_deg": azimuth, "elevation_deg": 0.0, "confidence": confidence,
                                   "energy": 0.3, "ts": time.time()})


def _settled(looks: list[tuple[Any, ...]], n: int) -> list[tuple[Any, ...]]:
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and len(looks) < n:
        time.sleep(0.02)
    time.sleep(0.1)  # nothing more should arrive
    return looks


def test_plain_noise_never_turns_the_head(attention) -> None:  # type: ignore[no-untyped-def]
    reflex, looks, _ = attention
    reflex.on_event(_sound(120.0, confidence=0.9))
    assert _settled(looks, 1) == []


def test_rejected_utterance_does_not_turn_toward_unseen_voice(attention) -> None:  # type: ignore[no-untyped-def]
    reflex, looks, _ = attention
    reflex.on_event(Event("speech.started", {"at": time.time()}))
    reflex.on_event(_sound(90.0))
    reflex.on_event(Event("speech.aborted", {"reason": "too_short"}))
    assert _settled(looks, 1) == []


def test_confirmed_speech_turns_toward_the_voice(attention) -> None:  # type: ignore[no-untyped-def]
    reflex, looks, _ = attention
    t0 = time.time()
    reflex.on_event(Event("speech.started", {"at": t0}))
    reflex.on_event(_sound(90.0))
    reflex.on_event(Event("speech.final", {"text": "hello", "started_at": t0}))
    looks = _settled(looks, 1)
    # 90 degrees is too far for the head alone: the base turns left, then Pepper looks straight ahead
    assert reflex.rt.motion.get_pose()["theta_deg"] == pytest.approx(90.0, abs=0.5)
    assert len(looks) == 1 and looks[0][0] == "point"
    _, x, y = looks[0]
    assert x > 1.0 and abs(y) < 0.01


def test_voice_slightly_to_the_side_only_turns_the_head(attention) -> None:  # type: ignore[no-untyped-def]
    reflex, looks, _ = attention
    t0 = time.time()
    reflex.on_event(Event("speech.started", {"at": t0}))
    reflex.on_event(_sound(30.0))
    reflex.on_event(Event("speech.final", {"text": "hello", "started_at": t0}))
    looks = _settled(looks, 1)
    assert reflex.rt.motion.get_pose()["theta_deg"] == 0.0
    _, x, y = looks[0]
    assert y / x == pytest.approx(0.577, abs=0.01)  # tan(30 deg)


def test_voice_from_a_visible_person_looks_at_them_immediately(attention) -> None:  # type: ignore[no-untyped-def]
    reflex, looks, people = attention
    reflex.params["turn_body"] = False
    people += [{"person_id": 1, "bearing_deg": -10.0, "position_m": [1.0, -0.2, 1.6]},
               {"person_id": 2, "bearing_deg": 50.0, "position_m": [0.8, 1.0, 1.6]}]
    t0 = time.time()
    reflex.on_event(Event("speech.started", {"at": t0}))
    reflex.on_event(_sound(60.0))
    assert _settled(looks, 1) == [("person", 50.0)]
    # the transcript for the same speaker does not move the head again
    reflex.on_event(Event("speech.final", {"text": "hi", "started_at": t0}))
    assert _settled(looks, 2) == [("person", 50.0)]


def test_newcomer_is_ignored_right_after_a_conversation_turn(attention) -> None:  # type: ignore[no-untyped-def]
    reflex, looks, _ = attention
    t0 = time.time()
    reflex.on_event(Event("speech.started", {"at": t0}))
    reflex.on_event(Event("speech.final", {"text": "hi", "started_at": t0}))
    reflex.on_event(Event("person.appeared", {"person_id": 3, "bearing_deg": -70.0}))
    assert _settled(looks, 1) == []


def test_speaker_far_to_the_side_turns_the_body_toward_them(attention) -> None:  # type: ignore[no-untyped-def]
    reflex, looks, people = attention
    people.append({"person_id": 6, "bearing_deg": -100.0, "position_m": [-0.3, -1.6, 1.7]})
    reflex.on_event(Event("speech.started", {"at": time.time()}))
    reflex.on_event(_sound(-95.0))
    looks = _settled(looks, 1)
    assert reflex.rt.motion.get_pose()["theta_deg"] == pytest.approx(-100.0, abs=0.5)
    _, x, y = looks[0]
    assert x == pytest.approx(1.63, abs=0.01) and y == 0.0


def test_late_transcript_of_previous_utterance_keeps_the_new_one_open(attention) -> None:  # type: ignore[no-untyped-def]
    reflex, looks, people = attention
    people.append({"person_id": 4, "bearing_deg": 30.0, "position_m": [1.2, 0.7, 1.6]})
    t0 = time.time() - 2
    reflex.on_event(Event("speech.started", {"at": t0}))
    t1 = time.time()
    reflex.on_event(Event("speech.started", {"at": t1}))  # next utterance before STT finished the first
    reflex.on_event(Event("speech.final", {"text": "first", "started_at": t0}))
    reflex.on_event(_sound(25.0))
    assert _settled(looks, 1) == [("person", 30.0)]


def test_start_handled_after_its_own_end_is_ignored(attention) -> None:  # type: ignore[no-untyped-def]
    reflex, looks, people = attention
    people.append({"person_id": 5, "bearing_deg": 0.0, "position_m": [1.0, 0.0, 1.6]})
    t0 = time.time()
    reflex.on_event(Event("speech.aborted", {"reason": "too_short", "started_at": t0}))
    reflex.on_event(Event("speech.started", {"at": t0}))
    reflex.on_event(_sound(0.0))  # noise afterwards must not count as speech
    assert _settled(looks, 1) == []


def test_no_head_moves_while_the_tracker_owns_the_head(attention) -> None:  # type: ignore[no-untyped-def]
    reflex, looks, _ = attention
    reflex.rt.tracker.following = True
    t0 = time.time()
    reflex.on_event(Event("speech.started", {"at": t0}))
    reflex.on_event(_sound(45.0))
    reflex.on_event(Event("speech.final", {"text": "hi", "started_at": t0}))
    assert _settled(looks, 1) == []


def test_look_requests_after_stop_do_not_restart_the_worker(attention) -> None:  # type: ignore[no-untyped-def]
    reflex, looks, _ = attention
    reflex.stop()
    reflex._request_look("voice", 20.0)
    assert _settled(looks, 1) == []
    assert reflex._look_thread is None or not reflex._look_thread.is_alive()


def test_sound_azimuth_wraps_to_plus_minus_180() -> None:
    rt = Runtime(make_settings(reflexes_enabled=False), FakeConnection(FakeRobot()))
    rt.start()
    try:
        rt.perception._on_sound("ALSoundLocalization/SoundLocated",
                                [[0, 0], [2.5, 0.0, 0.8, 0.3], [0, 0, 0, 0, 0, 1.5], [0, 0, 1, 0, 0, 1.5]])
        assert rt.perception.last_sound()["azimuth_deg"] == pytest.approx(-130.8, abs=0.2)
    finally:
        rt.stop()


def test_sound_azimuth_is_converted_to_robot_frame() -> None:
    robot = FakeRobot()
    rt = Runtime(make_settings(reflexes_enabled=False), FakeConnection(robot))
    rt.start()
    try:
        perception: Perception = rt.perception
        head_yaw = 0.5  # rad, head turned to the left
        perception._on_sound("ALSoundLocalization/SoundLocated",
                             [[0, 0], [0.25, 0.0, 0.8, 0.3], [0, 0, 0, 0, 0, head_yaw], [0, 0, 1, 0, 0, head_yaw]])
        last = perception.last_sound()
        assert last is not None
        assert last["azimuth_deg"] == pytest.approx(42.97, abs=0.1)
        assert last["head_azimuth_deg"] == pytest.approx(14.3, abs=0.1)
    finally:
        rt.stop()


def test_blocked_body_turn_still_turns_the_head(attention) -> None:  # type: ignore[no-untyped-def]
    reflex, looks, _ = attention
    reflex.rt.motion.MOVE_FAILED_GRACE_S = 0.0
    reflex.rt.motion.conn.robot.block_moves = True
    t0 = time.time()
    reflex.on_event(Event("speech.started", {"at": t0}))
    reflex.on_event(_sound(90.0))
    reflex.on_event(Event("speech.final", {"text": "hello", "started_at": t0}))
    looks = _settled(looks, 1)
    _, x, y = looks[0]
    assert y > 1.0 and abs(x) < 0.01  # head toward 90 degrees left
