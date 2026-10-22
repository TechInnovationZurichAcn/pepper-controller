"""Microphone stream from the robot (ALAudioDevice → remote module).

NAOqi pushes PCM buffers to any registered service that implements
``processRemote(nbOfChannels, nbOfSamplesByChannel, timeStamp, buffer)`` after
``ALAudioDevice.setClientPreferences(name, rate, channels, deinterleave)`` +
``subscribe(name)``. At 16 kHz only one channel is allowed (3 = front mic), which is
exactly what speech recognition wants; 48 kHz gives all four mics interleaved.

Buffers are 16-bit little-endian PCM. Depending on the libqi-python build the binary
arrives as ``bytes`` or as a latin-1 ``str`` – both are handled.
"""

from __future__ import annotations

import collections
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import numpy as np

from pepper_mcp.robot.connection import RobotConnection, RobotError

log = logging.getLogger(__name__)

MODULE_NAME = "PepperMcpAudio"
CHANNEL_FRONT = 3
CHANNEL_ALL = 0

AudioConsumer = Callable[[np.ndarray, int, float], None]  # (int16 mono samples, rate, timestamp)


def _bind(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Declare the processRemote signature for libqi when qi is importable."""
    try:
        import qi

        return qi.bind(returnType=qi.Void, paramsType=[qi.Int32, qi.Int32, qi.Dynamic, qi.Dynamic],
                       methodName="processRemote")(fn)
    except Exception:
        return fn


class _AudioModule:
    """The object exposed to NAOqi. Keeps the callback tiny: convert and hand off."""

    def __init__(self, owner: MicrophoneStream):
        self._owner = owner

    @_bind
    def processRemote(self, nb_channels: int, nb_samples: int, timestamp: Any, buffer: Any) -> None:
        self._owner._on_buffer(int(nb_channels), int(nb_samples), timestamp, buffer)

    def process(self, *args: Any) -> None:  # some builds call process()
        if len(args) == 4:
            self.processRemote(*args)


class MicrophoneStream:
    def __init__(self, conn: RobotConnection, rate: int = 16000, ring_seconds: float = 30.0):
        self.conn = conn
        self.rate = rate
        self.channels = 1 if rate == 16000 else 4
        self._module = _AudioModule(self)
        self._consumers: list[AudioConsumer] = []
        self._lock = threading.Lock()
        self._ring: collections.deque[np.ndarray] = collections.deque()
        self._ring_samples = 0
        self._ring_max = int(ring_seconds * rate)
        self.running = False
        self.buffers = 0
        self.last_buffer_at: float | None = None
        self.last_level = 0.0
        self._registered = False
        self._callback_rate_times: collections.deque[float] = collections.deque(maxlen=20)

    @property
    def audio(self) -> Any:
        return self.conn.service("ALAudioDevice")

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self.running:
            return
        audio = self.audio
        if not self._registered:
            self.conn.register_service(MODULE_NAME, self._module)
            self._registered = True
        channel = CHANNEL_FRONT if self.rate == 16000 else CHANNEL_ALL
        try:
            audio.unsubscribe(MODULE_NAME)
        except Exception:
            pass
        audio.setClientPreferences(MODULE_NAME, self.rate, channel, 0)
        audio.subscribe(MODULE_NAME)
        self.running = True
        log.info("microphone stream started (%d Hz, channel %s)", self.rate, channel)

    def stop(self) -> None:
        if not self.running:
            return
        try:
            self.audio.unsubscribe(MODULE_NAME)
        except Exception as exc:
            log.debug("audio unsubscribe: %s", exc)
        self.running = False

    def close(self) -> None:
        self.stop()
        if self._registered:
            self.conn.unregister_service(MODULE_NAME)
            self._registered = False

    # ------------------------------------------------------------- consumers
    def add_consumer(self, fn: AudioConsumer) -> Callable[[], None]:
        with self._lock:
            self._consumers.append(fn)

        def _remove() -> None:
            with self._lock:
                if fn in self._consumers:
                    self._consumers.remove(fn)

        return _remove

    # ------------------------------------------------------------- callback
    def _on_buffer(self, nb_channels: int, nb_samples: int, timestamp: Any, buffer: Any) -> None:
        now = time.time()
        try:
            samples = decode_pcm(buffer, nb_channels, nb_samples)
        except Exception as exc:
            log.debug("bad audio buffer: %s", exc)
            return
        self.buffers += 1
        self.last_buffer_at = now
        self._callback_rate_times.append(now)
        if samples.size:
            self.last_level = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))
        with self._lock:
            self._ring.append(samples)
            self._ring_samples += samples.size
            while self._ring_samples > self._ring_max and self._ring:
                self._ring_samples -= self._ring.popleft().size
            consumers = list(self._consumers)
        for fn in consumers:
            try:
                fn(samples, self.rate, now)
            except Exception as exc:
                log.warning("audio consumer failed: %s", exc)

    # ------------------------------------------------------------- queries
    def recent(self, seconds: float) -> np.ndarray:
        with self._lock:
            chunks = list(self._ring)
        if not chunks:
            return np.zeros(0, dtype=np.int16)
        data = np.concatenate(chunks)
        n = int(seconds * self.rate)
        return data[-n:] if n < data.size else data

    def callback_rate(self) -> float:
        times = list(self._callback_rate_times)
        if len(times) < 2:
            return 0.0
        return round((len(times) - 1) / (times[-1] - times[0]), 1) if times[-1] > times[0] else 0.0

    def status(self) -> dict[str, Any]:
        return {"running": self.running, "rate": self.rate, "buffers": self.buffers,
                "callbacks_per_s": self.callback_rate(),
                "last_buffer_age_ms": int((time.time() - self.last_buffer_at) * 1000) if self.last_buffer_at else None,
                "level_rms": round(self.last_level, 1)}

    def record(self, seconds: float) -> np.ndarray:
        """Block for ``seconds`` and return the captured mono int16 samples."""
        if not self.running:
            raise RobotError("microphone stream is not running")
        time.sleep(min(10.0, max(0.1, seconds)))
        return self.recent(seconds)


def decode_pcm(buffer: Any, nb_channels: int, nb_samples: int) -> np.ndarray:
    """Return mono int16 samples (front channel / channel 0 when interleaved)."""
    if isinstance(buffer, str):
        raw = buffer.encode("latin-1")
    elif isinstance(buffer, (bytes, bytearray, memoryview)):
        raw = bytes(buffer)
    elif isinstance(buffer, (list, tuple)):
        if buffer and isinstance(buffer[0], int) and all(-32768 <= b <= 32767 for b in buffer[:4]):
            arr = np.asarray(buffer, dtype=np.int16)
            return arr[::nb_channels] if nb_channels > 1 else arr
        raw = bytes(bytearray(buffer))
    else:
        raw = bytes(buffer)
    arr = np.frombuffer(raw, dtype="<i2")
    if nb_channels > 1 and arr.size >= nb_channels * nb_samples:
        arr = arr[: nb_channels * nb_samples].reshape(nb_samples, nb_channels)[:, 0]
    return np.ascontiguousarray(arr)


def pcm_to_wav(samples: np.ndarray, rate: int) -> bytes:
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(np.asarray(samples, dtype="<i2").tobytes())
    return buf.getvalue()
