"""Import Choregraphe animations and dances into ``assets/`` (developer tool).

    python -m pepper_mcp.choregraphe_import <extracted 03_Pepper folder>

* ``01_Project/animations/*/*.qianim`` are copied as they are (with their Readme files) into
  ``assets/animations/``.
* Each dance ``behavior.xar`` is flattened into one ``assets/dances/<name>.qianim``: every
  timed motion timeline is placed at its absolute start time, the music box's start gives the
  music offset, and the ``.ogg`` is copied next to it. Base moves (``Move To``, ``moveAlong``),
  LED flashes and tablet boxes are dropped: Pepper dances on the spot.
* ``assets/choreography.json`` lists every animation, dance and random-variant group with the
  name, description and duration the gesture registry uses.

The music files are committed next to the dances; a dance whose ``.ogg`` is missing plays
silently.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

from pepper_mcp.robot.motion import JOINT_LIMITS
from pepper_mcp.robot.qianim import RATIO_UNITS, Key, Timeline, _strip_ns, dedupe_keys, parse_qianim

ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
MANIFEST = "choreography.json"
OUT_FPS = 100  # converted dances: one frame = 10 ms

# --------------------------------------------------------------------------- animations
# category folder -> (group name or None, tags, description template, return_to_rest)
CATEGORIES: dict[str, tuple[str | None, list[str], str, bool]] = {
    "00-BackToStand": ("back_to_stand", ["utility"], "return to the standing pose naturally", False),
    "01-Hello": ("greeting", ["social", "greeting"], "say hello with the arms and head", True),
    "02-Body_Parts": (None, ["deictic", "explain"], "", True),
    "03-Tablet": ("show_tablet", ["deictic", "tablet"], "present what is on the chest tablet", True),
    "04-Make_Space": ("make_space", ["social"], "ask people to step back and make room", True),
    "05-Enumeration": ("enumerate", ["explain"], "list points one after another while explaining", True),
    "06-Solitaries": ("idle", ["idle"], "", True),
    "07-Reactions": (None, ["emotion"], "", True),
    "08-Attract": ("attract", ["social"], "beckon someone to come closer", True),
    "09-Orientation": (None, ["deictic"], "", True),
    "10-Loading": ("loading", ["waiting"], "patient waiting pose while something loads; chain several", False),
}

SPECIFIC: dict[str, str] = {
    "Show_Body": "present its whole body",
    "Show_Bumper_Left": "show the left bumper (front left of the base)",
    "Show_Bumper_Right": "show the right bumper (front right of the base)",
    "Show_Hand_Both": "show both hands",
    "Show_Hand_Left": "show the left hand",
    "Show_Hand_Right": "show the right hand",
    "Show_Head": "show its head",
    "Show_Micro": "show the microphones on top of its head",
    "Show_Self": "point at itself ('me')",
    "Show_Shoulders": "show its shoulders",
    "CheckLeft": "check something on its left",
    "CheckRight": "check something on its right",
    "Funny": "small funny fidget",
    "LookAtSidesLeft": "look to both sides, starting left",
    "LookAtSidesRight": "look to both sides, starting right",
    "LookBumpersLeft": "look down at the left bumper",
    "LookBumpersRight": "look down at the right bumper",
    "LookFarLeft": "look far away to the left",
    "LookFarRight": "look far away to the right",
    "LookHandLeft": "look at its left hand",
    "LookHandRight": "look at its right hand",
    "LookLeft": "look to the left",
    "LookRight": "look to the right",
    "Looking_around": "look around",
    "Looking_around_wide": "look around widely",
    "MotorSoundLeft": "listen to the motor of its left arm",
    "MotorSoundRight": "listen to the motor of its right arm",
    "PlayWithHandLeft": "play with its left hand",
    "PlayWithHandRight": "play with its right hand",
    "NiceReaction": "pleased, happy reaction",
    "SadReaction": "sad, disappointed reaction",
}

# Pointing: Front = straight ahead, Medium = ~30° to the side, Half = ~60° to the side;
# Short = near (low arm), otherwise far (arm level).
_POINT_DIR = {"Front": "straight ahead", "Medium": "diagonally (~30°) to its", "Half": "to the side (~60°) on its"}


MULTI_VARIANT = frozenset({"Back_to_stand", "Hello", "Show_Tablet", "Make_Space", "Enumeration", "Show_Body",
                           "Show_Hand_Both", "Show_Hand_Left", "Show_Hand_Right", "Funny", "NiceReaction",
                           "SadReaction"})


def _snake(stem: str) -> str:
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", "_", stem).lower()
    return re.sub(r"_+", "_", s)


def animation_entry(category: str, filename: str) -> dict[str, Any]:
    group, tags, template, rest = CATEGORIES[category]
    stem = filename.removesuffix(".qianim")
    base = re.sub(r"_?0*\d+$", "", stem)
    number = re.search(r"(\d+)$", stem)
    variant = int(number.group(1)) if number else 1
    if m := re.fullmatch(r"Point(Short)?(Front|Medium|Half)([LR])_01", stem):
        short, direction, side = m.groups()
        side_word = "left" if side == "L" else "right"
        name = f"point_{'near' if short else 'far'}_{direction.lower()}_{side_word}"
        where = _POINT_DIR[direction] + ("" if direction == "Front" else f" {side_word}")
        desc = f"point {'at something close by' if short else 'into the distance'} {where}"
        if direction == "Front":
            desc += f" with the {side_word} arm"
    elif m := re.fullmatch(r"Attract_([LR])(\d+)", stem):
        side_word = "left" if m.group(1) == "L" else "right"
        name = f"attract_{side_word}_{int(m.group(2))}"
        desc = f"{template} with the {side_word} arm (variant {int(m.group(2))})"
    elif m := re.fullmatch(r"Pose(Left|Right)_(\d+)", stem):
        name = f"loading_{m.group(1).lower()}_{int(m.group(2))}"
        desc = f"{template} (leaning {m.group(1).lower()}, variant {int(m.group(2))})"
    elif base in MULTI_VARIANT:
        name = f"{_snake(base)}_{variant}"
        desc = f"{SPECIFIC.get(base) or template} (variant {variant})"
    else:
        name = _snake(base)
        desc = SPECIFIC.get(base) or template
    if group is None and base in ("NiceReaction", "SadReaction"):
        group = "nice_reaction" if base == "NiceReaction" else "sad_reaction"
    return {"name": name, "file": f"animations/{category}/{filename}", "description": desc,
            "tags": ["library", *tags], "group": group, "return_to_rest": rest}


GROUPS: dict[str, tuple[str, list[str]]] = {
    "greeting": ("say hello, a random one of 10 greeting animations", ["social", "greeting"]),
    "back_to_stand": ("return to the standing pose naturally (random variant)", ["utility"]),
    "show_tablet": ("present what is on the chest tablet (random variant)", ["deictic", "tablet"]),
    "make_space": ("ask people to step back and make room, e.g. before a dance (random variant)", ["social"]),
    "enumerate": ("list points one after another while explaining (random variant)", ["explain"]),
    "idle": ("small idle fidget: look around, play with a hand… (random; leave 15-30 s between them)",
             ["idle"]),
    "nice_reaction": ("pleased, happy reaction (random variant)", ["emotion"]),
    "sad_reaction": ("sad, disappointed reaction (random variant)", ["emotion"]),
    "attract": ("beckon someone to come closer (random variant, either arm)", ["social"]),
    "loading": ("patient waiting pose while something takes time (random variant)", ["waiting"]),
    "dance": ("dance a whole song with music (random choice of the music dances)", ["dance", "fun"]),
}

# --------------------------------------------------------------------------- dances
# (behaviour relative to the 03_Pepper folder, gesture name, song title)
DANCES: list[tuple[str, str, str]] = [
    ("pepperApplications/danses/asereje/behavior.xar", "dance_asereje", "Asereje (The Ketchup Song)"),
    ("pepperApplications/danses/danceoftheknights/behavior.xar", "dance_knights",
     "Dance of the Knights (Prokofiev, Romeo and Juliet)"),
    ("pepperApplications/danses/danceofthereedflutes/behavior.xar", "dance_reed_flutes",
     "Dance of the Reed Flutes (Tchaikovsky, The Nutcracker)"),
    ("pepperApplications/danses/dango3kyodai/behavior.xar", "dance_dango", "Dango 3 Kyodai (Japanese children's song)"),
    ("pepperApplications/danses/hungarian/behavior.xar", "dance_hungarian", "Hungarian Dance (Brahms)"),
    ("pepperApplications/danses/jgangnamstyle/behavior.xar", "dance_gangnam_style", "Gangnam Style (PSY)"),
    ("pepperApplications/danses/la_bamba/behavior.xar", "dance_la_bamba", "La Bamba"),
    ("pepperApplications/danses/mika_relax/behavior.xar", "dance_relax", "Relax, Take It Easy (Mika)"),
    ("pepperApplications/danses/walk_this_way/behavior.xar", "dance_walk_this_way", "Walk This Way (Aerosmith)"),
    ("Date_Dance/behavior.xar", "dance_date", "Date Dance"),
    ("DancingQueen/DancingQueen/behavior.xar", "dance_disco", "short disco moves"),
]
SOUND_EXT = (".ogg", ".mp3", ".wav")


@dataclass
class _Segment:
    start_s: float
    fps: float
    actuators: ET.Element


@dataclass
class _Flat:
    segments: list[_Segment] = field(default_factory=list)
    sounds: list[tuple[float, str]] = field(default_factory=list)


def _timed(timeline: ET.Element) -> tuple[bool, float]:
    fps = float(timeline.get("fps") or 0)
    return timeline.get("enable") == "1" and fps > 0, fps


def _motion_span(box: ET.Element) -> float:
    """Longest single motion timeline inside ``box`` (seconds), to tell a dance from its
    'go to pose' prelude when both sit in the same diagram."""
    best = 0.0
    for timeline in box.iter("Timeline"):
        timed, fps = _timed(timeline)
        actuators = timeline.find("ActuatorList")
        if timed and actuators is not None:
            frames = [int(k.get("frame", 0)) for k in actuators.iter("Key")]
            if frames:
                best = max(best, max(frames) / fps)
    return best


def _sound_file(box: ET.Element) -> str | None:
    for param in box.findall("Parameter"):
        value = param.get("value") or ""
        if param.get("name") == "File name" and value.lower().endswith(SOUND_EXT):
            return value
    return None


def _walk(box: ET.Element, start_s: float, flat: _Flat) -> None:
    sound = _sound_file(box)
    if sound:
        flat.sounds.append((start_s, sound))
    timeline = box.find("Timeline")
    if timeline is None:
        return
    timed, fps = _timed(timeline)
    actuators = timeline.find("ActuatorList")
    if timed and actuators is not None and actuators.find("ActuatorCurve") is not None:
        flat.segments.append(_Segment(start_s, fps, actuators))
    for layer in timeline.findall("BehaviorLayer"):
        for keyframe in layer.findall("BehaviorKeyframe"):
            at = start_s + (int(keyframe.get("index", 1)) / fps if timed else 0.0)
            diagram = keyframe.find("Diagram")
            boxes = diagram.findall("Box") if diagram is not None else []
            moving = [b for b in boxes if _motion_span(b) > 0]
            main = max(moving, key=_motion_span) if moving else None
            for child in boxes:
                if child in moving and child is not main:
                    continue  # pose prelude/postlude started by the flow, not by time
                _walk(child, at, flat)


def flatten_xar(path: str) -> tuple[Timeline, str | None, float]:
    """(timeline, music file relative to the package, music start in timeline seconds)."""
    root = _strip_ns(ET.parse(path).getroot())
    box = root.find("Box")
    if box is None:
        raise ValueError(f"{path}: no root box")
    flat = _Flat()
    _walk(box, 0.0, flat)
    if not flat.segments:
        raise ValueError(f"{path}: no motion timeline")
    curves: dict[str, list[Key]] = {}
    for seg in sorted(flat.segments, key=lambda s: s.start_s):
        for curve in seg.actuators.findall("ActuatorCurve"):
            joint = curve.get("actuator") or ""
            if joint not in JOINT_LIMITS or curve.get("mute", "0") in ("1", "true"):
                continue
            ratio = curve.get("unit") in RATIO_UNITS
            new = [Key(t=seg.start_s + int(k.get("frame", 0)) / seg.fps,
                       value=float(k.get("value", 0)) if ratio else math.radians(float(k.get("value", 0))))
                   for k in curve.findall("Key")]
            if not new:
                continue
            first = min(k.t for k in new)
            kept = [k for k in curves.get(joint, []) if k.t < first - 1e-3]  # later segment wins
            curves[joint] = dedupe_keys(kept + new)
    music = next((s for s in flat.sounds if s[1].lower().endswith(".ogg")), flat.sounds[0] if flat.sounds else None)
    if music is None:
        return Timeline(curves), None, 0.0
    return Timeline(curves), music[1], music[0]


def write_qianim(timeline: Timeline, fps: int = OUT_FPS) -> str:
    """Serialise keys only (no tangents: the player computes Choregraphe's auto tangents)."""
    root = ET.Element("Animation", {"typeVersion": "2.0", "fps": str(fps)})
    for joint in sorted(timeline.curves):
        ratio = joint.endswith("Hand")
        curve = ET.SubElement(root, "ActuatorCurve", {"actuator": joint, "mute": "0", "fps": str(fps),
                                                      "unit": "dimensionless" if ratio else "degree"})
        for key in dedupe_keys([Key(round(k.t * fps) / fps, k.value) for k in timeline.curves[joint]]):
            value = key.value if ratio else math.degrees(key.value)
            ET.SubElement(curve, "Key", {"frame": str(round(key.t * fps)), "value": f"{value:.4f}"})
    ET.indent(root)
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


# --------------------------------------------------------------------------- driver
def import_all(src: str, assets: str = ASSETS_DIR) -> dict[str, Any]:
    anim_src = os.path.join(src, "01_Project", "animations")
    animations: list[dict[str, Any]] = []
    for category in sorted(CATEGORIES):
        folder = os.path.join(anim_src, category)
        out = os.path.join(assets, "animations", category)
        os.makedirs(out, exist_ok=True)
        for filename in sorted(os.listdir(folder)):
            path = os.path.join(folder, filename)
            if filename == "Readme.txt":
                shutil.copyfile(path, os.path.join(out, filename))
            if not filename.endswith(".qianim"):
                continue
            with open(path, encoding="utf-8") as fh:
                timeline = parse_qianim(fh.read())
            shutil.copyfile(path, os.path.join(out, filename))
            entry = animation_entry(category, filename)
            entry["duration_s"] = round(timeline.end_s, 2)
            animations.append(entry)

    dances: list[dict[str, Any]] = []
    out = os.path.join(assets, "dances")
    os.makedirs(out, exist_ok=True)
    for rel, name, title in DANCES:
        path = os.path.join(src, rel)
        timeline, music, music_at = flatten_xar(path)
        with open(os.path.join(out, f"{name}.qianim"), "w", encoding="utf-8") as fh:
            fh.write(write_qianim(timeline))
        entry: dict[str, Any] = {"name": name, "file": f"dances/{name}.qianim", "title": title,
                                 "duration_s": round(timeline.end_s, 2), "music": None, "music_offset_s": 0.0}
        if music:
            package = os.path.dirname(path)
            while package != src and not os.path.exists(os.path.join(package, "manifest.xml")):
                package = os.path.dirname(package)
            music_src = os.path.join(package, music.lstrip("/"))
            if os.path.exists(music_src):
                ext = os.path.splitext(music_src)[1].lower()
                shutil.copyfile(music_src, os.path.join(out, f"{name}{ext}"))
                entry["music"] = f"dances/{name}{ext}"
                entry["music_offset_s"] = round(music_at, 3)
        length = f"{entry['duration_s'] / 60:.1f} min" if entry["duration_s"] >= 60 else f"{entry['duration_s']:.0f} s"
        entry["description"] = (f"dance to {title} ({length}, with music)" if entry["music"]
                                else f"{title} ({length}, no music)")
        dances.append(entry)

    groups = []
    for name, (desc, tags) in GROUPS.items():
        if name == "dance":
            members = [d["name"] for d in dances if d["music"]]
        else:
            members = [a["name"] for a in animations if a["group"] == name]
        if members:
            groups.append({"name": name, "description": desc, "tags": tags, "members": members})
    for a in animations:
        del a["group"]
    manifest = {"source": "Choregraphe animation library (O. Landais, 2019) and dance apps from "
                          "pepperApplications (IMERIR); imported by pepper_mcp.choregraphe_import",
                "animations": animations, "dances": dances, "groups": groups}
    with open(os.path.join(assets, MANIFEST), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1, ensure_ascii=False)
        fh.write("\n")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("source", help="extracted 03_Pepper folder (with 01_Project/ and pepperApplications/)")
    args = parser.parse_args(argv)
    manifest = import_all(os.path.abspath(args.source))
    silent = [d["name"] for d in manifest["dances"] if not d["music"]]
    print(f"{len(manifest['animations'])} animations, {len(manifest['dances'])} dances "
          f"({len(silent)} without music: {', '.join(silent) or '-'}), {len(manifest['groups'])} groups")
    return 0


if __name__ == "__main__":
    sys.exit(main())
