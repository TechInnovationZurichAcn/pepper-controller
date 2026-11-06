"""Choregraphe animations and dances: .qianim parsing, Bezier plans, the importer, timeline
gestures with music, random-variant groups and the idle reflex."""

from __future__ import annotations

import json
import math
import os
import threading
import time
import xml.etree.ElementTree as ET
from typing import Any

import pytest

from pepper_mcp import choregraphe_import as imp
from pepper_mcp.robot import gestures as gestures_mod
from pepper_mcp.robot.connection import RobotError
from pepper_mcp.robot.events import EventBus
from pepper_mcp.robot.fake import FakeConnection, FakeRobot
from pepper_mcp.robot.gestures import (
    CHOREOGRAPHY_MANIFEST,
    GestureContext,
    GestureGroup,
    GestureRegistry,
    TimelineGesture,
    library_gestures,
)
from pepper_mcp.robot.motion import JOINT_LIMITS, Motion
from pepper_mcp.robot.qianim import Key, Timeline, parse_qianim, to_bezier

ASSETS = gestures_mod.ASSETS_DIR

QIANIM = """<?xml version="1.0" encoding="UTF-8"?>
<Animation xmlns:editor="http://www.aldebaran.com/animation/editor" typeVersion="2.0">
  <ActuatorCurve actuator="HeadYaw" mute="0" unit="degree" fps="25">
    <Key frame="25" value="10">
      <Tangent side="right" abscissaParam="5" ordinateParam="2" editor:interpType="bezier_auto"/>
    </Key>
    <Key frame="50" value="-10"/>
  </ActuatorCurve>
  <ActuatorCurve actuator="RHand" mute="0" unit="dimensionless" fps="25">
    <Key frame="25" value="0.6"/>
  </ActuatorCurve>
  <ActuatorCurve actuator="HeadPitch" mute="true" unit="degree" fps="25"><Key frame="5" value="9"/></ActuatorCurve>
  <ActuatorCurve actuator="LHipYawPitch" mute="0" unit="degree" fps="25"><Key frame="5" value="9"/></ActuatorCurve>
</Animation>"""


# ---------------------------------------------------------------- parsing
def test_parse_qianim_converts_units_and_skips_muted_and_unknown_curves() -> None:
    tl = parse_qianim(QIANIM)
    assert set(tl.curves) == {"HeadYaw", "RHand"}
    first, second = tl.curves["HeadYaw"]
    assert first.t == pytest.approx(1.0) and first.value == pytest.approx(math.radians(10))
    assert first.right == pytest.approx((0.2, math.radians(2)))  # frames → s, degrees → rad
    assert second.t == pytest.approx(2.0) and second.left is None
    assert tl.curves["RHand"][0].value == pytest.approx(0.6)  # hands stay 0..1
    with pytest.raises(RobotError):
        parse_qianim("<Animation/>")
    with pytest.raises(RobotError):
        parse_qianim("not xml")


def test_auto_tangents_match_choregraphe_bezier_auto() -> None:
    # The library's keys carry the tangents Choregraphe computed; dropping them and letting the
    # player recompute must give the same handles (the converted dances rely on this).
    path = os.path.join(ASSETS, "animations", "01-Hello", "Hello_01.qianim")
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    stored = to_bezier(parse_qianim(text), lead_in_s=0.0)
    root = ET.fromstring(text)
    auto = [el for el in root.iter() if el.get("{http://www.aldebaran.com/animation/editor}interpType") == "bezier_auto"]
    assert auto  # the file uses auto tangents
    bare = Timeline({j: [Key(k.t, k.value) for k in keys] for j, keys in parse_qianim(text).curves.items()})
    recomputed = to_bezier(bare, lead_in_s=0.0)
    i = stored.names.index("HeadPitch")
    assert len(stored.keys[i]) > 2
    for (v1, l1, r1), (v2, l2, r2) in list(zip(stored.keys[i], recomputed.keys[i], strict=True))[1:-1]:
        assert v1 == pytest.approx(v2)
        assert l1[1:] == pytest.approx(l2[1:], abs=1e-6) and r1[1:] == pytest.approx(r2[1:], abs=1e-6)


