"""Gesture buttons pinned from the dashboard to Pepper's tablet, and the tablet returning to them
after a tic-tac-toe game."""

from __future__ import annotations

import time
from typing import Any

import pytest

from pepper_mcp import dashboard, tablet_home
from pepper_mcp.games.tictactoe import OWNER
from pepper_mcp.preview import PreviewServer
from pepper_mcp.robot.connection import RobotError
from tests.test_server import _json, connected
from tests.test_tictactoe import _free_port, _req, tablet_shown


@pytest.fixture
def preview(runtime):  # type: ignore[no-untyped-def]
    server = PreviewServer(runtime, "127.0.0.1", _free_port())
    server.start()
    runtime.preview = server
    try:
        yield f"http://127.0.0.1:{server.port}", runtime
    finally:
        runtime.preview = None
        server.stop()


def wait_for(predicate: Any, timeout_s: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    return bool(predicate())


def finish_game(rt: Any) -> dict[str, Any]:
    """Play the person's moves (first free cell) until the game is over."""
    st = rt.tictactoe.start("X", True, "hard")
    while st["active"]:
        st = rt.tictactoe.human_move(st["free_cells"][0])
    return st


def key_headers(rt: Any) -> dict[str, str]:
    return {tablet_home.KEY_HEADER: rt.tablet_home.key}


# ------------------------------------------------------------------ pinning from the dashboard
def test_pin_and_unpin_every_gesture(preview) -> None:  # type: ignore[no-untyped-def]
    _, rt = preview
    tablet = rt.conn.robot.services["ALTabletService"]
    names = [g["name"] for g in dashboard.state(rt)["gestures"]]
    assert len(names) > 1

    out = dashboard.run_action(rt, "tablet_gesture", {"name": names[0], "pinned": True})
    assert out == {"pinned": [names[0]], "shown": True}
    url = tablet_shown(tablet)
    assert url.startswith(f"{rt.preview.base_url}{tablet_home.PAGE_PATH}?k={rt.tablet_home.key}")

    for name in names[1:]:
        assert dashboard.run_action(rt, "tablet_gesture", {"name": name, "pinned": True})["shown"] is True
    assert dashboard.state(rt)["tablet_gestures"] == names
    assert tablet.count("showWebview") == 1  # the page polls; it is not reloaded per button

    assert dashboard.run_action(rt, "tablet_gesture", {"name": names[0], "pinned": True})["shown"] is None
    for name in names[:-1]:
        assert dashboard.run_action(rt, "tablet_gesture", {"name": name, "pinned": False})["shown"] is True
    assert tablet.count("hideWebview") == 0
    out = dashboard.run_action(rt, "tablet_gesture", {"name": names[-1], "pinned": False})
    assert out == {"pinned": [], "shown": False}
    assert wait_for(lambda: tablet.count("hideWebview") == 1)


def test_pin_refuses_unknown_gestures_and_bad_arguments(runtime) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(RobotError, match="unknown gesture"):
        dashboard.run_action(runtime, "tablet_gesture", {"name": "moonwalk", "pinned": True})
    with pytest.raises(dashboard.ActionError, match="pinned"):
        dashboard.run_action(runtime, "tablet_gesture", {"name": "wave"})
    with pytest.raises(dashboard.ActionError, match="name"):
        dashboard.run_action(runtime, "tablet_gesture", {"pinned": True})
    assert runtime.tablet_home.pinned() == []


def test_pin_without_a_reachable_tablet_page_is_kept_but_not_shown(runtime) -> None:  # type: ignore[no-untyped-def]
    # No preview server: the tablet has nothing to load, but the button is remembered.
    out = dashboard.run_action(runtime, "tablet_gesture", {"name": "wave", "pinned": True})
    assert out == {"pinned": ["wave"], "shown": False}


def test_pin_does_not_cover_a_game_board(preview) -> None:  # type: ignore[no-untyped-def]
    _, rt = preview
    rt.tablet.hold(OWNER)
    assert dashboard.run_action(rt, "tablet_gesture", {"name": "wave", "pinned": True})["shown"] is False
    time.sleep(0.05)
    assert rt.conn.robot.services["ALTabletService"].count("showWebview") == 0


def test_removed_user_gesture_disappears_from_the_tablet(runtime) -> None:  # type: ignore[no-untyped-def]
    runtime.gestures.define("my_bow", "a bow", [{"t_s": 0.5, "joints": {"HeadPitch": 0.2}}])
    runtime.tablet_home.pin("my_bow", True)
    runtime.gestures.remove("my_bow")
    assert runtime.tablet_home.pinned() == [] and runtime.tablet_home.state()["gestures"] == []


# ------------------------------------------------------------------ the tablet page
def test_tablet_page_lists_and_plays_pinned_gestures(preview, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    base, rt = preview
    played: list[str] = []
    real_play = rt.gestures.play
    monkeypatch.setattr(rt.gestures, "play", lambda name, *a, **k: (played.append(name), real_play(name, *a, **k))[1])
    status, page = _req(base + tablet_home.PAGE_PATH)
    assert status == 200 and tablet_home.API_PATH.encode() in page and b"X-Tablet-Key" in page

    rt.tablet_home.pin("wave", True)
    status, st = _req(base + tablet_home.API_PATH)
    assert status == 200 and [g["name"] for g in st["gestures"]] == ["wave"]
    assert st["gestures"][0]["label"] == "Wave" and st["playing"] == []

    play = base + tablet_home.API_PATH + "/play"
    assert _req(play, {"name": "wave"})[0] == 403  # no key
    assert _req(play, {"name": "wave"}, {tablet_home.KEY_HEADER: "guess"})[0] == 403
    assert _req(play, {"name": "wave"}, {**key_headers(rt), "Origin": "http://evil.example"})[0] == 403
    other = next(n for n in (g["name"] for g in dashboard.state(rt)["gestures"]) if n != "wave")
    status, err = _req(play, {"name": other}, key_headers(rt))
    assert status == 400 and "not on the tablet" in err["error"]
    assert _req(play, b"[1]", key_headers(rt))[0] == 400

    status, _ = _req(play, {"name": "wave"}, key_headers(rt))
    assert status == 200
    assert wait_for(lambda: played == ["wave"])

    # A press on the gesture that is playing stops it instead (the fake robot finishes at once).
    stops: list[int] = []
    monkeypatch.setattr(rt.gestures, "active", lambda: ["wave"])
    monkeypatch.setattr(rt.gestures, "stop_all", lambda: stops.append(1) or 1)
    status, st = _req(play, {"name": "wave"}, key_headers(rt))
    assert status == 200 and st["playing"] == ["wave"]
    assert stops and played == ["wave"]


def test_tablet_key_is_random_per_runtime(runtime) -> None:  # type: ignore[no-untyped-def]
    other = tablet_home.TabletHome(runtime)
    assert other.key != runtime.tablet_home.key and len(other.key) >= 16
    assert not runtime.tablet_home.key_ok(None) and runtime.tablet_home.key_ok(runtime.tablet_home.key)


# ------------------------------------------------------------------ back after a game
@pytest.mark.parametrize("pinned", [True, False])
def test_finished_game_returns_to_the_gesture_buttons(preview, pinned: bool) -> None:  # type: ignore[no-untyped-def]
    _, rt = preview
    tablet = rt.conn.robot.services["ALTabletService"]
    rt.tablet_home.return_after_s = 0.1
    if pinned:
        rt.tablet_home.pin("wave", True)
    rt.tablet.hold(OWNER)
    rt.tablet.show_url(f"{rt.preview.base_url}/game/tictactoe?g=1", owner=OWNER)
    rt.tictactoe.set_display("interactive")
    shown = pinned + 1  # the gesture page (when something is pinned), then the board

    st = finish_game(rt)
    assert st["result"] in tablet_home.GAME_RESULTS
    time.sleep(0.05)
    assert rt.tablet.holder == OWNER  # the result screen stays for a moment

    if pinned:
        assert wait_for(lambda: rt.tablet.holder is None)
        assert tablet_home.PAGE_PATH in tablet_shown(tablet, after=shown + 1)
        assert rt.tictactoe.state()["display"] is None
    else:
        time.sleep(0.3)
        assert rt.tablet.holder == OWNER and tablet.count("showWebview") == shown


def test_new_game_during_the_pause_keeps_the_board(preview) -> None:  # type: ignore[no-untyped-def]
    _, rt = preview
    rt.tablet_home.return_after_s = 0.1
    rt.tablet_home.pin("wave", True)
    rt.tablet.hold(OWNER)
    finish_game(rt)
    rt.tictactoe.restart()  # "Play again" on the tablet
    time.sleep(0.3)
    assert rt.tablet.holder == OWNER and rt.tictactoe.state()["active"]


def test_stopped_game_does_not_schedule_a_return(runtime) -> None:  # type: ignore[no-untyped-def]
    runtime.tablet_home.pin("wave", True)
    runtime.tictactoe.start()
    runtime.tictactoe.stop()
    assert runtime.tablet_home._timer is None


async def test_tictactoe_stop_goes_back_to_the_gesture_buttons() -> None:
    async with connected() as client:
        rt = client.rt  # type: ignore[attr-defined]
        tablet = client.robot.services["ALTabletService"]  # type: ignore[attr-defined]
        server = PreviewServer(rt, "127.0.0.1", _free_port())
        server.start()
        rt.preview = server
        try:
            await client.call_tool("tictactoe_start", {})
            assert "/game/tictactoe" in tablet_shown(tablet)
            rt.tablet_home.pin("wave", True)  # a game holds the tablet: remembered, not shown
            time.sleep(0.05)
            assert tablet.count("showWebview") == 1
            st = _json(await client.call_tool("tictactoe_stop", {}))
            assert st["result"] == "stopped"
            assert tablet_home.PAGE_PATH in tablet_shown(tablet, after=2)
            assert tablet.count("hideWebview") == 0
        finally:
            rt.preview = None
            server.stop()
