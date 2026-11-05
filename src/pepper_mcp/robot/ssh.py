"""Non-interactive SSH to the robot with its password (no sshpass, no key setup needed)."""

from __future__ import annotations

import contextlib
import os
import tempfile
from collections.abc import Iterator

SSH_OPTS = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=10",
            "-o", "LogLevel=ERROR"]


@contextlib.contextmanager
def askpass_env(password: str) -> Iterator[dict[str, str]]:
    """Environment that makes ssh/scp read ``password`` from an SSH_ASKPASS script.

    The script is deleted on exit, so keep the context open until ssh has authenticated.
    """
    askpass = tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False)
    try:
        askpass.write(f"#!/bin/sh\necho '{password}'\n")
        askpass.close()
        os.chmod(askpass.name, 0o700)
        env = dict(os.environ, SSH_ASKPASS=askpass.name, SSH_ASKPASS_REQUIRE="force")
        env.setdefault("DISPLAY", ":0")
        yield env
    finally:
        with contextlib.suppress(OSError):
            os.unlink(askpass.name)


def detached(cmd: list[str]) -> list[str]:
    """Run without a controlling terminal, or ssh asks on the tty instead of using SSH_ASKPASS."""
    return ["setsid", "-w", *cmd] if os.name == "posix" else cmd