def test_bezier_plan_clamps_keys_and_handles_and_delays_the_first_key() -> None:
    lo, hi = JOINT_LIMITS["HeadPitch"]
    tl = Timeline({"HeadPitch": [Key(0.0, 0.0), Key(1.0, hi - 0.01, right=(5.0, 1.0)), Key(2.0, 9.0)]})
    plan = to_bezier(tl, lead_in_s=0.8)
    assert plan.shift_s == pytest.approx(0.8) and plan.times[0] == pytest.approx([0.8, 1.8, 2.8])
    assert plan.end_s == pytest.approx(2.8)
    for value, (_, ldt, ldv), (_, rdt, rdv) in plan.keys[0]:
        assert lo <= value <= hi
        assert lo <= value + ldv <= hi and lo <= value + rdv <= hi
        assert ldt <= 0 <= rdt
    # a handle reaching past the next key is shortened to half the interval (handles never cross)
    assert plan.keys[0][1][2][1] == pytest.approx(0.5)
    # the first key is reached from the robot's current pose over the lead-in
    assert plan.keys[0][0][1][1] == pytest.approx(-0.8 / 3)
    assert to_bezier(tl, lead_in_s=0.0, min_shift_s=2.0).shift_s == pytest.approx(2.0)


def test_joint_first_used_late_holds_its_current_angle_until_shortly_before() -> None:
    # Regression: ALMotion glides from the current angle to the first key over all the time
    # before it, so a joint a dance first uses after a minute crept for that whole minute.
    tl = Timeline({"HeadYaw": [Key(0.0, 0.1)], "LHand": [Key(60.0, 1.0)]})
    plan = to_bezier(tl, lead_in_s=0.8, current={"HeadYaw": 0.0, "LHand": 0.3})
    hand = plan.names.index("LHand")
    assert plan.times[hand] == pytest.approx([59.8, 60.8])
    assert plan.keys[hand][0][0] == pytest.approx(0.3)  # held where it is
    assert plan.times[plan.names.index("HeadYaw")] == pytest.approx([0.8])  # early joints unchanged


# ---------------------------------------------------------------- bundled assets
def test_every_bundled_animation_and_dance_parses_within_limits() -> None:
    with open(os.path.join(ASSETS, CHOREOGRAPHY_MANIFEST), encoding="utf-8") as fh:
        manifest = json.load(fh)
    entries = manifest["animations"] + manifest["dances"]
    assert len(manifest["animations"]) == 100 and len(manifest["dances"]) == 11
    names = [e["name"] for e in entries] + [g["name"] for g in manifest["groups"]]
    assert len(names) == len(set(names))
    for entry in entries:
        with open(os.path.join(ASSETS, entry["file"]), encoding="utf-8") as fh:
            tl = parse_qianim(fh.read())
        assert tl.end_s == pytest.approx(entry["duration_s"], abs=0.02), entry["name"]
        plan = to_bezier(tl)
        for joint, keys in zip(plan.names, plan.keys, strict=True):
            lo, hi = JOINT_LIMITS[joint]
            assert all(lo <= k[0] <= hi for k in keys), (entry["name"], joint)
    known = set(names)
    for group in manifest["groups"]:
        assert group["members"] and set(group["members"]) <= known


def test_registry_exposes_library_groups_and_dances() -> None:
    reg = GestureRegistry(lambda: GestureContext(motion=None))  # type: ignore[arg-type]
    names = set(reg.names())
    assert {"greeting", "attract", "idle", "dance", "hello_3", "point_far_half_left", "dance_gangnam_style",
            "dance_disco", "wave", "clown_dance"} <= names
    dances = {g["name"] for g in reg.list("dance")}
    assert "dance_la_bamba" in dances and "wave" not in dances
    assert "hello_1" in reg.get("greeting").to_dict()["variants"]
    assert not reg.remove("hello_1")  # library gestures are not user-defined
    assert reg.get("dance_relax").duration_s > 60


def test_library_gestures_tolerate_a_missing_or_broken_manifest(tmp_path) -> None:  # type: ignore[no-untyped-def]
    assert library_gestures(str(tmp_path / "missing.json")) == []
    broken = tmp_path / "broken.json"
    broken.write_text("{nope")
    assert library_gestures(str(broken)) == []


