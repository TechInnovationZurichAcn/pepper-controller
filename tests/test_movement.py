"""Gemini-facing movement: following people, driving, and moving single body parts."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from pepper_mcp.robot.fake import FakeConnection, FakeRobot
from pepper_mcp.robot.tracker import Tracker


def _wait_for(cond: Any, timeout: float = 2.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def _tracker(robot: FakeRobot, runtime: Any) -> Tracker:
    tracker = Tracker(FakeConnection(robot), runtime.bus, runtime.memory)
    tracker.LOOP_S = 0.02
    return tracker


def test_follow_pauses_basic_awareness_and_resumes_it_on_stop(fake_robot, runtime) -> None:  # type: ignore[no-untyped-def]
    ba = fake_robot.services["ALBasicAwareness"]
    ba.returns["isAwarenessRunning"] = True
    tracker = _tracker(fake_robot, runtime)
    tracker.follow(0.8)
    assert ba.count("pauseAwareness") == 1
    tracker.stop()
    assert ba.count("resumeAwareness") == 1
    tracker.stop()  # a second stop must not resume what someone else paused
    assert ba.count("resumeAwareness") == 1


def test_follow_distance_is_never_closer_than_the_base_can_drive(fake_robot, runtime) -> None:  # type: ignore[no-untyped-def]
    tracker = _tracker(fake_robot, runtime)
    try:
        assert tracker.follow()["follow_distance_m"] == 0.5
        assert tracker.follow(0.2)["follow_distance_m"] == 0.5
    finally:
        tracker.stop()


def test_follow_leaves_stopped_awareness_alone(fake_robot, runtime) -> None:  # type: ignore[no-untyped-def]
    ba = fake_robot.services["ALBasicAwareness"]
    ba.returns["isAwarenessRunning"] = False
    tracker = _tracker(fake_robot, runtime)
    tracker.follow()
    tracker.stop()
    assert ba.count("pauseAwareness") == 0 and ba.count("resumeAwareness") == 0


def test_follow_tracks_people_in_move_mode(fake_robot, runtime) -> None:  # type: ignore[no-untyped-def]
    fake_robot.add_person(4)
    alt = fake_robot.services["ALTracker"]
    alt.returns["isActive"] = True
    tracker = _tracker(fake_robot, runtime)
    tracker.follow(0.7)
    try:
        assert _wait_for(lambda: alt.count("track") >= 1)
        assert alt.last("setMode") == ("Move",)
        assert alt.last("setRelativePosition")[0][0] == -0.7
    finally:
        tracker.stop()


def test_follow_rearms_when_naoqi_stops_the_tracker(fake_robot, runtime) -> None:  # type: ignore[no-untyped-def]
    # Regression: on the robot follow_person returned robot_tracking=false and never recovered.
    fake_robot.add_person(4)
    alt = fake_robot.services["ALTracker"]
    alt.returns["isActive"] = False
    tracker = _tracker(fake_robot, runtime)
    tracker.follow()
    try:
        assert _wait_for(lambda: alt.count("track") >= 3)
    finally:
        tracker.stop()


def test_stale_follow_loop_is_not_revived_by_a_new_follow(fake_robot, runtime) -> None:  # type: ignore[no-untyped-def]
    fake_robot.add_person(4)
    tracker = _tracker(fake_robot, runtime)
    tracker.follow()
    first_stop = tracker._stop
    tracker.stop()
    tracker.follow()
    try:
        assert first_stop.is_set()  # the old loop's event stays set: it exits even if it was slow
        assert tracker._stop is not first_stop
    finally:
        tracker.stop()


def test_track_failure_gives_the_head_back_to_awareness(fake_robot, runtime) -> None:  # type: ignore[no-untyped-def]
    ba = fake_robot.services["ALBasicAwareness"]
    ba.returns["isAwarenessRunning"] = True

    def _boom(*_: Any) -> None:
        raise RuntimeError("tracker refused")

    fake_robot.services["ALTracker"].returns["track"] = _boom
    tracker = _tracker(fake_robot, runtime)
    with pytest.raises(RuntimeError):
        tracker.track("face", "head")
    assert ba.count("pauseAwareness") == 1 and ba.count("resumeAwareness") == 1


def test_follow_reports_lost_and_found(fake_robot, runtime) -> None:  # type: ignore[no-untyped-def]
    seen: list[str] = []
    runtime.bus.subscribe("follow.*", lambda e: seen.append(e.type))
    tracker = _tracker(fake_robot, runtime)
    tracker.LOST_S = 0.1
    out = tracker.follow()
    try:
        assert "hint" in out  # nobody visible yet: the model is told so
        assert _wait_for(lambda: "follow.lost" in seen)
        assert tracker.status()["person_lost"] is True
        fake_robot.add_person(2)
        assert _wait_for(lambda: "follow.found" in seen)
        assert seen.count("follow.lost") == 1
    finally:
        tracker.stop()


def _json(result: Any) -> dict[str, Any]:
    return json.loads(result.content[0].text)


async def test_drive_command_takes_the_base_from_follow() -> None:
    from mcp.client import Client

    from pepper_mcp.server import create_server
    from tests.conftest import make_settings

    server = create_server(make_settings())
    rt = server.runtime  # type: ignore[attr-defined]
    async with Client(server) as client:
        assert _json(await client.call_tool("follow_person", {"enabled": True}))["following"] is True
        moved = _json(await client.call_tool("turn", {"degrees": 90}))
        assert moved["stopped_following"] is True
        assert rt.tracker.following is False
        drive = _json(await client.call_tool("move", {"vx": 0.3, "vtheta": 0.2, "duration_s": 0.3}))
        assert drive["vx"] == 0.3 and "stopped_following" not in drive
        await client.call_tool("stop_motion", {})


async def test_drive_wakes_a_resting_robot() -> None:
    from mcp.client import Client

    from pepper_mcp.server import create_server
    from tests.conftest import make_settings

    server = create_server(make_settings())
    rt = server.runtime  # type: ignore[attr-defined]
    async with Client(server) as client:
        motion = rt.conn.service("ALMotion")
        motion.returns["robotIsWakeUp"] = False
        before = motion.count("wakeUp")
        await client.call_tool("move_to", {"x_m": 0.5})
        assert motion.count("wakeUp") == before + 1
        await client.call_tool("stop_motion", {})


async def test_move_joint_absolute_relative_hand_and_clamp() -> None:
    from mcp.client import Client

    from pepper_mcp.server import create_server
    from tests.conftest import make_settings

    server = create_server(make_settings())
    rt = server.runtime  # type: ignore[attr-defined]
    async with Client(server) as client:
        motion = rt.conn.service("ALMotion")
        assert _json(await client.call_tool("move_joint", {"joint": "HeadYaw", "degrees": 30})) == {
            "joint": "HeadYaw", "degrees": 30.0}
        assert motion.last("setAngles")[0] == ["HeadYaw"]
        rel = _json(await client.call_tool("move_joint", {"joint": "RShoulderPitch", "degrees": -20, "relative": True}))
        assert rel["degrees"] == -20.0  # fake joints read 0
        hand = _json(await client.call_tool("move_joint", {"joint": "LHand", "degrees": 60}))
        assert hand == {"joint": "LHand", "open_percent": 60}
        clamped = _json(await client.call_tool("move_joint", {"joint": "HeadPitch", "degrees": 90}))
        assert clamped["clamped"] is True and clamped["degrees"] < 30
        bad = await client.call_tool("move_joint", {"joint": "Tail", "degrees": 10})
        assert bad.is_error


def test_move_joint_is_declared_to_gemini_with_a_joint_enum() -> None:
    import asyncio

    from mcp.client import Client

    from pepper_mcp.gemini_live import tool_declarations
    from pepper_mcp.server import create_server
    from tests.conftest import make_settings

    async def _list() -> list[Any]:
        async with Client(create_server(make_settings())) as client:
            return (await client.list_tools()).tools

    decls = {d["name"]: d for d in tool_declarations(asyncio.run(_list()))}
    for name in ("move", "move_to", "turn", "move_joint", "arm_pose", "set_hands", "set_body_lean",
                 "follow_person", "navigate_to", "look", "stop_motion"):
        assert name in decls, name
    joint = decls["move_joint"]["parameters_json_schema"]["properties"]["joint"]
    assert "KneePitch" in joint["enum"] and "RHand" in joint["enum"]


def test_follow_events_reach_gemini() -> None:
    from pepper_mcp.gemini_live import format_event

    assert format_event("follow.lost", {"unseen_s": 3.0}) is not None
    assert format_event("follow.found", {"people": [1]}) is not None


async def test_open_charging_hatch_is_reported_instead_of_a_fake_obstacle() -> None:
    # Regression: with the hatch open NAOqi refused every base move as "Safety" and the tools
    # blamed an obstacle; turn 180 and follow_person silently did nothing.
    from mcp.client import Client

    from pepper_mcp.robot.motion import HATCH_KEY
    from pepper_mcp.server import create_server
    from tests.conftest import make_settings

    server = create_server(make_settings())
    rt = server.runtime  # type: ignore[attr-defined]
    async with Client(server) as client:
        rt.conn.service("ALMemory").insertData(HATCH_KEY, 1.0)
        for name, args in (("turn", {"degrees": 180}), ("move", {"vx": 0.2}), ("move_to", {"x_m": 0.5}),
                           ("follow_person", {"enabled": True})):
            res = await client.call_tool(name, args)
            assert res.is_error and "hatch" in res.content[0].text, name
        assert rt.tracker.following is False
        assert _json(await client.call_tool("robot_status", {}))["charging_hatch_open"] is True
        rt.conn.service("ALMemory").insertData(HATCH_KEY, 0.0)
        assert not (await client.call_tool("turn", {"degrees": 90})).is_error
        await client.call_tool("stop_motion", {})
