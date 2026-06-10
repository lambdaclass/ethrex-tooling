"""BAL (Block-Level Access List, EIP-7928) inspector.

Fetches /v1/slot/{slot}/block_access_list for ethrex-built canonical slots
and stores the access_count in the bal_inspect table.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from .config import devnet_entry
from .dora import _get_with_backoff
from .store import connect, migrate, upsert

logger = logging.getLogger(__name__)

_DEFAULT_LIMIT = 50


def _max_bal_slot(conn: Any, devnet: str) -> int | None:
    """Return the highest slot already in bal_inspect for this devnet."""
    row = conn.execute(
        "SELECT MAX(slot) FROM bal_inspect WHERE devnet = ?", (devnet,)
    ).fetchone()
    if row and row[0] is not None:
        return int(row[0])
    return None


def collect_bal(devnet: str, limit: int = _DEFAULT_LIMIT) -> None:
    """
    Collect BAL data for recent ethrex-built canonical slots.

    Strategy: query the local slots table for ethrex proposer slots above the
    current watermark (max bal slot), then fetch
    /v1/slot/{slot}/block_access_list for each one not yet stored.
    Uses per-fetch try/except so one failure does not abort the chain.
    """
    entry = devnet_entry(devnet)
    dora_base = entry.get("dora_base", "").rstrip("/")
    if not dora_base:
        logger.error("collect_bal: dora_base missing for devnet %s", devnet)
        return

    conn = connect()
    migrate(conn)

    watermark = _max_bal_slot(conn, devnet)

    # Find ethrex canonical slots above the watermark from the local slots table.
    # proposer_name contains "ethrex" for ethrex-built slots.
    query = """
        SELECT slot, proposer_name, eth_block_number
        FROM slots
        WHERE devnet = ?
          AND status = 'Canonical'
          AND proposer_name LIKE '%ethrex%'
    """
    params: list[Any] = [devnet]
    if watermark is not None:
        query += " AND slot > ?"
        params.append(watermark)
    query += " ORDER BY slot DESC LIMIT ?"
    params.append(limit)

    candidate_rows = conn.execute(query, params).fetchall()

    if not candidate_rows:
        print(f"collect_bal({devnet}): no new ethrex canonical slots above watermark")
        conn.close()
        return

    fetched = 0
    errors = 0
    now = int(time.time())

    for row in candidate_rows:
        slot = row["slot"]
        proposer_name = row["proposer_name"] or ""
        eth_block_number = row["eth_block_number"]

        url = f"{dora_base}/api/v1/slot/{slot}/block_access_list"
        try:
            resp = _get_with_backoff(url, {})
            if resp is None:
                logger.warning("collect_bal: no response for slot %d", slot)
                errors += 1
                continue

            if resp.status_code != 200:
                logger.warning(
                    "collect_bal: HTTP %d for slot %d", resp.status_code, slot
                )
                errors += 1
                continue

            data = resp.json().get("data", {})
            block_root = data.get("block_root", "")
            access_count = data.get("count", 0) or 0

            bal_row: dict[str, Any] = {
                "devnet": devnet,
                "slot": slot,
                "block_root": block_root,
                "proposer_name": proposer_name,
                "access_count": access_count,
                "eth_block_number": eth_block_number,
                "fetched_at": now,
            }
            upsert(conn, "bal_inspect", bal_row)
            fetched += 1

        except Exception as exc:
            logger.warning("collect_bal: error on slot %d: %s", slot, exc)
            errors += 1
            continue

    conn.commit()
    conn.close()
    print(
        f"collect_bal({devnet}): {fetched} slots fetched, "
        f"{errors} errors, watermark was {watermark}"
    )


def get_bal_data(devnet: str, limit: int = 200) -> dict[str, Any] | None:
    """
    Return BAL inspection data for dashboard rendering.

    Keys:
      rows          list[dict]  recent bal_inspect rows (newest first)
      access_counts list[int]   distribution of access_count values
      zero_count    int         slots with access_count == 0
      total         int         total rows
    """
    conn = connect()
    migrate(conn)

    rows = conn.execute(
        """
        SELECT slot, block_root, proposer_name, access_count,
               eth_block_number, fetched_at
        FROM bal_inspect
        WHERE devnet = ?
        ORDER BY slot DESC
        LIMIT ?
        """,
        (devnet, limit),
    ).fetchall()
    conn.close()

    if not rows:
        return None

    def _fmt(ts: int | None) -> str:
        if ts is None:
            return "-"
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M UTC"
            )
        except Exception:
            return str(ts)

    out_rows = []
    access_counts = []
    zero_count = 0
    for r in rows:
        ac = r["access_count"] if r["access_count"] is not None else 0
        access_counts.append(ac)
        if ac == 0:
            zero_count += 1
        out_rows.append({
            "slot": r["slot"],
            "block_root": r["block_root"] or "",
            "block_root_short": (r["block_root"] or "")[:14] + "...",
            "proposer_name": r["proposer_name"] or "",
            "access_count": ac,
            "eth_block_number": r["eth_block_number"],
            "fetched_at": _fmt(r["fetched_at"]),
        })

    avg_ac = sum(access_counts) / len(access_counts) if access_counts else 0.0

    return {
        "rows": out_rows,
        "access_counts": access_counts,
        "zero_count": zero_count,
        "total": len(out_rows),
        "avg_access_count": round(avg_ac, 1),
    }


def show_bal(devnet: str) -> None:
    """Print BAL inspection summary to stdout."""
    data = get_bal_data(devnet)
    if data is None:
        print(
            f"bal({devnet}): no BAL data. "
            f"Run: dv collect {devnet} slow"
        )
        return

    print(f"\nBAL inspection ({devnet})")
    print(f"Total entries: {data['total']}, zero-access slots: {data['zero_count']}, "
          f"avg access_count: {data['avg_access_count']}\n")

    print(
        f"{'SLOT':>8} {'ETH_BLOCK':>10} {'ACCESS_COUNT':>13} "
        f"{'PROPOSER':<30} {'FETCHED'}"
    )
    print("-" * 90)
    for r in data["rows"][:40]:
        print(
            f"{r['slot']:>8} {str(r['eth_block_number'] or ''):>10} "
            f"{r['access_count']:>13} "
            f"{r['proposer_name']:<30} {r['fetched_at']}"
        )
    print()
