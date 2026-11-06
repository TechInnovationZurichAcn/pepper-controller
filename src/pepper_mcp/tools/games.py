"""Games on the tablet: tic-tac-toe against Pepper.

Pepper's own moves are computed by the server (``games/tictactoe.py``), never by the LLM, so the
game keeps pace with a person tapping the tablet. The LLM starts and stops games, relays spoken
moves and comments on the ``game.move`` / ``game.over`` events.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from pepper_mcp.games.tictactoe import OWNER, static_board_html
from pepper_mcp.tools._common import ACTION, READ_ONLY, tool

HINTS = {
    "interactive": "The board is on the tablet: the person taps a square and you answer automatically. "
                   "Spoken moves work too (tictactoe_move). Comment on game.move / game.over events.",
    "static": "The tablet cannot reach the preview server (no --preview-port, or the relay on the head "
              "failed to start), so it "
              "shows a static board with numbered squares: ask the person for a number 1-9 and call "
              "tictactoe_move.",
    "unavailable": "No tablet: describe the board in words (rows) and take moves 1-9 by voice with tictactoe_move.",
}


def register(mcp, rt) -> None:  # type: ignore[no-untyped-def]
    game = rt.tictactoe

    def show(state: dict[str, Any]) -> str:
        """Put the board on the tablet; returns how it is displayed."""
        tablet = rt.tablet
        if tablet is None or not tablet.available():
            display = "unavailable"
        else:
            # Held until tictactoe_stop: the model likes to put "Let's play!" on the tablet right
            # after starting, which would replace the board.
            tablet.hold(OWNER)
            base = rt.tablet_base_url()
            if base:
                # The game id in the query string defeats the WebView cache between games.
                tablet.show_url(f"{base}/game/tictactoe?g={state['game_id']}", owner=OWNER)
                display = "interactive"
            else:
                tablet.show_html(static_board_html(state), owner=OWNER)
                display = "static"
        game.set_display(display)
        return display

    def refresh(state: dict[str, Any]) -> dict[str, Any]:
        # The interactive page polls on its own; the static one has to be re-sent after each move.
        if state.get("display") == "static" and rt.tablet is not None:
            rt.tablet.show_html(static_board_html(state), owner=OWNER)
        return state

    @tool(mcp, ACTION)
    def tictactoe_start(
        human_symbol: Annotated[Literal["X", "O"], Field(description="the person's symbol; Pepper gets the other")] = "X",
        human_first: Annotated[bool, Field(description="false lets Pepper open")] = True,
        difficulty: Annotated[Literal["easy", "medium", "hard"],
                              Field(description="hard is unbeatable; medium blocks and wins but can be "
                                                "out-forked; easy plays mostly at random")] = "medium",
    ) -> dict[str, Any]:
        """Play tic-tac-toe against the person on Pepper's tablet. Use when someone wants to play a
        game. This puts the board on the tablet by itself: don't show text on the tablet as well.
        Pepper's moves are made automatically right after the person's; you only cheer, tease
        and comment. Squares are numbered 1-9: 1 2 3 top row, 4 5 6 middle, 7 8 9 bottom. Starting
        again replaces the current game (the score is kept)."""
        state = game.start(human_symbol, human_first, difficulty)
        display = show(state)
        state = game.state()
        state["hint"] = HINTS[display]
        return state

    @tool(mcp, ACTION)
    def tictactoe_move(cell: Annotated[int, Field(ge=1, le=9, description="1 top-left, 5 centre, 9 bottom-right")]
                       ) -> dict[str, Any]:
        """Play the PERSON's move when they say it out loud ("the middle" = 5, "top right" = 3).
        Pepper replies immediately; the result contains Pepper's move (last_move) and the board."""
        return refresh(game.human_move(cell))

    @tool(mcp, READ_ONLY)
    def tictactoe_state() -> dict[str, Any]:
        """Current tic-tac-toe board (rows, top to bottom; '.' = empty), whose turn it is, result and score."""
        return game.state()

    @tool(mcp, ACTION)
    def tictactoe_stop(hide_tablet: bool = True) -> dict[str, Any]:
        """End the tic-tac-toe game (e.g. the person wants to stop) and optionally clear the tablet
        (it goes back to the gesture buttons when the operator pinned any)."""
        state = game.stop()
        if rt.tablet is not None:
            rt.tablet.release(OWNER)
            if hide_tablet and not rt.tablet_home.show():
                rt.tablet.hide()
        return state
