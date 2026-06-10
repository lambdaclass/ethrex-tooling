"""Hive conformance run collector and summary view."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import requests

from .config import devnet_entry
from .store import connect, migrate, upsert

logger = logging.getLogger(__name__)

_HIVE_BASE = "https://hive.ethpandaops.io"


def _parse_iso(value: str | None) -> int | None:
    """Convert ISO-8601 string to unix int. Returns None on failure."""
    if not value:
        return None
    try:
        s = value.rstrip("Z")
        dt = datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return None


def _extract_ethrex_version(versions: dict[str, str]) -> str | None:
    """Extract ethrex version string from the versions dict. Returns None if absent."""
    for key, val in versions.items():
        if "ethrex" in key.lower():
            # Version string looks like "ethrex/v13.0.0-bal-devnet-7-<sha>/..."
            # Return the raw version string, truncated at 80 chars
            return val.strip()[:80]
    return None


def _suite_id(entry: dict[str, Any]) -> str:
    """
    Derive a stable suite_id from the run. Use `fileName` (the JSON log filename)
    as it encodes a timestamp + hash and is unique per run.
    """
    fname = entry.get("fileName", "")
    if fname:
        # Strip .json extension for cleanliness
        return fname.replace(".json", "")
    # Fallback: use start timestamp + name hash
    return f"{entry.get('start', '')}_{entry.get('name', '')}"


def _is_ethrex_run(entry: dict[str, Any]) -> bool:
    """Return True if this run involves ethrex as the execution client."""
    clients = entry.get("clients") or []
    for c in clients:
        if "ethrex" in str(c).lower():
            return True
    versions = entry.get("versions") or {}
    for k in versions:
        if "ethrex" in k.lower():
            return True
    return False


def collect_hive(devnet: str) -> None:
    """
    For each group in the devnet's hive_groups, fetch listing.jsonl from
    hive.ethpandaops.io and upsert ethrex suite runs into hive_runs.

    listing.jsonl is ordered newest-first. We parse all lines and upsert the
    latest ethrex run per (group, suite name) combination.

    Real endpoint shape (confirmed):
      {"name": "eels/consume-engine", "ntests": 38208, "passes": 38208,
       "fails": 0, "timeout": false, "clients": ["ethrex_default"],
       "versions": {"ethrex_default": "ethrex/v13.0.0-..."},
       "start": "2026-06-08T15:18:27.36983134Z",
       "fileName": "1780938802-1b35c1f406f2494248d3edc0039df8de.json",
       "size": 136901252,
       "simLog": "..."}
    """
    entry = devnet_entry(devnet)
    hive_groups: list[str] = entry.get("hive_groups") or []
    if not hive_groups:
        print(f"collect_hive({devnet}): no hive_groups configured")
        return

    conn = connect()
    migrate(conn)

    total_inserted = 0

    for group in hive_groups:
        url = f"{_HIVE_BASE}/{group}/listing.jsonl"
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.error("hive fetch error for group %s: %s", group, exc)
            print(f"collect_hive({devnet}): error fetching {url}: {exc}")
            continue

        inserted = 0
        for line in resp.text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                import json
                run = json.loads(line)
            except Exception:
                continue

            if not _is_ethrex_run(run):
                continue

            suite_id = _suite_id(run)
            versions = run.get("versions") or {}
            ethrex_ver = _extract_ethrex_version(versions)
            started_at = _parse_iso(run.get("start"))

            web_url = f"{_HIVE_BASE}/{group}/{run.get('fileName', '')}"

            row: dict[str, Any] = {
                "devnet": devnet,
                "group_name": group,
                "suite_id": suite_id,
                "ethrex_version": ethrex_ver,
                "fork_filter": run.get("name"),
                "ntests": run.get("ntests"),
                "passes": run.get("passes"),
                "fails": run.get("fails"),
                "started_at": started_at,
                "web_url": web_url,
            }
            upsert(conn, "hive_runs", row)
            inserted += 1

        conn.commit()
        total_inserted += inserted
        print(
            f"collect_hive({devnet}): group={group}, {inserted} ethrex runs upserted"
        )

    conn.close()
    print(f"collect_hive({devnet}): total {total_inserted} runs across all groups")


def show_hive(devnet: str) -> None:
    """
    Print a summary table of the most recent ethrex Hive run per
    (group_name, fork_filter) pair from hive_runs.

    Columns: group | suite | version | passes/fails/ntests | started_at | url
    """
    conn = connect()
    migrate(conn)

    rows = conn.execute(
        """
        SELECT group_name, fork_filter, ethrex_version,
               passes, fails, ntests, started_at, web_url
        FROM hive_runs
        WHERE devnet = ?
        ORDER BY group_name, fork_filter, started_at DESC
        """,
        (devnet,),
    ).fetchall()
    conn.close()

    if not rows:
        print(f"hive({devnet}): no runs found. Run: dv collect {devnet} hive")
        return

    # Deduplicate: keep the newest run per (group, suite)
    seen: set[tuple[str, str]] = set()
    deduped = []
    for r in rows:
        key = (r["group_name"], r["fork_filter"] or "")
        if key not in seen:
            seen.add(key)
            deduped.append(r)

    print(f"\nHive runs for {devnet} (most recent ethrex run per suite)\n")
    header = f"{'GROUP':<12} {'SUITE':<30} {'PASS/FAIL/TOTAL':<18} {'STARTED':<22} URL"
    print(header)
    print("-" * len(header))

    for r in deduped:
        group = r["group_name"] or ""
        suite = (r["fork_filter"] or "")[:28]
        passes = r["passes"] if r["passes"] is not None else "?"
        fails = r["fails"] if r["fails"] is not None else "?"
        ntests = r["ntests"] if r["ntests"] is not None else "?"
        pft = f"{passes}/{fails}/{ntests}"

        started = ""
        if r["started_at"]:
            started = datetime.fromtimestamp(r["started_at"], tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M UTC"
            )

        url = r["web_url"] or ""
        print(f"{group:<12} {suite:<30} {pft:<18} {started:<22} {url}")
