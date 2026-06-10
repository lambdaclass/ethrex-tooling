"""Event detection backbone: record, resolve, and run detectors."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

from .store import connect, migrate

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dedup key helper
# ---------------------------------------------------------------------------


def _dedup_key(kind: str, node: str | None, discriminator: str) -> str:
    return f"{kind}:{node or '-'}:{discriminator}"


# ---------------------------------------------------------------------------
# record_event / resolve_stale
# ---------------------------------------------------------------------------


def record_event(
    conn: Any,
    devnet: str,
    kind: str,
    severity: str,
    node: str | None,
    message: str,
    details: dict,
    now: int,
    discriminator: str = "",
) -> str:
    """
    Upsert an event row using (devnet, dedup_key) as the identity.

    - Absent: INSERT with first_seen=last_seen=now, count=1.
    - Present, active (resolved_at IS NULL): UPDATE last_seen, count+1, message, details.
    - Present, resolved: REOPEN (resolved_at=NULL, first_seen=now, count=1).

    Returns the dedup_key.
    """
    key = _dedup_key(kind, node, discriminator)
    details_json = json.dumps(details)

    row = conn.execute(
        "SELECT count, resolved_at FROM events WHERE devnet=? AND dedup_key=?",
        (devnet, key),
    ).fetchone()

    if row is None:
        conn.execute(
            """INSERT INTO events
               (devnet, dedup_key, kind, severity, node, message, details,
                first_seen, last_seen, resolved_at, count)
               VALUES (?,?,?,?,?,?,?,?,?,NULL,1)""",
            (devnet, key, kind, severity, node, message, details_json, now, now),
        )
    elif row["resolved_at"] is not None:
        # Reopen: treat as a fresh occurrence
        conn.execute(
            """UPDATE events SET resolved_at=NULL, first_seen=?, last_seen=?,
               count=1, severity=?, message=?, details=?
               WHERE devnet=? AND dedup_key=?""",
            (now, now, severity, message, details_json, devnet, key),
        )
    else:
        conn.execute(
            """UPDATE events SET last_seen=?, count=count+1, severity=?,
               message=?, details=?
               WHERE devnet=? AND dedup_key=?""",
            (now, severity, message, details_json, devnet, key),
        )
    return key


def resolve_stale(conn: Any, devnet: str, seen_keys: set, now: int) -> None:
    """
    Resolve all active events for this devnet whose dedup_key is NOT in seen_keys.
    Only touches rows for this specific devnet.
    """
    if not seen_keys:
        # Resolve everything active for this devnet
        conn.execute(
            "UPDATE events SET resolved_at=? WHERE devnet=? AND resolved_at IS NULL",
            (now, devnet),
        )
        return

    placeholders = ",".join("?" for _ in seen_keys)
    conn.execute(
        f"UPDATE events SET resolved_at=? WHERE devnet=? AND resolved_at IS NULL "
        f"AND dedup_key NOT IN ({placeholders})",
        (now, devnet, *seen_keys),
    )


# ---------------------------------------------------------------------------
# Detector registry
# ---------------------------------------------------------------------------

# Each detector: fn(conn, devnet, now, seen) -> None
# Detectors call record_event and add each returned key to seen.
DETECTORS: list[Callable] = []


def _register(fn: Callable) -> Callable:
    DETECTORS.append(fn)
    return fn


# ---------------------------------------------------------------------------
# run_detectors
# ---------------------------------------------------------------------------


def run_detectors(devnet: str) -> None:
    """Open DB, run every registered detector, resolve stale events, commit."""
    conn = connect()
    migrate(conn)
    now = int(time.time())
    seen: set[str] = set()

    errors = 0
    for fn in DETECTORS:
        try:
            fn(conn, devnet, now, seen)
        except Exception as exc:
            logger.warning("detector %s failed for %s: %s", fn.__name__, devnet, exc)
            errors += 1

    # If every detector crashed (e.g. schema/import problem), do NOT resolve
    # active events -- an empty `seen` would otherwise wipe legitimate events.
    if DETECTORS and errors == len(DETECTORS):
        logger.error("all detectors failed for %s; skipping resolve_stale", devnet)
        conn.commit()
        conn.close()
        print(f"run_detectors({devnet}): ALL {errors} detectors failed; events untouched")
        return

    resolve_stale(conn, devnet, seen, now)
    conn.commit()
    conn.close()
    print(
        f"run_detectors({devnet}): {len(DETECTORS)} detectors, "
        f"{len(seen)} active events"
    )


# ---------------------------------------------------------------------------
# get_events_data / show_events (CLI + web seam)
# ---------------------------------------------------------------------------


def get_events_data(
    devnet: str,
    kind: str | None = None,
    severity: str | None = None,
    active_only: bool = False,
    include_resolved: bool = True,
) -> list[dict[str, Any]]:
    """
    Query events for a devnet. Returns a list of dicts ordered by:
    active first (severity crit > warn > info), then last_seen desc; resolved after.

    Annotates rows where count>2 AND last_seen-first_seen < 7200 as 'flapping'.
    """
    conn = connect()
    migrate(conn)

    clauses = ["devnet = ?"]
    params: list[Any] = [devnet]

    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    if severity:
        clauses.append("severity = ?")
        params.append(severity)
    if active_only:
        clauses.append("resolved_at IS NULL")
    elif not include_resolved:
        clauses.append("resolved_at IS NULL")

    where = " AND ".join(clauses)
    rows = conn.execute(
        f"SELECT * FROM events WHERE {where} ORDER BY last_seen DESC",
        params,
    ).fetchall()
    conn.close()

    _sev_order = {"crit": 0, "warn": 1, "info": 2}

    from datetime import datetime, timezone

    def _fmt(ts: int | None) -> str:
        if ts is None:
            return "-"
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            return str(ts)

    result = []
    for r in rows:
        det = {}
        if r["details"]:
            try:
                det = json.loads(r["details"])
            except Exception:
                det = {"raw": r["details"]}
        # Flapping = many re-observations in a short span (genuine churn), not just
        # a steadily-persistent condition. At a 15-min cadence, >5 counts in <1h
        # means it has been resolving and reopening.
        flapping = (
            (r["count"] or 0) > 5
            and (r["last_seen"] - r["first_seen"]) < 3600
        )
        result.append({
            "devnet": r["devnet"],
            "dedup_key": r["dedup_key"],
            "kind": r["kind"],
            "severity": r["severity"],
            "node": r["node"],
            "message": r["message"],
            "details": det,
            "first_seen": r["first_seen"],
            "last_seen": r["last_seen"],
            "first_seen_str": _fmt(r["first_seen"]),
            "last_seen_str": _fmt(r["last_seen"]),
            "resolved_at": r["resolved_at"],
            "resolved_at_str": _fmt(r["resolved_at"]),
            "count": r["count"],
            "active": r["resolved_at"] is None,
            "flapping": flapping,
        })

    # Sort: active first by severity then last_seen desc; resolved after by last_seen desc
    def _sort_key(e: dict) -> tuple:
        active_rank = 0 if e["active"] else 1
        sev_rank = _sev_order.get(e["severity"], 9)
        return (active_rank, sev_rank, -e["last_seen"])

    result.sort(key=_sort_key)
    return result


def show_events(
    devnet: str,
    kind: str | None = None,
    severity: str | None = None,
    active_only: bool = False,
    include_resolved: bool = True,
) -> None:
    """Print events table to stdout."""
    from datetime import datetime, timezone

    def _ts(ts: int | None) -> str:
        if ts is None:
            return "-"
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        except Exception:
            return str(ts)

    rows = get_events_data(
        devnet,
        kind=kind,
        severity=severity,
        active_only=active_only,
        include_resolved=include_resolved,
    )
    if not rows:
        print(f"events({devnet}): no events found")
        return

    print(f"\nEvents for {devnet}  ({len(rows)} total)\n")
    print(
        f"{'SEV':<5} {'KIND':<18} {'NODE':<28} {'MSG':<45} {'COUNT':>5} "
        f"{'LAST SEEN':>16} {'STATUS':<12}"
    )
    print("-" * 135)
    for e in rows:
        status = "ACTIVE" if e["active"] else "resolved"
        if e["flapping"]:
            status += " FLAPPING"
        node_col = (e["node"] or "-")[:27]
        msg_col = e["message"][:44]
        print(
            f"{e['severity']:<5} {e['kind']:<18} {node_col:<28} {msg_col:<45} "
            f"{e['count']:>5} {_ts(e['last_seen']):>16} {status:<12}"
        )
    print()


# ---------------------------------------------------------------------------
# Detector implementations. All detectors live in this module and register via
# @_register at import time, so DETECTORS is fully populated on import (no
# lazy-load hack, no double-registration risk, no cross-module import order).
# Detectors only query the SQLite tables, so they need no collector modules.
# ---------------------------------------------------------------------------


@_register
def detect_version_change(conn: Any, devnet: str, now: int, seen: set) -> None:
    """
    Detect an ethrex version/commit change on a node (deploy event).

    Compares each ethrex node's two most recent DISTINCT commits in node_health.
    Emits an info event for the latest transition, keyed by the new commit so the
    current deploy stays active and older transitions resolve as new ones land.
    """
    from .config import is_ethrex_node

    node_rows = conn.execute(
        "SELECT DISTINCT node FROM node_health WHERE devnet=?", (devnet,)
    ).fetchall()

    for nr in node_rows:
        node = nr["node"]
        if not is_ethrex_node(conn, devnet, node):
            continue
        # Distinct commit values in time order (newest first), ignoring rows with
        # no commit (e.g. a probe-error snapshot).
        rows = conn.execute(
            """SELECT "commit", buildnum, ts FROM node_health
               WHERE devnet=? AND node=? AND "commit" IS NOT NULL AND "commit" != ''
               ORDER BY ts DESC LIMIT 40""",
            (devnet, node),
        ).fetchall()
        # Collapse consecutive duplicates into distinct version points.
        distinct: list[dict] = []
        for r in rows:
            c = r["commit"]
            if not distinct or distinct[-1]["commit"] != c:
                distinct.append({"commit": c, "buildnum": r["buildnum"], "ts": r["ts"]})
        if len(distinct) < 2:
            continue  # only ever one version seen -> no change
        new, old = distinct[0], distinct[1]
        # Node is its own column; buildnums are in the message. Keep details to
        # just the two commits (rendered as clickable GitHub links) so the row
        # isn't a verbatim restatement of the message.
        details = {
            "from_commit": (old["commit"] or "")[:12],
            "to_commit": (new["commit"] or "")[:12],
        }
        key = record_event(
            conn, devnet,
            kind="version_change",
            severity="info",
            node=node,
            message=(
                f"ethrex upgraded {(old['commit'] or '')[:8]} -> "
                f"{(new['commit'] or '')[:8]} (bn {old['buildnum']} -> {new['buildnum']})"
            ),
            details=details,
            now=now,
            discriminator=(new["commit"] or "")[:12],
        )
        seen.add(key)


@_register
def detect_assertoor_fail(conn: Any, devnet: str, now: int, seen: set) -> None:
    """
    Detect failed Assertoor test runs (warn), one event per failed run_id.

    Only considers runs that stopped within the last 3 days so old failures
    (or pre-reset runs) age out and resolve instead of alerting forever.
    """
    rows = conn.execute(
        """
        SELECT run_id, name, test_id, status, started_at, stopped_at
        FROM assertoor_runs
        WHERE devnet=? AND status='failure'
          AND (stopped_at IS NULL OR stopped_at >= ?)
        ORDER BY run_id DESC
        LIMIT 20
        """,
        (devnet, now - 3 * 86400),
    ).fetchall()

    for r in rows:
        details = {
            "run_id": r["run_id"],
            "test_id": r["test_id"] or "",
            "started_at": r["started_at"],
        }
        key = record_event(
            conn, devnet,
            kind="assertoor_fail",
            severity="warn",
            node=None,
            message=f"Assertoor test failed: {r['name'] or r['test_id']} (run_id={r['run_id']})",
            details=details,
            now=now,
            discriminator=str(r["run_id"]),
        )
        seen.add(key)


# ---------------------------------------------------------------------------
# Phase 1 detectors (registered at module import via @_register)
# ---------------------------------------------------------------------------


@_register
def detect_chain_split(conn: Any, devnet: str, now: int, seen: set) -> None:
    """
    Detect ethrex nodes on a minority head root or different fork than majority.
    Requires >= 3 reporting nodes to avoid false positives on tiny devnets.
    """
    from .network import _latest_network_rows

    splits = _latest_network_rows(conn, devnet)
    if not splits:
        return

    # Count total reporting nodes across all forks
    total_nodes = sum(s.get("head_count", 0) or 0 for s in splits)
    if total_nodes < 3:
        return

    # Majority head = strictly > 50% of reporting nodes
    majority = None
    for s in splits:
        cnt = s.get("head_count", 0) or 0
        if cnt > total_nodes / 2:
            majority = s
            break

    if majority is None:
        # No majority: fire crit for every ethrex node that is not on the biggest fork
        majority = max(splits, key=lambda s: s.get("head_count", 0) or 0)

    majority_root = majority.get("head_root", "")

    from .config import is_ethrex_node

    # Each split row is one fork (head_root) carrying that fork's clients_json.
    # An ethrex node listed in a NON-majority fork is on the wrong head -> fire.
    for fork in splits:
        fork_root = fork.get("head_root", "") or ""
        if fork_root == majority_root:
            continue
        try:
            clients = json.loads(fork.get("clients_json") or "[]")
        except Exception:
            continue
        for client in clients:
            node_name = client.get("name", "") or ""
            if not is_ethrex_node(conn, devnet, node_name):
                continue
            details = {
                "node": node_name,
                "node_head_root": fork_root,
                "node_head_slot": fork.get("head_slot"),
                "majority_head_root": majority_root,
            }
            key = record_event(
                conn, devnet,
                kind="chain_split",
                severity="crit",
                node=node_name,
                message=f"{node_name} on minority fork (head {fork_root[:12]}..., majority {majority_root[:12]}...)",
                details=details,
                now=now,
                discriminator="minority_fork",
            )
            seen.add(key)


@_register
def detect_orphan_spike(conn: Any, devnet: str, now: int, seen: set) -> None:
    """
    Over last 256 slots, compare ethrex orphan rate vs other clients.
    Fire warn if ethrex_rate > 2 * others_rate AND ethrex_rate > 0.05.
    """
    from .blobtrack import _client_from_proposer

    max_row = conn.execute(
        "SELECT MAX(slot) FROM slots WHERE devnet=?", (devnet,)
    ).fetchone()
    if not max_row or max_row[0] is None:
        return
    cutoff = max_row[0] - 256

    rows = conn.execute(
        "SELECT proposer_name, status FROM slots WHERE devnet=? AND slot>=?",
        (devnet, cutoff),
    ).fetchall()
    if not rows:
        return

    ethrex_total = ethrex_orphaned = 0
    other_total = other_orphaned = 0
    for r in rows:
        client = _client_from_proposer(r["proposer_name"] or "unknown")
        orphaned = (r["status"] or "").lower() == "orphaned"
        if client == "ethrex":
            ethrex_total += 1
            if orphaned:
                ethrex_orphaned += 1
        else:
            other_total += 1
            if orphaned:
                other_orphaned += 1

    if ethrex_total == 0:
        return

    ethrex_rate = ethrex_orphaned / ethrex_total
    others_rate = other_orphaned / other_total if other_total > 0 else 0.0

    if ethrex_rate > 2 * others_rate and ethrex_rate > 0.05:
        details = {
            "ethrex_orphaned": ethrex_orphaned,
            "ethrex_total": ethrex_total,
            "ethrex_rate": round(ethrex_rate, 4),
            "others_orphaned": other_orphaned,
            "others_total": other_total,
            "others_rate": round(others_rate, 4),
            "window": "last 256 slots",
        }
        key = record_event(
            conn, devnet,
            kind="orphan_spike",
            severity="warn",
            node=None,
            message=(
                f"ethrex orphan rate {ethrex_rate:.1%} vs others "
                f"{others_rate:.1%} over last 256 slots"
            ),
            details=details,
            now=now,
            discriminator="",
        )
        seen.add(key)


@_register
def detect_wedge(conn: Any, devnet: str, now: int, seen: set) -> None:
    """
    For each ethrex node, compare two most recent node_health rows.
    Fire crit on: head not advancing, state_at_head != 'yes', restart climbing, peers == 0.
    """
    from .config import is_ethrex_node

    # Get all ethrex nodes with at least 2 health rows
    node_rows = conn.execute(
        "SELECT DISTINCT node FROM node_health WHERE devnet=?", (devnet,)
    ).fetchall()

    for nr in node_rows:
        node = nr["node"]
        if not is_ethrex_node(conn, devnet, node):
            continue

        rows = conn.execute(
            """SELECT head, peers, state_at_head, restart, syncing
               FROM node_health WHERE devnet=? AND node=?
               ORDER BY ts DESC LIMIT 2""",
            (devnet, node),
        ).fetchall()

        if not rows:
            continue

        latest = rows[0]

        # head not advancing (requires 2 rows)
        if len(rows) == 2:
            prev = rows[1]
            if (
                latest["head"] is not None
                and prev["head"] is not None
                and latest["head"] == prev["head"]
                and latest["head"] > 0
            ):
                details = {"head": latest["head"], "node": node}
                key = record_event(
                    conn, devnet,
                    kind="wedge",
                    severity="crit",
                    node=node,
                    message=f"{node}: head stuck at {latest['head']}",
                    details=details,
                    now=now,
                    discriminator="head_stuck",
                )
                seen.add(key)

            # restart climbing
            if (
                latest["restart"] is not None
                and prev["restart"] is not None
                and latest["restart"] > prev["restart"]
            ):
                details = {
                    "node": node,
                    "restart_prev": prev["restart"],
                    "restart_now": latest["restart"],
                }
                key = record_event(
                    conn, devnet,
                    kind="wedge",
                    severity="crit",
                    node=node,
                    message=f"{node}: restart count climbing ({prev['restart']} -> {latest['restart']})",
                    details=details,
                    now=now,
                    discriminator="restart_climb",
                )
                seen.add(key)

        # state_at_head not "yes" -- but a node that is still syncing legitimately
        # has no state at head yet, so only flag when NOT syncing.
        state = (latest["state_at_head"] or "").lower()
        syncing_raw = (latest["syncing"] or "").lower()
        is_syncing = syncing_raw.startswith("cur=") or syncing_raw in ("true", "1", "yes")
        if state and state != "yes" and not is_syncing:
            details = {"node": node, "state_at_head": latest["state_at_head"]}
            key = record_event(
                conn, devnet,
                kind="wedge",
                severity="crit",
                node=node,
                message=f"{node}: state_at_head={latest['state_at_head']}",
                details=details,
                now=now,
                discriminator="state_at_head",
            )
            seen.add(key)

        # peers == 0
        if latest["peers"] is not None and latest["peers"] == 0:
            details = {"node": node, "peers": 0}
            key = record_event(
                conn, devnet,
                kind="wedge",
                severity="crit",
                node=node,
                message=f"{node}: 0 peers",
                details=details,
                now=now,
                discriminator="no_peers",
            )
            seen.add(key)


@_register
def detect_node_unreachable(conn: Any, devnet: str, now: int, seen: set) -> None:
    """
    Latest node_health row with a probe error (syncing field contains the error) -> crit.
    Health stores probe errors in the syncing column (see health.py).
    """
    from .config import is_ethrex_node

    rows = conn.execute(
        """SELECT node, syncing, ts
           FROM node_health
           WHERE devnet=? AND ts = (
               SELECT MAX(ts) FROM node_health nh2
               WHERE nh2.devnet = node_health.devnet AND nh2.node = node_health.node
           )""",
        (devnet,),
    ).fetchall()

    for r in rows:
        node = r["node"]
        if not is_ethrex_node(conn, devnet, node):
            continue
        syncing_val = r["syncing"] or ""
        # Probe errors are stored verbatim in the syncing column by health.py.
        # Reachable values produced by remote.py:
        #   "synced(false)" / "synced(true)"  -- synced
        #   "cur=N->hi=M"                      -- actively syncing (reachable!)
        #   "unknown"                          -- rpc responded, odd shape (reachable)
        #   "true"/"false"/"yes"/"no"/"1"/"0"
        # Real errors: "Command ..." (timeout), "ssh exit N: ..."
        sv_lower = syncing_val.lower()
        is_error = bool(syncing_val) and (
            sv_lower.startswith("command ")
            or sv_lower.startswith("ssh exit")
        )
        if is_error:
            # Turn the raw subprocess/ssh error into a concise human message;
            # keep the raw text in details for debugging.
            if "timed out" in sv_lower:
                msg = f"{node}: ssh probe timed out (node unreachable)"
            elif sv_lower.startswith("ssh exit"):
                # "ssh exit N: <stderr>" -> keep the tail, trimmed
                msg = f"{node}: ssh failed ({syncing_val.split(':', 1)[-1].strip()[:80] or 'connection error'})"
            else:
                msg = f"{node}: probe failed"
            details = {"node": node, "raw_error": syncing_val[:300]}
            key = record_event(
                conn, devnet,
                kind="node_unreachable",
                severity="crit",
                node=node,
                message=msg,
                details=details,
                now=now,
                discriminator="probe_error",
            )
            seen.add(key)


@_register
def detect_blob_decay(conn: Any, devnet: str, now: int, seen: set) -> None:
    """
    Ethrex proposer avg blob_count over a window trending to ~0 -> warn.
    Uses last 64 slots as the window.

    Spamoor guard: if the latest spamoor snapshot shows no active blob spammer
    (scenario contains 'blob' AND status==1), skip blob_decay and instead record
    a blob_load_off info event explaining the gap. If spamoor data is absent
    entirely, fall through to the existing relative check.
    """
    from .blobtrack import _client_from_proposer
    from .spamoor import _blob_spammer_active

    # --- Spamoor guard ---
    blob_active = _blob_spammer_active(conn, devnet)
    if blob_active is False:
        # No blob spammer running; a 0-blob window is expected. Record info event.
        details = {"reason": "no active blob spammer (spamoor status: stopped)"}
        key = record_event(
            conn, devnet,
            kind="blob_load_off",
            severity="info",
            node=None,
            message="blob load off: no active blob spammer (spamoor confirms)",
            details=details,
            now=now,
            discriminator="",
        )
        seen.add(key)
        return
    # blob_active is None: no spamoor data; fall through to relative check.
    # blob_active is True: spammer is running; proceed with decay check.

    max_row = conn.execute(
        "SELECT MAX(slot) FROM slots WHERE devnet=?", (devnet,)
    ).fetchone()
    if not max_row or max_row[0] is None:
        return
    cutoff = max_row[0] - 64

    rows = conn.execute(
        """SELECT proposer_name, blob_count FROM slots
           WHERE devnet=? AND slot>=? AND status='Canonical'""",
        (devnet, cutoff),
    ).fetchall()
    if not rows:
        return

    ethrex_blobs: list[int] = []
    other_blobs: list[int] = []
    for r in rows:
        client = _client_from_proposer(r["proposer_name"] or "unknown")
        bc = r["blob_count"] if r["blob_count"] is not None else 0
        if client == "ethrex":
            ethrex_blobs.append(bc)
        else:
            other_blobs.append(bc)

    # Need enough ethrex samples AND a peer baseline to make a relative call.
    if len(ethrex_blobs) < 4 or len(other_blobs) < 4:
        return

    ethrex_avg = sum(ethrex_blobs) / len(ethrex_blobs)
    others_avg = sum(other_blobs) / len(other_blobs)

    # Decay is RELATIVE: only fire when peers are including blobs (so the spammer
    # is clearly active) but ethrex is not. If everyone is ~0 (spammer off or a
    # network-wide issue), this is NOT ethrex decay; stay quiet.
    if others_avg >= 0.5 and ethrex_avg < 0.2 * others_avg:
        details = {
            "ethrex_avg_blobs": round(ethrex_avg, 3),
            "others_avg_blobs": round(others_avg, 3),
            "ethrex_slots": len(ethrex_blobs),
            "other_slots": len(other_blobs),
            "window": "last 64 canonical slots",
        }
        key = record_event(
            conn, devnet,
            kind="blob_decay",
            severity="warn",
            node=None,
            message=(
                f"ethrex avg blobs {ethrex_avg:.2f} vs peers {others_avg:.2f} "
                f"(last 64 canonical slots)"
            ),
            details=details,
            now=now,
            discriminator="",
        )
        seen.add(key)


@_register
def detect_bal_anomaly(conn: Any, devnet: str, now: int, seen: set) -> None:
    """
    Detect a run of recent ethrex BAL entries where access_count == 0.

    A single slot with zero BAL entries can be a legitimately empty block,
    so this fires only when 3 or more of the last 10 ethrex BAL entries have
    access_count == 0. That avoids per-slot noise while still catching a
    systematic BAL bug. Discriminator is fixed ("recent_zero_bal") so the
    event updates in-place rather than spawning one event per slot.
    """
    rows = conn.execute(
        """
        SELECT slot, access_count
        FROM bal_inspect
        WHERE devnet = ?
        ORDER BY slot DESC
        LIMIT 10
        """,
        (devnet,),
    ).fetchall()

    # Require a meaningful sample before concluding anything; 3-of-3 on a brand
    # new devnet is not signal.
    if len(rows) < 5:
        return

    zero_slots = [r["slot"] for r in rows if (r["access_count"] or 0) == 0]
    total = len(rows)

    if len(zero_slots) < 3:
        return  # Threshold not met; stay quiet.

    details = {
        "zero_access_slots": zero_slots,
        "zero_count": len(zero_slots),
        "window": f"last {total} ethrex BAL entries",
    }
    key = record_event(
        conn, devnet,
        kind="bal_anomaly",
        severity="warn",
        node=None,
        message=(
            f"{len(zero_slots)}/{total} recent ethrex slots have BAL access_count=0 "
            f"(possible EIP-7928 bug)"
        ),
        details=details,
        now=now,
        discriminator="recent_zero_bal",
    )
    seen.add(key)


# ---------------------------------------------------------------------------
# T3 detectors: exec_regression, finality_stall, head_lag
# ---------------------------------------------------------------------------


@_register
def detect_exec_regression(conn: Any, devnet: str, now: int, seen: set) -> None:
    """Detect ethrex execution-time regression vs peers and vs its own prior baseline.

    Over the last ~200 canonical slots, compare:
    - ethrex recent mean vs peer median (non-ethrex clients)
    - ethrex recent mean vs ethrex prior-window baseline

    Fires warn only when:
      ethrex_mean > 50 ms  AND
      (ethrex_mean > 1.5 * peer_median  OR  ethrex_mean > 1.5 * ethrex_baseline)

    Requires >= 20 ethrex samples and >= 1 peer with samples; stays quiet otherwise.
    """
    from .analyze import median as _median, peer_ratio as _peer_ratio, baseline_shift as _baseline_shift

    WINDOW = 200
    HALF = WINDOW // 2

    max_row = conn.execute(
        "SELECT MAX(slot) FROM slot_exec_times WHERE devnet=?", (devnet,)
    ).fetchone()
    if not max_row or max_row[0] is None:
        return
    max_slot = int(max_row[0])
    cutoff_recent = max_slot - HALF
    cutoff_prior  = max_slot - WINDOW

    # Fetch recent window (second half of the 200-slot span)
    recent_rows = conn.execute(
        """SELECT client_type, avg_time FROM slot_exec_times
           WHERE devnet=? AND slot > ? AND avg_time IS NOT NULL""",
        (devnet, cutoff_recent),
    ).fetchall()
    # Fetch prior window (first half)
    prior_rows = conn.execute(
        """SELECT client_type, avg_time FROM slot_exec_times
           WHERE devnet=? AND slot > ? AND slot <= ? AND avg_time IS NOT NULL""",
        (devnet, cutoff_prior, cutoff_recent),
    ).fetchall()

    ethrex_recent: list[float] = []
    ethrex_prior:  list[float] = []
    peer_recent:   dict[str, list[float]] = {}

    for r in recent_rows:
        ct = r["client_type"] or "unknown"
        if ct == "ethrex":
            ethrex_recent.append(r["avg_time"])
        else:
            peer_recent.setdefault(ct, []).append(r["avg_time"])

    for r in prior_rows:
        ct = r["client_type"] or "unknown"
        if ct == "ethrex":
            ethrex_prior.append(r["avg_time"])

    # Guard: thin data -> skip
    if len(ethrex_recent) < 20:
        return
    if not peer_recent:
        return

    ethrex_mean = sum(ethrex_recent) / len(ethrex_recent)

    # Must exceed 50 ms to even consider firing
    if ethrex_mean <= 50.0:
        return

    # Peer median: compute median per-client, then median of those medians.
    # This keeps one slow outlier (erigon) from suppressing a real ethrex regression.
    per_peer_medians = [_median(v) for v in peer_recent.values() if v]
    valid_peer_meds = [m for m in per_peer_medians if m is not None]
    peer_med = _median(valid_peer_meds)

    ratio_vs_peers = _peer_ratio(ethrex_mean, valid_peer_meds)
    ratio_vs_baseline = _baseline_shift(ethrex_recent, ethrex_prior)

    fires = False
    if ratio_vs_peers is not None and ratio_vs_peers > 1.5:
        fires = True
    if ratio_vs_baseline is not None and ratio_vs_baseline > 1.5:
        fires = True

    if not fires:
        return

    baseline_ms = (sum(ethrex_prior) / len(ethrex_prior)) if ethrex_prior else None
    details = {
        "ethrex_ms": round(ethrex_mean, 1),
        "peer_median_ms": round(peer_med, 1) if peer_med is not None else None,
        "baseline_ms": round(baseline_ms, 1) if baseline_ms is not None else None,
        "ratio_vs_peers": round(ratio_vs_peers, 2) if ratio_vs_peers is not None else None,
        "ratio_vs_baseline": round(ratio_vs_baseline, 2) if ratio_vs_baseline is not None else None,
        "window": f"last {WINDOW} slots (recent/prior halves of {HALF} each)",
    }
    peer_med_str = f"{peer_med:.1f}" if peer_med is not None else "n/a"
    baseline_str = f"{baseline_ms:.1f}" if baseline_ms is not None else "n/a"
    key = record_event(
        conn, devnet,
        kind="exec_regression",
        severity="warn",
        node=None,
        message=(
            f"ethrex exec time elevated: {ethrex_mean:.1f} ms avg "
            f"(peer median {peer_med_str} ms, baseline {baseline_str} ms)"
        ),
        details=details,
        now=now,
        discriminator="exec_regression",
    )
    seen.add(key)


@_register
def detect_finality_stall(conn: Any, devnet: str, now: int, seen: set) -> None:
    """Detect finality not advancing across a span of >= 20 minutes.

    Requires >= 3 snapshots in network_overview. Fires crit if the latest
    finalized_epoch equals the finalized_epoch from the earliest snapshot
    in a window of at least ~30 min, and the non-advancing span is >= 20 min.
    """
    # Finality advances every ~2 epochs (~12.8 min); use a generous span so a
    # Dora collection gap that recovers with same-epoch rows doesn't false-fire.
    WINDOW_SECS   = 60 * 60  # 60-minute look-back
    MIN_SPAN_SECS = 40 * 60  # stall must span >= 40 minutes

    cutoff = now - WINDOW_SECS

    rows = conn.execute(
        """SELECT ts, finalized_epoch FROM network_overview
           WHERE devnet=? AND ts >= ? AND finalized_epoch IS NOT NULL
           ORDER BY ts ASC""",
        (devnet, cutoff),
    ).fetchall()

    if len(rows) < 3:
        return

    earliest = rows[0]
    latest   = rows[-1]
    span = latest["ts"] - earliest["ts"]

    if span < MIN_SPAN_SECS:
        return

    if latest["finalized_epoch"] != earliest["finalized_epoch"]:
        return

    epoch = latest["finalized_epoch"]
    span_minutes = span / 60

    details = {
        "finalized_epoch": epoch,
        "span_minutes": round(span_minutes, 1),
        "snapshots": len(rows),
    }
    key = record_event(
        conn, devnet,
        kind="finality_stall",
        severity="warn",
        node=None,
        message=f"finality stalled at epoch {epoch} (~{span_minutes:.0f} min, no advance)",
        details=details,
        now=now,
        discriminator="finality_stall",
    )
    seen.add(key)


@_register
def detect_head_lag(conn: Any, devnet: str, now: int, seen: set) -> None:
    """Detect ethrex nodes whose EL head is > 30 blocks behind the fleet max.

    Fleet max is computed across all nodes (ethrex + others). Nodes that are
    actively syncing are excluded to avoid false positives during catch-up.
    """
    from .config import is_ethrex_node

    LAG_THRESHOLD = 30
    STALE_SECS = 1800  # a snapshot older than this means the node is unreachable

    # Latest snapshot per node (correlated-subquery pattern from existing detectors)
    rows = conn.execute(
        """SELECT node, head, syncing, ts FROM node_health
           WHERE devnet=? AND ts = (
               SELECT MAX(ts) FROM node_health nh2
               WHERE nh2.devnet = node_health.devnet AND nh2.node = node_health.node
           )""",
        (devnet,),
    ).fetchall()

    if not rows:
        return

    # Fleet max head across all nodes with a valid (non-None, > 0) head
    heads = [r["head"] for r in rows if r["head"] is not None and r["head"] > 0]
    if not heads:
        return

    fleet_max = max(heads)

    for r in rows:
        node = r["node"]
        if not is_ethrex_node(conn, devnet, node):
            continue
        head = r["head"]
        if head is None:
            continue

        # Skip nodes whose latest snapshot is stale (unreachable) -- node_unreachable
        # already covers those; an old head would look falsely far behind.
        if r["ts"] is not None and r["ts"] < now - STALE_SECS:
            continue

        # Skip nodes that are actively syncing
        syncing_raw = (r["syncing"] or "").lower()
        is_syncing = (
            syncing_raw.startswith("cur=")
            or syncing_raw in ("yes", "true", "1")
        )
        if is_syncing:
            continue

        lag = fleet_max - head
        if lag > LAG_THRESHOLD:
            details = {
                "node": node,
                "head": head,
                "fleet_max_head": fleet_max,
                "lag_blocks": lag,
            }
            key = record_event(
                conn, devnet,
                kind="head_lag",
                severity="warn",
                node=node,
                message=(
                    f"{node} is {lag} blocks behind fleet head {fleet_max} "
                    f"(node head: {head})"
                ),
                details=details,
                now=now,
                discriminator="head_lag",
            )
            seen.add(key)
