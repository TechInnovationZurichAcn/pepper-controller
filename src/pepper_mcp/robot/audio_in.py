"""Microphone stream from the robot (ALAudioDevice → remote module).

NAOqi pushes PCM buffers to any registered service that implements
``processRemote(nbOfChannels, nbOfSamplesByChannel, timeStamp, buffer)`` after
``ALAudioDevice.setClientPreferences(name, rate, channels, deinterleave)`` +
``subscribe(name)``. At 16 kHz only one mic is allowed (1 left, 2 right, 3 front, 4 rear),
which is exactly what speech recognition wants; 48 kHz gives all four mics interleaved
(left, right, front, rear).

A single head mic can die while the others keep working (seen on a real robot: the front mic
went flat at the ADC noise floor). With ``mic="auto"`` the stream reads NAOqi's per-mic energy
meters at start and skips mics that are far quieter than the rest, so speech still gets through.

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
CHANNEL_ALL = 0
# name -> (16 kHz channel id, index in the 48 kHz interleave, ALAudioDevice energy getter)
MICS: dict[str, tuple[int, int, str]] = {
    "front": (3, 2, "getFrontMicEnergy"),
    "left": (1, 0, "getLeftMicEnergy"),
    "right": (2, 1, "getRightMicEnergy"),
    "rear": (4, 3, "getRearMicEnergy"),
}
MIC_CHOICES = ("auto", *MICS)  # auto prefers MICS order: front first, it faces the person
# A working mic always picks up the fans; a dead one sits at the ADC noise floor (~1/6 of them).
DEAD_RATIO = 0.4
PROBE_READS = 6
PROBE_PERIOD_S = 0.1

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
    def __init__(self, conn: RobotConnection, rate: int = 16000, mic: str = "auto", *,
                 ring_seconds: float = 30.0):
        if mic not in MIC_CHOICES:
            raise ValueError(f"mic must be one of {', '.join(MIC_CHOICES)}")
        self.conn = conn
        self.rate = rate
        self.channels = 1 if rate == 16000 else 4
        self.mic_setting = mic
        self.mic = "front" if mic == "auto" else mic
        self.mic_energy: dict[str, float] = {}
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
        self._subscribe(audio)
        try:
            if self.mic_setting == "auto":
                self._switch_to(audio, self._pick_live_mic(audio))
        except BaseException:  # incl. Ctrl+C/SIGTERM mid-probe: stop() can't undo it, running is False
            self.stop_subscription(audio)
            raise
        self.running = True  # only once the final subscription is in place
        log.info("microphone stream started (%d Hz, %s mic)", self.rate, self.mic)

    def _switch_to(self, audio: Any, mic: str) -> None:
        if mic == self.mic:
            return
        previous, self.mic = self.mic, mic
        if self.rate != 16000:  # all four channels already arrive; only the decoded index changes
            return
        try:
            self._subscribe(audio)  # one channel per subscription at 16 kHz: switch it
        except Exception as exc:
            # The previous subscription was just torn down; a deaf robot is worse than a worse mic.
            log.warning("could not switch to the %s mic (%s); staying on the %s mic", mic, exc, previous)
            self.mic = previous
            self._subscribe(audio)

    def _subscribe(self, audio: Any) -> None:
        channel = MICS[self.mic][0] if self.rate == 16000 else CHANNEL_ALL
        try:
            audio.unsubscribe(MODULE_NAME)
        except Exception:
            pass
        audio.setClientPreferences(MODULE_NAME, self.rate, channel, 0)
        audio.subscribe(MODULE_NAME)

    @staticmethod
    def stop_subscription(audio: Any) -> None:
        try:
            audio.unsubscribe(MODULE_NAME)
        except Exception as exc:
            log.debug("audio unsubscribe: %s", exc)

    def _pick_live_mic(self, audio: Any) -> str:
        """First mic (in ``MICS`` order) whose energy isn't far below the others; keeps the current
        one when the meters can't be read. Needs a subscription running to have energies."""
        self.mic_energy = {}
        # NAOqi 2.5 has no getter for this switch; nothing else in the server uses it, so it is
        # simply switched off again after the probe.
        try:
            audio.enableEnergyComputation()
            try:
                reads: dict[str, list[float]] = {name: [] for name in MICS}
                for _ in range(PROBE_READS):
                    time.sleep(PROBE_PERIOD_S)
                    for name, (_ch, _idx, getter) in MICS.items():
                        reads[name].append(float(getattr(audio, getter)()))
            finally:
                audio.disableEnergyComputation()
        except Exception as exc:
            log.warning("mic energy probe failed (%s); keeping the %s mic", exc, self.mic)
            return self.mic
        self.mic_energy = {name: round(float(np.median(v)), 1) for name, v in reads.items()}
        # Compare with the median, not the loudest: someone talking right next to one mic during
        # the probe lifts only that mic and must not make the others look dead.
        typical = float(np.median(list(self.mic_energy.values())))
        if typical <= 0:
            log.warning("mic energies all zero; keeping the %s mic", self.mic)
            return self.mic
        dead = [name for name, e in self.mic_energy.items() if e < DEAD_RATIO * typical]
        if dead:
            log.warning("microphone(s) %s look dead (energies %s): Pepper won't hear through them",
                        ", ".join(dead), self.mic_energy)
        return next((name for name in MICS if name not in dead), self.mic)

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
            samples = decode_pcm(buffer, nb_channels, nb_samples, MICS[self.mic][1])
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
        return {"running": self.running, "rate": self.rate, "mic": self.mic,
                "mic_energy": self.mic_energy, "buffers": self.buffers,
                "callbacks_per_s": self.callback_rate(),
                "last_buffer_age_ms": int((time.time() - self.last_buffer_at) * 1000) if self.last_buffer_at else None,
                "level_rms": round(self.last_level, 1)}

    def record(self, seconds: float) -> np.ndarray:
        """Block for ``seconds`` and return the captured mono int16 samples."""
        if not self.running:
            raise RobotError("microphone stream is not running")
        time.sleep(min(10.0, max(0.1, seconds)))
        return self.recent(seconds)


def decode_pcm(buffer: Any, nb_channels: int, nb_samples: int, index: int = 0) -> np.ndarray:
    """Return mono int16 samples; ``index`` picks the channel when the buffer is interleaved."""
    if isinstance(buffer, str):
        raw = buffer.encode("latin-1")
    elif isinstance(buffer, (bytes, bytearray, memoryview)):
        raw = bytes(buffer)
    elif isinstance(buffer, (list, tuple)):
        if buffer and isinstance(buffer[0], int) and all(-32768 <= b <= 32767 for b in buffer[:4]):
            arr = np.asarray(buffer, dtype=np.int16)
            return arr[index::nb_channels] if nb_channels > 1 else arr
        raw = bytes(bytearray(buffer))
    else:
        raw = bytes(buffer)
    arr = np.frombuffer(raw, dtype="<i2")
    if nb_channels > 1 and arr.size >= nb_channels * nb_samples:
        arr = arr[: nb_channels * nb_samples].reshape(nb_samples, nb_channels)[:, index]
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
