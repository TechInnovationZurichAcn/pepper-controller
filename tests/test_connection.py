from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from pepper_mcp.robot import connection
from pepper_mcp.robot.connection import RobotConnection, RobotError


class _FakeQi:
    """Mimics the libqi rules that matter here: a Session may be created and fail any number of
    times, but only one Application may ever exist per process."""

    def __init__(self, fail_connects: int, booting: list[list[str]] | None = None) -> None:
        self.fail_connects = fail_connects
        self.connect_attempts = 0
        self.apps = 0
        #: services() answers, one per call, the last one repeating (NAOqi registering as it boots)
        self.booting = booting or [list(connection.BOOT_SERVICES)]
        qi = self

        class Session:
            def connect(self, url: str) -> None:
                qi.connect_attempts += 1
                if qi.connect_attempts <= qi.fail_connects:
                    raise RuntimeError("Connect error: 65: No route to host")

            def close(self) -> None:
                pass

            def isConnected(self) -> bool:
                return True

            def endpoints(self) -> list[str]:
                return ["tcp://192.168.178.87:50000"]

            def services(self) -> list[dict[str, str]]:
                names = qi.booting.pop(0) if len(qi.booting) > 1 else qi.booting[0]
                return [{"name": n} for n in names]

            @property
            def disconnected(self) -> Any:
                return types.SimpleNamespace(connect=lambda cb: None)

        class Application:
            def __init__(self, args: list[str]) -> None:
                if qi.apps:
                    raise RuntimeError("Application was already initialized")
                qi.apps += 1
                self.session = Session()

            def start(self) -> None:
                self.session.connect("")

        self.module = types.SimpleNamespace(Session=Session, Application=Application)


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(connection.time, "sleep", lambda s: None)


def test_connect_retries_until_naoqi_answers(monkeypatch: pytest.MonkeyPatch, no_sleep: None) -> None:
    # Pepper still booting / macOS Local Network prompt pending: the first connects fail. The
    # retry must not create a second qi.Application, which libqi refuses.
    fake = _FakeQi(fail_connects=2)
    monkeypatch.setitem(sys.modules, "qi", fake.module)

    conn = RobotConnection("192.168.178.86")
    conn.connect(timeout_s=60)

    assert conn.is_connected()
    assert fake.apps == 1


def test_connect_reports_the_real_error(monkeypatch: pytest.MonkeyPatch, no_sleep: None) -> None:
    fake = _FakeQi(fail_connects=10**6)
    monkeypatch.setitem(sys.modules, "qi", fake.module)
    clock = iter(range(0, 1000, 5))
    monkeypatch.setattr(connection.time, "monotonic", lambda: next(clock))

    with pytest.raises(RobotError) as exc_info:
        RobotConnection("192.168.178.86").connect(timeout_s=10)

    assert "No route to host" in str(exc_info.value)
    assert "already initialized" not in str(exc_info.value)


def test_connect_waits_until_naoqi_has_finished_starting(monkeypatch: pytest.MonkeyPatch, no_sleep: None) -> None:
    # On the Mac mini right after Pepper restarted, NAOqi answered with only some services
    # registered; the server cached ALTextToSpeech/ALAudioDevice/ALVideoDevice as missing for good
    # and pepper-mcp-gemini exited with "Pepper's microphone is not available".
    early = ["ALMemory", "ALMotion", "ALRobotPosture"]
    fake = _FakeQi(fail_connects=0, booting=[early, early, early + ["ALTextToSpeech"], list(connection.BOOT_SERVICES)])
    monkeypatch.setitem(sys.modules, "qi", fake.module)

    conn = RobotConnection("192.168.178.86")
    conn.connect(timeout_s=60)

    assert fake.booting == [list(connection.BOOT_SERVICES)]  # every boot stage was waited out
    assert conn.wait_until_booted() == []


def test_boot_wait_gives_up_and_names_what_is_missing(monkeypatch: pytest.MonkeyPatch, no_sleep: None) -> None:
    fake = _FakeQi(fail_connects=0, booting=[[s for s in connection.BOOT_SERVICES if s != "ALTabletService"]])
    monkeypatch.setitem(sys.modules, "qi", fake.module)
    conn = RobotConnection("192.168.178.86")
    conn.connect(timeout_s=60, boot_timeout_s=0)
    clock = iter(range(0, 1000, 5))
    monkeypatch.setattr(connection.time, "monotonic", lambda: next(clock))

    assert conn.wait_until_booted(timeout_s=10) == ["ALTabletService"]
