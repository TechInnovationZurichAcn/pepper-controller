from __future__ import annotations

import os

import pytest

from pepper_mcp import verify
from pepper_mcp.robot.fake import FakeConnection, FakeRobot


class _Exited(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


def test_verify_skips_libqi_teardown_on_exit(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    # libqi's native teardown at interpreter exit can deadlock (seen on macOS after all checks
    # passed), so verify must leave through os._exit like pepper-mcp-gemini does.
    def fake_exit(code: int) -> None:
        raise _Exited(code)

    monkeypatch.setattr(verify, "RobotConnection", lambda ip, port: FakeConnection(FakeRobot()))
    monkeypatch.setattr(os, "_exit", fake_exit)

    with pytest.raises(_Exited) as exc_info:
        verify.main(["--ip", "fake", "--no-move", "--quiet"])

    assert exc_info.value.code in (0, 1)
    assert "checks passed" in capsys.readouterr().out
