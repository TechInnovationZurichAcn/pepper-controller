"""MCP tool modules. Every module in this package exposes ``register(mcp, rt)``; they are
discovered automatically so adding a domain is just adding a file."""

from __future__ import annotations

import importlib
import logging
import pkgutil

log = logging.getLogger(__name__)


def register_all(mcp, rt) -> list[str]:  # type: ignore[no-untyped-def]
    registered = []
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{info.name}")
        register = getattr(module, "register", None)
        if register is None:
            continue
        register(mcp, rt)
        registered.append(info.name)
    return registered
