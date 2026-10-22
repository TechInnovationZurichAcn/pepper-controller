from __future__ import annotations

import pytest

from pepper_mcp.config import Settings
from pepper_mcp.robot.fake import FakeConnection, FakeRobot
from pepper_mcp.runtime import Runtime


def make_settings(**overrides) -> Settings:  # type: ignore[no-untyped-def]
    s = Settings(fake=True, stt_backend="none", preview_port=0, memory_poll_s=0.03)
    s.apply_overrides(overrides)
    s.validate()
    return s


@pytest.fixture
def fake_robot() -> FakeRobot:
    return FakeRobot()


@pytest.fixture
def runtime(fake_robot: FakeRobot):  # type: ignore[no-untyped-def]
    rt = Runtime(make_settings(), FakeConnection(fake_robot))
    rt.start()
    try:
        yield rt
    finally:
        rt.stop()
