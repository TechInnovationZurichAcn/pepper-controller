"""ALNavigation-based mapping and spot memory for the Pepper teleop menu.

The flow this exposes:

  1. Explore   -- Pepper drives itself around and builds a metrical map of the
                  space (ALNavigation.explore). When it finishes it auto-saves the
                  map on the robot and keeps localization running.
  2. Remember  -- record the robot's current pose in that map under a name
                  (getRobotPositionInMap) so you can come back to it later.
  3. Go to     -- drive back to a remembered pose (navigateToInMap), avoiding
                  obstacles on the way.

Multiple named maps are supported (one per room/area): explore/remember/go-to all
act on the *current* map, and you can switch between them (drive Pepper to that
room's start point, pick the map, it relocalizes). All maps + their named spots
are persisted to a small JSON file next to this script so they survive teleop
restarts; each map raster itself lives on the robot at the path saveExploration()
returns and is re-read with loadExploration(). The store auto-migrates from the
older single-map format.

Like the other feature modules, every NAOqi call is wrapped defensively: a
missing service or a failed call degrades gracefully (prints a WARN) instead of
crashing the teleop loop. While exploring or navigating, ALNavigation owns the
base, so the teleop loop checks `busy` and stops sending moveToward (exactly how
it already backs off for follow-me).
"""
from __future__ import print_function

import json
import os
import threading


def _say(tts, text):
    if tts is None:
        print("(no TTS) {}".format(text))
        return
    try:
        tts.say(text)
    except Exception as exc:
        print("WARN: TTS failed: {}".format(exc))


def _default_store():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "pepper_map.json")


