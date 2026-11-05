"""The relay that lets Pepper's tablet reach the preview server through the head."""

from __future__ import annotations

import http.server
import socket
import sys
import threading
import time
import urllib.request
from typing import Any

import pytest

from pepper_mcp.robot.connection import RobotError
from pepper_mcp.robot.tablet_relay import RELAY_SCRIPT, TabletRelay
from tests.test_server import _json, connected


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LocalRelay(TabletRelay):
    """Runs the relay script on this machine instead of over SSH on the head."""

    def command(self) -> list[str]:
        return [sys.executable, "-u", "-c", RELAY_SCRIPT, self.listen_ip, str(self.port),
                self.target_host, str(self.target_port)]


@pytest.fixture
def upstream():  # type: ignore[no-untyped-def]
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = f"board {self.path}".encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_relay_forwards_to_the_preview_server_and_exits_with_its_session(upstream: int) -> None:
    relay = LocalRelay("robot", "nao", "pw", "127.0.0.1", _free_port(), "127.0.0.1", upstream)
    base = relay.ensure()
    try:
        for _ in range(3):  # several connections, like the page's polling
            with urllib.request.urlopen(f"{base}/game/tictactoe", timeout=5) as r:
                assert r.read() == b"board /game/tictactoe"
        assert relay.ensure() == base and relay.alive()
    finally:
        relay.stop()
    assert not relay.alive()
    with pytest.raises(OSError):
        urllib.request.urlopen(f"{base}/game/tictactoe", timeout=2)


def test_relay_reports_why_it_did_not_start() -> None:
    class Broken(TabletRelay):
        def command(self) -> list[str]:
            return [sys.executable, "-c", "import sys; sys.stderr.write('Permission denied\\n'); sys.exit(255)"]

    relay = Broken("robot", "nao", "pw", "127.0.0.1", _free_port(), "127.0.0.1", 1)
    with pytest.raises(RobotError, match="Permission denied"):
        relay.ensure(timeout_s=5)
    assert not relay.alive()


def test_dead_relay_restarts_in_the_background(upstream: int) -> None:
    relay = LocalRelay("robot", "nao", "pw", "127.0.0.1", _free_port(), "127.0.0.1", upstream)
    relay.ensure()
    try:
        relay._proc.kill()  # type: ignore[union-attr]  # e.g. the SSH session dropped
        relay._proc.wait()  # type: ignore[union-attr]
        assert relay.url() is None  # returns at once instead of blocking the tool call
        deadline = time.monotonic() + 5
        while relay.url() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert relay.url() == relay.base_url
        with urllib.request.urlopen(f"{relay.base_url}/x", timeout=5) as r:
            assert r.read() == b"board /x"
    finally:
        relay.stop()


def test_failed_relay_is_retried_only_after_a_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    starts: list[float] = []

    class Unreachable(TabletRelay):
        def command(self) -> list[str]:
            starts.append(time.monotonic())
            return [sys.executable, "-c", "import sys; sys.stderr.write('No route to host\\n'); sys.exit(255)"]

    relay = Unreachable("robot", "nao", "pw", "127.0.0.1", _free_port(), "127.0.0.1", 1)
    assert relay.url() is None
    deadline = time.monotonic() + 5
    while (relay._restarting or not starts) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert len(starts) == 1 and relay._failed_at is not None
    assert relay.url() is None and relay.url() is None
    time.sleep(0.2)
    assert len(starts) == 1  # no SSH attempt on every tool call while the robot is unreachable
    monkeypatch.setattr("pepper_mcp.robot.tablet_relay.RETRY_AFTER_S", 0.0)
    relay.url()
    deadline = time.monotonic() + 5
    while len(starts) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert len(starts) == 2


def test_missing_ssh_is_a_robot_error() -> None:
    class NoSsh(TabletRelay):
        def command(self) -> list[str]:
            return ["/nonexistent/ssh"]

    with pytest.raises(RobotError, match=r"needs ssh|No such file"):
        NoSsh("robot", "nao", "pw", "127.0.0.1", _free_port(), "127.0.0.1", 1).ensure(timeout_s=5)


def test_stop_does_not_wait_for_a_hanging_start() -> None:
    class Hanging(TabletRelay):
        def command(self) -> list[str]:
            return [sys.executable, "-c", "import time; time.sleep(30)"]  # ssh stuck connecting

    relay = Hanging("robot", "nao", "pw", "127.0.0.1", _free_port(), "127.0.0.1", 1)
    errors: list[Exception] = []

    def start() -> None:
        try:
            relay.ensure(timeout_s=30)
        except RobotError as exc:
            errors.append(exc)

    t = threading.Thread(target=start)
    t.start()
    time.sleep(0.5)
    assert not relay.alive()  # starting is not up
    t0 = time.monotonic()
    relay.stop()
    t.join(10)
    assert not t.is_alive() and errors and time.monotonic() - t0 < 8
    assert relay.url() is None and not relay._restarting  # stopped for good


def test_ssh_command_runs_the_relay_on_the_head() -> None:
    cmd = TabletRelay("192.168.178.86", "nao", "secret", "198.18.0.1", 8765, "192.168.178.84", 8765).command()
    assert cmd[0] == "ssh" and "nao@192.168.178.86" in cmd and "secret" not in " ".join(cmd)
    assert cmd[-1].startswith("python -u -c") and cmd[-1].endswith("198.18.0.1 8765 192.168.178.84 8765")


class _Preview:
    port = 8765
    base_url = "http://192.168.178.84:8765"


class _Relay:
    base_url = "http://198.18.0.1:8765"

    def __init__(self, up: bool = True):
        self.up = up

    def url(self) -> str | None:
        return self.base_url if self.up else None


async def test_board_goes_through_the_head_when_the_tablet_is_off_the_lan() -> None:
    # On the robot the tablet's Wi-Fi is DISCONNECTED and the head does not forward its network,
    # so the preview server's LAN URL showed a blank white page.
    async with connected() as client:
        rt = client.rt  # type: ignore[attr-defined]
        tablet = client.robot.services["ALTabletService"]  # type: ignore[attr-defined]
        tablet.returns["getWifiStatus"] = "DISCONNECTED"
        rt.preview = _Preview()

        st = _json(await client.call_tool("tictactoe_start", {}))
        assert st["display"] == "static"  # never the unreachable LAN URL

        rt.tablet_relay = _Relay()
        st = _json(await client.call_tool("tictactoe_start", {}))
        assert st["display"] == "interactive"
        assert rt.tablet_base_url() == "http://198.18.0.1:8765"

        assert rt.assets_url("67.html") == "http://198.18.0.1:8765/assets/67.html"

        rt.tablet_relay = _Relay(up=False)  # died: no blocking restart on the tool path
        assert rt.tablet_base_url() is None
        tablet.returns["getWifiStatus"] = "CONNECTED"  # the tablet's own Wi-Fi reaches the laptop
        assert rt.tablet_base_url() == "http://192.168.178.84:8765"
        rt.tablet_relay = _Relay()
        assert rt.assets_url("67.html") == "http://198.18.0.1:8765/assets/67.html"  # not stuck on the old URL
        rt.preview = None
