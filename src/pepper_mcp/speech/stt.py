"""Speech-to-text backends behind one tiny interface.

* ``FasterWhisperSTT`` – local, offline (CTranslate2). ``small`` gives a good
  speed/accuracy trade-off on a laptop CPU; ``tiny``/``base`` are faster.
* ``OpenAICompatibleSTT`` – POSTs a WAV to any ``/v1/audio/transcriptions`` endpoint
  (OpenAI, Groq, a local whisper server) using only the standard library.
* ``NullSTT`` – for tests / when hearing is disabled.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np

from pepper_mcp.robot.audio_in import pcm_to_wav

log = logging.getLogger(__name__)


@dataclass
class Transcript:
    text: str
    language: str | None = None
    confidence: float | None = None
    latency_s: float = 0.0
    backend: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "language": self.language, "confidence": self.confidence,
                "stt_latency_ms": int(self.latency_s * 1000), "backend": self.backend}


class SpeechToText(Protocol):
    name: str

    def transcribe(self, samples: np.ndarray, rate: int) -> Transcript: ...

    def warm_up(self) -> None: ...


class NullSTT:
    name = "none"

    def transcribe(self, samples: np.ndarray, rate: int) -> Transcript:
        return Transcript(text="", backend=self.name)

    def warm_up(self) -> None:
        return None


class FasterWhisperSTT:
    name = "faster-whisper"

    def __init__(self, model_size: str = "small", device: str = "auto", compute_type: str = "default",
                 language: str | None = "en", download_root: str | None = None):
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.language = language or None
        self.download_root = download_root
        self._model = None

    def _load(self) -> Any:
        if self._model is None:
            from faster_whisper import WhisperModel

            t0 = time.monotonic()
            self._model = WhisperModel(self.model_size, device=self.device, compute_type=self.compute_type,
                                       download_root=self.download_root)
            log.info("faster-whisper %s loaded in %.1fs", self.model_size, time.monotonic() - t0)
        return self._model

    def warm_up(self) -> None:
        model = self._load()
        try:
            model.transcribe(np.zeros(16000, dtype=np.float32), language=self.language, beam_size=1)
        except Exception as exc:
            log.debug("warm-up transcribe: %s", exc)

    def transcribe(self, samples: np.ndarray, rate: int) -> Transcript:
        model = self._load()
        audio = samples.astype(np.float32) / 32768.0
        if rate != 16000:
            audio = _resample(audio, rate, 16000)
        t0 = time.monotonic()
        segments, info = model.transcribe(audio, language=self.language, beam_size=1, best_of=1,
                                          vad_filter=False, condition_on_previous_text=False,
                                          without_timestamps=True)
        texts, probs = [], []
        for seg in segments:
            texts.append(seg.text.strip())
            if getattr(seg, "avg_logprob", None) is not None:
                probs.append(float(np.exp(seg.avg_logprob)))
        text = " ".join(t for t in texts if t)
        return Transcript(text=text, language=getattr(info, "language", self.language),
                          confidence=round(sum(probs) / len(probs), 2) if probs else None,
                          latency_s=time.monotonic() - t0, backend=self.name)


class OpenAICompatibleSTT:
    name = "openai"

    def __init__(self, url: str, api_key: str, model: str = "whisper-1", language: str | None = "en",
                 timeout_s: float = 20.0):
        self.url = url
        self.api_key = api_key
        self.model = model if model and not model.startswith(("tiny", "base", "small", "medium", "large")) else "whisper-1"
        self.language = language or None
        self.timeout_s = timeout_s

    def warm_up(self) -> None:
        return None

    def transcribe(self, samples: np.ndarray, rate: int) -> Transcript:
        wav = pcm_to_wav(samples, rate)
        boundary = uuid.uuid4().hex
        parts = [f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n{self.model}\r\n"]
        if self.language:
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"language\"\r\n\r\n{self.language}\r\n")
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"response_format\"\r\n\r\njson\r\n")
        body = "".join(parts).encode() + (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"speech.wav\"\r\n"
            f"Content-Type: audio/wav\r\n\r\n").encode() + wav + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(self.url, data=body, method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        if self.api_key:
            req.add_header("Authorization", f"Bearer {self.api_key}")
        t0 = time.monotonic()
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return Transcript(text=str(payload.get("text", "")).strip(), language=payload.get("language", self.language),
                          latency_s=time.monotonic() - t0, backend=self.name)


def _resample(audio: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst or audio.size == 0:
        return audio
    n = int(audio.size * dst / src)
    x_old = np.linspace(0.0, 1.0, audio.size, endpoint=False)
    x_new = np.linspace(0.0, 1.0, n, endpoint=False)
    return np.interp(x_new, x_old, audio).astype(np.float32)


def make_stt(backend: str, model: str = "small", device: str = "auto", compute_type: str = "default",
             language: str | None = "en", url: str = "", api_key: str = "",
             download_root: str | None = None) -> SpeechToText:
    if backend == "faster-whisper":
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            log.warning("faster-whisper not installed (pip install 'pepper-mcp[speech]'); hearing disabled")
            return NullSTT()
        return FasterWhisperSTT(model, device, compute_type, language, download_root)
    if backend == "openai":
        if not api_key:
            log.warning("PEPPER_STT_API_KEY not set; hearing disabled")
            return NullSTT()
        return OpenAICompatibleSTT(url, api_key, model, language)
    return NullSTT()
