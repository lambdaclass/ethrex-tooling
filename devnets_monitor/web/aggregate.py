"""Command-center data aggregator.

All queries are DB-only (read from SQLite). No SSH, no HTTP, no subprocess calls
happen in these functions; they are safe to call from FastAPI request handlers.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from typing import Any


def _fmt_ts(ts: int | None) -> str:
    if ts is None:
        return "-"
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return str(ts)


def _node_rollup(health: dict[str, Any], node_events: list[dict[str, Any]]) -> str:
    """
    Compute red/amber/green rollup for a single node health row.

    red   = active wedge or node_unreachable event for this node
    amber = syncing == "yes" (or syncing starts with cur=) OR snapshot is stale (>1h)
    green = synced, state_at_head="yes", recent snapshot
    """
    # Check for red events on this node
    node_name = health.get("node", "")
    for ev in node_events:
        if ev.get("active") and ev.get("node") == node_name:
            if ev.get("kind") in ("wedge", "node_unreachable"):
                return "red"

    syncing_raw = (health.get("syncing") or "").lower()
    is_syncing = (
        syncing_raw == "yes"
        or syncing_raw.startswith("cur=")
        or syncing_raw in ("true", "1")
    )

    state_ok = (health.get("state_at_head") or "").lower() == "yes"

    # Stale snapshot: ts older than 3600s
    ts = health.get("ts_raw")
    stale = False
    if ts is not None:
        now = time.time()
        stale = (now - ts) > 3600

    if is_syncing or not state_ok or stale:
        return "amber"

    return "green"


def _get_finality(conn: sqlite3.Connection, devnet: str) -> dict[str, Any] | None:
    """Return the latest network_overview row for the devnet, or None."""
    try:
        row = conn.execute(
            """SELECT current_slot, current_epoch, finalized_epoch, justified_epoch
               FROM network_overview WHERE devnet=? ORDER BY ts DESC LIMIT 1""",
            (devnet,),
        ).fetchone()
        if row is None:
            return None
        return {
            "current_slot": row["current_slot"],
            "current_epoch": row["current_epoch"],
            "finalized_epoch": row["finalized_epoch"],
            "justified_epoch": row["justified_epoch"],
        }
    except Exception:
        return None


def _get_blob_flow(conn: sqlite3.Connection, devnet: str) -> str | None:
    """
    Return "ON", "OFF", or None (no data).
    Uses the latest spamoor_status snapshot.
    """
    try:
        ts_row = conn.execute(
            "SELECT MAX(ts) AS max_ts FROM spamoor_status WHERE devnet=?", (devnet,)
        ).fetchone()
        if not ts_row or ts_row["max_ts"] is None:
            return None
        latest_ts = ts_row["max_ts"]
        rows = conn.execute(
            "SELECT scenario, status FROM spamoor_status WHERE devnet=? AND ts=?",
            (devnet, latest_ts),
        ).fetchall()
        for r in rows:
            scenario = (r["scenario"] or "").lower()
            if "blob" in scenario and r["status"] == 1:
                return "ON"
        return "OFF"
    except Exception:
        return None


def _get_next_fork(conn: sqlite3.Connection, devnet: str) -> dict[str, Any] | None:
    """Return the next upcoming fork name + countdown string, or None."""
    try:
        now = time.time()
        row = conn.execute(
            """SELECT fork, activation_ts FROM fork_schedule
               WHERE devnet=? AND activation_ts IS NOT NULL AND activation_ts > ?
               ORDER BY activation_ts ASC LIMIT 1""",
            (devnet, now),
        ).fetchone()
        if row is None:
            return None
        ts = row["activation_ts"]
        diff = ts - now
        days = int(diff // 86400)
        hours = int((diff % 86400) // 3600)
        minutes = int((diff % 3600) // 60)
        if days > 0:
            countdown = f"in {days}d {hours}h"
        elif hours > 0:
            countdown = f"in {hours}h {minutes}m"
        else:
            countdown = f"in {minutes}m"
        time_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        return {"fork": row["fork"], "countdown": countdown, "time_str": time_str}
    except Exception:
        return None


def _get_latest_health_with_raw_ts(conn: sqlite3.Connection, devnet: str) -> list[dict[str, Any]]:
    """Like app._latest_health but also includes raw ts for rollup calculations."""
    try:
        rows = conn.execute(
            """
            SELECT nh.*
            FROM node_health nh
            INNER JOIN (
                SELECT node, MAX(ts) AS max_ts
                FROM node_health
                WHERE devnet = ?
                GROUP BY node
            ) latest ON nh.node = latest.node AND nh.ts = latest.max_ts
            WHERE nh.devnet = ?
            ORDER BY nh.node
            """,
            (devnet, devnet),
        ).fetchall()
    except Exception:
        return []

    result = []
    for r in rows:
        syncing = r["syncing"]
        if isinstance(syncing, str):
            syncing_disp = "yes" if syncing.lower() in ("true", "1", "yes") else "no"
        else:
            syncing_disp = "yes" if syncing else "no"
        ts_raw = r["ts"]
        ts_disp = _fmt_ts(ts_raw)
        result.append({
            "node": r["node"],
            "head": r["head"],
            "peers": r["peers"],
            "state_at_head": r["state_at_head"],
            "syncing": syncing_disp,
            "syncing_raw": r["syncing"],
            "buildnum": r["buildnum"],
            "commit": r["commit"],
            "ts": ts_disp,
            "ts_raw": ts_raw,
        })
    return result


def command_center_data(conn: sqlite3.Connection, devnet: str) -> dict[str, Any]:
    """
    Assemble the command-center panel data for one devnet from the DB only.

    Returns a dict with keys:
      nodes         list[dict]  -- health tiles with rollup color
      finality      dict|None   -- current_slot, current_epoch, finalized_epoch
      next_fork     dict|None   -- fork, countdown, time_str
      blob_flow     str|None    -- "ON" | "OFF" | None
      events        list[dict]  -- top 8 active events sorted crit>warn>info
    """
    result: dict[str, Any] = {
        "nodes": [],
        "finality": None,
        "next_fork": None,
        "blob_flow": None,
        "events": [],
    }

    # --- Events (needed first so rollup can check by node) ---
    active_events: list[dict[str, Any]] = []  # full set, for the node rollup
    try:
        _sev_order = {"crit": 0, "warn": 1, "info": 2}
        ev_rows = conn.execute(
            """SELECT kind, severity, node, message, last_seen, resolved_at, count,
                      first_seen
               FROM events WHERE devnet=? ORDER BY last_seen DESC""",
            (devnet,),
        ).fetchall()
        all_events = []
        for e in ev_rows:
            all_events.append({
                "kind": e["kind"],
                "severity": e["severity"],
                "node": e["node"],
                "message": e["message"],
                "last_seen": e["last_seen"],
                "last_seen_str": _fmt_ts(e["last_seen"]),
                "first_seen_str": _fmt_ts(e["first_seen"]),
                "count": e["count"],
                "active": e["resolved_at"] is None,
            })
        all_events.sort(
            key=lambda e: (0 if e["active"] else 1, _sev_order.get(e["severity"], 9), -e["last_seen"])
        )
        active_events = [e for e in all_events if e["active"]]
        result["events"] = active_events[:8]  # display cap
    except Exception:
        pass

    # --- Node tiles ---
    try:
        health_rows = _get_latest_health_with_raw_ts(conn, devnet)
        for h in health_rows:
            # Pass ALL active events (not the display-capped 8) so a red event on
            # the 9th+ node still colors that node's tile red.
            rollup = _node_rollup(h, active_events)
            h["rollup"] = rollup
        result["nodes"] = health_rows
    except Exception:
        pass

    # --- Finality ---
    result["finality"] = _get_finality(conn, devnet)

    # --- Next fork ---
    result["next_fork"] = _get_next_fork(conn, devnet)

    # --- Blob flow ---
    result["blob_flow"] = _get_blob_flow(conn, devnet)

    return result
