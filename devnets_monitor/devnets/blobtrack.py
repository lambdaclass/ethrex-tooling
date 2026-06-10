"""Blob inclusion analysis: per-proposer and ethrex-vs-others comparison."""

from __future__ import annotations

import time
from typing import Any

from .store import connect, migrate


# ---------------------------------------------------------------------------
# Data-returning helpers (used by the dashboard)
# ---------------------------------------------------------------------------


def get_blob_data(
    devnet: str,
    proposer: str | None = None,
    since: str | None = None,
) -> dict[str, Any] | None:
    """
    Return blob inclusion data as a dict suitable for template rendering.

    Returns None if there is no slot data.  Dict keys:
      window_label   str
      min_slot       int
      max_slot       int
      total_slots    int
      proposers      list[dict]  sorted by avg_blobs desc
                       keys: name, count, avg_blobs, sparkline
      clients        list[dict]  sorted by avg_blobs desc
                       keys: client, slots, avg_blobs, total_blobs, is_ethrex
    """
    conn = connect()
    migrate(conn)

    slot_count = _resolve_since(since)

    base_query = "SELECT slot, proposer_name, blob_count, status FROM slots WHERE devnet = ?"
    params: list[Any] = [devnet]

    if slot_count is not None:
        max_row = conn.execute(
            "SELECT MAX(slot) FROM slots WHERE devnet = ?", (devnet,)
        ).fetchone()
        if max_row and max_row[0] is not None:
            cutoff = max_row[0] - slot_count
            base_query += " AND slot >= ?"
            params.append(cutoff)

    if proposer:
        base_query += " AND proposer_name LIKE ?"
        params.append(f"%{proposer}%")

    base_query += " ORDER BY slot ASC"

    rows = conn.execute(base_query, params).fetchall()
    conn.close()

    if not rows:
        return None

    proposer_data: dict[str, list[int]] = {}
    for r in rows:
        name = r["proposer_name"] or "unknown"
        bc = r["blob_count"] if r["blob_count"] is not None else 0
        proposer_data.setdefault(name, []).append(bc)

    def mean(vals: list[int]) -> float:
        return sum(vals) / len(vals) if vals else 0.0

    sorted_proposers = sorted(proposer_data.items(), key=lambda kv: mean(kv[1]), reverse=True)

    total_slots = len(rows)
    max_slot_num = max(r["slot"] for r in rows)
    min_slot_num = min(r["slot"] for r in rows)
    window_label = f"slots {min_slot_num}-{max_slot_num} ({total_slots} slots)"

    proposers_out = []
    for name, vals in sorted_proposers:
        proposers_out.append({
            "name": name,
            "count": len(vals),
            "avg_blobs": round(mean(vals), 2),
            "sparkline": _sparkline(vals),
        })

    client_data: dict[str, list[int]] = {}
    for name, vals in proposer_data.items():
        client = _client_from_proposer(name)
        client_data.setdefault(client, []).extend(vals)

    sorted_clients = sorted(client_data.items(), key=lambda kv: mean(kv[1]), reverse=True)

    clients_out = []
    for client, vals in sorted_clients:
        clients_out.append({
            "client": client,
            "slots": len(vals),
            "avg_blobs": round(mean(vals), 2),
            "total_blobs": sum(vals),
            "is_ethrex": client == "ethrex",
        })

    # Per-slot blob counts grouped by client for the time-series chart
    slot_series: dict[str, list[tuple[int, int]]] = {}
    for r in rows:
        name = r["proposer_name"] or "unknown"
        client = _client_from_proposer(name)
        bc = r["blob_count"] if r["blob_count"] is not None else 0
        slot_series.setdefault(client, []).append((r["slot"], bc))

    return {
        "window_label": window_label,
        "min_slot": min_slot_num,
        "max_slot": max_slot_num,
        "total_slots": total_slots,
        "proposers": proposers_out,
        "clients": clients_out,
        "slot_series": slot_series,
    }


def _client_from_proposer(proposer_name: str) -> str:
    """
    Extract EL client name from proposer_name by taking the SECOND dash-delimited
    token. Examples:
      lighthouse-ethrex-1  -> ethrex
      grandine-erigon-1    -> erigon
      teku-nethermind-2    -> nethermind
    Falls back to the full name if the pattern does not match.
    """
    parts = proposer_name.split("-")
    if len(parts) >= 2:
        return parts[1]
    return proposer_name


def _sparkline(values: list[float]) -> str:
    """Build an 8-character sparkline from a list of values using Unicode blocks."""
    blocks = " ▁▂▃▄▅▆▇█"
    if not values:
        return ""
    lo = min(values)
    hi = max(values)
    span = hi - lo
    result = []
    for v in values[-16:]:  # Last 16 slots for the sparkline
        if span == 0:
            idx = 4
        else:
            idx = int((v - lo) / span * 8)
        idx = max(0, min(8, idx))
        result.append(blocks[idx])
    return "".join(result)


