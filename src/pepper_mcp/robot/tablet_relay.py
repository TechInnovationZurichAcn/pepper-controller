"""Let Pepper's tablet reach the laptop's preview server through the robot's head.

The tablet is a separate Android device on the head's internal USB network (198.18.0.0/15). The
head does not forward that network (``ip_forward`` is 0), and the tablet's own Wi-Fi is usually not
set up, so a page served by the laptop loads as a blank white page. The head itself is on the LAN,
so a tiny TCP relay runs on it, listening on the head's tablet-side address and forwarding to the
laptop. The tablet then loads ``http://198.18.0.1:<port>/...``.

The relay is started over SSH (Python 2.7 is on the head, no root needed) and exits when that SSH
session ends: it blocks on stdin, which closes when this process stops or dies.
"""

from __future__ import annotations

import base64
import collections
import contextlib
import logging
import shlex
import subprocess
import threading
import time
from typing import IO

from pepper_mcp.robot.connection import RobotError
from pepper_mcp.robot.ssh import SSH_OPTS, askpass_env, detached

log = logging.getLogger(__name__)

START_TIMEOUT_S = 15.0
#: after a failed start, wait this long before trying again (the robot is probably unreachable)
RETRY_AFTER_S = 30.0

#: Runs on the head (Python 2.7) as well as on Python 3: argv = listen_ip listen_port target_ip target_port.
RELAY_SCRIPT = r'''
import socket, sys, threading

LISTEN = (sys.argv[1], int(sys.argv[2]))
TARGET = (sys.argv[3], int(sys.argv[4]))


def pipe(src, dst, done):
    # Clean EOF: pass it on, and whichever direction ends second closes both sockets.
    # Error: close both at once, which also wakes the other direction.
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
        dst.shutdown(socket.SHUT_WR)
        done.append(1)
        if len(done) < 2:
            return
    except Exception:
        pass
    for s in (src, dst):
        try:
            s.close()
        except Exception:
            pass


def handle(client):
    try:
        upstream = socket.create_connection(TARGET, 5)
    except Exception:
        client.close()
        return
    done = []
    t = threading.Thread(target=pipe, args=(upstream, client, done))
    t.daemon = True
    t.start()
    pipe(client, upstream, done)


def serve(srv):
    while True:
        client, _ = srv.accept()
        t = threading.Thread(target=handle, args=(client,))
        t.daemon = True
        t.start()


srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(LISTEN)
srv.listen(32)
t = threading.Thread(target=serve, args=(srv,))
t.daemon = True
t.start()
sys.stdout.write("ready\n")
sys.stdout.flush()
sys.stdin.read()
'''


