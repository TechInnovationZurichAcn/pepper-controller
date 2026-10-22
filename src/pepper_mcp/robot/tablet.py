"""Chest tablet (ALTabletService). Calls that can block for seconds (``showWebview``)
run on a worker thread so a tool call returns immediately."""

from __future__ import annotations

import html as html_lib
import logging
import threading
from typing import Any
from urllib.parse import quote

from pepper_mcp.robot.connection import RobotConnection

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


class Tablet:
    def __init__(self, conn: RobotConnection):
        self.conn = conn
        self.last: dict[str, Any] | None = None

    @property
    def tablet(self) -> Any:
        return self.conn.service("ALTabletService")

    def available(self) -> bool:
        return self.conn.has_service("ALTabletService")

    def _bg(self, fn: str, *args: Any) -> None:
        tablet = self.tablet

        def _run() -> None:
            try:
                getattr(tablet, fn)(*args)
            except Exception as exc:
                log.warning("tablet %s failed: %s", fn, exc)

        threading.Thread(target=_run, name="tablet", daemon=True).start()

    def show_html(self, html: str) -> None:
        self.last = {"kind": "html", "size": len(html)}
        uri = "data:text/html;charset=utf-8," + quote(html)
        self._bg("showWebview", uri)

    def show_text(self, text: str, background: str = "#0b1020", color: str = "#eaf2ff", font_px: int = 64) -> None:
        self.last = {"kind": "text", "text": text}
        self.show_html(text_page(text, background, color, font_px))

    def show_url(self, url: str) -> None:
        self.last = {"kind": "url", "url": url}
        self._bg("showWebview", url)

    def show_image(self, url: str) -> None:
        self.last = {"kind": "image", "url": url}
        self._bg("showImage", url)

    def hide(self) -> None:
        self.last = None
        tablet = self.tablet
        for fn in ("hideWebview", "hideImage", "hide"):
            try:
                getattr(tablet, fn)()
            except Exception:
                pass

    def execute_js(self, script: str) -> None:
        self._bg("executeJS", script)

    def play_video(self, url: str) -> None:
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
