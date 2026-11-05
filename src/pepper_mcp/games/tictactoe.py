"""Tic-tac-toe between a person and Pepper.

Pepper's moves are chosen here, server-side, the moment the person moves: the game never waits
for the LLM, which only comments on ``game.*`` events. Cells are numbered 1-9 in reading order
everywhere outside this module (tools, HTTP, events):

    1 | 2 | 3
    4 | 5 | 6
    7 | 8 | 9
"""

from __future__ import annotations

import functools
import html as html_lib
import os
import random
import threading
from typing import Any

HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tictactoe.html")

LINES = ((0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6))
DIFFICULTIES = ("easy", "medium", "hard")
SYMBOLS = ("X", "O")
EMPTY = ""
#: Pause before Pepper answers a tablet touch, so its move reads as a reply rather than a glitch.
PEPPER_DELAY_S = 0.7

Board = tuple[str, ...]


class GameError(ValueError):
    """Illegal request: no game, game over, not the person's turn, occupied or invalid cell."""


# ------------------------------------------------------------------ pure rules
def other(symbol: str) -> str:
    return "O" if symbol == "X" else "X"


def winning_line(board: Board) -> tuple[int, int, int] | None:
    for line in LINES:
        a, b, c = line
        if board[a] and board[a] == board[b] == board[c]:
            return line
    return None


def winner(board: Board) -> str | None:
    """``"X"``, ``"O"``, ``"draw"`` or ``None`` while the game is open."""
    line = winning_line(board)
    if line:
        return board[line[0]]
    if all(board):
        return "draw"
    return None


def free_cells(board: Board) -> list[int]:
    return [i for i, v in enumerate(board) if not v]


def _play(board: Board, cell: int, symbol: str) -> Board:
    return board[:cell] + (symbol,) + board[cell + 1:]


@functools.cache
def _score(board: Board, to_move: str, me: str) -> int:
    """Minimax value for ``me``: +10-depth for a win, -(10-depth) for a loss, 0 for a draw.
    The depth term makes Pepper win as fast as possible and lose as slowly as possible."""
    result = winner(board)
    depth = sum(1 for v in board if v)
    if result == me:
        return 10 - depth
    if result == "draw":
        return 0
    if result is not None:
        return depth - 10
    scores = [_score(_play(board, c, to_move), other(to_move), me) for c in free_cells(board)]
    return max(scores) if to_move == me else min(scores)


def best_moves(board: Board, symbol: str) -> list[int]:
    """Every optimal move for ``symbol`` (0-based); playing any of them never loses."""
    cells = free_cells(board)
    if not cells:
        return []
    scored = [(_score(_play(board, c, symbol), other(symbol), symbol), c) for c in cells]
    top = max(s for s, _ in scored)
    return [c for s, c in scored if s == top]


def _immediate(board: Board, symbol: str) -> list[int]:
    return [c for c in free_cells(board) if winner(_play(board, c, symbol)) == symbol]


def choose_move(board: Board, symbol: str, difficulty: str, rng: random.Random) -> int:
    """Pepper's move (0-based).

    * ``hard``: perfect play, cannot be beaten.
    * ``medium``: takes a win, blocks a loss, otherwise plays perfectly half of the time; forks beat it.
    * ``easy``: takes a win, otherwise random.
    """
    cells = free_cells(board)
    if not cells:
        raise GameError("the board is full")
    if difficulty == "hard":
        return rng.choice(best_moves(board, symbol))
    wins = _immediate(board, symbol)
    if wins:
        return rng.choice(wins)
    if difficulty == "easy":
        return rng.choice(cells)
    blocks = _immediate(board, other(symbol))
    if blocks:
        return rng.choice(blocks)
    if rng.random() < 0.5:
        return rng.choice(best_moves(board, symbol))
    return rng.choice(cells)


def board_rows(board: Board) -> list[str]:
    """``["X.O", ".X.", "..O"]`` – compact and unambiguous for an LLM."""
    return ["".join(v or "." for v in board[r * 3:r * 3 + 3]) for r in range(3)]


