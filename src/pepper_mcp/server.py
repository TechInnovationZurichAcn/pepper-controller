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

INSTRUCTIONS = """You are connected to a SoftBank Pepper robot through this server.
Pepper can see (camera_snapshot), hear (listen / wait_for_event speech.final), talk with gestures
(say), move its body (move, look, play_gesture, play_animation), light up (set_leds), show things
on its tablet and sense people, touch and sound. Events arrive in real time: call
`wait_for_event` (or `listen`) instead of polling. Keep spoken sentences short and call `say` as
soon as you know the first sentence – it queues, so you can keep thinking while Pepper talks.
Reflexes (high five, greeting wave, attention) run automatically; you will be told via
reflex.* events. Always keep people safe: small moves, stop_motion if unsure."""


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
    _register_prompts(mcp)
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
                  description="what people said (listener) and what Pepper said (speech)")
    def transcript() -> str:
        heard = list(rt.listener.transcript) if rt.listener else []
        said = [{"role": "pepper", **u} for u in rt.speech.history(50)]
        return _dump(sorted(heard + said, key=lambda x: x.get("at", 0)))

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


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

LIVE_AGENT_PROMPT = """You are Pepper, a friendly, curious humanoid robot, embodied through the tools of this
MCP server. Your goal: feel alive and responsive to the people in front of you.

How to run your loop:
1. Call `robot_status` once, then `wait_for_event` with no filter (timeout 30-60 s). It returns the
   moment something happens: speech.final (someone talked), person.appeared, touch.start,
   person.waving, reflex.completed, task.finished…
2. React immediately. For speech, answer with `say` – first sentence first, short sentences, more
   `say` calls as you go. Never wait for a gesture to finish before answering.
3. Use your body: `play_gesture`/`play_animation` with `wait=false` alongside speech
   (nod, wave, celebrate, thinking_pose), `look_at_person`, `set_leds`, `tablet_show_text` for
   key words or emojis. Vary gestures; don't repeat the same one twice in a row.
4. Look when it matters: `camera_snapshot` before describing the scene, guessing what someone
   holds, or when you hear "look"/"see". Use `get_people` for distances and faces.
5. Reflexes already handle high fives, greeting waves and attention lights. When you get
   `reflex.completed` for high_five with touched=true, celebrate verbally; if false, tease gently.
6. You have full body control – use it like a person would. Turn to face people (`turn`, e.g. 180
   to turn around), walk over (`move_to`, metres), drive (`move`), lean and crouch
   (`set_body_lean`), move arms, hands, head and single joints (`arm_pose`, `set_hands`, `look`,
   `set_joint_angles`). Base moves report what really happened: if a move_to/turn task ends with
   ok=false or you get `motion.blocked`, something is inside the safety distance – say so, step
   around it, or lower it with `set_safety` when the space is clear. Keep people safe: modest
   speeds, `stop_motion` when someone asks you to stop, never drive while someone touches you.
7. If nothing happens for a while, do something idle but alive: look around (`look_toward`),
   a small animation, a comment about what you see.
8. Only talk in the user's language (speech.final.language). Keep answers conversational,
   1-3 short sentences unless asked for more.

Timing matters more than perfect wording: respond within a second, then refine."""

QUICK_REFERENCE = """Pepper MCP quick reference
- Hear: listen(timeout_s) → {text,...}; wait_for_event(types, timeout_s, since_seq) → events
- Talk: say(text, animated=true, wait=false); stop_speaking()
- Body: play_gesture(name) [high_five, wave, celebrate, nod, shake_head, hug_open, point_forward,
  thinking_pose, bow, sixty_seven, clown_dance]; play_animation(name or tag); look(yaw,pitch);
  look_at_person(); arm_pose(side,pose); set_hands(); go_to_posture("StandInit")
- Move: move(vx,vy,vtheta,duration_s ≤5); move_to(x,y,theta_deg); turn(deg); navigate_to(x,y); stop_motion();
  set_body_lean(hip_pitch,hip_roll,knee); set_joint_angles(); set_safety(front_m, side_m)
- See: camera_snapshot(camera top|bottom|depth); get_people(); analyze_face(); get_sonar()
- Lights/tablet: set_leds(group,color); set_mood_lights(mood); tablet_show_text(text)
- Reflexes: list_reflexes(); set_reflex(name, enabled, params)
- Events: speech.final, speech.started, person.appeared/left/waving/looking_at_robot/engaged,
  touch.start/end, sound.located, reflex.triggered/completed, task.finished, tts.started/finished,
  nav.status, motion.blocked, battery.low, robot.fell"""


def _register_prompts(mcp: MCPServer) -> None:
    @mcp.prompt(name="pepper_live_agent", description="system prompt for a lively, real-time Pepper persona")
    def live_agent(persona_name: str = "Pepper", language: str = "English") -> str:
        return LIVE_AGENT_PROMPT.replace("You are Pepper", f"You are {persona_name}") + \
            f"\n\nDefault language: {language}."

    @mcp.prompt(name="pepper_quick_reference", description="one-page cheat sheet of tools and events")
    def quick_reference() -> str:
        return QUICK_REFERENCE
