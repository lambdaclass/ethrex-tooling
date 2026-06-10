"""Spamoor status collector: fetch active spammers and persist into SQLite."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from .store import connect, migrate, upsert

logger = logging.getLogger(__name__)

# Real Spamoor API shape (verified against live glamsterdam-devnet-5):
#
# GET https://spamoor.<devnet>.ethpandaops.io/api/spammers
#   Returns a JSON ARRAY (no envelope) of:
#     id, name, description, scenario, status (1=running, 0=stopped),
#     created_at, is_group, group_id,
#     member_config: { weight, enabled, sort_order }
#
# Blob spammer: id=3, scenario="blob-combined", status=1 (confirmed live).


def _fetch_spammers(devnet: str) -> list[dict[str, Any]] | None:
    """GET /api/spammers from the Spamoor instance for this devnet."""
    import requests

    url = f"https://spamoor.{devnet}.ethpandaops.io/api/spammers"
    for attempt in range(2):
        try:
            resp = requests.get(url, timeout=20)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    return data
                logger.warning("spamoor: unexpected response shape for %s", devnet)
                return None
            if resp.status_code == 404:
                logger.info("spamoor: 404 for %s (not deployed on this devnet)", devnet)
                return None
            logger.warning(
                "spamoor fetch %s -> HTTP %d (attempt %d)",
                devnet, resp.status_code, attempt,
            )
        except Exception as exc:
            logger.warning("spamoor fetch error for %s (attempt %d): %s", devnet, attempt, exc)
        if attempt == 0:
            time.sleep(1)
    return None


def collect_spamoor(devnet: str) -> None:
    """
    Fetch Spamoor spammer list and upsert into spamoor_status.
    Degrades gracefully if the API is absent or returns 404.
    """
    spammers = _fetch_spammers(devnet)
    if spammers is None:
        print(f"collect_spamoor({devnet}): API unavailable, skipped (link-out only)")
        return

    conn = connect()
    migrate(conn)
    ts = int(time.time())

    inserted = 0
    for s in spammers:
        spammer_id = s.get("id")
        if spammer_id is None:
            continue
        member_config = s.get("member_config") or {}
        enabled_val = member_config.get("enabled")
        row: dict[str, Any] = {
            "devnet": devnet,
            "ts": ts,
            "spammer_id": int(spammer_id),
            "name": str(s.get("name") or "")[:200],
            "scenario": str(s.get("scenario") or "")[:200],
            "status": int(s.get("status") or 0),
            "enabled": 1 if enabled_val else 0,
        }
        upsert(conn, "spamoor_status", row)
        inserted += 1

    conn.commit()
    conn.close()
    print(f"collect_spamoor({devnet}): {inserted} spammer rows at ts={ts}")


def _blob_spammer_active(conn: Any, devnet: str) -> bool | None:
    """
    Return True if a blob spammer is currently active (latest snapshot has
    scenario containing 'blob' AND status==1). Return False if data exists
    but no blob spammer is active. Return None if no spamoor data at all.
    """
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
            return True
    return False


def get_spamoor_data(devnet: str) -> dict[str, Any] | None:
    """
    Return spamoor data suitable for template rendering.
    Returns None if no data is available.
    """
    conn = connect()
    migrate(conn)

    ts_row = conn.execute(
        "SELECT MAX(ts) AS max_ts FROM spamoor_status WHERE devnet=?", (devnet,)
    ).fetchone()
    if not ts_row or ts_row["max_ts"] is None:
        conn.close()
        return None

    latest_ts = ts_row["max_ts"]
    rows = conn.execute(
        """SELECT spammer_id, name, scenario, status, enabled
           FROM spamoor_status WHERE devnet=? AND ts=?
           ORDER BY spammer_id""",
        (devnet, latest_ts),
    ).fetchall()

    blob_active = _blob_spammer_active(conn, devnet)
    conn.close()

    ts_str = ""
    try:
        ts_str = datetime.fromtimestamp(latest_ts, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
    except Exception:
        ts_str = str(latest_ts)

    spamoor_url = f"https://spamoor.{devnet}.ethpandaops.io"

    return {
        "ts": latest_ts,
        "ts_str": ts_str,
        "spammers": [dict(r) for r in rows],
        "blob_active": blob_active,
        "spamoor_url": spamoor_url,
    }


def show_spamoor(devnet: str) -> None:
    """Print spamoor status summary to stdout."""
    data = get_spamoor_data(devnet)
    if data is None:
        print(f"spamoor({devnet}): no data. Run: dv collect {devnet} spamoor")
        print(f"  Link: https://spamoor.{devnet}.ethpandaops.io")
        return

    print(f"\nSpamoor status for {devnet}  (at {data['ts_str']})\n")

    blob_active = data["blob_active"]
    if blob_active:
        print("  BLOB LOAD: ON  (blob spammer active)")
    elif blob_active is False:
        print("  BLOB LOAD: OFF  (no active blob spammer)")
    else:
        print("  BLOB LOAD: unknown (no spamoor data)")

    print()
    print(f"  {'ID':>4}  {'NAME':<36}  {'SCENARIO':<24}  {'STATUS':<8}  {'ENABLED'}")
    print("  " + "-" * 84)
    for s in data["spammers"]:
        status_str = "running" if s["status"] == 1 else "stopped"
        enabled_str = "yes" if s["enabled"] else "no"
        name = (s["name"] or "")[:35]
        scenario = (s["scenario"] or "")[:23]
        print(
            f"  {s['spammer_id']:>4}  {name:<36}  {scenario:<24}  "
            f"{status_str:<8}  {enabled_str}"
        )

    print()
    print(f"  Spamoor UI: {data['spamoor_url']}")
    print()