# ------------------------------------------------------------------ session
class TicTacToe:
    """One game at a time, shared by the MCP tools (voice moves) and the tablet page (touch).

    Thread-safe: HTTP handlers, tool calls and Pepper's delayed reply run on different threads.
    ``version`` increases on every change so the tablet page knows when to redraw.
    """

    def __init__(self, bus: Any, rng: random.Random | None = None):
        self.bus = bus
        self.rng = rng or random.Random()
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self.game_id = 0
        self.version = 0
        self.active = False
        self.board: Board = (EMPTY,) * 9
        self.human = "X"
        self.pepper = "O"
        self.human_first = True
        self.difficulty = "medium"
        self.turn: str | None = None
        self.result: str | None = None  # "human" | "pepper" | "draw" | "stopped"
        self.line: tuple[int, int, int] | None = None
        self.last_move: dict[str, Any] | None = None
        self.score = {"human": 0, "pepper": 0, "draw": 0}
        #: how the board is shown: "interactive" (touch page), "static" (voice only) or None
        self.display: str | None = None

    # ---------------------------------------------------------------- queries
    def state(self) -> dict[str, Any]:
        with self._lock:
            return self._state()

    def _state(self) -> dict[str, Any]:
        whose = None
        if self.turn is not None:
            whose = "human" if self.turn == self.human else "pepper"
        return {
            "game": "tictactoe",
            "game_id": self.game_id,
            "version": self.version,
            "active": self.active,
            "board": list(self.board),
            "rows": board_rows(self.board),
            "human": self.human,
            "pepper": self.pepper,
            "difficulty": self.difficulty,
            "turn": whose,
            "result": self.result,
            "winning_cells": [c + 1 for c in self.line] if self.line else None,
            "last_move": self.last_move,
            "free_cells": [c + 1 for c in free_cells(self.board)] if self.active else [],
            "score": dict(self.score),
            "display": self.display,
        }

    # ---------------------------------------------------------------- commands
    def start(self, human_symbol: str = "X", human_first: bool = True, difficulty: str = "medium",
              pepper_delay_s: float = 0.0) -> dict[str, Any]:
        if human_symbol not in SYMBOLS:
            raise GameError("human_symbol must be X or O")
        if difficulty not in DIFFICULTIES:
            raise GameError(f"difficulty must be one of {list(DIFFICULTIES)}")
        with self._lock:
            self._cancel_timer()
            self.game_id += 1
            self.version += 1
            self.active = True
            self.board = (EMPTY,) * 9
            self.human, self.pepper = human_symbol, other(human_symbol)
            self.human_first = human_first
            self.difficulty = difficulty
            self.turn = self.human if human_first else self.pepper
            self.result = None
            self.line = None
            self.last_move = None
            started = {"game": "tictactoe", "game_id": self.game_id, "human": self.human, "pepper": self.pepper,
                       "first": "human" if human_first else "pepper", "difficulty": difficulty}
        self.bus.publish("game.started", started, source="game")
        if not human_first:
            self._pepper_turn(pepper_delay_s)
        return self.state()

    def restart(self, pepper_delay_s: float = 0.0) -> dict[str, Any]:
        """New round with the same players; whoever did not open the last round opens this one."""
        with self._lock:
            if self.game_id == 0:
                raise GameError("no game has been started yet")
            human_symbol, human_first, difficulty = self.human, not self.human_first, self.difficulty
        return self.start(human_symbol, human_first, difficulty, pepper_delay_s)

    def human_move(self, cell: int, pepper_delay_s: float = 0.0) -> dict[str, Any]:
        """Play the person's ``cell`` (1-9); Pepper answers after ``pepper_delay_s`` (0 = before returning)."""
        if isinstance(cell, bool) or not isinstance(cell, int) or not 1 <= cell <= 9:
            raise GameError("cell must be a number from 1 (top left) to 9 (bottom right)")
        with self._lock:
            if not self.active:
                raise GameError("no game is running; start one first" if self.result is None
                                else "this game is over; start a new one")
            if self.turn != self.human:
                raise GameError("it is Pepper's turn")
            if self.board[cell - 1]:
                raise GameError(f"cell {cell} is already taken")
            events = self._apply(cell - 1, self.human, "human")
            pepper_next = self.active
        self._publish(events)
        if pepper_next:
            self._pepper_turn(pepper_delay_s)
        return self.state()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._cancel_timer()
            was_active = self.active
            if was_active:
                self.active = False
                self.turn = None
                self.result = "stopped"
                self.version += 1
            self.display = None
            game_id = self.game_id
        if was_active:
            self.bus.publish("game.over", {"game": "tictactoe", "game_id": game_id, "result": "stopped"},
                             source="game")
        return self.state()

    def shutdown(self) -> None:
        with self._lock:
            self._cancel_timer()

    def set_display(self, display: str | None) -> None:
        with self._lock:
            self.display = display

    # ---------------------------------------------------------------- internals (lock held)
    def _apply(self, cell: int, symbol: str, player: str) -> list[tuple[str, dict[str, Any]]]:
        self.board = _play(self.board, cell, symbol)
        self.version += 1
        self.last_move = {"player": player, "cell": cell + 1}
        result = winner(self.board)
        self.turn = None if result else other(symbol)
        move = {"game": "tictactoe", "game_id": self.game_id, "player": player, "cell": cell + 1,
                "rows": board_rows(self.board), "next": None if result else ("pepper" if player == "human" else "human")}
        events = [("game.move", move)]
        if result:
            self.active = False
            self.line = winning_line(self.board)
            self.result = "draw" if result == "draw" else ("human" if result == self.human else "pepper")
            self.score[self.result] += 1
            events.append(("game.over", {"game": "tictactoe", "game_id": self.game_id, "result": self.result,
                                         "winning_cells": [c + 1 for c in self.line] if self.line else None,
                                         "rows": board_rows(self.board), "score": dict(self.score)}))
        return events

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    # ---------------------------------------------------------------- Pepper's move
    def _publish(self, events: list[tuple[str, dict[str, Any]]]) -> None:
        for kind, data in events:
            self.bus.publish(kind, data, source="game")

    def _pepper_turn(self, delay_s: float) -> None:
        with self._lock:
            game_id = self.game_id
            if delay_s <= 0:
                events = self._pepper_move_locked(game_id)
            else:
                self._cancel_timer()
                self._timer = threading.Timer(delay_s, self._pepper_move, (game_id,))
                self._timer.daemon = True
                self._timer.start()
                return
        self._publish(events)

    def _pepper_move(self, game_id: int) -> None:
        with self._lock:
            self._timer = None
            events = self._pepper_move_locked(game_id)
        self._publish(events)

    def _pepper_move_locked(self, game_id: int) -> list[tuple[str, dict[str, Any]]]:
        # A restart or stop while Pepper was "thinking" makes this move stale.
        if not self.active or self.game_id != game_id or self.turn != self.pepper:
            return []
        cell = choose_move(self.board, self.pepper, self.difficulty, self.rng)
        return self._apply(cell, self.pepper, "pepper")