class Navigator(object):
    """Wraps ALNavigation for explore -> remember spots -> return to spots.

    `busy` is True while ALNavigation is driving the base (exploring or heading to
    a spot) — the teleop loop reads it to stop fighting for the base. `status` is a
    short human-readable string for the HUD/console.
    """

    # explore(radius) caps how far from the start Pepper wanders while mapping, so
    # a bigger radius maps a bigger area. Odometry drift grows with area, so ~5-10 m
    # is the practical range; beyond that the map gets inaccurate. Cycled from the menu.
    RADIUS_CHOICES_M = [3.0, 5.0, 8.0, 12.0]
    DEFAULT_RADIUS_IDX = 1   # start at 5 m, not the old 3 m
    NAV_RESULT_OK = 0        # navigateToInMap success code

    def __init__(self, session, store_path=None):
        self.nav = session.service("ALNavigation")   # raises if unavailable -> feature disabled
        self.motion = session.service("ALMotion")
        try:
            self.tts = session.service("ALTextToSpeech")
        except Exception:
            self.tts = None

        self.store_path = store_path or _default_store()
        # One entry per named map (e.g. a room): name -> {"map_path", "spots"}.
        # `current` is the active map; explore/remember/go-to all act on it.
        self.maps = {}
        self.current = None
        self.localized = False
        self.busy = False
        self.status = "idle"
        self.radius_idx = self.DEFAULT_RADIUS_IDX
        self._thread = None
        self._load_store()

    @property
    def explore_radius(self):
        return self.RADIUS_CHOICES_M[self.radius_idx]

    def cycle_radius(self):
        """Step to the next explore radius (menu control). Returns the new value."""
        self.radius_idx = (self.radius_idx + 1) % len(self.RADIUS_CHOICES_M)
        return self.explore_radius

    # -- multiple maps -----------------------------------------------------
    # `map_path` and `spots` are views onto the *current* map so the rest of the
    # code (and the menu) can stay map-agnostic.

    @property
    def current_map(self):
        return self.current or "none"

    @property
    def map_path(self):
        rec = self.maps.get(self.current)
        return rec.get("map_path") if rec else None

    @property
    def spots(self):
        rec = self.maps.get(self.current)
        return rec["spots"] if rec else {}

    def _cur_record(self):
        """The current map's record, lazily creating a first map if none exists."""
        if self.current is None or self.current not in self.maps:
            self.new_map(persist=False)
        return self.maps[self.current]

    def new_map(self, name=None, persist=True):
        """Start a fresh, empty map and make it current (auto-named 'map N')."""
        if not name:
            i = len(self.maps) + 1
            name = "map {}".format(i)
            while name in self.maps:
                i += 1
                name = "map {}".format(i)
        self.maps.setdefault(name, {"map_path": None, "spots": {}})
        self.current = name
        self.localized = False   # a new/other map is not localized until loaded
        if persist:
            self._save_store()
        print("nav: current map is now '{}'".format(name))
        return name

    def switch_map(self, name):
        """Make `name` current and load+localize it (drive Pepper to that room's
        start point first). Returns True once localization starts."""
        if name not in self.maps:
            print("nav: no map named '{}'".format(name))
            return False
        self.current = name
        self.localized = False
        self._save_store()
        print("nav: switched to map '{}'".format(name))
        _say(self.tts, "Switching to {}.".format(name))
        return self.load_map()

    def delete_map(self, name):
        if name in self.maps:
            del self.maps[name]
            if self.current == name:
                self.current = next(iter(self.maps), None)
                self.localized = False
            self._save_store()
            print("nav: deleted map '{}'".format(name))

    def map_names(self):
        return sorted(self.maps.keys())

    # -- persistence -------------------------------------------------------

    def _load_store(self):
        try:
            with open(self.store_path) as fh:
                data = json.load(fh)
        except (IOError, ValueError):
            return  # no store yet, or corrupt -> start fresh

        def _rec(raw):
            return {"map_path": raw.get("map_path"),
                    "spots": {k: list(v) for k, v in (raw.get("spots") or {}).items()}}

        if "maps" in data:                       # current multi-map format
            self.maps = {name: _rec(raw) for name, raw in (data.get("maps") or {}).items()}
            self.current = data.get("current")
            if self.current not in self.maps:
                self.current = next(iter(self.maps), None)
        elif data.get("map_path") or data.get("spots"):
            # Old single-map format -> migrate into "map 1" so nothing is lost.
            self.maps = {"map 1": _rec(data)}
            self.current = "map 1"
            print("nav: migrated old single-map store into 'map 1'")

        if self.maps:
            print("nav: loaded {} map(s) from {}; current = {}".format(
                len(self.maps), self.store_path, self.current))

    def _save_store(self):
        try:
            with open(self.store_path, "w") as fh:
                json.dump({"current": self.current, "maps": self.maps}, fh, indent=2)
        except IOError as exc:
            print("WARN: nav could not save {}: {}".format(self.store_path, exc))

    # -- helpers -----------------------------------------------------------

    def _explore_call(self, radius):
        """explore(radius) is the 2.5 name; some builds expose exploreArea()."""
        for name in ("explore", "exploreArea"):
            fn = getattr(self.nav, name, None)
            if fn is not None:
                return fn(radius)
        raise RuntimeError("no explore()/exploreArea() on ALNavigation")

    def _current_pose(self):
        """[x, y, theta] of the robot in the loaded map, or None if unknown."""
        try:
            res = self.nav.getRobotPositionInMap()
        except Exception as exc:
            print("WARN: getRobotPositionInMap failed: {}".format(exc))
            return None
        # 2.5 returns [[x, y, theta], [confidence...]]; be tolerant of a flat list.
        if res and isinstance(res[0], (list, tuple)):
            pose = list(res[0])
        else:
            pose = list(res or [])
        return pose[:3] if len(pose) >= 3 else None

    def _run_async(self, target):
        self._thread = threading.Thread(target=target)
        self._thread.daemon = True
        self._thread.start()

    # -- mapping -----------------------------------------------------------

    def explore(self, radius=None):
        """Map the surrounding area, then save it and keep localization running.

        Blocking on the robot (minutes), so it runs in a background thread and sets
        `busy` for the whole time — the teleop loop won't drive the base meanwhile.
        """
        if self.busy:
            print("nav: already busy ({}), ignoring explore".format(self.status))
            return False
        radius = self.explore_radius if radius is None else radius

        def _work():
            self.busy = True
            self.status = "exploring {:.0f}m".format(radius)
            try:
                self.motion.wakeUp()
            except Exception:
                pass
            print("nav: exploring up to {:.0f} m from start".format(radius))
            _say(self.tts, "Exploring the area. Please keep it clear while I map it.")
            try:
                self._explore_call(radius)
            except Exception as exc:
                print("WARN: explore failed: {}".format(exc))
                _say(self.tts, "Sorry, I could not finish mapping.")
                self.status = "explore failed"
                self.busy = False
                return
            # explore() stops itself when done; make sure the base is released.
            try:
                self.nav.stopExploration()
            except Exception:
                pass
            self.status = "saving map"
            saved = self._save_map()
            self.busy = False
            if saved:
                self._start_localization()
                _say(self.tts, "Done mapping. I can remember spots now.")
                self.status = "mapped"
            else:
                self.status = "map not saved"

        self._run_async(_work)
        return True

    def _save_map(self):
        try:
            path = self.nav.saveExploration()
        except Exception as exc:
            print("WARN: saveExploration failed: {}".format(exc))
            return False
        self._cur_record()["map_path"] = path
        self._save_store()
        print("nav: map '{}' saved on robot at {}".format(self.current_map, path))
        return True

    def load_map(self):
        """Reload a previously saved map and start localizing in it.

        The robot must be physically at (or near) where mapping started — we
        relocalize at the map origin. Use this once at the start of a new session
        before returning to remembered spots.
        """
        if not self.map_path:
            print("nav: no saved map to load")
            _say(self.tts, "I do not have a saved map yet.")
            return False
        try:
            self.nav.loadExploration(self.map_path)
        except Exception as exc:
            print("WARN: loadExploration failed: {}".format(exc))
            _say(self.tts, "I could not load the map.")
            return False
        ok = self._start_localization(relocalize=True)
        if ok:
            _say(self.tts, "Map loaded. Make sure I am where I started mapping.")
        return ok

    def _start_localization(self, relocalize=False):
        try:
            if relocalize:
                # Assume the robot is back at the map origin; refine from there.
                self.nav.relocalizeInMap([0.0, 0.0, 0.0])
            self.nav.startLocalization()
            self.localized = True
            print("nav: localization started")
            return True
        except Exception as exc:
            print("WARN: startLocalization failed: {}".format(exc))
            self.localized = False
            return False

    # -- spots -------------------------------------------------------------

    def remember_spot(self, name=None):
        """Record the current pose under `name` (auto-named 'spot N' if omitted)."""
        if not self.localized:
            print("nav: not localized — explore or load a map first")
            _say(self.tts, "I need to map or load a map before I can remember spots.")
            return None
        pose = self._current_pose()
        if pose is None:
            _say(self.tts, "I could not tell where I am.")
            return None
        if not name:
            name = "spot {}".format(len(self.spots) + 1)
        self._cur_record()["spots"][name] = pose
        self._save_store()
        print("nav: remembered '{}' at {}".format(name, pose))
        _say(self.tts, "I will remember this as {}.".format(name))
        return name

    def go_to_spot(self, name):
        """Drive back to a remembered spot (background thread; `busy` while moving)."""
        pose = self.spots.get(name)
        if pose is None:
            print("nav: no spot named '{}'".format(name))
            return False
        if self.busy:
            print("nav: already busy ({}), ignoring go-to".format(self.status))
            return False
        if not self.localized and not self.load_map():
            _say(self.tts, "I am not localized, so I cannot navigate.")
            return False

        def _work():
            self.busy = True
            self.status = "going to {}".format(name)
            _say(self.tts, "Going to {}.".format(name))
            try:
                result = self.nav.navigateToInMap(list(pose))
            except Exception as exc:
                print("WARN: navigateToInMap failed: {}".format(exc))
                _say(self.tts, "I could not get to {}.".format(name))
                self.status = "nav failed"
                self.busy = False
                return
            ok = (result == self.NAV_RESULT_OK) if isinstance(result, int) else True
            self.status = "arrived" if ok else "nav blocked"
            _say(self.tts, "I arrived at {}.".format(name) if ok
                 else "I stopped before {}.".format(name))
            self.busy = False

        self._run_async(_work)
        return True

    def forget_spots(self):
        """Clear the spots of the current map only (other maps are untouched)."""
        self._cur_record()["spots"] = {}
        self._save_store()
        print("nav: forgot all spots in map '{}'".format(self.current_map))
        _say(self.tts, "I forgot the spots in this map.")

    def spot_names(self):
        return sorted(self.spots.keys())

    # -- lifecycle ---------------------------------------------------------

    def stop(self):
        """Abort any in-progress explore/navigate and release the base."""
        for fn_name in ("stopExploration", "stopNavigateTo"):
            fn = getattr(self.nav, fn_name, None)
            if fn is not None:
                try:
                    fn()
                except Exception:
                    pass
        try:
            self.motion.stopMove()
        except Exception:
            pass
        if self.busy:
            print("nav: stopped ({})".format(self.status))
        self.busy = False
        self.status = "idle"
