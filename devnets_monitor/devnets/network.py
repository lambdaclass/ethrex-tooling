"""Network splits, client distribution, and overview collector."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from .config import devnet_entry
from .store import connect, migrate, upsert

logger = logging.getLogger(__name__)

# Real Dora API shapes (verified against live glamsterdam-devnet-5):
#
# GET /api/v1/network/splits
#   data.splits[]: fork_id, head_slot, head_root, head_block_hash,
#     head_execution_number, total_chain_weight, last_epoch_votes[],
#     last_epoch_participation[], is_canonical
#
# GET /api/v1/network/client_head_forks
#   data.forks[]: head_slot, head_root, client_count,
#     clients[]: index, name, version, status, head_slot, distance, last_refresh
#   data.fork_count (int)
#
# GET /api/v1/network/overview
#   data.network_info: network_name, genesis_time, ...
#   data.current_state: current_slot, current_epoch, current_epoch_progress, ...
#   data.checkpoints: finalized_epoch, finalized_root, justified_epoch, ...
#   data.forks[]: name, version, epoch, active, scheduled, time, type, fork_digest
#
# GET /api/v1/clients/execution
#   clients[]: client_name, node_id, enode, ip, port, version, status, last_update
#
# GET /api/v1/clients/consensus
#   clients[]: client_name, client_type, version, peer_id, head_slot, head_root,
#     status, peer_count, peers_inbound, peers_outbound, last_refresh


def _fetch(url: str) -> dict | None:
    """GET url with a single retry on transient failure. Returns parsed JSON or None."""
    import requests

    for attempt in range(2):
        try:
            resp = requests.get(url, timeout=20)
            if resp.status_code == 200:
                return resp.json()
            logger.warning("network fetch %s -> HTTP %d", url, resp.status_code)
        except Exception as exc:
            logger.warning("network fetch %s error (attempt %d): %s", url, attempt, exc)
    return None


def collect_network(devnet: str) -> None:
    """
    Fetch network splits and client head forks from Dora, upsert into SQLite.
    Each fetch is wrapped in its own try/except so a partial failure does not abort.
    """
    entry = devnet_entry(devnet)
    dora_base = entry.get("dora_base", "").rstrip("/")
    if not dora_base:
        logger.error("collect_network: dora_base missing for devnet %s", devnet)
        print(f"collect_network({devnet}): dora_base missing, skipped")
        return

    conn = connect()
    migrate(conn)
    ts = int(time.time())

    splits_inserted = 0
    forks_processed = 0
    dist_inserted = 0

    # --- Fetch splits ---
    try:
        data = _fetch(f"{dora_base}/api/v1/network/splits")
        if data and data.get("status") == "OK":
            splits = (data.get("data") or {}).get("splits") or []
            for s in splits:
                head_root = s.get("head_root", "") or ""
                row: dict[str, Any] = {
                    "devnet": devnet,
                    "ts": ts,
                    "head_root": head_root,
                    "head_slot": s.get("head_slot"),
                    "head_count": None,  # filled from client_head_forks
                    "is_canonical": 1 if s.get("is_canonical") else 0,
                    "clients_json": None,
                    "fork_id": str(s.get("fork_id", "")),
                }
                upsert(conn, "network_splits", row)
                splits_inserted += 1
    except Exception as exc:
        logger.warning("collect_network splits error for %s: %s", devnet, exc)

    # --- Fetch client_head_forks (includes per-client data + head counts) ---
    try:
        data = _fetch(f"{dora_base}/api/v1/network/client_head_forks")
        if data and data.get("status") == "OK":
            forks = (data.get("data") or {}).get("forks") or []
            for fork in forks:
                head_root = fork.get("head_root", "") or ""
                clients = fork.get("clients") or []
                clients_json = json.dumps(clients)
                head_count = fork.get("client_count") or len(clients)

                # Merge into the fork row from the splits fetch WITHOUT clobbering
                # is_canonical / fork_id (a full upsert would overwrite them with
                # NULL). Targeted insert-or-update of only the client-side fields.
                conn.execute(
                    """INSERT INTO network_splits
                       (devnet, ts, head_root, head_slot, head_count, clients_json,
                        is_canonical, fork_id)
                       VALUES (?,?,?,?,?,?,NULL,NULL)
                       ON CONFLICT(devnet, ts, head_root) DO UPDATE SET
                         head_count=excluded.head_count,
                         clients_json=excluded.clients_json,
                         head_slot=COALESCE(excluded.head_slot, network_splits.head_slot)""",
                    (devnet, ts, head_root, fork.get("head_slot"), head_count, clients_json),
                )

                # Extract client distribution from version strings
                cl_counts: dict[str, int] = {}
                for c in clients:
                    version = c.get("version") or ""
                    cl_name = _parse_cl_client(version)
                    cl_counts[cl_name] = cl_counts.get(cl_name, 0) + 1

                for cl_name, cnt in cl_counts.items():
                    dist_row: dict[str, Any] = {
                        "devnet": devnet,
                        "ts": ts,
                        "layer": "cl",
                        "client": cl_name,
                        "version": "",
                        "count": cnt,
                    }
                    upsert(conn, "client_dist", dist_row)
                    dist_inserted += 1

                forks_processed += 1
    except Exception as exc:
        logger.warning("collect_network client_head_forks error for %s: %s", devnet, exc)

    conn.commit()
    conn.close()
    print(
        f"collect_network({devnet}): {splits_inserted} split rows, "
        f"{forks_processed} fork entries, {dist_inserted} client_dist rows at ts={ts}"
    )


def _parse_cl_client(version: str) -> str:
    """
    Extract CL client name from version string.
    Examples: 'Lighthouse/v8.1.3-...' -> 'lighthouse',
              'Prysm/v7.1.3-...' -> 'prysm',
              'Grandine/2.0.4-...' -> 'grandine'.
    """
    if not version:
        return "unknown"
    name = version.split("/")[0].split(" ")[0].lower()
    return name or "unknown"


def _latest_network_rows(conn: Any, devnet: str) -> list[dict]:
    """
    Return the most recent network_splits rows for a devnet (all forks at latest ts).
    Used by detectors.
    """
    ts_row = conn.execute(
        "SELECT MAX(ts) AS max_ts FROM network_splits WHERE devnet=?", (devnet,)
    ).fetchone()
    if not ts_row or ts_row["max_ts"] is None:
        return []
    latest_ts = ts_row["max_ts"]
    rows = conn.execute(
        "SELECT * FROM network_splits WHERE devnet=? AND ts=?",
        (devnet, latest_ts),
    ).fetchall()
    return [dict(r) for r in rows]


def get_network_data(devnet: str) -> dict[str, Any] | None:
    """
    Return network data suitable for template rendering.
    Returns None if no data is available.
    """
    conn = connect()
    migrate(conn)

    splits = _latest_network_rows(conn, devnet)
    if not splits:
        conn.close()
        return None

    latest_ts = splits[0]["ts"] if splits else None

    # Load client distribution at same ts
    dist_rows = []
    if latest_ts:
        dist_rows = conn.execute(
            """SELECT layer, client, version, count FROM client_dist
               WHERE devnet=? AND ts=? ORDER BY layer, count DESC""",
            (devnet, latest_ts),
        ).fetchall()

    # Parse clients_json for the canonical fork
    canonical_fork = None
    for s in splits:
        if s.get("is_canonical") or s.get("clients_json"):
            # Prefer the row with clients_json
            if s.get("clients_json"):
                canonical_fork = s
                break
    if canonical_fork is None and splits:
        canonical_fork = splits[0]

    clients_list = []
    if canonical_fork and canonical_fork.get("clients_json"):
        try:
            clients_list = json.loads(canonical_fork["clients_json"])
        except Exception:
            pass

    conn.close()

    return {
        "ts": latest_ts,
        "splits": splits,
        "clients": clients_list,
        "client_dist": [dict(r) for r in dist_rows],
        "fork_count": len(splits),
    }


def show_network(devnet: str) -> None:
    """Print network split and client distribution summary."""
    from datetime import datetime, timezone

    data = get_network_data(devnet)
    if data is None:
        print(f"network({devnet}): no data. Run: dv collect {devnet} network")
        return

    ts_str = ""
    if data["ts"]:
        try:
            ts_str = datetime.fromtimestamp(data["ts"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            ts_str = str(data["ts"])

    print(f"\nNetwork status for {devnet}  (at {ts_str})\n")

    print(f"Head forks: {data['fork_count']}")
    for s in data["splits"]:
        canonical = " [canonical]" if s.get("is_canonical") else ""
        count = s.get("head_count", "?")
        print(
            f"  fork_id={s.get('fork_id', '?')} "
            f"head_slot={s.get('head_slot', '?')} "
            f"head_root={str(s.get('head_root', ''))[:12]}... "
            f"clients={count}{canonical}"
        )

    if data["clients"]:
        print(f"\nClients on canonical head ({len(data['clients'])}):\n")
        print(f"  {'NAME':<32} {'STATUS':<12} {'DISTANCE':>8} {'HEAD_SLOT':>10}")
        print("  " + "-" * 68)
        for c in data["clients"]:
            name = (c.get("name") or "")[:31]
            status = (c.get("status") or "")[:11]
            distance = c.get("distance", 0)
            head_slot = c.get("head_slot", "?")
            print(f"  {name:<32} {status:<12} {distance:>8} {head_slot:>10}")

    if data["client_dist"]:
        print("\nCL client distribution:\n")
        for r in data["client_dist"]:
            print(f"  {r['client']:<16} {r['count']:>4}")

    print()


# ---------------------------------------------------------------------------
# collect_clients / get_clients_data / show_clients (T1)
# ---------------------------------------------------------------------------


def _parse_el_client(version: str) -> tuple[str, str]:
    """
    Extract EL client name and short version from version string.
    Examples:
      'ethrex/v15.0.0-glamsterdam...' -> ('ethrex', 'v15.0.0-glamsterdam...')
      'Nethermind/v1.39.0-...'        -> ('nethermind', 'v1.39.0-...')
      'erigon/v3.6.0-...'             -> ('erigon', 'v3.6.0-...')
    """
    if not version:
        return "unknown", ""
    parts = version.split("/", 1)
    name = parts[0].lower()
    ver = parts[1].split("/")[0] if len(parts) > 1 else ""
    return name or "unknown", ver


def collect_clients(devnet: str) -> None:
    """
    Fetch EL+CL client lists and network overview from Dora.
    Upserts EL distribution into client_dist (layer='el').
    Upserts overview summary into network_overview table.
    Each fetch is wrapped in its own try/except.
    """
    entry = devnet_entry(devnet)
    dora_base = entry.get("dora_base", "").rstrip("/")
    if not dora_base:
        logger.error("collect_clients: dora_base missing for devnet %s", devnet)
        print(f"collect_clients({devnet}): dora_base missing, skipped")
        return

    conn = connect()
    migrate(conn)
    ts = int(time.time())

    el_dist_inserted = 0
    overview_stored = 0

    # --- EL clients ---
    try:
        data = _fetch(f"{dora_base}/api/v1/clients/execution")
        if data is not None:
            clients = data.get("clients") or []
            # Aggregate by (client_name, version)
            el_counts: dict[tuple[str, str], int] = {}
            for c in clients:
                name, ver = _parse_el_client(c.get("version") or "")
                # Truncate version to short tag to avoid unbounded cardinality
                ver_short = ver[:64]
                el_counts[(name, ver_short)] = el_counts.get((name, ver_short), 0) + 1

            for (name, ver_short), cnt in el_counts.items():
                dist_row: dict[str, Any] = {
                    "devnet": devnet,
                    "ts": ts,
                    "layer": "el",
                    "client": name,
                    "version": ver_short,
                    "count": cnt,
                }
                upsert(conn, "client_dist", dist_row)
                el_dist_inserted += 1
    except Exception as exc:
        logger.warning("collect_clients EL error for %s: %s", devnet, exc)

    # --- Network overview ---
    try:
        data = _fetch(f"{dora_base}/api/v1/network/overview")
        if data and data.get("status") == "OK":
            payload = data.get("data") or {}
            cur = payload.get("current_state") or {}
            chk = payload.get("checkpoints") or {}
            overview_row: dict[str, Any] = {
                "devnet": devnet,
                "ts": ts,
                "current_slot": cur.get("current_slot"),
                "current_epoch": cur.get("current_epoch"),
                "finalized_epoch": chk.get("finalized_epoch"),
                "justified_epoch": chk.get("justified_epoch"),
                "json": json.dumps(payload),
            }
            upsert(conn, "network_overview", overview_row)
            overview_stored = 1
    except Exception as exc:
        logger.warning("collect_clients overview error for %s: %s", devnet, exc)

    conn.commit()
    conn.close()
    print(
        f"collect_clients({devnet}): {el_dist_inserted} EL dist rows, "
        f"{overview_stored} overview rows at ts={ts}"
    )


def get_clients_data(devnet: str) -> dict[str, Any] | None:
    """
    Return clients data suitable for template rendering.
    Includes EL+CL distribution, ethrex versions live, fork agreement, overview.
    Returns None if no data is available.
    """
    conn = connect()
    migrate(conn)

    # Latest ts that has any client_dist row for this devnet
    ts_row = conn.execute(
        "SELECT MAX(ts) AS max_ts FROM client_dist WHERE devnet=?", (devnet,)
    ).fetchone()
    if not ts_row or ts_row["max_ts"] is None:
        conn.close()
        return None

    latest_ts = ts_row["max_ts"]

    dist_rows = conn.execute(
        """SELECT layer, client, version, count FROM client_dist
           WHERE devnet=? AND ts=? ORDER BY layer, count DESC""",
        (devnet, latest_ts),
    ).fetchall()

    # ethrex-specific: gather versions from client_dist (layer='el', client='ethrex')
    ethrex_versions = [
        {"version": r["version"], "count": r["count"]}
        for r in dist_rows
        if r["layer"] == "el" and r["client"] == "ethrex"
    ]

    # Latest network_overview for is-finalized status
    overview = None
    ov_row = conn.execute(
        """SELECT current_slot, current_epoch, finalized_epoch, justified_epoch, json
           FROM network_overview WHERE devnet=? ORDER BY ts DESC LIMIT 1""",
        (devnet,),
    ).fetchone()
    if ov_row:
        overview = dict(ov_row)
        try:
            overview["parsed"] = json.loads(ov_row["json"] or "{}")
        except Exception:
            overview["parsed"] = {}

    # Fork agreement from network_splits (most recent)
    ts_ns = conn.execute(
        "SELECT MAX(ts) AS max_ts FROM network_splits WHERE devnet=?", (devnet,)
    ).fetchone()
    fork_count = 0
    if ts_ns and ts_ns["max_ts"] is not None:
        fc = conn.execute(
            "SELECT COUNT(*) AS c FROM network_splits WHERE devnet=? AND ts=?",
            (devnet, ts_ns["max_ts"]),
        ).fetchone()
        fork_count = fc["c"] if fc else 0

    conn.close()

    ts_str = ""
    try:
        ts_str = datetime.fromtimestamp(latest_ts, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
    except Exception:
        ts_str = str(latest_ts)

    return {
        "ts": latest_ts,
        "ts_str": ts_str,
        "client_dist": [dict(r) for r in dist_rows],
        "ethrex_versions": ethrex_versions,
        "fork_count": fork_count,
        "overview": overview,
    }


def show_clients(devnet: str) -> None:
    """Print EL+CL client diversity and ethrex version summary."""
    data = get_clients_data(devnet)
    if data is None:
        print(f"clients({devnet}): no data. Run: dv collect {devnet} clients")
        return

    print(f"\nClients for {devnet}  (at {data['ts_str']})\n")

    if data["ethrex_versions"]:
        print("ethrex versions live:")
        for ev in data["ethrex_versions"]:
            print(f"  {ev['version'][:80]}  (count={ev['count']})")
        print()

    if data["client_dist"]:
        print(f"{'LAYER':<6} {'CLIENT':<16} {'COUNT':>5}  VERSION")
        print("-" * 60)
        for r in data["client_dist"]:
            ver = (r["version"] or "")[:40]
            print(f"  {r['layer']:<4} {r['client']:<16} {r['count']:>5}  {ver}")
        print()

    ov = data.get("overview")
    if ov:
        print(
            f"Network: slot={ov.get('current_slot')}, epoch={ov.get('current_epoch')}, "
            f"finalized={ov.get('finalized_epoch')}"
        )
        print()

    print(f"Head forks (splits): {data['fork_count']}")
    print(
        f"Forkmon: https://forkmon.{devnet}.ethpandaops.io  "
        f"(no probed API, link-out only)"
    )
    print()
