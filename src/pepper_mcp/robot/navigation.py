"""ALNavigation: local obstacle-avoiding moves, exploration/mapping, remembered spots.

Port of the Navigator validated on the robot (multi-map store, explore → save →
localize, navigateToInMap). Speech feedback was removed – the server publishes
``nav.status`` events and the LLM decides what to say.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

from pepper_mcp.robot.connection import RobotConnection, RobotError
from pepper_mcp.robot.events import EventBus

log = logging.getLogger(__name__)


class Navigator:
    RADIUS_CHOICES_M = [3.0, 5.0, 8.0, 12.0]
    NAV_OK = 0

    def __init__(self, conn: RobotConnection, bus: EventBus, store_path: str = "pepper_maps.json"):
        self.conn = conn
        self.bus = bus
        self.store_path = store_path
        self.maps: dict[str, dict[str, Any]] = {}
        self.current: str | None = None
        self.localized = False
        self.busy = False
        self.status_text = "idle"
        self._lock = threading.Lock()
        self._load_store()

    @property
    def nav(self) -> Any:
        return self.conn.service("ALNavigation")

    def available(self) -> bool:
        return self.conn.has_service("ALNavigation")

    # ------------------------------------------------------------- store
    def _load_store(self) -> None:
        try:
            with open(self.store_path) as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        maps = data.get("maps") or {}
        self.maps = {name: {"map_path": rec.get("map_path"),
                            "spots": {k: list(v) for k, v in (rec.get("spots") or {}).items()}}
                     for name, rec in maps.items()}
        self.current = data.get("current") if data.get("current") in self.maps else next(iter(self.maps), None)

    def _save_store(self) -> None:
        try:
            with open(self.store_path, "w") as fh:
                json.dump({"current": self.current, "maps": self.maps}, fh, indent=2)
        except OSError as exc:
            log.warning("could not save map store: %s", exc)

    def _record(self) -> dict[str, Any]:
        if self.current is None or self.current not in self.maps:
            self.new_map()
        return self.maps[self.current]  # type: ignore[index]

    # ------------------------------------------------------------- status
    def _set_status(self, text: str, busy: bool | None = None) -> None:
        self.status_text = text
        if busy is not None:
            self.busy = busy
        self.bus.publish("nav.status", self.status(), source="navigation")

    def status(self) -> dict[str, Any]:
        return {"busy": self.busy, "status": self.status_text, "current_map": self.current,
                "localized": self.localized, "maps": sorted(self.maps),
                "spots": sorted(self._record()["spots"]) if self.current else []}

    # ------------------------------------------------------------- maps
    def new_map(self, name: str | None = None) -> str:
        if not name:
            i = len(self.maps) + 1
            name = f"map {i}"
            while name in self.maps:
                i += 1
                name = f"map {i}"
        self.maps.setdefault(name, {"map_path": None, "spots": {}})
        self.current = name
        self.localized = False
        self._save_store()
        return name

    def switch_map(self, name: str) -> dict[str, Any]:
        if name not in self.maps:
            raise RobotError(f"no map named {name!r}; maps: {sorted(self.maps)}")
        self.current = name
        self.localized = False
        self._save_store()
        self.load_map()
        return self.status()

    def load_map(self) -> bool:
        path = self._record().get("map_path")
        if not path:
            raise RobotError("the current map has not been explored/saved yet")
        self.nav.loadExploration(path)
        return self._start_localization(relocalize=True)

    def _start_localization(self, relocalize: bool = False) -> bool:
        try:
            if relocalize:
                self.nav.relocalizeInMap([0.0, 0.0, 0.0])
            self.nav.startLocalization()
            self.localized = True
            return True
        except Exception as exc:
            log.warning("startLocalization failed: %s", exc)
            self.localized = False
            return False

    # ------------------------------------------------------------- actions (blocking; run as tasks)
    def explore(self, radius_m: float) -> dict[str, Any]:
        if self.busy:
            raise RobotError(f"navigation is busy ({self.status_text})")
        radius_m = max(1.0, min(15.0, float(radius_m)))
        self._set_status(f"exploring {radius_m:.0f} m", busy=True)
        try:
            fn = getattr(self.nav, "explore", None) or self.nav.exploreArea
            fn(radius_m)
            try:
                self.nav.stopExploration()
            except Exception:
                pass
            self._set_status("saving map")
            path = self.nav.saveExploration()
            self._record()["map_path"] = path
            self._save_store()
            self._start_localization()
            self._set_status("mapped", busy=False)
            return {"map": self.current, "map_path": path, "localized": self.localized}
        except Exception as exc:
            self._set_status(f"explore failed: {exc}", busy=False)
            raise RobotError(f"explore failed: {exc}") from exc

    def navigate_to(self, x: float, y: float) -> dict[str, Any]:
        """Local obstacle-avoiding move in the robot frame (no map needed)."""
        if self.busy:
            raise RobotError(f"navigation is busy ({self.status_text})")
        self._set_status(f"navigating to ({x:.1f}, {y:.1f})", busy=True)
        try:
            result = self.nav.navigateTo(float(x), float(y))
            ok = bool(result) if result is not None else True
            self._set_status("arrived" if ok else "blocked", busy=False)
            return {"arrived": ok}
        except Exception as exc:
            self._set_status(f"navigate failed: {exc}", busy=False)
            raise RobotError(f"navigateTo failed: {exc}") from exc

    def remember_spot(self, name: str | None = None) -> dict[str, Any]:
        if not self.localized:
            raise RobotError("not localized – explore_area or load_map first")
        pose = self._current_pose()
        if pose is None:
            raise RobotError("cannot read the robot position in the map")
        rec = self._record()
        name = name or f"spot {len(rec['spots']) + 1}"
        rec["spots"][name] = pose
        self._save_store()
        return {"name": name, "pose": pose, "map": self.current}

    def forget_spot(self, name: str | None = None) -> list[str]:
        rec = self._record()
        if name is None:
            rec["spots"] = {}
        else:
            rec["spots"].pop(name, None)
        self._save_store()
        return sorted(rec["spots"])

    def go_to_spot(self, name: str) -> dict[str, Any]:
        rec = self._record()
        pose = rec["spots"].get(name)
        if pose is None:
            raise RobotError(f"no spot named {name!r}; spots: {sorted(rec['spots'])}")
        if self.busy:
            raise RobotError(f"navigation is busy ({self.status_text})")
        if not self.localized:
            self.load_map()
        self._set_status(f"going to {name}", busy=True)
        try:
            result = self.nav.navigateToInMap(list(pose))
            ok = (result == self.NAV_OK) if isinstance(result, int) else True
            self._set_status("arrived" if ok else "blocked", busy=False)
            return {"spot": name, "arrived": ok}
        except Exception as exc:
            self._set_status(f"navigation failed: {exc}", busy=False)
            raise RobotError(f"navigateToInMap failed: {exc}") from exc

    def _current_pose(self) -> list[float] | None:
        try:
            res = self.nav.getRobotPositionInMap()
        except Exception as exc:
            log.warning("getRobotPositionInMap: %s", exc)
            return None
        pose = list(res[0]) if res and isinstance(res[0], (list, tuple)) else list(res or [])
        return [float(v) for v in pose[:3]] if len(pose) >= 3 else None

    def stop(self) -> None:
        for fn_name in ("stopExploration", "stopNavigateTo", "stopLocalization"):
            fn = getattr(self.conn.try_service("ALNavigation"), fn_name, None)
            if fn is not None:
                try:
                    fn()
                except Exception:
                    pass
        if self.busy:
            self._set_status("stopped", busy=False)
