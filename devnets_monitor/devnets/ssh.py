"""SSH helpers: run host-side shell snippets via 'ssh <host> bash -s'."""

from __future__ import annotations

import subprocess
from typing import Sequence


SSH_OPTS: list[str] = [
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ConnectTimeout=5",
    # Trip a dead/hung connection fast (server-alive probes; ~6s total) rather
    # than waiting on the subprocess timeout.
    "-o", "ServerAliveInterval=3",
    "-o", "ServerAliveCountMax=2",
]


def run_remote(
    host: str,
    script: str,
    args: Sequence[str] | None = None,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    """
    Run a shell script on a remote host via 'ssh <host> bash -s [args...]',
    feeding the script on stdin. Positional args become $1, $2, ... on the remote.

    Values are passed as positional args, never interpolated into the script string.
    Returns a CompletedProcess with stdout/stderr captured as text.
    """
    cmd: list[str] = ["ssh"] + SSH_OPTS + [host, "bash", "-s"]
    if args:
        cmd.extend(args)
    return subprocess.run(
        cmd,
        input=script,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def run_remote_checked(
    host: str,
    script: str,
    args: Sequence[str] | None = None,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    """
    Same as run_remote but raises subprocess.CalledProcessError on nonzero exit.
    """
    result = run_remote(host, script, args=args, timeout=timeout)
    result.check_returncode()
    return result
