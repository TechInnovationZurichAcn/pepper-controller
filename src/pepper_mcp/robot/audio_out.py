"""Speaker output: ALAudioPlayer / ALAudioDevice volume + uploading sound files."""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import threading
from typing import Any

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
