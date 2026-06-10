"""Node health collector: snapshot all nodes into node_health table."""

from __future__ import annotations

import logging
import time
from typing import Any

from .store import connect, migrate, upsert

logger = logging.getLogger(__name__)


def _safe_int(value: Any) -> int | None:
    """Convert a value to int, returning None on failure."""
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def collect_health(devnet: str) -> None:
    """
    Probe every node in the devnet via SSH and insert a snapshot row into
    node_health for each. One row per node per invocation (ts = now).

    Calls devnets.status.gather() directly rather than scraping stdout,
    keeping a clean in-process seam.
    """
    from .status import gather

    conn = connect()
    migrate(conn)

    ts = int(time.time())
    results = gather(devnet, None)

    inserted = 0
    errors = 0
    for d in results:
        node = d.get("node", "unknown")
        if "_error" in d:
            logger.warning("health probe error on %s: %s", node, d["_error"])
            errors += 1
            # Still store the error row so gaps are visible
            row: dict[str, Any] = {
                "devnet": devnet,
                "node": node,
                "ts": ts,
                "image": None,
                "buildnum": None,
                "commit": None,
                "restart": None,
                "head": None,
                "peers": None,
                "syncing": d["_error"][:200],
                "state_at_head": None,
                "watchtower": None,
                "cl_line": None,
            }
        else:
            row = {
                "devnet": devnet,
                "node": node,
                "ts": ts,
                "image": d.get("image"),
                "buildnum": d.get("buildnum"),
                "commit": d.get("commit"),
                "restart": _safe_int(d.get("restart")),
                "head": _safe_int(d.get("head")),
                "peers": _safe_int(d.get("peers")),
                "syncing": d.get("syncing"),
                "state_at_head": d.get("state_at_head"),
                "watchtower": d.get("watchtower"),
                "cl_line": d.get("cl_line"),
            }

        upsert(conn, "node_health", row)
        inserted += 1

    conn.commit()
    conn.close()

    print(
        f"collect_health({devnet}): {inserted} nodes snapshotted "
        f"({errors} errors) at ts={ts}"
    )
