"""Real-time events and background tasks – the heart of the interactive loop."""

from __future__ import annotations

import time
from typing import Annotated, Any

from pydantic import Field

from pepper_mcp.tools._common import ACTION, READ_ONLY, tool

EVENT_CATALOGUE: dict[str, str] = {
    "speech.started": "a person started talking {at}",
    "speech.final": "transcript of what a person said {text, language, confidence, duration_s, stt_latency_ms}",
    "speech.aborted": "speech segment dropped {reason, started_at}",
    "speech.barge_in": "a person talked over Pepper and TTS was stopped",
    "speech.keyword": "recognize_keywords heard a word {word, confidence}",
    "tts.started": "Pepper started saying {utterance_id, text}",
    "tts.finished": "Pepper finished saying {utterance_id, text, duration_s, error}",
    "person.appeared": "new person perceived {person_id, distance_m, bearing_deg, position_m, face_detected…}",
    "person.left": "person no longer perceived {person_id, last_distance_m}",
    "person.waving": "person waved / raised a hand {person_id, side}",
    "person.looking_at_robot": "gaze toward the robot changed {person_id, looking}",
    "person.engaged": "person entered the engagement zone / approached {person_id, zone, event}",
    "gesture.hand_raised": "laptop vision saw a raised hand {side, confidence}",
    "touch.start": "a tactile sensor was touched {sensor, key}",
    "touch.end": "a tactile sensor was released {sensor, key}",
    "sound.located": "a loud sound with direction {azimuth_deg (robot frame, left positive), head_azimuth_deg, elevation_deg, confidence, energy}",
    "reflex.triggered": "a reflex started reacting {reflex, trigger, person_id}",
    "reflex.completed": "a reflex finished {reflex, trigger, result} e.g. high_five result.touched",
    "task.finished": "a background action finished {task_id, kind, ok, error}",
    "tracking.changed": "follow/track state changed",
    "nav.status": "navigation status changed {status, busy, current_map, localized}",
    "robot.fell": "the robot is falling / has fallen",
    "motion.blocked": "NAOqi stopped a base move {reason} – usually an obstacle inside the safety distance",
    "robot.pushed": "the robot was pushed",
    "robot.hot_joint": "a joint is overheating {joints}",
    "robot.stopped": "stop_motion was executed",
    "battery.low": "battery below 15 % {percent}",
    "tablet.touched": "someone touched the tablet {x, y}",
    "server.started": "the server finished starting",
}


def register(mcp, rt) -> None:  # type: ignore[no-untyped-def]
    @tool(mcp, READ_ONLY)
    async def wait_for_event(types: Annotated[list[str] | None, Field(description="event types or prefixes with '*' (e.g. ['speech.final', 'person.*', 'touch.start']); omit for any event")] = None,
                             timeout_s: Annotated[float, Field(gt=0, le=600)] = 30.0,
                             since_seq: Annotated[int | None, Field(description="return events newer than this seq first (from a previous call) so nothing is missed")] = None,
                             ignore_internal: Annotated[bool, Field(description="skip tts.*, task.finished and speech.aborted noise")] = True
                             ) -> dict[str, Any]:
        """Block until something happens on the robot (someone speaks, appears, touches, waves…) and
        return the events. This is how you stay responsive: call it in a loop instead of polling.
        Returns {events: [], timeout: true} when nothing happened within timeout_s."""
        types_t = tuple(types) if types else None
        if types_t is None and ignore_internal:
            noise = {"tts.started", "tts.finished", "speech.aborted", "task.finished", "server.started"}
        else:
            noise = set()
        deadline = time.monotonic() + timeout_s
        seq = since_seq
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"events": [], "timeout": True, "last_seq": rt.bus.last_seq}
            events = await rt.bus.wait(types_t, remaining, seq)
            events = [e for e in events if e.type not in noise]
            if events:
                return {"events": [e.to_dict() for e in events], "last_seq": events[-1].seq, "timeout": False}
            seq = rt.bus.last_seq  # only noise arrived; keep waiting for newer events

    @tool(mcp, READ_ONLY)
    def get_events(since_seq: int = 0, types: list[str] | None = None,
                   limit: Annotated[int, Field(ge=1, le=200)] = 50) -> dict[str, Any]:
        """Recent events without waiting (history of the last 500)."""
        events = rt.bus.recent(since_seq, types, limit)
        return {"events": [e.to_dict() for e in events], "last_seq": rt.bus.last_seq}

    @tool(mcp, READ_ONLY)
    def list_event_types() -> dict[str, str]:
        """All event types with their payload description."""
        return EVENT_CATALOGUE

    @tool(mcp, READ_ONLY)
    def list_tasks(limit: Annotated[int, Field(ge=1, le=100)] = 20) -> dict[str, Any]:
        """Background tasks (animations, moves, navigation…) with their state."""
        return {"tasks": [t.to_dict() for t in rt.tasks.all(limit)]}

    @tool(mcp, READ_ONLY)
    async def wait_task(task_id: str, timeout_s: Annotated[float, Field(gt=0, le=600)] = 30.0) -> dict[str, Any]:
        """Wait until a background task finishes (or the timeout passes) and return its state."""
        task = rt.tasks.get(task_id)
        if task is None:
            raise KeyError(f"unknown task {task_id}")
        import anyio

        await anyio.to_thread.run_sync(task.wait, timeout_s)
        return task.to_dict()

    @tool(mcp, ACTION)
    def cancel_task(task_id: str) -> dict[str, Any]:
        """Cancel a running background task when possible."""
        return {"cancelled": rt.tasks.cancel(task_id)}
