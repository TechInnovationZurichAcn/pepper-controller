"""ALSystem / ALBehaviorManager helpers."""

from __future__ import annotations

import logging
from typing import Any

from pepper_mcp.robot.connection import RobotConnection, RobotError

log = logging.getLogger(__name__)


class System:
    def __init__(self, conn: RobotConnection):
        self.conn = conn
        self._info: dict[str, Any] | None = None

    def info(self) -> dict[str, Any]:
        if self._info is None:
            info: dict[str, Any] = {}
            sys_svc = self.conn.try_service("ALSystem")
            if sys_svc is not None:
                for key, fn in (("robot_name", "robotName"), ("naoqi_version", "systemVersion"),
                                ("timezone", "timezone")):
                    try:
                        info[key] = getattr(sys_svc, fn)()
                    except Exception:
                        pass
            self._info = info
        return dict(self._info)

    # ------------------------------------------------------------- behaviours
    @property
    def behaviors(self) -> Any:
        return self.conn.service("ALBehaviorManager")

    def list_behaviors(self, filter_: str | None = None, include_animations: bool = False) -> dict[str, Any]:
        bm = self.behaviors
        installed = list(bm.getInstalledBehaviors())
        running = set(bm.getRunningBehaviors())
        try:
            defaults = set(bm.getDefaultBehaviors())
        except Exception:
            defaults = set()
        items = []
        for name in installed:
            if not include_animations and "animations/" in name:
                continue
            if filter_ and filter_.lower() not in name.lower():
                continue
            items.append({"name": name, "running": name in running, "runs_at_boot": name in defaults})
        return {"behaviors": items, "count": len(items)}

    def run_behavior_async(self, name: str) -> Any:
        bm = self.behaviors
        if not bm.isBehaviorInstalled(name):
            raise RobotError(f"behavior not installed: {name}")
        return RobotConnection.call_async(bm, "runBehavior", name)

    def stop_behavior(self, name: str) -> None:
        self.behaviors.stopBehavior(name)

    def stop_all_behaviors(self) -> None:
        try:
            self.behaviors.stopAllBehaviors()
        except Exception as exc:
            log.debug("stopAllBehaviors: %s", exc)
