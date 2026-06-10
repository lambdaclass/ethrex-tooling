"""
Wipe: recover a wedged EL node. MUTATING; gated behind --yes.
Sends WIPE_SEQUENCE to the host with the image tag as $1.
"""

from __future__ import annotations

import subprocess
import sys

from .config import host_of, load_cache
from .remote import WIPE_SEQUENCE
from .ssh import SSH_OPTS


def wipe(devnet: str, node: str, yes: bool) -> None:
    """
    Run the incident-tested wipe sequence on the given node.
    Requires yes=True; refuses and exits nonzero otherwise.
    """
    if not yes:
        print(
            "error: 'dv wipe' is a MUTATING operation and requires --yes to confirm.\n"
            "  Usage: dv wipe [devnet] <node> --yes",
            file=sys.stderr,
        )
        sys.exit(1)

    cache = load_cache(devnet)
    if not cache:
        print(
            f"error: no cache found for '{devnet}'. "
            f"Run 'dv discover {devnet}' first to populate config/devnets/{devnet}.yaml",
            file=sys.stderr,
        )
        sys.exit(1)

    image_tag: str = cache.get("image_tag", "")
    if not image_tag:
        print(
            f"error: no image_tag in cache for '{devnet}'. "
            f"Run 'dv discover {devnet}' to refresh the cache.",
            file=sys.stderr,
        )
        sys.exit(1)

    host = host_of(devnet, node)
    print(f"==> Wiping {node} ({host}) with image {image_tag}")
    print(f"==> Sending wipe sequence via SSH...")

    # Stream output: use Popen so we can print in real time
    cmd = ["ssh"] + SSH_OPTS + [host, "bash", "-s", image_tag]
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert proc.stdin is not None
        assert proc.stdout is not None
        proc.stdin.write(WIPE_SEQUENCE)
        proc.stdin.close()
        for line in proc.stdout:
            print(line, end="")
        proc.wait()
    except Exception as exc:
        print(f"error: SSH wipe failed: {exc}", file=sys.stderr)
        sys.exit(1)

    if proc.returncode != 0:
        print(
            f"\nerror: wipe sequence exited with code {proc.returncode}",
            file=sys.stderr,
        )
        sys.exit(proc.returncode)
