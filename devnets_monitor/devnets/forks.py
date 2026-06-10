"""Fork schedule + EIP collector: read from discovered cache and eips.json."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .config import load_cache, repo_root
from .store import connect, migrate, upsert

logger = logging.getLogger(__name__)


def collect_forks(devnet: str) -> None:
    """
    Read the fork_schedule from the discovered cache (config/devnets/<devnet>.yaml)
    and upsert into the fork_schedule table.

    If config/eips.json exists, also load fork -> EIP entries into fork_eips.

    eipmcp is an MCP tool (not an HTTP API reachable from Python). Therefore
    eips-refresh cannot call it at runtime. Instead, eips.json is a
    hand/agent-maintained file. If it is absent, collect_forks still loads the
    fork schedule; only the EIP enrichment is skipped. Run `dv eips-refresh` to
    populate eips.json from eipmcp data.
    """
    cache = load_cache(devnet)
    if not cache:
        print(
            f"collect_forks({devnet}): no discovered cache found. "
            f"Run: dv discover {devnet}"
        )
        return

    fork_schedule: dict[str, Any] = cache.get("fork_schedule") or {}
    if not fork_schedule:
        print(f"collect_forks({devnet}): no fork_schedule in cache")
        return

    conn = connect()
    migrate(conn)

    inserted = 0
    for fork_name, fork_data in fork_schedule.items():
        if not isinstance(fork_data, dict):
            continue
        row: dict[str, Any] = {
            "devnet": devnet,
            "fork": fork_name,
            "activation_ts": fork_data.get("activation_ts"),
            "blob_target": fork_data.get("blob_target"),
            "blob_max": fork_data.get("blob_max"),
        }
        upsert(conn, "fork_schedule", row)
        inserted += 1

    conn.commit()
    print(f"collect_forks({devnet}): {inserted} forks upserted from cache")

    # Load EIP enrichment if available
    eips_path = repo_root() / "config" / "eips.json"
    if eips_path.exists():
        _load_eips(conn, devnet, eips_path)
    else:
        print(
            f"collect_forks({devnet}): config/eips.json not found; "
            "EIP enrichment skipped. Run: dv eips-refresh"
        )

    conn.close()


def _load_eips(conn: Any, devnet: str, eips_path: Path) -> None:
    """Load fork->EIP mapping from eips.json into fork_eips table."""
    try:
        with eips_path.open() as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to parse config/eips.json: %s", exc)
        print(f"collect_forks: failed to parse config/eips.json: {exc}")
        return

    forks_data = data.get("forks") or {}
    # Clean reload: eips.json is the full source of truth for this devnet, so drop
    # existing rows first (otherwise EIPs removed from the file, e.g. now-Declined
    # ones, would linger as stale rows since upsert never deletes).
    conn.execute("DELETE FROM fork_eips WHERE devnet = ?", (devnet,))
    inserted = 0
    for fork_name, eip_list in forks_data.items():
        if not isinstance(eip_list, list):
            continue
        for item in eip_list:
            if not isinstance(item, dict):
                continue
            eip_num = item.get("eip")
            if eip_num is None:
                continue
            row: dict[str, Any] = {
                "devnet": devnet,
                "fork": fork_name,
                "eip": int(eip_num),
                "title": item.get("title"),
                "stage": item.get("stage"),
                "status": item.get("status"),
            }
            upsert(conn, "fork_eips", row)
            inserted += 1

    conn.commit()
    print(f"collect_forks({devnet}): {inserted} EIP entries loaded from eips.json")
