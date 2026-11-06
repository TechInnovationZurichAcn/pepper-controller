"""ssh/scp helpers must work on macOS, which has no util-linux ``setsid`` command."""

from __future__ import annotations

import os
import sys

import pytest

from pepper_mcp.robot.audio_out import AudioOut
from pepper_mcp.robot.connection import RobotError
from pepper_mcp.robot.fake import FakeConnection, FakeRobot
from pepper_mcp.robot.tablet_relay import TabletRelay

# Without a controlling terminal ssh reads the password from SSH_ASKPASS instead of the tty.
SESSION_LEADER = "import os, sys; sys.exit(0 if os.getsid(0) == os.getpid() else 3)"


@pytest.fixture
def no_setsid_on_path(monkeypatch: pytest.MonkeyPatch, tmp_path: os.PathLike[str]) -> None:
    """PATH as on macOS: no ``setsid`` binary to wrap commands with."""
    monkeypatch.setenv("PATH", str(tmp_path))


def test_ssh_run_detaches_without_setsid_binary(no_setsid_on_path: None) -> None:
    AudioOut(FakeConnection(FakeRobot()))._ssh_run([sys.executable, "-c", SESSION_LEADER], 10)


def test_ssh_run_failure_is_a_robot_error(no_setsid_on_path: None) -> None:
    with pytest.raises(RobotError, match="ssh/scp failed"):
        AudioOut(FakeConnection(FakeRobot()))._ssh_run([sys.executable, "-c", "raise SystemExit(1)"], 10)


def test_tablet_relay_detaches_without_setsid_binary(no_setsid_on_path: None) -> None:
    class Local(TabletRelay):
        def command(self) -> list[str]:  # says "ready" like the relay, but only as session leader
            return [sys.executable, "-u", "-c",
                    "import os, sys\nif os.getsid(0) == os.getpid(): print('ready')\nsys.stdin.read()"]

    relay = Local("robot", "nao", "pw", "127.0.0.1", 1, "127.0.0.1", 1)
    try:
        assert relay.ensure(timeout_s=10)
    finally:
        relay.stop()
