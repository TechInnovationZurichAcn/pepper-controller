"""Speaker output: ALAudioPlayer / ALAudioDevice volume, uploading sound files and streaming
raw PCM (e.g. a cloud voice) to the loudspeakers."""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import threading
import time
from typing import Any

import numpy as np

from pepper_mcp.robot.connection import RobotConnection, RobotError

log = logging.getLogger(__name__)

ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets")


class AudioOut:
    def __init__(self, conn: RobotConnection, ssh_user: str = "nao", ssh_password: str = "pepper"):
        self.conn = conn
        self.ssh_user = ssh_user
        self.ssh_password = ssh_password
        self._uploaded: dict[str, str] = {}
        self._lock = threading.Lock()
        self._loop_thread: threading.Thread | None = None

    @property
    def player(self) -> Any:
        return self.conn.service("ALAudioPlayer")

    @property
    def device(self) -> Any:
        return self.conn.service("ALAudioDevice")

    # ------------------------------------------------------------- volume
    def get_volume(self) -> int:
        return int(self.device.getOutputVolume())

    def set_volume(self, level: int) -> int:
        level = int(max(0, min(100, level)))
        self.device.setOutputVolume(level)
        return level

    # ------------------------------------------------------------- playback
    def play_async(self, source: str, volume: float | None = None, loop: bool = False) -> Any:
        """Return a future for a file on the robot or an http(s) stream."""
        player = self.player
        if source.startswith(("http://", "https://")):
            return RobotConnection.call_async(player, "playWebStream", source,
                                              1.0 if volume is None else float(volume), 0.0)
        if loop:
            return RobotConnection.call_async(player, "playFileInLoop", source)
        if volume is not None:
            return RobotConnection.call_async(player, "playFile", source, float(volume), 0.0)
        return RobotConnection.call_async(player, "playFile", source)

    def play(self, source: str, loop: bool = False) -> None:
        """Fire-and-forget (used by procedural gestures)."""
        self.play_async(source, loop=loop)

    def play_sine(self, frequency_hz: int, duration_s: float, volume: int = 50) -> None:
        self.player.playSine(int(frequency_hz), int(max(0, min(100, volume))), 0, float(duration_s))

    def stop(self) -> None:
        try:
            self.player.stopAll()
        except Exception as exc:
            log.debug("stopAll audio: %s", exc)

    def speaker_stream(self, input_rate: int) -> SpeakerStream:
        """Live PCM output at ``input_rate`` (16-bit mono) through the loudspeakers."""
        return SpeakerStream(self.device, input_rate)

    # ------------------------------------------------------------- upload
    def robot_asset_path(self, filename: str) -> str | None:
        """Robot-side path of a bundled asset, uploading it on first use (needs SSH)."""
        with self._lock:
            if filename in self._uploaded:
                return self._uploaded[filename]
        local = os.path.join(ASSETS_DIR, filename)
        if not os.path.exists(local):
            return None
        try:
            remote = self.upload(local)
        except RobotError as exc:
            log.info("asset upload failed (%s); %s unavailable on the robot", exc, filename)
            return None
        return remote

    def _ssh_run(self, cmd: list[str], timeout_s: float) -> None:
        """Run scp/ssh non-interactively with the robot password via SSH_ASKPASS."""
        askpass = tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False)
        askpass.write(f"#!/bin/sh\necho '{self.ssh_password}'\n")
        askpass.close()
        os.chmod(askpass.name, 0o700)
        env = dict(os.environ, SSH_ASKPASS=askpass.name, SSH_ASKPASS_REQUIRE="force")
        env.setdefault("DISPLAY", ":0")
        if os.name == "posix":
            cmd = ["setsid", "-w", *cmd]
        try:
            subprocess.run(cmd, env=env, check=True, timeout=timeout_s, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except Exception as exc:
            raise RobotError(f"ssh/scp failed: {exc}") from exc
        finally:
            try:
                os.unlink(askpass.name)
            except OSError:
                pass

    SSH_OPTS = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=10"]

    def upload(self, local_path: str, remote_name: str | None = None, remote_dir: str | None = None,
               timeout_s: float = 120) -> str:
        """scp a file to the robot. Default directory is the nao home; pass ``remote_dir`` to
        put it e.g. under the robot web root (``~/.local/share/PackageManager/apps/<app>/html``)."""
        if not os.path.exists(local_path):
            raise RobotError(f"file not found: {local_path}")
        remote_name = remote_name or os.path.basename(local_path)
        remote_dir = remote_dir or f"/home/{self.ssh_user}"
        target = f"{self.ssh_user}@{self.conn.ip}"
        if remote_dir != f"/home/{self.ssh_user}":
            self._ssh_run(["ssh", *self.SSH_OPTS, target, f"mkdir -p {remote_dir}"], 30)
        remote = f"{remote_dir.rstrip('/')}/{remote_name}"
        self._ssh_run(["scp", *self.SSH_OPTS, local_path, f"{target}:{remote}"], timeout_s)
        with self._lock:
            self._uploaded[os.path.basename(local_path)] = remote
        return remote

    def deploy_web_asset(self, filename: str, app: str = "pepper_mcp") -> str | None:
        """Upload a bundled asset to the robot's own web server; returns its http URL."""
        local = os.path.join(ASSETS_DIR, filename)
        if not os.path.exists(local):
            return None
        try:
            self.upload(local, remote_dir=f"/home/{self.ssh_user}/.local/share/PackageManager/apps/{app}/html")
        except RobotError as exc:
            log.info("web asset deploy failed: %s", exc)
            return None
        return f"http://{self.conn.ip}/apps/{app}/{filename}"


class SpeakerStream:
    """Plays a live stream of 16-bit mono PCM on the loudspeakers (ALAudioDevice remote output).

    ``sendRemoteBufferToOutput`` wants interleaved stereo at the output rate (48 kHz) and returns
    at once instead of at playback speed, so ``write`` paces itself and stays at most ``LEAD_S``
    ahead of what the robot plays: that bounds the audio still queued on the robot when the stream
    is cut (barge-in, stop), and keeps ``remaining()`` an honest "still audible" estimate.
    """

    LEAD_S = 0.15
    MAX_FRAMES = 4800  # 100 ms per call at 48 kHz, well under NAOqi's 16384-frame limit

    def __init__(self, device: Any, input_rate: int):
        self.device = device
        self.input_rate = input_rate
        self.rate = int(device.getParameter("outputSampleRate") or 48000)
        if self.rate % input_rate:
            raise RobotError(f"output rate {self.rate} Hz is not a multiple of {input_rate} Hz")
        self._last = 0.0  # previous input sample, keeps interpolation continuous across chunks
        self._play_until = 0.0
        self._generation = 0  # bumped by cancel(); a write in progress stops at its next block
        self._lock = threading.Lock()

    def cancel(self) -> None:
        """Stop sending the chunk being written (barge-in, reconnect). What the robot already
        has (at most ``LEAD_S`` plus one block) still plays out."""
        self._generation += 1

    def remaining(self) -> float:
        """Seconds of already-sent audio the robot is still playing."""
        return max(0.0, self._play_until - time.monotonic())

    def convert(self, pcm: bytes) -> np.ndarray:
        """Mono ``input_rate`` PCM → interleaved stereo int16 at the output rate (linear upsampling)."""
        x = np.frombuffer(pcm[:len(pcm) - len(pcm) % 2], "<i2").astype(np.float32)
        if not len(x):
            return np.zeros(0, np.int16)
        factor = self.rate // self.input_rate
        prev = np.concatenate(([self._last], x[:-1])).astype(np.float32)
        self._last = float(x[-1])
        steps = np.arange(1, factor + 1, dtype=np.float32) / factor
        up = (prev[:, None] + (x - prev)[:, None] * steps).ravel()
        return np.repeat(np.round(up).astype(np.int16), 2)

    def write(self, pcm: bytes) -> None:
        """Queue ``pcm`` on the robot; blocks just enough to stay ``LEAD_S`` ahead of playback."""
        generation = self._generation
        with self._lock:
            stereo = self.convert(pcm)
            for i in range(0, len(stereo) // 2, self.MAX_FRAMES):
                if self._generation != generation:
                    return
                block = stereo[2 * i:2 * (i + self.MAX_FRAMES)]
                ahead = self.remaining()
                if ahead > self.LEAD_S:
                    time.sleep(ahead - self.LEAD_S)
                    if self._generation != generation:
                        return
                frames = len(block) // 2
                self.device.sendRemoteBufferToOutput(frames, block.tobytes())
                self._play_until = max(self._play_until, time.monotonic()) + frames / self.rate
