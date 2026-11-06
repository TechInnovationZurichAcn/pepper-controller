"""Gesture buttons on Pepper's tablet, pinned from the dashboard.

The operator presses ``+`` on a gesture in the dashboard and it shows up as a big button on the
tablet; people standing in front of Pepper press it to play the gesture. This page is the tablet's
home screen: it comes back 5 s after a tic-tac-toe game ends (win, loss or draw) and when a game is
stopped.

The tablet is another device, so its requests cannot pass the dashboard's "this machine only"
check. Instead the page URL carries a random key that only the tablet is given (NAOqi opens the
URL), and it may only play the gestures the operator pinned.
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
import threading
from typing import Any

from pepper_mcp.games.tictactoe import OWNER as GAME_OWNER
from pepper_mcp.robot.tablet import TabletBusy

log = logging.getLogger(__name__)

HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tablet_gestures.html")
PAGE_PATH = "/tablet/gestures"
API_PATH = "/api/tablet/gestures"
KEY_HEADER = "X-Tablet-Key"
#: how long the win/loss/draw screen stays before the gesture buttons come back
GAME_RETURN_S = 5.0
GAME_RESULTS = ("human", "pepper", "draw")


def page_html() -> bytes:
    with open(HTML_PATH, "rb") as fh:
        return fh.read()


def label(name: str) -> str:
    """``hand_wave`` -> ``Hand wave``, as the dashboard shows it."""
    text = name.replace("_", " ")
    return text[:1].upper() + text[1:]


class TabletHome:
    """Which gestures are pinned to the tablet, and putting that page on the screen.

    Thread-safe: dashboard actions, tablet requests and the game-over timer run on different threads.
    """

    def __init__(self, rt: Any, return_after_s: float = GAME_RETURN_S):
        self.rt = rt
        self.return_after_s = return_after_s
        self.key = secrets.token_urlsafe(16)
        self._pinned: list[str] = []
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self.version = 0

    # ---------------------------------------------------------------- pinned gestures
    def pinned(self) -> list[str]:
        """Pinned gestures that still exist (a user-defined gesture can be removed later)."""
        known = set(self.rt.gestures.names())
        with self._lock:
            return [n for n in self._pinned if n in known]

    def is_pinned(self, name: str) -> bool:
        return name in self.pinned()

    def pin(self, name: str, pinned: bool) -> dict[str, Any]:
        """Add (``pinned=True``) or remove a gesture button, then refresh the tablet."""
        if pinned:
            self.rt.gestures.get(name)  # RobotError for an unknown gesture
        with self._lock:
            changed = pinned != (name in self._pinned)
            if changed:
                if pinned:
                    self._pinned.append(name)
                else:
                    self._pinned.remove(name)
                self.version += 1
            empty = not self._pinned
        if not changed:
            return self._result(shown=None)
        if empty:
            self._hide_if_showing()
            return self._result(shown=False)
        return self._result(shown=self.show())

    def state(self) -> dict[str, Any]:
        """What the tablet page polls: its buttons and which one is playing."""
        gestures = {g["name"]: g.get("description", "") for g in self.rt.gestures.list()}
        names = [n for n in self.pinned() if n in gestures]
        return {"version": self.version,
                "gestures": [{"name": n, "label": label(n), "description": gestures[n]} for n in names],
                "playing": self.rt.gestures.active(),
                "dancing": self.rt.gestures.dancing()}

    def _result(self, shown: bool | None) -> dict[str, Any]:
        return {"pinned": self.pinned(), "shown": shown}

    def key_ok(self, key: str | None) -> bool:
        return hmac.compare_digest((key or "").encode(), self.key.encode())

    # ---------------------------------------------------------------- tablet screen
    def url(self) -> str | None:
        base = self.rt.tablet_base_url()
        if not base:
            return None
        # The version in the query string defeats the WebView cache.
        return f"{base}{PAGE_PATH}?k={self.key}&v={self.version}"

    def showing(self) -> bool:
        tablet = self.rt.tablet
        last = tablet.last if tablet is not None else None
        return bool(last and last.get("kind") == "url" and PAGE_PATH in str(last.get("url")))

    def show(self) -> bool:
        """Put the gesture buttons on the tablet. False when nothing is pinned, a game holds the
        tablet, or the tablet cannot reach the preview server."""
        tablet = self.rt.tablet
        if not self.pinned() or tablet is None or not tablet.available() or tablet.holder is not None:
            return False
        if self.showing():
            return True  # the page polls and picks up the change by itself
        url = self.url()
        if url is None:
            log.info("gesture buttons not shown: the tablet cannot reach the preview server")
            return False
        try:
            tablet.show_url(url)
        except TabletBusy:
            return False  # a game took the tablet since the check above; its board stays
        # tablet_status reports what is shown to the model: keep the key out of it.
        tablet.last = {"kind": "url", "url": url.replace(f"k={self.key}", "k=…")}
        return True

    def _hide_if_showing(self) -> None:
        tablet = self.rt.tablet
        if tablet is not None and tablet.holder is None and self.showing():
            tablet.hide()

    # ---------------------------------------------------------------- after a game
    def on_event(self, event: Any) -> None:
        """Bus hook: a finished game returns to the gesture buttons after ``return_after_s``."""
        if event.type != "game.over" or event.data.get("result") not in GAME_RESULTS:
            return
        game_id = event.data.get("game_id")
        with self._lock:
            self._cancel_timer()
            self._timer = threading.Timer(self.return_after_s, self._return_from_game, (game_id,))
            self._timer.daemon = True
            self._timer.start()

    def _return_from_game(self, game_id: int) -> None:
        with self._lock:
            self._timer = None
        if not self.pinned():
            return  # nothing to go back to: the result screen stays, as without pinned gestures
        tablet = self.rt.tablet
        if tablet is None or not tablet.available() or self.url() is None:
            log.info("game over, but the tablet cannot load the gesture buttons; the board stays")
            return
        if not self.rt.tictactoe.stop_if_over(game_id):
            return  # "Play again" (or a new game) during the pause keeps the board
        tablet.release(GAME_OWNER)
        try:
            self.show()
        except Exception as exc:
            log.warning("returning the tablet to the gesture buttons failed: %s", exc)

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def shutdown(self) -> None:
        with self._lock:
            self._cancel_timer()
