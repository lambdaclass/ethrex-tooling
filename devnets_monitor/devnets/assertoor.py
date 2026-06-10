"""Assertoor test run collector and detector.

API shape verified against live glamsterdam-devnet-5:
  GET /api/v1/test_runs
  Response: {"status": "OK", "data": [
    {"run_id": int, "test_id": str, "name": str, "status": str,
     "start_time": int, "stop_time": int}, ...
  ]}
  status values observed: "success", "failure", "running", "pending"
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from .store import connect, migrate, upsert

logger = logging.getLogger(__name__)


def _fetch_test_runs(devnet: str) -> list[dict[str, Any]] | None:
    """
    GET /api/v1/test_runs from the Assertoor instance for this devnet.
    Returns the list of run objects, or None on failure/absence.
    Degrades gracefully on 404 (assertoor not deployed for this devnet).
    """
    import requests

    url = f"https://assertoor.{devnet}.ethpandaops.io/api/v1/test_runs"
    for attempt in range(2):
        try:
            resp = requests.get(url, timeout=20)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") == "OK":
                    return data.get("data") or []
                logger.warning("assertoor: unexpected response for %s: %s", devnet, data.get("status"))
                return None
            if resp.status_code == 404:
                logger.info("assertoor: 404 for %s (not deployed)", devnet)
                return None
            logger.warning(
                "assertoor fetch %s -> HTTP %d (attempt %d)",
                devnet, resp.status_code, attempt,
            )
        except Exception as exc:
            logger.warning("assertoor fetch error for %s (attempt %d): %s", devnet, attempt, exc)
        if attempt == 0:
            time.sleep(1)
    return None


def collect_assertoor(devnet: str) -> None:
    """
    Fetch Assertoor test runs and upsert into assertoor_runs.
    Degrades gracefully if the API is absent or returns 404.
    """
    runs = _fetch_test_runs(devnet)
    if runs is None:
        print(f"collect_assertoor({devnet}): API unavailable, skipped (link-out only)")
        return

    conn = connect()
    migrate(conn)

    web_base = f"https://assertoor.{devnet}.ethpandaops.io"
    inserted = 0

    for r in runs:
        run_id = r.get("run_id")
        if run_id is None:
            continue
        row: dict[str, Any] = {
            "devnet": devnet,
            "run_id": int(run_id),
            "test_id": str(r.get("test_id") or "")[:200],
            "name": str(r.get("name") or "")[:300],
            "status": str(r.get("status") or "")[:64],
            "started_at": r.get("start_time"),
            "stopped_at": r.get("stop_time"),
            "web_url": f"{web_base}",
        }
        upsert(conn, "assertoor_runs", row)
        inserted += 1

    conn.commit()
    conn.close()
    print(f"collect_assertoor({devnet}): {inserted} run rows upserted")


def get_assertoor_data(devnet: str) -> dict[str, Any] | None:
    """
    Return assertoor data for template rendering.
    Returns None if no data is available (link-out-only mode).
    """
    conn = connect()
    migrate(conn)

    cnt = conn.execute(
        "SELECT COUNT(*) AS c FROM assertoor_runs WHERE devnet=?", (devnet,)
    ).fetchone()
    if not cnt or cnt["c"] == 0:
        conn.close()
        return None

    rows = conn.execute(
        """
        SELECT run_id, test_id, name, status, started_at, stopped_at, web_url
        FROM assertoor_runs
        WHERE devnet=?
        ORDER BY run_id DESC
        """,
        (devnet,),
    ).fetchall()
    conn.close()

    def _fmt(ts: int | None) -> str:
        if ts is None:
            return ""
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            return str(ts)

    result = []
    for r in rows:
        result.append({
            "run_id": r["run_id"],
            "test_id": r["test_id"] or "",
            "name": r["name"] or "",
            "status": r["status"] or "",
            "started_at": _fmt(r["started_at"]),
            "stopped_at": _fmt(r["stopped_at"]),
            "web_url": r["web_url"] or f"https://assertoor.{devnet}.ethpandaops.io",
        })

    assertoor_url = f"https://assertoor.{devnet}.ethpandaops.io"
    return {
        "runs": result,
        "assertoor_url": assertoor_url,
    }


def show_assertoor(devnet: str) -> None:
    """Print Assertoor test run summary to stdout."""
    data = get_assertoor_data(devnet)
    if data is None:
        print(f"assertoor({devnet}): no data. Run: dv collect {devnet} assertoor")
        print(f"  Link: https://assertoor.{devnet}.ethpandaops.io")
        return

    runs = data["runs"]
    print(f"\nAssertoor test runs for {devnet}  ({len(runs)} total)\n")
    print(f"  {'ID':>5}  {'STATUS':<10}  {'STARTED':<20}  NAME")
    print("  " + "-" * 80)
    for r in runs:
        status = r["status"]
        started = r["started_at"][:16] if r["started_at"] else "-"
        name = r["name"][:50]
        print(f"  {r['run_id']:>5}  {status:<10}  {started:<20}  {name}")

    print()
    print(f"  Assertoor UI: {data['assertoor_url']}")
    print()


# Note: the `detect_assertoor_fail` detector lives in detect.py (queries the
# assertoor_runs table directly) so all detectors register uniformly via
# @_register without cross-module import-order fragility.
