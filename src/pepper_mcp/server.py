"""Builds the MCP server: lifespan owns the Runtime, tool modules register themselves,
resources expose the live state, prompts teach the LLM how to be a good robot."""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from mcp.server.mcpserver import MCPServer

from pepper_mcp import __version__
from pepper_mcp.config import Settings
from pepper_mcp.runtime import Runtime
from pepper_mcp.tools import register_all

log = logging.getLogger(__name__)

INSTRUCTIONS = """Tools of a SoftBank Pepper robot, driven by Gemini Live (pepper-mcp-gemini), which hears
Pepper's microphone and speaks through its loudspeakers itself. Move its body (move, look,
play_gesture, play_animation), light it up (set_leds), use its tablet and tic-tac-toe, and sense
people, touch and sound. Long actions run as background tasks that report task.finished.
Reflexes (high five, greeting wave, attention) run automatically and report reflex.* events.
Always keep people safe: small moves, stop_motion if unsure."""


def create_server(settings: Settings, runtime: Runtime | None = None) -> MCPServer:
    rt = runtime or Runtime(settings)

    @contextlib.asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[dict[str, Any]]:
        import anyio

        await anyio.to_thread.run_sync(rt.start)
        try:
            yield {"runtime": rt}
        finally:
            await anyio.to_thread.run_sync(rt.stop)

    mcp = MCPServer("pepper", title="Pepper robot", instructions=INSTRUCTIONS, version=__version__,
                    lifespan=lifespan)
    mcp.runtime = rt  # type: ignore[attr-defined]
    modules = register_all(mcp, rt)
    log.info("registered tool modules: %s", ", ".join(modules))
    _register_resources(mcp, rt)
    return mcp


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------

def _dump(data: Any) -> str:
    return json.dumps(data, default=str, indent=1)


def _register_resources(mcp: MCPServer, rt: Runtime) -> None:
    @mcp.resource("pepper://status", mime_type="application/json", description="live robot status")
    def status() -> str:
        return _dump(rt.status())

    @mcp.resource("pepper://capabilities", mime_type="application/json",
                  description="services, features and settings of this robot/server")
    def capabilities() -> str:
        return _dump(rt.capabilities())

    @mcp.resource("pepper://people", mime_type="application/json", description="people currently perceived")
    def people() -> str:
        return _dump(rt.perception.people() if rt.perception else [])

    @mcp.resource("pepper://events", mime_type="application/json", description="the last 100 events")
    def events() -> str:
        return _dump([e.to_dict() for e in rt.bus.recent(0, None, 100)])

    @mcp.resource("pepper://events/types", mime_type="application/json", description="event catalogue")
    def event_types() -> str:
        from pepper_mcp.tools.events import EVENT_CATALOGUE

        return _dump(EVENT_CATALOGUE)

    @mcp.resource("pepper://transcript", mime_type="application/json",
                  description="the conversation as the voice agent heard and spoke it, plus robot TTS")
    def transcript() -> str:
        said = [{"role": "pepper", **u} for u in rt.speech.history(50)]
        return _dump(sorted(list(rt.transcript) + said, key=lambda x: x.get("at", 0)))

    @mcp.resource("pepper://gestures", mime_type="application/json", description="gesture registry")
    def gestures() -> str:
        return _dump(rt.gestures.list())

    @mcp.resource("pepper://animations", mime_type="application/json", description="installed stock animations")
    def animations() -> str:
        return _dump(rt.animations.list())

    @mcp.resource("pepper://reflexes", mime_type="application/json", description="reflex state")
    def reflexes() -> str:
        return _dump(rt.reflexes.list() if rt.reflexes else [])

    @mcp.resource("pepper://camera/{camera}/latest.jpg", mime_type="image/jpeg",
                  description="latest JPEG frame from camera top|bottom|depth")
    def camera_frame(camera: str) -> bytes:
        if rt.cameras is None:
            raise ValueError("camera disabled")
        data, _frame = rt.cameras.snapshot(camera, 640)
        return data