class TabletRelay:
    """``listen_ip:port`` on the head → ``target_host:target_port`` on the laptop."""

    def __init__(self, robot_ip: str, ssh_user: str, ssh_password: str, listen_ip: str, port: int,
                 target_host: str, target_port: int):
        self.robot_ip = robot_ip
        self.ssh_user = ssh_user
        self.ssh_password = ssh_password
        self.listen_ip = listen_ip
        self.port = port
        self.target_host = target_host
        self.target_port = target_port
        self._proc: subprocess.Popen[bytes] | None = None
        self._ready = False  # the relay printed "ready" (listening on the head)
        self._closed = False
        self._stderr: collections.deque[str] = collections.deque(maxlen=20)
        self._lock = threading.Lock()  # serialises starts; stop() never waits on it
        self._restarting = False
        self._flag_lock = threading.Lock()
        self._failed_at: float | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.listen_ip}:{self.port}"

    def command(self) -> list[str]:
        script = base64.b64encode(RELAY_SCRIPT.encode()).decode()
        remote = (f"python -u -c \"import base64;exec(base64.b64decode('{script}'))\" "
                  + " ".join(shlex.quote(str(a)) for a in
                             (self.listen_ip, self.port, self.target_host, self.target_port)))
        # Keepalives make ssh exit when the robot drops off the network, so alive() notices.
        return ["ssh", *SSH_OPTS, "-T", "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3",
                f"{self.ssh_user}@{self.robot_ip}", remote]

    def alive(self) -> bool:
        proc = self._proc
        return self._ready and proc is not None and proc.poll() is None

    def ensure(self, timeout_s: float = START_TIMEOUT_S) -> str:
        """Start the relay unless it is running (blocks up to ``timeout_s``); returns its URL base."""
        with self._lock:
            if not self.alive():
                try:
                    self._start(timeout_s)
                except RobotError:
                    self._failed_at = time.monotonic()
                    raise
                self._failed_at = None
        return self.base_url

    def url(self) -> str | None:
        """The URL base if the relay is up, else ``None`` at once while it restarts in the background.

        Never blocks: tool calls pause the Gemini conversation until they return, and a start can
        take seconds (or time out) when the robot is unreachable.
        """
        if self.alive():
            return self.base_url
        with self._flag_lock:  # short guard, never the start lock
            failed = self._failed_at
            if not self._closed and not self._restarting and (
                    failed is None or time.monotonic() - failed >= RETRY_AFTER_S):
                self._restarting = True
                threading.Thread(target=self._restart, name="tablet-relay-start", daemon=True).start()
        return None

    def _restart(self) -> None:
        try:
            self.ensure()
        except RobotError as exc:
            log.warning("%s (retrying in %.0f s)", exc, RETRY_AFTER_S)
        finally:
            with self._flag_lock:
                self._restarting = False

    def _start(self, timeout_s: float) -> None:
        if self._closed:
            raise RobotError("tablet relay is stopped")
        self._kill(self._proc)
        self._proc, self._ready = None, False
        self._stderr.clear()
        settled = threading.Event()  # "ready" was printed, or the process ended
        ready: list[bool] = []
        with askpass_env(self.ssh_password) as env:
            try:
                proc = subprocess.Popen(detached(self.command()), env=env, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            except OSError as exc:  # ssh or setsid missing
                raise RobotError(f"tablet relay needs ssh: {exc}") from exc
            self._proc = proc  # so stop() can kill it while we wait
            threading.Thread(target=self._read_stdout, args=(proc.stdout, settled, ready), name="tablet-relay-out",
                             daemon=True).start()
            threading.Thread(target=self._read_stderr, args=(proc.stderr,), name="tablet-relay-err",
                             daemon=True).start()
            # The askpass script must outlive authentication, so wait inside the context.
            settled.wait(timeout_s)
        if not ready or proc.poll() is not None or self._closed:
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(1)  # let the stderr reader catch ssh's last words
            detail = " | ".join(self._stderr) or ("timed out" if proc.returncode is None
                                                  else f"exit code {proc.returncode}")
            self._kill(proc)
            self._proc = None
            raise RobotError(f"tablet relay on the robot did not start: {detail}")
        self._ready = True
        log.info("tablet relay: %s -> http://%s:%d", self.base_url, self.target_host, self.target_port)

    def _read_stdout(self, out: IO[bytes] | None, settled: threading.Event, ready: list[bool]) -> None:
        if out is not None:
            for line in out:
                if line.strip() == b"ready":
                    ready.append(True)
                    settled.set()
        settled.set()

    def _read_stderr(self, err: IO[bytes] | None) -> None:
        if err is None:
            return
        for line in err:
            text = line.decode(errors="replace").strip()
            if text:
                self._stderr.append(text)

    def stop(self) -> None:
        """Stop for good. Does not wait for a start in progress: it kills that process, so the
        start fails at once instead of holding up shutdown."""
        self._closed = True
        self._ready = False
        self._kill(self._proc)

    @staticmethod
    def _kill(proc: subprocess.Popen[bytes] | None) -> None:
        if proc is None or proc.poll() is not None:
            return
        # Closing stdin ends the remote `sys.stdin.read()`, so the relay on the head exits too.
        with contextlib.suppress(OSError):
            if proc.stdin:
                proc.stdin.close()
        try:
            proc.wait(1.5)  # ssh exits within a fraction of a second once its stdin closes
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(2)
            except subprocess.TimeoutExpired:
                proc.kill()
