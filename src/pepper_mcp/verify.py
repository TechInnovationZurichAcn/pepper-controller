"""``pepper-mcp-verify``: run the hardware checklist from docs/ROBOT_VERIFICATION.md."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Callable
from typing import Any

from pepper_mcp.config import Settings
from pepper_mcp.robot.connection import RobotConnection


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="pepper-mcp-verify", description="Check Pepper MCP assumptions on a real robot")
    p.add_argument("--ip", required=True)
    p.add_argument("--port", type=int, default=9559)
    p.add_argument("--no-move", action="store_true", help="skip the tiny base move")
    p.add_argument("--quiet", action="store_true", help="skip speaking")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

    results: list[tuple[str, bool, str]] = []

    def check(name: str, fn: Callable[[], Any]) -> Any:
        t0 = time.monotonic()
        try:
            detail = fn()
            ok = True
        except Exception as exc:
            detail, ok = str(exc), False
        ms = int((time.monotonic() - t0) * 1000)
        results.append((name, ok, f"{detail}"))
        print(f"[{'OK ' if ok else 'FAIL'}] {name:<18} {ms:>5} ms  {str(detail)[:110]}")
        return detail if ok else None

    conn = RobotConnection(args.ip, args.port)
    if check("connect", lambda: (conn.connect(), conn.endpoints())[1]) is None:
        sys.exit(1)
    services = check("services", conn.service_names) or []
    needed = ["ALMotion", "ALRobotPosture", "ALTextToSpeech", "ALAnimatedSpeech", "ALAnimationPlayer",
              "ALVideoDevice", "ALAudioDevice", "ALMemory", "ALPeoplePerception", "ALWavingDetection",
              "ALGazeAnalysis", "ALEngagementZones", "ALSoundLocalization", "ALTracker", "ALLeds",
              "ALTabletService", "ALNavigation", "ALBehaviorManager", "ALAutonomousLife", "ALBasicAwareness"]
    missing = [n for n in needed if n not in services]
    print(f"       missing services: {missing or 'none'}")

    settings = Settings(ip=args.ip, port=args.port, audio_enabled=True, camera_enabled=True, listening=False,
                        reflexes_enabled=False, preview_port=0, autonomous_life="keep", wake_up=False)
    from pepper_mcp.runtime import Runtime

    rt = Runtime(settings, conn)
    rt.memory.start()

    def tts() -> str:
        if args.quiet:
            return "skipped"
        utt = rt.speech.say("Pepper MCP check.", animated=False, wait=True, timeout_s=15)
        rt.speech.start()
        return f"done in {utt.finished_at - utt.started_at:.1f}s error={utt.error}"

    rt.speech.start()
    check("tts", tts)
    check("animated_speech", lambda: ("skipped" if args.quiet else
                                      rt.speech.say("^startTag(hello) Hello there!", animated=True, wait=True, timeout_s=20).to_dict()["state"]))

    def memory_sub() -> str:
        got = []
        rt.memory.watch("PepperMcpVerify/Ping", lambda k, v: got.append(v), poll_s=0.05)
        rt.memory.memory.insertData("PepperMcpVerify/Ping", 42)
        time.sleep(0.4)
        return f"native_subscriber={rt.memory.native_supported} delivered={got}"

    check("memory_subscriber", memory_sub)

    def audio() -> str:
        rt.mic.start()
        time.sleep(1.5)
        st = rt.mic.status()
        rt.mic.stop()
        if st["buffers"] == 0:
            raise RuntimeError(f"no processRemote callbacks received ({st}) – check firewall / listen URL")
        return f"{st['callbacks_per_s']} callbacks/s, level_rms={st['level_rms']}"

    check("audio_callback", audio)

    def camera() -> str:
        stream = rt.cameras.get("top")
        data, frame = stream.jpeg(320, timeout_s=3.0)
        out = f"top {frame.width}x{frame.height} jpeg={len(data)}B age={frame.age_ms}ms"
        try:
            d2, f2 = rt.cameras.get("depth").jpeg(320, timeout_s=3.0)
            out += f"; depth {f2.width}x{f2.height}"
        except Exception as exc:
            out += f"; depth unavailable ({exc})"
        rt.cameras.stop()
        return out

    check("camera", camera)

    def waving() -> str:
        svc = conn.service("ALWavingDetection")
        svc.subscribe("pepper_mcp_verify")
        time.sleep(0.5)
        keys = [k for k in rt.memory.memory.getDataListName() if "Waving" in k][:6]
        svc.unsubscribe("pepper_mcp_verify")
        return f"subscribed; keys={keys}"

    check("waving", waving)
    check("animations", lambda: f"{len(rt.animations.installed(refresh=True))} found, discovered={rt.animations.discovered}, "
                                 f"e.g. {rt.animations.installed()[:3]}")
    check("tablet", lambda: (rt.tablet.show_text("Pepper MCP ✓"), time.sleep(2), rt.tablet.hide(), "shown")[-1])
    check("leds", lambda: (rt.leds.set("eyes", "green", 0.2), time.sleep(0.5), rt.leds.reset("eyes"), "ok")[-1])
    check("people", lambda: f"{len(rt.perception.people())} person(s) visible" if rt.perception.start() else "no extractors")

    def move() -> str:
        if args.no_move:
            return "skipped"
        rt.motion.wake_up()
        rt.motion.move(0.15, 0, 0, 0.3)
        time.sleep(0.5)
        rt.motion.stop_base()
        return "moved forward briefly and stopped"

    check("move", move)
    rt.stop()
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed" + (f"; failed: {failed}" if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
