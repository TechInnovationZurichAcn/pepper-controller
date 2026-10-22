"""Voice activity detection on 20 ms / 16 kHz frames.

``WebRtcVad`` is used when the ``webrtcvad`` wheel is installed, otherwise an adaptive
energy detector. Both expose ``is_speech(frame_int16) -> bool`` so the listener does
not care which one is active.
"""

from __future__ import annotations

import logging
from typing import Protocol

import numpy as np

log = logging.getLogger(__name__)

FRAME_MS = 20


class VAD(Protocol):
    name: str

    def is_speech(self, frame: np.ndarray, rate: int) -> bool: ...


class EnergyVad:
    """RMS threshold that tracks the noise floor (works anywhere, less robust)."""

    name = "energy"

    def __init__(self, ratio: float = 3.0, min_rms: float = 300.0, floor_alpha: float = 0.02):
        self.ratio = ratio
        self.min_rms = min_rms
        self.floor = 150.0
        self.alpha = floor_alpha

    def is_speech(self, frame: np.ndarray, rate: int) -> bool:
        if frame.size == 0:
            return False
        rms = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))
        speech = rms > max(self.min_rms, self.floor * self.ratio)
        if not speech:
            self.floor = (1 - self.alpha) * self.floor + self.alpha * rms
        return speech


class WebRtcVad:
    name = "webrtcvad"

    def __init__(self, aggressiveness: int = 2):
        import webrtcvad

        self._vad = webrtcvad.Vad(int(max(0, min(3, aggressiveness))))

    def is_speech(self, frame: np.ndarray, rate: int) -> bool:
        if rate not in (8000, 16000, 32000, 48000):
            return False
        return bool(self._vad.is_speech(np.asarray(frame, dtype="<i2").tobytes(), rate))


def make_vad(aggressiveness: int = 2) -> VAD:
    try:
        vad: VAD = WebRtcVad(aggressiveness)
        log.info("VAD: webrtcvad (aggressiveness %d)", aggressiveness)
        return vad
    except Exception as exc:
        log.info("webrtcvad unavailable (%s); using energy VAD", exc)
        return EnergyVad()