# ---------------------------------------------------------------- playing
class FakeAudio:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, float]] = []
        self.t0 = time.monotonic()

    def _log(self, what: str, arg: Any = None) -> None:
        self.calls.append((what, arg, time.monotonic() - self.t0))

    def robot_asset_path(self, name: str) -> str:
        self._log("upload", name)
        return f"/home/nao/{os.path.basename(name)}"

    def get_volume(self) -> int:
        return 40

    def set_volume(self, level: int) -> int:
        self._log("volume", level)
        return level

    def load(self, path: str) -> int:
        self._log("load", path)
        return 7

    def play_loaded_async(self, file_id: int) -> None:
        self._log("play", file_id)

    def stop(self) -> None:
        self._log("stop")


def _dance(tmp_path, monkeypatch, offset: float = 0.3, seconds: float = 0.6) -> TimelineGesture:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(gestures_mod, "ASSETS_DIR", str(tmp_path))
    timeline = Timeline({"HeadYaw": [Key(0.0, 0.0), Key(seconds, 0.3)], "RHand": [Key(seconds, 1.0)]})
    (tmp_path / "d.qianim").write_text(imp.write_qianim(timeline))
    (tmp_path / "d.ogg").write_bytes(b"ogg")
    return TimelineGesture(name="dance_t", description="t", duration_s=1, file="d.qianim", music="d.ogg",
                           music_offset_s=offset)


class SlowMotion(Motion):
    """Fake ALMotion whose Bezier interpolation takes as long as the timeline."""

    def interpolate_bezier_async(self, names, times, keys):  # type: ignore[no-untyped-def]
        super().interpolate_bezier_async(names, times, keys)
        end = time.monotonic() + max(t[-1] for t in times)
        done = threading.Event()

        class Fut:
            def isFinished(self) -> bool:  # qi.Future API
                return done.is_set() or time.monotonic() >= end

            def cancel(self) -> None:
                done.set()

        return Fut()


