"""Runtime settings.

Values come from environment variables (``PEPPER_*``) and are overridden by CLI flags
(see :mod:`pepper_mcp.cli`). Everything the server can tune lives here so that a new
knob is one dataclass field plus one ``--flag``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(f"PEPPER_{name}", default)


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    return int(raw) if raw not in (None, "") else default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    return float(raw) if raw not in (None, "") else default


#: PEPPER_* variables of removed features (server-side STT, standalone MCP transports); a stale
#: .env that still sets them gets a warning instead of being ignored without a word.
REMOVED_ENV = ("STT", "STT_MODEL", "STT_DEVICE", "STT_COMPUTE", "STT_LANGUAGE", "STT_URL", "STT_API_KEY",
               "VAD_LEVEL", "VAD_END_SILENCE_MS", "VAD_MIN_SPEECH_MS", "VAD_MAX_S", "LISTENING", "BARGE_IN",
               "ECHO_TAIL_MS", "MCP_TRANSPORT", "MCP_HOST", "MCP_PORT")


def removed_env_vars() -> list[str]:
    return [f"PEPPER_{name}" for name in REMOVED_ENV if f"PEPPER_{name}" in os.environ]


@dataclass
class Settings:
    # --- robot connection -------------------------------------------------
    ip: str | None = field(default_factory=lambda: _env("IP"))
    port: int = field(default_factory=lambda: _env_int("PORT", 9559))
    fake: bool = field(default_factory=lambda: _env_bool("FAKE", False))
    #: address the laptop listens on for callbacks from the robot (audio, signals)
    listen_url: str = field(default_factory=lambda: _env("LISTEN_URL", "tcp://0.0.0.0:0") or "")
    ssh_user: str = field(default_factory=lambda: _env("SSH_USER", "nao") or "nao")
    ssh_password: str = field(default_factory=lambda: _env("SSH_PASSWORD", "pepper") or "pepper")

    # --- dashboard -------------------------------------------------------
    #: optional HTTP side channel (MJPEG, live audio, SSE events); 0 disables it
    preview_port: int = field(default_factory=lambda: _env_int("PREVIEW_PORT", 0))
    preview_host: str = field(default_factory=lambda: _env("PREVIEW_HOST", "0.0.0.0") or "0.0.0.0")
    #: dashboard controls are always allowed from this machine; other devices (phone, tablet) must
    #: send this token (open ``/?token=...``). Empty = controls are local-only, viewing stays open.
    preview_token: str = field(default_factory=lambda: _env("PREVIEW_TOKEN", "") or "")

    # --- robot behaviour on startup --------------------------------------
    #: "disable" puts autonomous life off so our motions are not fought; "keep" leaves it
    autonomous_life: str = field(default_factory=lambda: _env("AUTONOMOUS_LIFE", "disable") or "")
    #: liveliness preset applied at start: off | calm | lively
    liveliness: str = field(default_factory=lambda: _env("LIVELINESS", "calm") or "calm")
    wake_up: bool = field(default_factory=lambda: _env_bool("WAKE_UP", True))
    language: str = field(default_factory=lambda: _env("LANGUAGE", "English") or "English")
    #: base safety bubble in metres. NAOqi's 0.4 m front bubble blocks almost every move indoors
    #: (anyone or anything within 40 cm aborts it with ALMotion/MoveFailed "Safety"); the old
    #: teleop drove fine with 0.1/0.05. Restored to NAOqi's defaults on a clean shutdown; a killed
    #: server leaves the robot at these values until the next start or a reboot.
    safety_front_m: float = field(default_factory=lambda: _env_float("SAFETY_FRONT_M", 0.15))
    safety_side_m: float = field(default_factory=lambda: _env_float("SAFETY_SIDE_M", 0.05))

    # --- camera -----------------------------------------------------------
    camera_enabled: bool = field(default_factory=lambda: _env_bool("CAMERA", True))
    camera_fps: int = field(default_factory=lambda: _env_int("CAMERA_FPS", 10))
    camera_resolution: str = field(default_factory=lambda: _env("CAMERA_RES", "qvga") or "qvga")
    jpeg_quality: int = field(default_factory=lambda: _env_int("JPEG_QUALITY", 80))

    # --- audio / speech ---------------------------------------------------
    audio_enabled: bool = field(default_factory=lambda: _env_bool("AUDIO", True))
    audio_rate: int = field(default_factory=lambda: _env_int("AUDIO_RATE", 16000))
    #: auto | front | left | right | rear  (auto = first mic that isn't dead, front preferred)
    mic: str = field(default_factory=lambda: _env("MIC", "auto") or "auto")

    # --- perception / reflexes -------------------------------------------
    perception_enabled: bool = field(default_factory=lambda: _env_bool("PERCEPTION", True))
    memory_poll_s: float = field(default_factory=lambda: _env_float("MEMORY_POLL_S", 0.1))
    reflexes_enabled: bool = field(default_factory=lambda: _env_bool("REFLEXES", True))
    #: comma separated reflex names to start disabled, e.g. "greet,touch_reaction"
    reflexes_off: str = field(default_factory=lambda: _env("REFLEXES_OFF", "") or "")
    hand_raise_vision: bool = field(default_factory=lambda: _env_bool("HAND_RAISE_VISION", False))

    # --- misc -------------------------------------------------------------
    map_store: str = field(default_factory=lambda: _env("MAP_STORE", "pepper_maps.json") or "")
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO") or "INFO")

    def validate(self) -> None:
        if not self.fake and not self.ip:
            raise ValueError("robot IP required: pass --ip <PEPPER_IP>, set PEPPER_IP, or use --fake")
        if self.camera_resolution not in ("qvga", "vga", "qqvga"):
            raise ValueError("camera resolution must be qqvga, qvga or vga")
        if self.audio_rate not in (16000, 48000):
            raise ValueError("audio rate must be 16000 (one mic) or 48000 (4 channels)")
        if self.mic not in ("auto", "front", "left", "right", "rear"):
            raise ValueError("mic must be auto, front, left, right or rear")

    def apply_overrides(self, overrides: dict[str, object]) -> Settings:
        names = {f.name for f in fields(self)}
        for key, value in overrides.items():
            if value is None or key not in names:
                continue
            setattr(self, key, value)
        return self

    def summary(self) -> dict[str, object]:
        data = {f.name: getattr(self, f.name) for f in fields(self)}
        if data.get("preview_token"):
            data["preview_token"] = "***"
        data["ssh_password"] = "***"
        return data
