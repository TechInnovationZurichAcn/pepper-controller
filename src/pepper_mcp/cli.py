"""Robot options and process helpers for the ``pepper-mcp-gemini`` command line."""

from __future__ import annotations

import argparse
import logging
import signal
import sys

from pepper_mcp import __version__


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pepper-mcp-gemini",
                                description="Gemini Live drives a Pepper robot through its own MCP tools.")
    p.add_argument("--ip", help="Pepper's IP address (chest button says it); or PEPPER_IP")
    p.add_argument("--port", type=int, help="NAOqi port (default 9559)")
    p.add_argument("--fake", action="store_true", help="run against an in-memory fake robot (no hardware)")
    p.add_argument("--preview-port", type=int, help="serve the control dashboard (and MJPEG/audio/events) on this port")
    p.add_argument("--preview-token", help="token that lets other devices use the dashboard controls")
    p.add_argument("--language", help="default conversation and robot TTS language, e.g. German (default) or English")
    p.add_argument("--no-audio", dest="audio_enabled", action="store_false", default=None, help="disable microphone stream")
    p.add_argument("--no-camera", dest="camera_enabled", action="store_false", default=None, help="disable cameras")
    p.add_argument("--no-perception", dest="perception_enabled", action="store_false", default=None)
    p.add_argument("--no-reflexes", dest="reflexes_enabled", action="store_false", default=None)
    p.add_argument("--reflexes-off", help="comma separated reflex names to start disabled")
    p.add_argument("--liveliness", choices=["off", "calm", "lively"], help="autonomous movement preset at start")
    p.add_argument("--keep-autonomous-life", dest="autonomous_life", action="store_const", const="keep",
                   help="do not disable NAOqi autonomous life at start")
    p.add_argument("--safety-front", dest="safety_front_m", type=float,
                   help="front safety distance in m for base moves (default 0.15; NAOqi default 0.4)")
    p.add_argument("--safety-side", dest="safety_side_m", type=float,
                   help="side safety distance in m for base moves (default 0.05; NAOqi default 0.1)")
    p.add_argument("--camera-fps", type=int)
    p.add_argument("--camera-res", dest="camera_resolution", choices=["qqvga", "qvga", "vga"])
    p.add_argument("--mic", choices=["auto", "front", "left", "right", "rear"],
                   help="head microphone to listen with (default auto: skips dead mics, front preferred)")
    p.add_argument("--log-level", help="DEBUG, INFO, WARNING")
    p.add_argument("--version", action="version", version=f"pepper-mcp-gemini {__version__}")
    return p


def configure_logging(level: str) -> None:
    # stdout carries the conversation transcript; logs go to stderr.
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", stream=sys.stderr)
    logging.getLogger("mcp").setLevel(logging.WARNING)


def stop_on_signals() -> None:
    """Make SIGTERM (kill, systemd, docker stop) shut down like Ctrl+C.

    Python's default SIGTERM action exits without running any ``finally``, which would leave the
    robot with the server's reduced base safety distances and a dangling audio subscription.
    SIGTERM is turned into SIGINT rather than a raised KeyboardInterrupt because asyncio's runner
    answers SIGINT by cancelling the main task, which unwinds lifespans and ``finally`` blocks;
    an exception thrown out of the event loop's selector would skip them. SIGINT is re-armed too:
    a launcher that starts us in the background may have ignored it. Call before the event loop
    starts so the runner installs its handler.
    """
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, lambda _signum, _frame: signal.raise_signal(signal.SIGINT))

