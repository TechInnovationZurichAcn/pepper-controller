"""Robot / system tools."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from pepper_mcp.tools._common import ACTION, READ_ONLY, require, task_result, tool


def register(mcp, rt) -> None:  # type: ignore[no-untyped-def]
    @tool(mcp, READ_ONLY)
    def robot_status() -> dict[str, Any]:
        """Overall robot state: connection, battery, posture, autonomous life, speaking/listening
        flags, people around, active reflexes, running tasks. Call once at the start."""
        return rt.status()

    @tool(mcp, READ_ONLY)
    def robot_capabilities() -> dict[str, Any]:
        """What this robot/server can do: NAOqi services present, camera/microphone/STT state,
        gestures, reflexes, settings."""
        return rt.capabilities()

    @tool(mcp, READ_ONLY)
    def list_services() -> dict[str, Any]:
        """Names of all NAOqi services running on the robot."""
        names = rt.conn.service_names()
        return {"count": len(names), "services": names}

    @tool(mcp, ACTION)
    def wake_up() -> dict[str, Any]:
        """Wake the robot up (motors on) and go to the StandInit posture."""
        rt.motion.prepare("StandInit")
        return {"awake": rt.motion.is_awake(), "posture": rt.motion.current_posture()}

    @tool(mcp, ACTION)
    def rest() -> dict[str, Any]:
        """Put the robot to rest (motors off, safe crouched pose). Use when done."""
        rt.stop_everything()
        rt.motion.rest()
        return {"awake": rt.motion.is_awake()}

    @tool(mcp, READ_ONLY)
    def get_battery() -> dict[str, Any]:
        """Battery charge percentage and charging flag."""
        return require(rt.perception, "perception").battery()

    @tool(mcp, ACTION)
    def set_autonomous_life(state: Literal["disabled", "solitary", "interactive", "safeguard"]) -> dict[str, Any]:
        """Set NAOqi autonomous life. 'disabled' = you fully control the body (default while this
        server runs); 'solitary'/'interactive' = Pepper's built-in idle behaviours take over."""
        return {"state": rt.awareness.set_life_state(state)}

    @tool(mcp, ACTION)
    def set_liveliness(preset: Literal["off", "calm", "lively"],
                       basic_awareness: bool | None = None, background_movement: bool | None = None,
                       listening_movement: bool | None = None, speaking_movement: bool | None = None,
                       blinking: bool | None = None) -> dict[str, Any]:
        """Choose how much Pepper moves on its own. off: nothing autonomous (you own every joint).
        calm: breathing, blinking, gestures while speaking. lively: also looks at people and
        sounds by itself (may fight your explicit head commands)."""
        overrides = {k: v for k, v in {"BasicAwareness": basic_awareness, "BackgroundMovement": background_movement,
                                       "ListeningMovement": listening_movement, "SpeakingMovement": speaking_movement,
                                       "AutonomousBlinking": blinking}.items() if v is not None}
        return rt.awareness.apply_preset(preset, overrides)

    @tool(mcp, ACTION)
    def set_autonomous_ability(
        ability: Literal["BasicAwareness", "BackgroundMovement", "ListeningMovement", "SpeakingMovement",
                         "AutonomousBlinking"], enabled: bool) -> dict[str, Any]:
        """Toggle one autonomous ability."""
        ok = rt.awareness.set_ability(ability, enabled)
        return {"ability": ability, "enabled": enabled, "applied": ok, "abilities": rt.awareness.abilities()}

    @tool(mcp, ACTION)
    def configure_awareness(engagement_mode: Literal["Unengaged", "SemiEngaged", "FullyEngaged"] | None = None,
                            tracking_mode: Literal["Head", "BodyRotation", "WholeBody", "MoveContextually"] | None = None,
                            engage_person_id: int | None = None) -> dict[str, Any]:
        """Tune BasicAwareness (only matters when it is enabled): engagement mode, how the body
        follows people, or lock onto one person id."""
        out = rt.awareness.configure_awareness(engagement_mode, tracking_mode)
        if engage_person_id is not None:
            out["engaged"] = rt.awareness.engage_person(engage_person_id)
        return out

    @tool(mcp, READ_ONLY)
    def list_behaviors(filter: str | None = None, include_animations: bool = False) -> dict[str, Any]:
        """Installed Choregraphe behaviours/apps (not the animation library)."""
        return rt.system.list_behaviors(filter, include_animations)

    @tool(mcp, ACTION)
    def run_behavior(name: Annotated[str, Field(description="behavior name from list_behaviors")],
                     wait: bool = False) -> dict[str, Any]:
        """Run an installed behaviour (e.g. a Choregraphe app)."""
        fut = rt.system.run_behavior_async(name)
        return task_result(rt.tasks.from_future("behavior", name, fut), wait, 300)

    @tool(mcp, ACTION)
    def stop_behavior(name: str | None = None) -> dict[str, Any]:
        """Stop one behaviour, or all running behaviours when no name is given."""
        if name:
            rt.system.stop_behavior(name)
        else:
            rt.system.stop_all_behaviors()
        return {"stopped": name or "all"}

    @tool(mcp, READ_ONLY)
    def get_volume() -> dict[str, int]:
        """Speaker volume 0-100."""
        return {"volume": require(rt.audio_out, "audio").get_volume()}

    @tool(mcp, ACTION)
    def set_volume(level: Annotated[int, Field(ge=0, le=100)]) -> dict[str, int]:
        """Set speaker volume 0-100 (affects speech and sounds)."""
        return {"volume": require(rt.audio_out, "audio").set_volume(level)}

    @tool(mcp, ACTION)
    def stop_motion() -> dict[str, Any]:
        """EMERGENCY STOP: halt the base, animations, gestures, tracking, navigation and speech."""
        return rt.stop_everything()
