"""Listener segmentation with synthetic audio and a stub STT (no model download)."""

from __future__ import annotations

import time

import numpy as np

from pepper_mcp.robot.audio_in import MicrophoneStream
from pepper_mcp.robot.events import EventBus
from pepper_mcp.robot.fake import FakeConnection, FakeRobot
from pepper_mcp.speech.listener import Listener
from pepper_mcp.speech.stt import Transcript
from pepper_mcp.speech.vad import EnergyVad


class StubSTT:
    name = "stub"

    def __init__(self) -> None:
        self.calls: list[int] = []

    def warm_up(self) -> None:
        return None

    def transcribe(self, samples: np.ndarray, rate: int) -> Transcript:
        self.calls.append(int(samples.size))
        return Transcript(text="hello pepper", language="en", backend=self.name)


class FakeSpeechState:
    speaking = False
    last_spoke_at = 0.0

    def stop_speaking(self) -> None:
        self.speaking = False


def speech_then_silence(t: float, n: int, rate: int) -> np.ndarray:
    """1.0 s of loud tone starting at t=0.5 s, silence otherwise."""
    ts = t + np.arange(n) / rate
    tone = (np.sin(2 * np.pi * 440 * ts) * 12000).astype(np.int16)
    mask = (ts >= 0.5) & (ts < 1.5)
    return np.where(mask, tone, np.zeros(n, dtype=np.int16))


def _wait_for(bus: EventBus, type_: str, timeout: float = 6.0):  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ev = bus.last(type_)
        if ev is not None:
            return ev
        time.sleep(0.02)
    return None


def test_listener_segments_speech_and_emits_final() -> None:
    robot = FakeRobot()
    robot.set_audio_source(speech_then_silence)
    bus = EventBus()
    mic = MicrophoneStream(FakeConnection(robot), 16000)
    stt = StubSTT()
    listener = Listener(mic, EnergyVad(min_rms=1000), stt, bus, FakeSpeechState(), end_silence_ms=300,
                        min_speech_ms=200, pre_roll_ms=100)
    mic.start()
    listener.start()
    try:
        final = _wait_for(bus, "speech.final")
        assert final is not None, bus.recent(0)
        assert final.data["text"] == "hello pepper"
        assert 0.8 < final.data["duration_s"] < 2.5
        assert bus.last("speech.started") is not None
        assert stt.calls and 12000 < stt.calls[0] < 40000  # ~1-2 s of 16 kHz audio
        assert listener.status()["utterances"] == 1
    finally:
        listener.stop()
        mic.close()
    bus.shutdown()


def test_listener_is_muted_while_robot_speaks() -> None:
    robot = FakeRobot()
    robot.set_audio_source(speech_then_silence)
    bus = EventBus()
    mic = MicrophoneStream(FakeConnection(robot), 16000)
    state = FakeSpeechState()
    state.speaking = True
    stt = StubSTT()
    listener = Listener(mic, EnergyVad(min_rms=1000), stt, bus, state, end_silence_ms=300, min_speech_ms=200)
    mic.start()
    listener.start()
    try:
        time.sleep(2.5)
        assert bus.last("speech.final") is None
        assert listener.status()["muted_frames"] > 0
    finally:
        listener.stop()
        mic.close()
    bus.shutdown()
