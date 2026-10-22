"""Reflexes: sub-second reactions that run without an LLM round-trip.

Add a reflex by subclassing :class:`pepper_mcp.reflexes.base.Reflex` and listing it in
:func:`builtin_reflexes`.
"""

from __future__ import annotations

from pepper_mcp.reflexes.attention import AttentionReflex
from pepper_mcp.reflexes.base import Reflex, ReflexManager
from pepper_mcp.reflexes.battery import LowBatteryReflex
from pepper_mcp.reflexes.greet import GreetReflex
from pepper_mcp.reflexes.high_five import HighFiveReflex
from pepper_mcp.reflexes.touch import TouchReflex


def builtin_reflexes(rt) -> list[Reflex]:  # type: ignore[no-untyped-def]
    return [AttentionReflex(rt), HighFiveReflex(rt), GreetReflex(rt), TouchReflex(rt), LowBatteryReflex(rt)]


__all__ = ["Reflex", "ReflexManager", "builtin_reflexes"]
