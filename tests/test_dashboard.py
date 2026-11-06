"""Dashboard served by the preview side channel: state snapshot, actions and access control."""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from email.message import Message
from typing import Any

import pytest

from pepper_mcp import dashboard
from pepper_mcp.preview import PreviewServer, control_denied
from pepper_mcp.robot.fake import FakeConnection, FakeRobot
from pepper_mcp.runtime import Runtime
from tests.conftest import make_settings


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def preview(runtime):  # type: ignore[no-untyped-def]
    server = PreviewServer(runtime, "127.0.0.1", _free_port())
    server.start()
    try:
        yield f"http://127.0.0.1:{server.port}", runtime
    finally:
        server.stop()


def _post(url: str, body: Any, headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_state_has_every_panel(runtime) -> None:  # type: ignore[no-untyped-def]
    st = dashboard.state(runtime)
    assert st["status"]["connected"] is True
    assert {r["name"] for r in st["reflexes"]} >= {"high_five", "greet", "attention"}
    assert any(g["name"] == "wave" for g in st["gestures"])
    assert st["features"]["reflexes"] is True
    assert set(st["options"]) >= {"liveliness", "moods", "abilities", "extractors"}
    json.dumps(st, default=str)


def test_state_survives_failing_subsystem(runtime) -> None:  # type: ignore[no-untyped-def]
    def boom() -> None:
        raise RuntimeError("robot gone")

    runtime.awareness.abilities = boom
    st = dashboard.state(runtime)
    assert st["abilities"] is None
    assert st["status"] is not None


def test_reflex_toggle_action(runtime) -> None:  # type: ignore[no-untyped-def]
    out = dashboard.run_action(runtime, "reflex", {"name": "greet", "enabled": False})
    assert out["enabled"] is False
    assert "greet" not in runtime.reflexes.active_names()


@pytest.mark.parametrize(("name", "args", "msg"), [
    ("nope", {}, "unknown action"),
    ("reflex", {"name": "greet", "enabled": "yes"}, "true or false"),
    ("reflex", {"name": "nonexistent", "enabled": True}, "unknown reflex"),
    ("liveliness", {"preset": "wild"}, "must be one of"),
    ("move", {"vx": "fast"}, "must be a number"),
    ("say", {"text": "   "}, "non-empty"),
])
def test_bad_actions_are_rejected(runtime, name: str, args: dict[str, Any], msg: str) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(dashboard.ActionError, match=msg):
        dashboard.run_action(runtime, name, args)


def test_hearing_card_shows_mic_voice_agent_and_conversation(runtime) -> None:  # type: ignore[no-untyped-def]
    st = dashboard.state(runtime)
    assert st["voice_agent"] is None and st["transcript"] == []
    assert st["mic"] == {"running": True, "mic": "front", "muted": False}
    runtime.voice_agent = "Gemini Live (gemini-3.8-live)"
    runtime.transcript.append({"role": "user", "text": "hi Pepper", "at": 1.0})
    st = dashboard.state(runtime)
    assert st["voice_agent"] == "Gemini Live (gemini-3.8-live)"
    assert st["transcript"][-1]["text"] == "hi Pepper"
    assert "listening" not in dashboard.ACTIONS  # no server-side STT left to switch


def test_conversation_card_explains_the_transcript_is_only_a_log() -> None:
    with open(dashboard.HTML_PATH, encoding="utf-8") as fh:
        html = fh.read()
    assert 'id="chat-note"' in html
    assert "Gemini answers from the audio itself" in html
    assert "vocabulary.txt" in html


def test_mic_switch_mutes_and_unmutes(runtime) -> None:  # type: ignore[no-untyped-def]
    assert dashboard.run_action(runtime, "mic", {"enabled": False}) == {"muted": True}
    assert runtime.mic_muted and dashboard.state(runtime)["mic"]["muted"]
    assert dashboard.run_action(runtime, "mic", {"enabled": True}) == {"muted": False}
    assert not runtime.mic_muted
    with pytest.raises(dashboard.ActionError, match="enabled"):
        dashboard.run_action(runtime, "mic", {})


def test_move_is_clamped_to_watchdog(runtime, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    seen: dict[str, float] = {}

    def fake_move(vx: float, vy: float, vtheta: float, duration_s: float) -> dict[str, Any]:
        seen.update(vx=vx, vy=vy, vtheta=vtheta, duration_s=duration_s)
        return {}

    monkeypatch.setattr(runtime.motion, "move", fake_move)
    dashboard.run_action(runtime, "move", {"vx": 9, "vtheta": -9, "duration_s": 60})
    assert seen == {"vx": 1.0, "vy": 0.0, "vtheta": -1.0, "duration_s": 5.0}


def test_move_refused_while_following(runtime) -> None:  # type: ignore[no-untyped-def]
    runtime.tracker.following = True
    with pytest.raises(dashboard.ActionError, match="follow person"):
        dashboard.run_action(runtime, "move", {"vx": 0.5})


def test_gesture_action_starts_task(runtime) -> None:  # type: ignore[no-untyped-def]
    out = dashboard.run_action(runtime, "gesture", {"name": "wave"})
    assert out["kind"] == "gesture"


@pytest.mark.parametrize(("ip", "headers", "token", "allowed"), [
    ("127.0.0.1", {"Content-Type": "application/json"}, "", True),
    ("::1", {"Content-Type": "application/json"}, "", True),
    ("127.0.0.1", {"Content-Type": "text/plain"}, "", False),
    ("127.0.0.1", {"Content-Type": "application/json", "Origin": "http://evil.example", "Host": "127.0.0.1:8765"}, "", False),
    ("127.0.0.1", {"Content-Type": "application/json", "Origin": "http://127.0.0.1:8765", "Host": "127.0.0.1:8765"}, "", True),
    ("192.168.1.20", {"Content-Type": "application/json"}, "", False),
    ("192.168.1.20", {"Content-Type": "application/json", "X-Pepper-Token": "wrong"}, "s3cret", False),
    ("192.168.1.20", {"Content-Type": "application/json", "X-Pepper-Token": "s3cret"}, "s3cret", True),
    # DNS rebinding: hostile domain resolved to 127.0.0.1 is same-origin but has a non-IP Host
    ("127.0.0.1", {"Content-Type": "application/json", "Host": "evil.example:8765",
                   "Origin": "http://evil.example:8765"}, "", False),
    ("127.0.0.1", {"Content-Type": "application/json", "Host": "localhost:8765"}, "", True),
    ("::1", {"Content-Type": "application/json", "Host": "[::1]:8765"}, "", True),
])
def test_control_access(ip: str, headers: dict[str, str], token: str, allowed: bool) -> None:
    msg = Message()
    for k, v in {"Host": "127.0.0.1:8765", **headers}.items():
        msg[k] = v
    assert (control_denied(ip, msg, token) is None) is allowed


def test_remote_viewer_may_always_stop() -> None:
    msg = Message()
    msg["Content-Type"] = "application/json"
    msg["Host"] = "192.168.1.10:8765"
    assert control_denied("192.168.1.20", msg, "", action="stop") is None
    assert control_denied("192.168.1.20", msg, "", action="move") is not None


def test_perception_reports_failed_extractor(runtime, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(runtime.perception, "set_extractor", lambda name, enabled: False)
    with pytest.raises(dashboard.ActionError, match="could not be enabled"):
        dashboard.run_action(runtime, "perception", {"name": "waving", "enabled": True})


def test_perception_has_no_laptop_hand_raise_detector(runtime) -> None:  # type: ignore[no-untyped-def]
    # hand raises come only from Pepper's own ALWavingDetection (person.waving)
    with pytest.raises(dashboard.ActionError):
        dashboard.run_action(runtime, "perception", {"name": "hand_raise_vision", "enabled": True})
    assert "hand_raise_vision" not in dashboard.state(runtime)["perception"]


def test_http_dashboard_state_and_action(preview) -> None:  # type: ignore[no-untyped-def]
    base, rt = preview
    with urllib.request.urlopen(base + "/", timeout=5) as r:
        assert b"Pepper Control" in r.read()
    with urllib.request.urlopen(base + "/api/state", timeout=5) as r:
        st = json.loads(r.read())
    assert st["control"] is True

    code, body = _post(base + "/api/action/reflex", {"name": "high_five", "enabled": False})
    assert code == 200 and body["result"]["enabled"] is False
    assert "high_five" not in rt.reflexes.active_names()

    code, body = _post(base + "/api/action/reflex", {"name": "high_five", "enabled": "maybe"})
    assert code == 400 and "true or false" in body["error"]

    code, body = _post(base + "/api/action/stop", {}, {"Origin": "http://evil.example"})
    assert code == 403

    req = urllib.request.Request(base + "/api/action/say", data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json", "Content-Length": "-1"})
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(req, timeout=5)
    assert err.value.code == 400


def test_settings_summary_masks_token() -> None:
    rt = Runtime(make_settings(preview_token="s3cret"), FakeConnection(FakeRobot()))
    assert rt.settings.summary()["preview_token"] == "***"


def _start_dance(rt) -> str:  # type: ignore[no-untyped-def]
    # Silent: the music would first be copied to the (fake) robot over scp.
    rt.tasks.run("gesture", "clown_dance", lambda: rt.gestures.play("clown_dance", {"music": False}))
    deadline = time.monotonic() + 2.0
    while rt.gestures.dancing() is None:
        assert time.monotonic() < deadline, "the dance never started"
        time.sleep(0.01)
    return "clown_dance"


def test_dance_shows_as_playing_and_locks_out_other_actions(runtime) -> None:  # type: ignore[no-untyped-def]
    dance = _start_dance(runtime)
    st = dashboard.state(runtime)
    assert st["dancing"] == dance and dance in st["playing"]
    for name, args in (("gesture", {"name": "wave"}), ("say", {"text": "hi"}), ("move", {"vx": 0.3}),
                       ("turn", {"degrees": 90}), ("look", {"direction": "left"})):
        with pytest.raises(dashboard.ActionError, match="dancing"):
            dashboard.run_action(runtime, name, args)
    assert runtime.gestures.dancing() == dance  # still dancing


def test_pressing_the_playing_dance_stops_it_and_resets_the_arms(runtime, fake_robot) -> None:  # type: ignore[no-untyped-def]
    _start_dance(runtime)
    posture = fake_robot.services["ALRobotPosture"]
    before = posture.count("goToPosture")
    out = dashboard.run_action(runtime, "stop_gesture", {})
    assert out["gestures_stopped"] == 1
    deadline = time.monotonic() + 5.0
    while posture.count("goToPosture") == before:
        assert time.monotonic() < deadline, "arms were never reset"
        time.sleep(0.02)
    assert posture.last("goToPosture")[0] == "StandInit"
    assert runtime.gestures.dancing() is None
    assert dashboard.state(runtime)["playing"] == []
    dashboard.run_action(runtime, "gesture", {"name": "wave"})  # unlocked again