def test_dance_starts_its_music_on_time_and_stops_it_at_the_end(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    gesture = _dance(tmp_path, monkeypatch)
    assert gesture.has_music
    robot = FakeRobot()
    motion = SlowMotion(FakeConnection(robot), EventBus())
    audio = FakeAudio()
    result = gesture.play(GestureContext(motion=motion, audio=audio), threading.Event(), {})
    assert result == {"interrupted": False, "music": True}
    kinds = [c[0] for c in audio.calls]
    assert kinds == ["upload", "load", "volume", "play", "stop", "volume"]
    assert audio.calls[2][1] == 70 and audio.calls[-1][1] == 40  # louder for the song, then restored
    play_at = audio.calls[3][2] - audio.calls[1][2]
    assert 0.8 + 0.3 - 0.05 <= play_at <= 0.8 + 0.3 + 0.5  # lead-in + music offset
    almotion = robot.services["ALMotion"]
    names, times, keys = almotion.last("angleInterpolationBezier")
    assert names == ["HeadYaw", "RHand"] and times[0][0] == pytest.approx(0.8)
    toggles = [a for m, a in almotion.calls if m == "setExternalCollisionProtectionEnabled"]
    assert toggles == [("Arms", False), ("Arms", True)]
    assert robot.services["ALRobotPosture"].count("goToPosture") == 1


def test_stopping_a_dance_stops_motion_and_music(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    gesture = _dance(tmp_path, monkeypatch, offset=0.0, seconds=5.0)
    robot = FakeRobot()
    motion = SlowMotion(FakeConnection(robot), EventBus())
    audio = FakeAudio()
    stop = threading.Event()

    def stop_once_music_plays() -> None:
        _wait_for(lambda: any(c[0] == "play" for c in audio.calls), 5.0)
        stop.set()

    threading.Thread(target=stop_once_music_plays, daemon=True).start()
    result = gesture.play(GestureContext(motion=motion, audio=audio), stop, {})
    assert result == {"interrupted": True, "music": True}
    assert [c[0] for c in audio.calls][-3:] == ["play", "stop", "volume"]
    assert robot.services["ALMotion"].count("killAll") >= 1
    assert robot.services["ALRobotPosture"].count("goToPosture") == 0
    assert robot.services["ALMotion"].last("setExternalCollisionProtectionEnabled") == ("Arms", True)


def _wait_for(cond, timeout_s: float) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout_s
    while not cond() and time.monotonic() < deadline:
        time.sleep(0.01)


def test_dance_keeps_going_silently_when_the_music_fails_to_start(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    gesture = _dance(tmp_path, monkeypatch, offset=0.0, seconds=0.3)
    robot = FakeRobot()
    motion = SlowMotion(FakeConnection(robot), EventBus())
    audio = FakeAudio()

    def broken(file_id: int) -> None:
        raise RuntimeError("ALAudioPlayer refused")

    audio.play_loaded_async = broken  # type: ignore[method-assign]
    assert gesture.play(GestureContext(motion=motion, audio=audio), threading.Event(), {})["interrupted"] is False
    assert robot.services["ALMotion"].count("killAll") == 0  # the dance was not cut off
    assert robot.services["ALMotion"].last("setExternalCollisionProtectionEnabled") == ("Arms", True)


def test_timeline_gesture_reenables_collision_protection_when_motion_fails(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    gesture = _dance(tmp_path, monkeypatch)
    robot = FakeRobot()
    motion = Motion(FakeConnection(robot), EventBus())

    def _fail(*_args: object) -> None:
        raise RobotError("ALMotion refused")

    motion.interpolate_bezier_async = _fail  # type: ignore[method-assign]
    with pytest.raises(RobotError):
        gesture.play(GestureContext(motion=motion, audio=FakeAudio()), threading.Event(), {})
    assert robot.services["ALMotion"].last("setExternalCollisionProtectionEnabled") == ("Arms", True)


def test_silent_dance_without_music_file_or_when_asked(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    gesture = _dance(tmp_path, monkeypatch, seconds=0.1)
    motion = Motion(FakeConnection(FakeRobot()), EventBus())
    audio = FakeAudio()
    assert gesture.play(GestureContext(motion=motion, audio=audio), threading.Event(), {"music": False})["music"] is False
    os.remove(tmp_path / "d.ogg")
    assert not gesture.has_music
    assert gesture.play(GestureContext(motion=motion, audio=audio), threading.Event(), {})["music"] is False
    assert audio.calls == []


def test_gesture_group_picks_variants_without_repeating() -> None:
    played: list[str] = []

    class Stub(TimelineGesture):
        def play(self, ctx, stop_event, params):  # type: ignore[no-untyped-def]
            played.append(self.name)
            return {"interrupted": False}

    members = [Stub(name=n, description="", duration_s=d, file="") for n, d in (("a", 1.0), ("b", 3.0))]
    group = GestureGroup(name="g", description="", duration_s=0, members=members)
    assert group.duration_s == 3.0
    results = [group.play(GestureContext(motion=None), threading.Event(), {}) for _ in range(6)]  # type: ignore[arg-type]
    assert all(played[i] != played[i + 1] for i in range(len(played) - 1))
    assert results[0] == {"variant": played[0], "interrupted": False}
    assert group.play(GestureContext(motion=None), threading.Event(), {"variant": "b"})["variant"] == "b"  # type: ignore[arg-type]
    with pytest.raises(RobotError, match="no variant"):
        group.pick({"variant": "zzz"})
    with pytest.raises(RobotError):
        GestureGroup(name="empty", description="", duration_s=0)


def test_registry_music_gestures_follow_the_music_files(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _dance(tmp_path, monkeypatch)  # writes d.qianim + d.ogg into the patched assets dir
    (tmp_path / CHOREOGRAPHY_MANIFEST).write_text(json.dumps({
        "animations": [],
        "dances": [{"name": "dance_t", "file": "d.qianim", "description": "t", "duration_s": 0.6,
                    "music": "d.ogg", "music_offset_s": 0.3},
                   {"name": "dance_quiet", "file": "d.qianim", "description": "q", "duration_s": 0.6}],
        "groups": [{"name": "dance", "description": "any", "members": ["dance_t", "dance_quiet"]}]}))
    reg = GestureRegistry(lambda: GestureContext(motion=None))  # type: ignore[arg-type]
    assert "music" in reg.get("dance_t").tags and "music" not in reg.get("dance_quiet").tags
    assert {"dance_t", "dance", "clown_dance"} <= reg.music_gestures()
    assert "dance_quiet" not in reg.music_gestures()
    os.remove(tmp_path / "d.ogg")
    assert reg.music_gestures() == frozenset({"clown_dance"})


def test_new_gesture_waits_for_the_previous_one_to_clean_up() -> None:
    # Regression: the preempted gesture's finally (collision protection on, music stop) ran after
    # the new gesture had set up, undoing it.
    log: list[str] = []

    class Slow(TimelineGesture):
        def play(self, ctx, stop_event, params):  # type: ignore[no-untyped-def]
            log.append(f"start {self.name}")
            stop_event.wait(5)
            time.sleep(0.2)  # cleanup takes a moment
            log.append(f"cleanup {self.name}")
            return {"interrupted": stop_event.is_set()}

    reg = GestureRegistry(lambda: GestureContext(motion=None))  # type: ignore[arg-type]
    for name in ("a", "b"):
        reg._gestures[name] = Slow(name=name, description="", duration_s=1, file="")
    first = threading.Thread(target=reg.play, args=("a",))
    first.start()
    _wait_for(lambda: reg.active() == ["a"], 2.0)
    assert reg.play("b", preempt=False) is None  # a background caller never cuts it off
    second = threading.Thread(target=reg.play, args=("b",))
    second.start()
    _wait_for(lambda: "start b" in log, 2.0)
    reg.stop_all()
    first.join(3)
    second.join(3)
    assert log[:3] == ["start a", "cleanup a", "start b"]


# ---------------------------------------------------------------- importer
XAR = """<?xml version="1.0" encoding="UTF-8" ?>
<ChoregrapheProject xmlns="http://www.aldebaran-robotics.com/schema/choregraphe/project.xsd" xar_version="3">
<Box name="root"><Timeline enable="0"><BehaviorLayer name="l"><BehaviorKeyframe name="k" index="1"><Diagram>
  <Box name="GoToPose"><Timeline enable="1" fps="25" start_frame="1" end_frame="25" size="25">
    <ActuatorList model=""><ActuatorCurve name="value" actuator="HeadYaw" mute="0" unit="0">
      <Key frame="25" value="50" /></ActuatorCurve></ActuatorList></Timeline></Box>
  <Box name="Timeline"><Timeline enable="1" fps="25" start_frame="1" end_frame="-1" size="-1">
    <BehaviorLayer name="dance">
      <BehaviorKeyframe name="intro" index="25"><Diagram>
        <Box name="Intro"><Timeline enable="1" fps="10" size="40">
          <ActuatorList model="">
            <ActuatorCurve name="value" actuator="HeadYaw" mute="0" unit="0">
              <Key frame="10" value="10" /><Key frame="40" value="20" /></ActuatorCurve>
            <ActuatorCurve name="value" actuator="LHand" mute="0" unit="1"><Key frame="10" value="1" /></ActuatorCurve>
          </ActuatorList></Timeline></Box></Diagram></BehaviorKeyframe>
      <BehaviorKeyframe name="verse" index="75"><Diagram>
        <Box name="Verse"><Timeline enable="1" fps="10" size="30">
          <ActuatorList model=""><ActuatorCurve name="value" actuator="HeadYaw" mute="0" unit="0">
            <Key frame="5" value="-30" /><Key frame="20" value="-10" /></ActuatorCurve></ActuatorList>
          <BehaviorLayer name="legs"><BehaviorKeyframe name="m" index="3"><Diagram>
            <Box name="Move To"><Parameter name="Distance X (m)" value="0.5" /></Box>
          </Diagram></BehaviorKeyframe></BehaviorLayer>
        </Timeline></Box></Diagram></BehaviorKeyframe>
    </BehaviorLayer>
    <BehaviorLayer name="music"><BehaviorKeyframe name="m" index="50"><Diagram>
      <Box name="Play Sound"><Parameter name="File name" value="/song.ogg" /></Box>
    </Diagram></BehaviorKeyframe></BehaviorLayer>
  </Timeline></Box>
</Diagram></BehaviorKeyframe></BehaviorLayer></Timeline></Box>
</ChoregrapheProject>"""


def test_flatten_xar_places_timelines_and_music_at_absolute_times(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "behavior.xar"
    path.write_text(XAR)
    timeline, music, music_at = imp.flatten_xar(str(path))
    assert music == "/song.ogg" and music_at == pytest.approx(2.0)  # keyframe 50 at 25 fps
    yaw = timeline.curves["HeadYaw"]
    # the GoToPose prelude (shorter sibling) is dropped; Intro starts at 1 s, Verse at 3 s and
    # replaces the Intro keys from its own first key on
    assert [round(k.t, 2) for k in yaw] == [2.0, 3.5, 5.0]
    assert [round(math.degrees(k.value)) for k in yaw] == [10, -30, -10]
    assert timeline.curves["LHand"][0].value == pytest.approx(1.0)
    again = parse_qianim(imp.write_qianim(timeline))
    assert [round(k.t, 2) for k in again.curves["HeadYaw"]] == [2.0, 3.5, 5.0]
    assert again.curves["LHand"][0].value == pytest.approx(1.0)


def test_animation_entries_get_readable_names() -> None:
    assert imp.animation_entry("09-Orientation", "PointShortHalfR_01.qianim")["name"] == "point_near_half_right"
    assert imp.animation_entry("08-Attract", "Attract_L03.qianim")["name"] == "attract_left_3"
    assert imp.animation_entry("01-Hello", "Hello_10.qianim")["name"] == "hello_10"
    assert imp.animation_entry("06-Solitaries", "LookFarLeft_01.qianim")["name"] == "look_far_left"
    entry = imp.animation_entry("07-Reactions", "SadReaction_02.qianim")
    assert entry["name"] == "sad_reaction_2" and entry["group"] == "sad_reaction"
    assert imp.animation_entry("10-Loading", "PoseLeft_04.qianim")["return_to_rest"] is False


# ---------------------------------------------------------------- idle reflex
def test_idle_reflex_fidgets_only_when_nobody_is_around(runtime) -> None:  # type: ignore[no-untyped-def]
    idle = runtime.reflexes.get("idle")
    assert idle.enabled is False  # opt-in
    played: list[str] = []
    runtime.gestures.play = lambda name, params=None, preempt=True: played.append(name) or {"interrupted": False}
    runtime.perception.people = lambda: []
    idle._last_activity = time.monotonic() - 60
    assert idle.tick() and played == ["idle"]
    assert not idle.tick()  # interval not over yet
    idle._last_fidget = 0.0
    runtime.perception.people = lambda: [{"person_id": 1}]
    assert not idle.tick()  # someone is there
    runtime.perception.people = lambda: []
    from pepper_mcp.robot.events import Event
    idle.react(Event("speech.started", {}))
    assert not idle.tick()  # something just happened
    assert played == ["idle"]


def test_dashboard_lists_groups_and_dances_but_not_every_library_animation(runtime) -> None:  # type: ignore[no-untyped-def]
    from pepper_mcp.dashboard import state

    names = {g["name"] for g in state(runtime)["gestures"]}
    assert {"wave", "greeting", "dance_gangnam_style"} <= names
    assert "hello_3" not in names


def test_subfolder_asset_is_uploaded_only_once(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Regression: upload() cached under the basename, so "dances/x.ogg" was never found in the
    # cache and every dance copied its music to the robot again.
    from pepper_mcp.robot.audio_out import AudioOut

    audio = AudioOut(FakeConnection(FakeRobot()))
    uploads: list[str] = []

    def fake_upload(local: str, *a: Any, **k: Any) -> str:
        uploads.append(local)
        audio._uploaded[os.path.basename(local)] = f"/home/nao/{os.path.basename(local)}"
        return f"/home/nao/{os.path.basename(local)}"

    monkeypatch.setattr(audio, "upload", fake_upload)
    name = next(f"dances/{f}" for f in os.listdir(os.path.join(ASSETS, "dances")) if f.endswith(".qianim"))
    assert audio.robot_asset_path(name) == audio.robot_asset_path(name)
    assert len(uploads) == 1
