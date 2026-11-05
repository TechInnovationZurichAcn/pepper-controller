"""Tic-tac-toe: rules, Pepper's play, the session, the tablet HTTP API and the MCP tools."""

from __future__ import annotations

import json
import random
import socket
import time
import urllib.error
import urllib.request
from typing import Any

import pytest

from pepper_mcp.games.tictactoe import (
    EMPTY,
    GameError,
    TicTacToe,
    best_moves,
    board_rows,
    choose_move,
    free_cells,
    other,
    static_board_html,
    winner,
    winning_line,
)
from pepper_mcp.gemini_live import format_event
from pepper_mcp.preview import PreviewServer, control_denied
from pepper_mcp.robot.events import EventBus
from tests.test_server import _json, connected


def B(s: str) -> tuple[str, ...]:
    """Board from 9 chars, '.' = empty."""
    return tuple(EMPTY if c == "." else c for c in s.replace(" ", ""))


def wait_state(game: TicTacToe, predicate: Any, timeout_s: float = 3.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    st = game.state()
    while not predicate(st) and time.monotonic() < deadline:
        time.sleep(0.01)
        st = game.state()
    return st


@pytest.fixture
def bus():  # type: ignore[no-untyped-def]
    b = EventBus()
    yield b
    b.shutdown()


def events(bus: EventBus, prefix: str = "game.") -> list[tuple[str, dict[str, Any]]]:
    return [(e.type, e.data) for e in bus.recent(0, None, 500) if e.type.startswith(prefix)]


# ------------------------------------------------------------------ rules
@pytest.mark.parametrize(("board", "result", "line"), [
    ("XXX OO. ...", "X", (0, 1, 2)),
    ("O.X .OX ..O", "O", (0, 4, 8)),
    ("X.O XO. O.X", "O", (2, 4, 6)),
    ("OX. OX. .X.", "X", (1, 4, 7)),
    ("XOX XOO OXX", "draw", None),
    ("X.. .O. ...", None, None),
    (".........", None, None),
])
def test_winner_and_line(board: str, result: str | None, line: tuple[int, ...] | None) -> None:
    assert winner(B(board)) == result
    assert winning_line(B(board)) == line


def test_rows_and_free_cells() -> None:
    board = B("X.O .X. ..O")
    assert board_rows(board) == ["X.O", ".X.", "..O"]
    assert free_cells(board) == [1, 3, 5, 6, 7]
    assert other("X") == "O" and other("O") == "X"


def test_best_moves_win_now_and_block() -> None:
    assert best_moves(B("OO. XX. X.."), "O") == [2]  # win beats blocking
    assert best_moves(B("XX. .O. ..."), "O") == [2]  # must block
    assert set(best_moves(B("........."), "X")) <= set(range(9))


def _never_loses(board: tuple[str, ...], to_move: str, pepper: str) -> bool:
    """Every human line against every optimal Pepper reply: Pepper never loses."""
    result = winner(board)
    if result is not None:
        return result in (pepper, "draw")
    if to_move == pepper:
        return all(_never_loses(board[:c] + (pepper,) + board[c + 1:], other(pepper), pepper)
                   for c in best_moves(board, pepper))
    return all(_never_loses(board[:c] + (to_move,) + board[c + 1:], pepper, pepper) for c in free_cells(board))


@pytest.mark.parametrize("pepper", ["X", "O"])
@pytest.mark.parametrize("pepper_first", [True, False])
def test_hard_is_unbeatable(pepper: str, pepper_first: bool) -> None:
    to_move = pepper if pepper_first else other(pepper)
    assert _never_loses((EMPTY,) * 9, to_move, pepper)


@pytest.mark.parametrize("difficulty", ["easy", "medium", "hard"])
def test_every_level_takes_an_immediate_win(difficulty: str) -> None:
    rng = random.Random(1)
    for _ in range(30):
        assert choose_move(B("OO. XX. X.."), "O", difficulty, rng) == 2


def test_medium_always_blocks() -> None:
    rng = random.Random(2)
    for _ in range(50):
        assert choose_move(B("XX. .O. ..."), "O", "medium", rng) == 2


def test_easy_is_beatable() -> None:
    """Easy ignores threats, so some seed lets the person win from a double line."""
    moves = {choose_move(B("XX. .O. ..."), "O", "easy", random.Random(seed)) for seed in range(40)}
    assert moves - {2}


def test_choose_move_on_full_board_raises() -> None:
    with pytest.raises(GameError):
        choose_move(B("XOX XOO OXX"), "O", "hard", random.Random())


# ------------------------------------------------------------------ session
def test_human_move_gets_an_immediate_reply(bus: EventBus) -> None:
    game = TicTacToe(bus, random.Random(3))
    st = game.start("X", True, "hard")
    assert st["turn"] == "human" and st["active"] and st["board"] == [""] * 9
    st = game.human_move(5)
    assert st["board"].count("X") == 1 and st["board"].count("O") == 1
    assert st["last_move"]["player"] == "pepper"
    assert st["turn"] == "human"
    kinds = [(k, d.get("player")) for k, d in events(bus)]
    assert kinds == [("game.started", None), ("game.move", "human"), ("game.move", "pepper")]


@pytest.mark.parametrize(("cell", "msg"), [(0, "from 1"), (10, "from 1"), (True, "number"),
                                           ("5", "number"), (None, "number")])
def test_invalid_cells_are_refused(bus: EventBus, cell: Any, msg: str) -> None:
    game = TicTacToe(bus)
    game.start()
    with pytest.raises(GameError, match=msg):
        game.human_move(cell)


def test_occupied_cell_and_no_game(bus: EventBus) -> None:
    game = TicTacToe(bus, random.Random(4))
    with pytest.raises(GameError, match="no game"):
        game.human_move(1)
    with pytest.raises(GameError, match="no game has been started"):
        game.restart()
    game.start()
    st = game.human_move(1)
    taken = st["last_move"]["cell"]
    with pytest.raises(GameError, match="already taken"):
        game.human_move(taken)


def test_pepper_opens_when_asked(bus: EventBus) -> None:
    game = TicTacToe(bus, random.Random(5))
    st = game.start("O", human_first=False, difficulty="hard")
    assert st["pepper"] == "X" and st["board"].count("X") == 1 and st["turn"] == "human"


def test_game_over_score_and_restart_alternates(bus: EventBus) -> None:
    game = TicTacToe(bus, random.Random(6))
    game.start("X", True, "hard")
    for _ in range(5):
        st = game.state()
        if not st["active"]:
            break
        game.human_move(st["free_cells"][0])
    st = game.state()
    assert not st["active"] and st["result"] in ("pepper", "draw")  # hard never loses
    assert st["score"][st["result"]] == 1
    over = [d for k, d in events(bus) if k == "game.over"]
    assert over and over[-1]["result"] == st["result"]
    with pytest.raises(GameError, match="over"):
        game.human_move(st["free_cells"][0] if st["free_cells"] else 1)
    st = game.restart()
    assert st["active"] and st["board"].count("X") + st["board"].count("O") == 1  # Pepper opens this round


def test_human_can_win_and_win_line_is_reported(bus: EventBus) -> None:
    game = TicTacToe(bus)
    game.start("X", True, "easy")
    # Rig the position: X on 1 and 2, Pepper's O on 4 and 5; the person completes the top row.
    with game._lock:
        game.board = B("XX. OO. ...")
    st = game.human_move(3)
    assert st["result"] == "human" and st["winning_cells"] == [1, 2, 3]
    assert st["score"]["human"] == 1 and st["turn"] is None
    assert [d.get("player") for k, d in events(bus) if k == "game.move"][-1] == "human"


def test_delayed_reply_blocks_moves_until_pepper_played(bus: EventBus) -> None:
    game = TicTacToe(bus, random.Random(7))
    game.start()
    st = game.human_move(5, pepper_delay_s=0.2)
    assert st["turn"] == "pepper" and st["board"].count("O") == 0
    with pytest.raises(GameError, match="Pepper's turn"):
        game.human_move(1)
    st = wait_state(game, lambda s: s["turn"] == "human")
    assert st["board"].count("O") == 1


def test_stop_and_restart_cancel_a_pending_reply(bus: EventBus) -> None:
    game = TicTacToe(bus, random.Random(8))
    game.start()
    game.human_move(5, pepper_delay_s=0.15)
    st = game.stop()
    assert st["result"] == "stopped" and not st["active"]
    time.sleep(0.3)
    assert game.state()["board"].count("O") == 0
    assert events(bus)[-1] == ("game.over", {"game": "tictactoe", "game_id": 1, "result": "stopped"})

    game.start()
    game.human_move(1, pepper_delay_s=0.15)
    st = game.start()  # a new game while Pepper "thinks"
    time.sleep(0.3)
    assert game.state()["board"] == [""] * 9 and st["game_id"] == 3


def test_static_board_escapes_and_numbers_free_cells(bus: EventBus) -> None:
    game = TicTacToe(bus, random.Random(9))
    game.start()
    page = static_board_html(game.human_move(1))
    assert "<script" not in page and ">X<" in page and ">9<" in page and "Your turn" in page


# ------------------------------------------------------------------ tablet HTTP API
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


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


def _req(url: str, body: Any = None, headers: dict[str, str] | None = None) -> tuple[int, Any]:
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    hdrs = {"Content-Type": "application/json"} if body is not None else {}
    req = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET",
                                 headers={**hdrs, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            raw = r.read()
            return r.status, json.loads(raw) if r.headers.get("Content-Type") == "application/json" else raw
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_tablet_page_and_touch_moves(preview) -> None:  # type: ignore[no-untyped-def]
    base, rt = preview
    status, page = _req(f"{base}/game/tictactoe")
    assert status == 200 and b"/api/game/tictactoe" in page
    assert _req(f"{base}/api/game/tictactoe/move", {"cell": 5})[0] == 409  # no game yet

    rt.tictactoe.start("X", True, "hard")
    status, st = _req(f"{base}/api/game/tictactoe/move", {"cell": 5})
    assert status == 200 and st["board"][4] == "X" and st["turn"] == "pepper"
    assert _req(f"{base}/api/game/tictactoe/move", {"cell": 1})[0] == 409  # Pepper is thinking
    st = wait_state(rt.tictactoe, lambda s: s["turn"] == "human")
    status, polled = _req(f"{base}/api/game/tictactoe")
    assert status == 200 and polled["version"] == st["version"] and polled["board"].count("O") == 1

    status, err = _req(f"{base}/api/game/tictactoe/move", {"cell": 5})
    assert status == 409 and "taken" in err["error"]
    assert _req(f"{base}/api/game/tictactoe/move", b"[1]")[0] == 400
    assert _req(f"{base}/api/game/tictactoe/move", b"{nope")[0] == 400
    assert _req(f"{base}/api/game/tictactoe/fly", {})[0] == 404
    assert _req(f"{base}/api/game/tictactoe/move", {"cell": 1}, {"Origin": "http://evil.example"})[0] == 403

    status, st = _req(f"{base}/api/game/tictactoe/new", {})
    assert status == 200 and st["game_id"] == 2


def test_game_moves_are_open_to_the_tablet_but_robot_controls_are_not() -> None:
    headers = {"Host": "192.168.1.20:8765", "Content-Type": "application/json",
               "Origin": "http://192.168.1.20:8765"}
    assert control_denied("198.18.0.1", headers, "", open_to_lan=True) is None
    assert control_denied("198.18.0.1", headers, "") is not None
    assert control_denied("198.18.0.1", {**headers, "Host": "evil.example:8765"}, "", open_to_lan=True)
    assert control_denied("198.18.0.1", {**headers, "Content-Type": "text/plain"}, "", open_to_lan=True)


# ------------------------------------------------------------------ MCP tools
async def test_tools_start_show_static_board_without_preview() -> None:
    async with connected() as client:
        tools = {t.name for t in (await client.list_tools()).tools}
        assert {"tictactoe_start", "tictactoe_move", "tictactoe_state", "tictactoe_stop"} <= tools
        st = _json(await client.call_tool("tictactoe_start", {"difficulty": "hard"}))
        assert st["display"] == "static" and "tictactoe_move" in st["hint"]
        tablet = client.robot.services["ALTabletService"]  # type: ignore[attr-defined]
        assert tablet_shown(tablet).startswith("data:text/html")

        st = _json(await client.call_tool("tictactoe_move", {"cell": 5}))
        assert st["board"][4] == "X" and st["last_move"]["player"] == "pepper"
        assert "%3EO%3C" in tablet_shown(tablet, after=2)  # the static board was re-sent with Pepper's O

        bad = await client.call_tool("tictactoe_move", {"cell": 5})
        assert bad.is_error and "taken" in bad.content[0].text
        assert _json(await client.call_tool("tictactoe_state", {}))["rows"][1][1] == "X"
        st = _json(await client.call_tool("tictactoe_stop", {}))
        assert st["result"] == "stopped" and st["display"] is None


async def test_tools_show_interactive_page_with_preview() -> None:
    async with connected() as client:
        rt = client.rt  # type: ignore[attr-defined]
        server = PreviewServer(rt, "127.0.0.1", _free_port())
        server.start()
        rt.preview = server
        try:
            st = _json(await client.call_tool("tictactoe_start", {"human_first": False}))
            assert st["display"] == "interactive" and st["board"].count("O") == 1
            tablet = client.robot.services["ALTabletService"]  # type: ignore[attr-defined]
            assert tablet_shown(tablet) == f"{server.base_url}/game/tictactoe?g={st['game_id']}"
        finally:
            rt.preview = None
            server.stop()


def tablet_shown(tablet: Any, after: int = 1, timeout_s: float = 2.0) -> str:
    """URL of the latest ``showWebview`` once at least ``after`` calls happened (they run on a thread)."""
    deadline = time.monotonic() + timeout_s
    while tablet.count("showWebview") < after and time.monotonic() < deadline:
        time.sleep(0.01)
    return tablet.last("showWebview")[0]


# ------------------------------------------------------------------ Gemini
def test_gemini_hears_pepper_moves_and_results_not_each_tap() -> None:
    human = {"player": "human", "cell": 5, "next": "pepper", "rows": ["...", ".X.", "..."]}
    assert format_event("game.move", human) is None
    assert format_event("game.move", {"player": "pepper", "cell": 1, "next": "human"}).startswith(
        "[robot event] game.move")
    assert format_event("game.move", {"player": "human", "cell": 3, "next": None}) is not None
    assert "human" in format_event("game.over", {"result": "human", "winning_cells": [1, 2, 3]})