# ------------------------------------------------------------------ tablet pages
def page_html() -> bytes:
    with open(HTML_PATH, "rb") as fh:
        return fh.read()


def static_board_html(state: dict[str, Any]) -> str:
    """Board without scripts, for tablets that cannot reach the preview server (voice moves only)."""
    win = set(state.get("winning_cells") or [])
    cells = []
    for i, v in enumerate(state["board"]):
        n = i + 1
        color = "#ff6b6b" if v == "X" else "#4dd2ff"
        bg = "#2e3b66" if n in win else "#18213d"
        label = html_lib.escape(v) if v else f"<span style='color:#3b4a78;font-size:6vh'>{n}</span>"
        cells.append(f"<td style='background:{bg};color:{color}'>{label}</td>")
    rows = "".join("<tr>" + "".join(cells[r * 3:r * 3 + 3]) + "</tr>" for r in range(3))
    result = state.get("result")
    if result == "human":
        msg = "You win!"
    elif result == "pepper":
        msg = "Pepper wins!"
    elif result == "draw":
        msg = "Draw!"
    elif state.get("turn") == "human":
        msg = "Your turn – say a number"
    else:
        msg = "Pepper is thinking…"
    sc = state["score"]
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<style>html,body{margin:0;height:100%;background:#0b1020;color:#eaf2ff;font-family:sans-serif;"
        "text-align:center;overflow:hidden}"
        "table{margin:3vh auto 0;border-spacing:1.2vh}"
        "td{width:24vh;height:24vh;font-size:18vh;font-weight:bold;border-radius:2vh;text-align:center;"
        "vertical-align:middle}"
        "h1{margin:3vh 0 0;font-size:7vh}p{margin:1vh;font-size:4vh;color:#9fb0d8}</style></head><body>"
        f"<h1>{html_lib.escape(msg)}</h1><table>{rows}</table>"
        f"<p>You {html_lib.escape(state['human'])} {sc['human']} · Pepper {html_lib.escape(state['pepper'])} "
        f"{sc['pepper']} · Draws {sc['draw']}</p></body></html>"
    )

