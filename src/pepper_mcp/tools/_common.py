"""Shared helpers for tool modules."""

from __future__ import annotations

import functools
import inspect
import json
import logging
from collections.abc import Callable
from typing import Any

from mcp import types
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from pepper_mcp.robot.connection import RobotError, ServiceUnavailable

log = logging.getLogger(__name__)

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
ACTION = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
MOVES_ROBOT = ToolAnnotations(title=None, read_only_hint=False, destructive_hint=True, idempotent_hint=False,
                              open_world_hint=False)


def guard(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Convert robot failures into ToolError so the LLM sees a clear message."""
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def _async(*args: Any, **kwargs: Any) -> Any:
            try:
                return await fn(*args, **kwargs)
            except ToolError:
                raise
            except ServiceUnavailable as exc:
                raise ToolError(f"feature unavailable on this robot: {exc}") from exc
            except (RobotError, KeyError, ValueError, TimeoutError) as exc:
                raise ToolError(str(exc)) from exc
            except Exception as exc:  # pragma: no cover - last resort
                log.exception("tool %s failed", fn.__name__)
                raise ToolError(f"{type(exc).__name__}: {exc}") from exc

        return _async

    @functools.wraps(fn)
    def _sync(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except ServiceUnavailable as exc:
            raise ToolError(f"feature unavailable on this robot: {exc}") from exc
        except (RobotError, KeyError, ValueError, TimeoutError) as exc:
            raise ToolError(str(exc)) from exc
        except Exception as exc:  # pragma: no cover
            log.exception("tool %s failed", fn.__name__)
            raise ToolError(f"{type(exc).__name__}: {exc}") from exc

    return _sync


def tool(mcp: MCPServer, annotations: ToolAnnotations = ACTION, name: str | None = None,
         description: str | None = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """``@tool(mcp)`` = ``@mcp.tool()`` + error guard."""

    def _decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        wrapped = guard(fn)
        mcp.tool(name=name or fn.__name__, description=description, annotations=annotations)(wrapped)
        return fn

    return _decorate


def require(obj: Any, what: str) -> Any:
    if obj is None:
        raise ToolError(f"{what} is not available (service missing on this robot or disabled by settings)")
    return obj


def text_block(data: Any) -> types.TextContent:
    return types.TextContent(type="text", text=json.dumps(data, default=str))


def task_result(task: Any, wait: bool, timeout_s: float = 60.0) -> dict[str, Any]:
    """Standard return for tools that start a tracked task."""
    if wait:
        task.wait(timeout_s)
    out = task.to_dict()
    if not wait:
        out["hint"] = "runs in the background; a task.finished event is published when done"
    return out
