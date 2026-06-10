"""Fork schedule viewer: human-readable table with countdown and EIP list."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from .store import connect, migrate


# ---------------------------------------------------------------------------
# Data-returning helpers (used by the dashboard)
# ---------------------------------------------------------------------------


def get_fork_data(devnet: str) -> dict[str, Any] | None:
    """
    Return fork schedule data as a dict suitable for template rendering.

    Returns None if there is no fork data.  Dict keys:
      forks   list[dict]  sorted by activation_ts
                keys: fork, activation_ts, time_str, blob_target, blob_max,
                      status (active/in Xd Yh/in Xh Ym), is_next (bool),
                      eips (list[dict] with keys eip, title)
      next_fork  str | None  name of the upcoming fork (for the countdown)
    """
    conn = connect()
    migrate(conn)

    forks = conn.execute(
        """
        SELECT fork, activation_ts, blob_target, blob_max
        FROM fork_schedule
        WHERE devnet = ?
        ORDER BY COALESCE(activation_ts, 0)
        """,
        (devnet,),
    ).fetchall()

    if not forks:
        conn.close()
        return None

    eip_rows = conn.execute(
        """
        SELECT fork, eip, title, stage
        FROM fork_eips
        WHERE devnet = ?
        ORDER BY fork, eip
        """,
        (devnet,),
    ).fetchall()
    conn.close()

    eips_by_fork: dict[str, list[dict[str, Any]]] = {}
    for r in eip_rows:
        eips_by_fork.setdefault(r["fork"], []).append(
            {"eip": r["eip"], "title": r["title"] or "", "stage": r["stage"]}
        )

    now = time.time()
    future_forks = [
        f["fork"]
        for f in forks
        if f["activation_ts"] is not None and f["activation_ts"] > now
    ]
    next_fork_name = future_forks[0] if future_forks else None

    result = []
    for row in forks:
        fork = row["fork"]
        ts = row["activation_ts"]
        result.append({
            "fork": fork,
            "activation_ts": ts,
            "time_str": _format_ts(ts),
            "blob_target": row["blob_target"],
            "blob_max": row["blob_max"],
            "status": _countdown(ts, now),
            "is_next": fork == next_fork_name,
            "eips": eips_by_fork.get(fork, []),
        })

    return {"forks": result, "next_fork": next_fork_name}


def _format_ts(ts: int | None) -> str:
    """Convert unix timestamp to a human-readable UTC string."""
    if ts is None:
        return "genesis"
    if ts == 0:
        return "genesis (ts=0)"
    try:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except (OSError, OverflowError, ValueError):
        return str(ts)


def _countdown(ts: int | None, now: float) -> str:
    """
    Return a human-readable label for a fork's activation relative to now.
    Past/genesis forks are marked 'active'. The first future fork gets a
    countdown string.
    """
    if ts is None or ts == 0:
        return "active"
    diff = ts - now
    if diff <= 0:
        return "active"
    days = int(diff // 86400)
    hours = int((diff % 86400) // 3600)
    minutes = int((diff % 3600) // 60)
    if days > 0:
        return f"in {days}d {hours}h"
    if hours > 0:
        return f"in {hours}h {minutes}m"
    return f"in {minutes}m"


def show_forks(devnet: str) -> None:
    """
    Print the fork schedule from the DB, sorted by activation_ts.
    For each fork: human-readable activation time (UTC), blob target/max,
    countdown to next activation, and EIP list if available in fork_eips.
    """
    conn = connect()
    migrate(conn)

    forks = conn.execute(
        """
        SELECT fork, activation_ts, blob_target, blob_max
        FROM fork_schedule
        WHERE devnet = ?
        ORDER BY COALESCE(activation_ts, 0)
        """,
        (devnet,),
    ).fetchall()

    if not forks:
        conn.close()
        print(
            f"fork({devnet}): no data. Run: dv collect {devnet} forks"
        )
        return

    # Load EIPs per fork
    eip_rows = conn.execute(
        """
        SELECT fork, eip, title, stage
        FROM fork_eips
        WHERE devnet = ?
        ORDER BY fork, eip
        """,
        (devnet,),
    ).fetchall()
    conn.close()

    eips_by_fork: dict[str, list[tuple[int, str, str]]] = {}
    for r in eip_rows:
        eips_by_fork.setdefault(r["fork"], []).append(
            (r["eip"], r["title"] or "", r["stage"] or "")
        )

    now = time.time()

    # Find the first future fork for the countdown marker
    future_forks = [
        f["fork"]
        for f in forks
        if f["activation_ts"] is not None and f["activation_ts"] > now
    ]
    next_fork = future_forks[0] if future_forks else None

    print(f"\nFork schedule for {devnet}\n")

    for row in forks:
        fork = row["fork"]
        ts = row["activation_ts"]
        blob_target = row["blob_target"]
        blob_max = row["blob_max"]

        time_str = _format_ts(ts)
        status = _countdown(ts, now)

        # Marker for the next upcoming fork
        marker = " <-- NEXT" if fork == next_fork else ""

        blob_str = ""
        if blob_target is not None and blob_max is not None:
            blob_str = f"  blobs: target={blob_target}, max={blob_max}"
        elif blob_target is not None:
            blob_str = f"  blobs: target={blob_target}"

        print(f"  {fork:<12}  {time_str:<25}  [{status}]{marker}{blob_str}")

        # EIP list (grouped by inclusion stage where present)
        eips = eips_by_fork.get(fork, [])
        if eips and any(stage for _, _, stage in eips):
            from collections import defaultdict
            by_stage: dict[str, list[tuple[int, str]]] = defaultdict(list)
            for eip_num, title, stage in eips:
                by_stage[stage or "Other"].append((eip_num, title))
            stage_order = ["SFI", "CFI", "PFI"]
            ordered = sorted(by_stage, key=lambda s: (stage_order.index(s) if s in stage_order else 99, s))
            for stage in ordered:
                print(f"             [{stage}]")
                for eip_num, title in by_stage[stage]:
                    title_str = f" - {title}" if title else ""
                    print(f"               EIP-{eip_num}{title_str}")
        else:
            for eip_num, title, _ in eips:
                title_str = f" - {title}" if title else ""
                print(f"             EIP-{eip_num}{title_str}")

    print()
