"""``pepper-mcp`` command line entry point."""

from __future__ import annotations

import argparse
import logging
import sys

from pepper_mcp import __version__
from pepper_mcp.config import Settings


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pepper-mcp",
                                description="MCP server that gives an LLM real-time control of a Pepper robot.")
    p.add_argument("--ip", help="Pepper's IP address (chest button says it); or PEPPER_IP")
    p.add_argument("--port", type=int, help="NAOqi port (default 9559)")
    p.add_argument("--fake", action="store_true", help="run against an in-memory fake robot (no hardware)")
    p.add_argument("--transport", choices=["stdio", "streamable-http", "sse"], help="MCP transport (default stdio)")
    p.add_argument("--host", dest="http_host", help="bind host for HTTP transports (default 127.0.0.1)")
    p.add_argument("--http-port", dest="http_port", type=int, help="port for HTTP transports (default 8808)")
    p.add_argument("--preview-port", type=int, help="enable the HTTP side channel (MJPEG/audio/events) on this port")
    p.add_argument("--language", help="TTS / STT language name, e.g. English (default)")
    p.add_argument("--stt", dest="stt_backend", choices=["faster-whisper", "openai", "none"], help="speech-to-text backend")
    p.add_argument("--stt-model", help="faster-whisper size (tiny/base/small/medium) or API model name")
    p.add_argument("--stt-language", help="ISO code for STT, e.g. en, de, fr; empty = auto")
    p.add_argument("--stt-device", help="faster-whisper device: auto, cpu, cuda")
    p.add_argument("--no-audio", dest="audio_enabled", action="store_false", default=None, help="disable microphone stream")
    p.add_argument("--no-camera", dest="camera_enabled", action="store_false", default=None, help="disable cameras")
    p.add_argument("--no-perception", dest="perception_enabled", action="store_false", default=None)
    p.add_argument("--no-reflexes", dest="reflexes_enabled", action="store_false", default=None)
    p.add_argument("--reflexes-off", help="comma separated reflex names to start disabled")
    p.add_argument("--barge-in", dest="barge_in", action="store_true", default=None, help="let people interrupt Pepper")
    p.add_argument("--hand-raise-vision", action="store_true", default=None, help="laptop MediaPipe hand-raise detector")
    p.add_argument("--liveliness", choices=["off", "calm", "lively"], help="autonomous movement preset at start")
    p.add_argument("--keep-autonomous-life", dest="autonomous_life", action="store_const", const="keep",
                   help="do not disable NAOqi autonomous life at start")
    p.add_argument("--camera-fps", type=int)
    p.add_argument("--camera-res", dest="camera_resolution", choices=["qqvga", "qvga", "vga"])
    p.add_argument("--audio-rate", type=int, choices=[16000, 48000])
    p.add_argument("--log-level", help="DEBUG, INFO, WARNING")
    p.add_argument("--version", action="version", version=f"pepper-mcp {__version__}")
    return p


def settings_from_args(argv: list[str] | None = None) -> Settings:
    args = build_parser().parse_args(argv)
    settings = Settings()
    settings.apply_overrides({k: v for k, v in vars(args).items()})
    settings.validate()
    return settings


def configure_logging(level: str, transport: str) -> None:
    # stdio transport uses stdout for the protocol: everything else must go to stderr.
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", stream=sys.stderr)
    logging.getLogger("mcp").setLevel(logging.WARNING)
    _ = transport


def main(argv: list[str] | None = None) -> None:
    settings = settings_from_args(argv)
    configure_logging(settings.log_level, settings.transport)
    from pepper_mcp.server import create_server

    mcp = create_server(settings)
    if settings.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport=settings.transport, host=settings.http_host, port=settings.http_port)


if __name__ == "__main__":
    main()