def _resolve_since(since: str | None) -> int | None:
    """
    Parse --since as either a slot count (integer string) or a duration string
    (e.g. '1h', '30m', '3600s'). Returns a slot count cutoff or None (no filter).
    """
    if since is None:
        return None
    s = since.strip()
    # Duration
    if s.endswith("h"):
        try:
            return int(float(s[:-1]) * 225)  # ~12 sec/slot
        except ValueError:
            pass
    if s.endswith("m"):
        try:
            return int(float(s[:-1]) * 3.75)
        except ValueError:
            pass
    if s.endswith("s"):
        try:
            return max(1, int(float(s[:-1]) / 12))
        except ValueError:
            pass
    # Plain integer = slot count
    try:
        return int(s)
    except ValueError:
        return None


def show_blobs(
    devnet: str,
    proposer: str | None = None,
    since: str | None = None,
) -> None:
    """
    Query the slots table and print:
    1. Per-proposer blob inclusion: recent avg blob_count and sparkline trend.
    2. Ethrex-vs-others: group proposer_name by EL client (second token), show
       mean blob_count per client -- the blob-decay lens.

    Arguments:
      proposer: filter to a single proposer_name substring
      since:    slot count or duration (e.g. '500', '2h', '30m') limiting the
                window to the most recent N slots
    """
    conn = connect()
    migrate(conn)

    # Determine the slot window
    slot_count = _resolve_since(since)

    base_query = "SELECT slot, proposer_name, blob_count, status FROM slots WHERE devnet = ?"
    params: list[Any] = [devnet]

    if slot_count is not None:
        max_row = conn.execute(
            "SELECT MAX(slot) FROM slots WHERE devnet = ?", (devnet,)
        ).fetchone()
        if max_row and max_row[0] is not None:
            cutoff = max_row[0] - slot_count
            base_query += " AND slot >= ?"
            params.append(cutoff)

    if proposer:
        base_query += " AND proposer_name LIKE ?"
        params.append(f"%{proposer}%")

    base_query += " ORDER BY slot ASC"

    rows = conn.execute(base_query, params).fetchall()
    conn.close()

    if not rows:
        print(f"blob({devnet}): no slot data. Run: dv collect {devnet} blobs")
        return

    # Build per-proposer data
    proposer_data: dict[str, list[int]] = {}
    for r in rows:
        name = r["proposer_name"] or "unknown"
        bc = r["blob_count"] if r["blob_count"] is not None else 0
        proposer_data.setdefault(name, []).append(bc)

    # Sort by mean blob_count descending
    def mean(vals: list[int]) -> float:
        return sum(vals) / len(vals) if vals else 0.0

    sorted_proposers = sorted(proposer_data.items(), key=lambda kv: mean(kv[1]), reverse=True)

    total_slots = len(rows)
    max_slot_num = max(r["slot"] for r in rows)
    min_slot_num = min(r["slot"] for r in rows)
    window_label = f"slots {min_slot_num}-{max_slot_num} ({total_slots} slots)"

    print(f"\nBlob inclusion per proposer ({devnet})")
    print(f"Window: {window_label}\n")

    col_w = max(len(p) for p, _ in sorted_proposers) + 2 if sorted_proposers else 20
    header = f"{'PROPOSER':<{col_w}} {'COUNT':>6} {'AVG_BLOBS':>10} SPARKLINE"
    print(header)
    print("-" * len(header))

    for name, vals in sorted_proposers:
        avg = mean(vals)
        spark = _sparkline(vals)
        print(f"{name:<{col_w}} {len(vals):>6} {avg:>10.2f} {spark}")

    # --- ethrex vs others ---
    print(f"\nEL client comparison ({devnet})\n")

    client_data: dict[str, list[int]] = {}
    for name, vals in proposer_data.items():
        client = _client_from_proposer(name)
        client_data.setdefault(client, []).extend(vals)

    sorted_clients = sorted(client_data.items(), key=lambda kv: mean(kv[1]), reverse=True)

    print(f"{'CLIENT':<16} {'SLOTS':>7} {'AVG_BLOBS':>10} {'TOTAL_BLOBS':>12}")
    print("-" * 50)
    for client, vals in sorted_clients:
        avg = mean(vals)
        total = sum(vals)
        marker = " <-- ethrex" if client == "ethrex" else ""
        print(
            f"{client:<16} {len(vals):>7} {avg:>10.2f} {total:>12}{marker}"
        )
    print()
