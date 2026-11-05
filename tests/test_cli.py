"""Process-level behaviour of the ``pepper-mcp`` entry point."""

from __future__ import annotations

import signal
import subprocess
import sys
import time
from pathlib import Path


def _ignore_sigint() -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # what a non-interactive shell does to `cmd &`


def test_sigterm_shuts_down_through_the_runtime_so_the_robot_is_released(tmp_path: Path) -> None:
    # On stdio (and in pepper-mcp-gemini) nothing else handles SIGTERM: Python would exit on the
    # spot, leaving the robot with reduced safety distances and its audio subscription.
    log = tmp_path / "stderr.log"
    with log.open("w") as err:
        proc = subprocess.Popen(
            [sys.executable, "-m", "pepper_mcp.cli", "--fake", "--stt", "none", "--transport", "stdio"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err, preexec_fn=_ignore_sigint)
    try:
        deadline = time.monotonic() + 30
        while "hearing disabled" not in log.read_text():
            assert proc.poll() is None, log.read_text()[-2000:]
            assert time.monotonic() < deadline, "runtime never started"
            time.sleep(0.2)
        proc.send_signal(signal.SIGTERM)
        while "shutting down" not in log.read_text():
            assert proc.poll() is None, f"exited without releasing the robot:\n{log.read_text()[-2000:]}"
            assert time.monotonic() < deadline + 20, "runtime never shut down"
            time.sleep(0.2)
        # The MCP SDK's stdin reader only returns once the client closes the pipe.
        assert proc.stdin is not None
        proc.stdin.close()
        proc.wait(timeout=10)
    finally:
        proc.kill()
