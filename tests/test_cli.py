"""Process-level behaviour of the command line helpers."""

from __future__ import annotations

import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

# Same shape as pepper-mcp-gemini's main: stop_on_signals(), then asyncio.run of a coroutine whose
# finally blocks release the robot (the server's lifespan runs Runtime.stop there).
CHILD = textwrap.dedent("""
    import asyncio, sys
    from pepper_mcp.cli import stop_on_signals

    marker = sys.argv[1]

    async def main():
        try:
            open(marker, "w").write("ready")
            await asyncio.Event().wait()
        finally:
            open(marker, "w").write("released")

    stop_on_signals()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
""")


def _ignore_sigint() -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # what a non-interactive shell does to `cmd &`


def test_sigterm_unwinds_finally_blocks_so_the_robot_is_released(tmp_path: Path) -> None:
    # Python's default SIGTERM exits on the spot: no safety-distance restore, no unsubscribe.
    marker = tmp_path / "state"
    proc = subprocess.Popen([sys.executable, "-c", CHILD, str(marker)], preexec_fn=_ignore_sigint)
    try:
        deadline = time.monotonic() + 20
        while not marker.exists() or marker.read_text() != "ready":
            assert proc.poll() is None, "child exited before it was ready"
            assert time.monotonic() < deadline, "child never became ready"
            time.sleep(0.05)
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
    finally:
        proc.kill()
    assert marker.read_text() == "released"
