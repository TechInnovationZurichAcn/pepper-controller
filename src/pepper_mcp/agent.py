"""``pepper-mcp-agent``: reference agent loop that makes Pepper alive with Claude.

It starts the MCP server in-process, hands its tools to Claude and runs the event loop
the ``pepper_live_agent`` prompt describes:

    wait_for_event → Claude (streamed) → `say` per finished sentence while still generating
    → execute the remaining tool calls → wait_for_event …

Streaming + sentence splitting is what makes the conversation feel immediate: Pepper
starts talking after the first sentence is generated, not after the whole answer.

Requires ``pip install 'pepper-mcp[agent]'`` and ``ANTHROPIC_API_KEY`` (or ``ant auth login``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
import time
from typing import Any

from pepper_mcp.cli import build_parser, configure_logging
from pepper_mcp.config import Settings

log = logging.getLogger("pepper_mcp.agent")

MODEL = "claude-opus-5-5"
SENTENCE_END = re.compile(r"(?<=[.!?…])\s+|(?<=[.!?…])$")
MAX_HISTORY = 40


def _parse(argv: list[str] | None) -> tuple[Settings, argparse.Namespace]:
    parser = build_parser()
    parser.prog = "pepper-mcp-agent"
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--persona", default="Pepper")
    parser.add_argument("--effort", default="low", choices=["low", "medium", "high"])
    parser.add_argument("--no-fallbacks", action="store_true", help="disable server-side refusal fallbacks")
    parser.add_argument("--idle-timeout", type=float, default=45.0, help="seconds to wait for events per loop")
    args = parser.parse_args(argv)
    settings = Settings()
    settings.apply_overrides({k: v for k, v in vars(args).items()})
    settings.transport = "stdio"
    settings.validate()
    return settings, args


async def run(settings: Settings, args: argparse.Namespace) -> None:
    import anthropic
    from mcp.client import Client

    from pepper_mcp.server import create_server

    server = create_server(settings)
    client_llm = anthropic.AsyncAnthropic()

    async with Client(server) as mcp:
        tools_result = await mcp.list_tools()
        tools = [{"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
                 for t in tools_result.tools]
        prompt = await mcp.get_prompt("pepper_live_agent", {"persona_name": args.persona, "language": settings.language})
        system = "\n".join(m.content.text for m in prompt.messages if getattr(m.content, "type", "") == "text")
        system += ("\n\nYou are running inside an automatic loop: every turn starts with the newest events as the user "
                   "message. Use tools freely; when there is nothing to do, reply with a short note and the loop will "
                   "wait for the next event.")
        history: list[dict[str, Any]] = []
        status = await mcp.call_tool("robot_status", {})
        last_seq = 0
        log.info("agent ready: %d tools", len(tools))
        history.append({"role": "user", "content": f"Robot status at start: {_text(status)}. Greet whoever is around briefly."})

        async def speak(text: str) -> None:
            if text.strip():
                await mcp.call_tool("say", {"text": text.strip(), "animated": True})

        while True:
            response = await _turn(client_llm, args, system, tools, history, speak)
            history.append({"role": "assistant", "content": response.content})
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if tool_uses:
                results = []
                for tu in tool_uses:
                    try:
                        res = await mcp.call_tool(tu.name, dict(tu.input))
                        content = _tool_content(res)
                    except Exception as exc:
                        content = [{"type": "text", "text": f"error: {exc}"}]
                    results.append({"type": "tool_result", "tool_use_id": tu.id, "content": content})
                history.append({"role": "user", "content": results})
                continue
            # No tool calls: wait for the world to change.
            ev = await mcp.call_tool("wait_for_event", {"timeout_s": args.idle_timeout, "since_seq": last_seq})
            payload = _json(ev)
            last_seq = payload.get("last_seq", last_seq) or last_seq
            if payload.get("timeout"):
                history.append({"role": "user", "content": f"[{time.strftime('%H:%M:%S')}] Nothing happened for "
                                                           f"{args.idle_timeout:.0f}s. Do something small and alive, or just wait."})
            else:
                history.append({"role": "user", "content": f"[{time.strftime('%H:%M:%S')}] Events: {json.dumps(payload['events'], default=str)}"})
            if len(history) > MAX_HISTORY:
                history[:] = _trim(history)


async def _turn(client: Any, args: argparse.Namespace, system: str, tools: list[dict[str, Any]],
                history: list[dict[str, Any]], speak: Any) -> Any:
    """Stream one assistant turn; speak sentences of the text as they complete."""
    kwargs: dict[str, Any] = dict(model=args.model, max_tokens=1200, system=system, tools=tools, messages=history,
                                  output_config={"effort": args.effort})
    spoken_upto = 0
    buffer = ""
    if not args.no_fallbacks:
        kwargs["betas"] = ["server-side-fallback-2026-07-01"]
        kwargs["fallbacks"] = "default"
        stream_cm = client.beta.messages.stream(**kwargs)
    else:
        stream_cm = client.messages.stream(**kwargs)
    async with stream_cm as stream:
        async for event in stream:
            if event.type == "text":
                buffer += event.text
                sentences = SENTENCE_END.split(buffer)
                if len(sentences) > 1:
                    for sentence in sentences[:-1]:
                        await speak(sentence)
                        spoken_upto += len(sentence)
                    buffer = sentences[-1]
        message = await stream.get_final_message()
    if buffer.strip():
        await speak(buffer)
    if message.stop_reason == "refusal":
        log.warning("model refused: %s", getattr(message, "stop_details", None))
    return message


def _text(res: Any) -> str:
    return " ".join(getattr(c, "text", "") for c in res.content if getattr(c, "type", "") == "text")[:2000]


def _json(res: Any) -> dict[str, Any]:
    sc = getattr(res, "structuredContent", None) or getattr(res, "structured_content", None)
    if isinstance(sc, dict):
        return sc
    try:
        return json.loads(_text(res))
    except Exception:
        return {"text": _text(res)}


def _tool_content(res: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in res.content:
        if c.type == "text":
            out.append({"type": "text", "text": c.text[:6000]})
        elif c.type == "image":
            out.append({"type": "image", "source": {"type": "base64", "media_type": c.mime_type, "data": c.data}})
        elif c.type == "audio":
            out.append({"type": "text", "text": "(audio clip recorded; audio input not forwarded)"})
    return out or [{"type": "text", "text": "ok"}]


def _trim(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the first message and the tail, cutting at a user turn so tool_use/result pairs stay intact."""
    tail = history[-(MAX_HISTORY // 2):]
    while tail and not (tail[0]["role"] == "user" and isinstance(tail[0]["content"], str)):
        tail.pop(0)
    return [history[0], *tail] if tail else history[-2:]


def main(argv: list[str] | None = None) -> None:
    settings, args = _parse(argv)
    configure_logging(settings.log_level, "stdio")
    try:
        asyncio.run(run(settings, args))
    except KeyboardInterrupt:
        print("bye", file=sys.stderr)


if __name__ == "__main__":
    main()
