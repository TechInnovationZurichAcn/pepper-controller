"""Chest tablet (ALTabletService). Calls that can block for seconds (``showWebview``)
run on one worker thread, in order, so a tool call returns immediately and the last thing asked
for is the last thing shown. A game can ``hold`` the tablet so nothing else covers its board."""

from __future__ import annotations

import html as html_lib
import logging
import queue
import threading
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from pepper_mcp.robot.connection import RobotConnection, RobotError

log = logging.getLogger(__name__)


def text_page(text: str, background: str = "#0b1020", color: str = "#eaf2ff", font_px: int = 64) -> str:
    safe = html_lib.escape(text).replace("\n", "<br>")
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<style>html,body{{margin:0;height:100%;background:{background};}}"
        "body{display:-webkit-box;-webkit-box-pack:center;-webkit-box-align:center;"
        "display:flex;justify-content:center;align-items:center;}"
        f"p{{color:{color};font-family:sans-serif;font-size:{int(font_px)}px;font-weight:bold;"
        "text-align:center;padding:0 40px;word-wrap:break-word;}</style></head>"
        f"<body><p>{safe}</p></body></html>"
    )


class TabletBusy(RobotError):
    """The tablet is held (a game board is on it) by someone other than the caller."""


class Tablet:
    def __init__(self, conn: RobotConnection):
        self.conn = conn
        self.last: dict[str, Any] | None = None
        #: who holds the screen (e.g. "tictactoe"); others may not replace or hide it
        self.holder: str | None = None
        self._jobs: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
        self._worker: threading.Thread | None = None
        self._worker_lock = threading.Lock()

    @property
    def tablet(self) -> Any:
        return self.conn.service("ALTabletService")

    def available(self) -> bool:
        return self.conn.has_service("ALTabletService")

    # ---------------------------------------------------------------- ownership
    def hold(self, owner: str) -> None:
        """Reserve the screen for ``owner``: other callers get :class:`TabletBusy`."""
        self.holder = owner

    def release(self, owner: str) -> None:
        if self.holder == owner:
            self.holder = None

    def _check(self, owner: str | None) -> None:
        if self.holder is not None and owner != self.holder:
            raise TabletBusy(f"the {self.holder} board is on the tablet; leave it there "
                             f"(call {self.holder}_stop first to use the tablet for something else)")

    # ---------------------------------------------------------------- worker
    def _bg(self, fn: str, *args: Any) -> None:
        tablet = self.tablet

        def _run() -> None:
            try:
                getattr(tablet, fn)(*args)
            except Exception as exc:
                log.warning("tablet %s failed: %s", fn, exc)

        self._enqueue(_run)

    def _enqueue(self, job: Callable[[], None]) -> None:
        self._jobs.put(job)
        with self._worker_lock:
            if self._worker is None:
                self._worker = threading.Thread(target=self._drain, name="tablet", daemon=True)
                self._worker.start()

    def _drain(self) -> None:
        while True:
            self._jobs.get()()

    # ---------------------------------------------------------------- display
    def show_html(self, html: str, owner: str | None = None) -> None:
        self._check(owner)
        self.last = {"kind": "html", "size": len(html)}
        uri = "data:text/html;charset=utf-8," + quote(html)
        self._bg("showWebview", uri)

    def show_text(self, text: str, background: str = "#0b1020", color: str = "#eaf2ff", font_px: int = 64) -> None:
        self._check(None)
        self.show_html(text_page(text, background, color, font_px))
        self.last = {"kind": "text", "text": text}

    def show_url(self, url: str, owner: str | None = None) -> None:
        self._check(owner)
        self.last = {"kind": "url", "url": url}
        self._bg("showWebview", url)

    def show_image(self, url: str) -> None:
        self._check(None)
        self.last = {"kind": "image", "url": url}
        self._bg("showImage", url)

    def hide(self, owner: str | None = None) -> None:
        self._check(owner)
        self.last = None
        tablet = self.tablet

        def _run() -> None:
            for fn in ("hideWebview", "hideImage", "hide"):
                try:
                    getattr(tablet, fn)()
                except Exception:
                    pass

        # Queued behind pending shows, or a slow showWebview would bring the page back after it.
        self._enqueue(_run)

    def execute_js(self, script: str) -> None:
        self._bg("executeJS", script)

    def play_video(self, url: str) -> None:
        self._check(None)
        self.last = {"kind": "video", "url": url}
        self._bg("playVideo", url)

    def stop_video(self) -> None:
        self._bg("stopVideo")

    def set_brightness(self, level: float) -> None:
        self.tablet.setBrightness(float(max(0.0, min(1.0, level))))

    def wake(self) -> None:
        for fn in ("wakeUp", "turnScreenOn"):
            try:
                getattr(self.tablet, fn)(True) if fn == "turnScreenOn" else getattr(self.tablet, fn)()
            except Exception:
                pass

    def open_settings(self) -> None:
        self.wake()
        self.tablet._openSettings()

    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {"available": self.available(), "showing": self.last}
        try:
            out["wifi"] = self.tablet.getWifiStatus()
        except Exception:
            pass
        return out
