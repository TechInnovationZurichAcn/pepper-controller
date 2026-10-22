"""End-to-end through the MCP protocol with the in-process client and the fake robot."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any

import pytest
from mcp.client import Client

from pepper_mcp.robot.fake import FakeConnection, FakeRobot
from pepper_mcp.runtime import Runtime
from pepper_mcp.server import create_server
from tests.conftest import make_settings


def _json(res: Any) -> Any:
    assert not res.is_error, res.content
    if res.structured_content is not None:
        return res.structured_content
    return json.loads(res.content[0].text)


@asynccontextmanager
async def connected():  # type: ignore[no-untyped-def]
    """In-process MCP client bound to a fresh fake robot (one per test, same task → no cancel-scope issues)."""
    robot = FakeRobot()
    rt = Runtime(make_settings(), FakeConnection(robot))
    server = create_server(make_settings(), rt)
    async with Client(server) as c:
        c.robot = robot  # type: ignore[attr-defined]
        c.rt = rt  # type: ignore[attr-defined]
        yield c


@pytest.fixture
def client_ctx():  # type: ignore[no-untyped-def]
    return connected


async def test_tools_resources_prompts_listed(client_ctx) -> None:  # type: ignore[no-untyped-def]
    async with client_ctx() as client:
        tools = {t.name for t in (await client.list_tools()).tools}
        expected = {"say", "listen", "wait_for_event", "camera_snapshot", "move", "play_gesture", "get_people",
                    "set_leds", "tablet_show_text", "follow_person", "navigate_to", "set_reflex", "robot_status",
                    "define_gesture", "recognize_keywords", "record_clip", "stop_motion"}
        assert expected <= tools
        assert len(tools) > 90
        uris = {str(r.uri) for r in (await client.list_resources()).resources}
        assert {"pepper://status", "pepper://events", "pepper://people", "pepper://transcript"} <= uris
        prompts = {p.name for p in (await client.list_prompts()).prompts}
        assert prompts == {"pepper_live_agent", "pepper_quick_reference"}
        prompt = await client.get_prompt("pepper_live_agent", {"persona_name": "Robo"})
        assert "You are Robo" in prompt.messages[0].content.text


async def test_status_say_and_speech_events(client_ctx) -> None:  # type: ignore[no-untyped-def]
    async with client_ctx() as client:
        status = _json(await client.call_tool("robot_status", {}))
        assert status["connected"] and status["robot"]["robot_name"] == "FakePepper"
        res = _json(await client.call_tool("say", {"text": "Hi there!", "wait": True}))
        assert res["state"] == "done"
        assert client.robot.spoken[-1] == "Hi there!"
        events = _json(await client.call_tool("get_events", {"types": ["tts.*"]}))["events"]
        assert [e["type"] for e in events][-2:] == ["tts.started", "tts.finished"]


async def test_camera_snapshot_returns_image(client_ctx) -> None:  # type: ignore[no-untyped-def]
    async with client_ctx() as client:
        res = await client.call_tool("camera_snapshot", {"camera": "top", "max_width": 160})
        assert not res.is_error
        assert res.content[0].type == "image" and res.content[0].mime_type == "image/jpeg"
        meta = json.loads(res.content[1].text)
        assert meta["width"] == 320 and meta["frame_age_ms"] < 2000
        blob = await client.read_resource("pepper://camera/bottom/latest.jpg")
        assert blob.contents[0].mime_type == "image/jpeg" and len(blob.contents[0].blob) > 100


async def test_wait_for_event_wakes_on_person_and_reflex_high_five(client_ctx) -> None:  # type: ignore[no-untyped-def]
    async with client_ctx() as client:
        robot: FakeRobot = client.robot

        async def inject() -> None:
            await asyncio.sleep(0.2)
            robot.add_person(7, distance=1.0, waving=True)

        task = asyncio.get_running_loop().create_task(inject())
        res = _json(await client.call_tool("wait_for_event", {"types": ["person.*"], "timeout_s": 5}))
        await task
        types = [e["type"] for e in res["events"]]
        assert "person.appeared" in types
        assert res["events"][0]["data"]["person_id"] == 7
        # the high_five reflex reacts to person.waving without any LLM involvement
        deadline = asyncio.get_running_loop().time() + 8
        completed = None
        while asyncio.get_running_loop().time() < deadline:
            evs = _json(await client.call_tool("get_events", {"types": ["reflex.completed"]}))["events"]
            completed = next((e for e in evs if e["data"]["reflex"] == "high_five"), None)
            if completed:
                break
            await asyncio.sleep(0.1)
        assert completed is not None, "high five reflex did not complete"
        assert completed["data"]["result"]["touched"] is False
        assert "High five!" in robot.spoken
        people = _json(await client.call_tool("get_people", {}))
        assert people["count"] == 1 and people["people"][0]["waving"] is True


async def test_touch_event_and_high_five_touch_detection(client_ctx) -> None:  # type: ignore[no-untyped-def]
    async with client_ctx() as client:
        robot: FakeRobot = client.robot
        await client.call_tool("set_reflex", {"name": "touch_reaction", "enabled": False})
        robot.touch("FrontTactilTouched", True)
        res = _json(await client.call_tool("wait_for_event", {"types": ["touch.start"], "timeout_s": 3}))
        assert res["events"][0]["data"]["sensor"] == "head_front"
        state = _json(await client.call_tool("get_touch_state", {}))
        assert state["head_front"] is True


async def test_tasks_gestures_and_validation(client_ctx) -> None:  # type: ignore[no-untyped-def]
    async with client_ctx() as client:
        res = _json(await client.call_tool("play_gesture", {"name": "nod", "wait": True}))
        assert res["state"] == "done" and res["ok"] is True
        res = _json(await client.call_tool("play_animation", {"name": "Hey_1"}))
        assert res["state"] in ("running", "done")
        done = _json(await client.call_tool("wait_task", {"task_id": res["task_id"], "timeout_s": 5}))
        assert done["state"] == "done"
        bad = await client.call_tool("play_gesture", {"name": "moonwalk"})
        assert bad.is_error and "unknown gesture" in bad.content[0].text
        bad = await client.call_tool("move", {"vx": 3})
        assert bad.is_error
        defined = _json(await client.call_tool("define_gesture", {
            "name": "Peek", "description": "peek left", "keyframes": [{"t_s": 0.4, "joints": {"HeadYaw": 30}}]}))
        assert defined["name"] == "peek"
        assert "peek" in {g["name"] for g in _json(await client.call_tool("list_gestures", {}))["gestures"]}


async def test_move_watchdog_and_stop_motion(client_ctx) -> None:  # type: ignore[no-untyped-def]
    async with client_ctx() as client:
        res = _json(await client.call_tool("move", {"vx": 0.4, "duration_s": 0.2}))
        assert res["duration_s"] == 0.2
        await asyncio.sleep(0.6)
        assert client.robot.services["ALMotion"].count("stopMove") >= 1
        report = _json(await client.call_tool("stop_motion", {}))
        assert report["base"] == "stopped"


async def test_listen_without_stt_reports_disabled(client_ctx) -> None:  # type: ignore[no-untyped-def]
    async with client_ctx() as client:
        res = _json(await client.call_tool("listen", {"timeout_s": 0.2}))
        assert "error" in res  # stt backend none -> hearing disabled, tool says so instead of timing out


async def test_leds_tablet_tracking_navigation(client_ctx) -> None:  # type: ignore[no-untyped-def]
    async with client_ctx() as client:
        assert _json(await client.call_tool("set_mood_lights", {"mood": "happy"}))["mood"] == "happy"
        assert _json(await client.call_tool("tablet_show_text", {"text": "Hello"}))["shown"] == "Hello"
        st = _json(await client.call_tool("follow_person", {"enabled": True, "distance_m": 0.8}))
        assert st["following"] is True
        bad = await client.call_tool("move", {"vx": 0.2})
        assert bad.is_error and "follow" in bad.content[0].text
        assert _json(await client.call_tool("follow_person", {"enabled": False}))["following"] is False
        nav = _json(await client.call_tool("navigate_to", {"x_m": 1.0, "wait": True}))
        assert nav["ok"] is True
        assert _json(await client.call_tool("new_map", {"name": "kitchen"}))["current"] == "kitchen"
        spot_err = await client.call_tool("remember_spot", {"name": "fridge"})
        assert spot_err.is_error  # not localized yet
        reflexes = {r["name"]: r for r in _json(await client.call_tool("list_reflexes", {}))["reflexes"]}
        assert reflexes["touch_reaction"]["enabled"] is True  # fresh runtime per test
