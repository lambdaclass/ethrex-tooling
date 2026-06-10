"""Proposals analysis: per-proposer canonical/missed/orphaned summary."""

from __future__ import annotations

from typing import Any

from .blobtrack import _client_from_proposer, _resolve_since
from .store import connect, migrate


def get_proposals_data(devnet: str, since: str | None = None) -> dict[str, Any] | None:
    """
    Return per-proposer canonical/missed/orphaned counts and orphan rates.
    Also aggregates by client (ethrex vs others).
    Returns None if no slot data exists.
    """
    conn = connect()
    migrate(conn)

    slot_count = _resolve_since(since)

    base_query = "SELECT slot, proposer_name, status FROM slots WHERE devnet = ?"
    params: list[Any] = [devnet]

    if slot_count is not None:
        max_row = conn.execute(
            "SELECT MAX(slot) FROM slots WHERE devnet = ?", (devnet,)
        ).fetchone()
        if max_row and max_row[0] is not None:
            cutoff = max_row[0] - slot_count
            base_query += " AND slot >= ?"
            params.append(cutoff)

    base_query += " ORDER BY slot ASC"

    rows = conn.execute(base_query, params).fetchall()
    conn.close()

    if not rows:
        return None

    # Per-proposer counts
    proposer_stats: dict[str, dict[str, int]] = {}
    for r in rows:
        name = r["proposer_name"] or "unknown"
        status = (r["status"] or "unknown").lower()
        if name not in proposer_stats:
            proposer_stats[name] = {"canonical": 0, "missed": 0, "orphaned": 0, "total": 0}
        proposer_stats[name]["total"] += 1
        if status == "canonical":
            proposer_stats[name]["canonical"] += 1
        elif status in ("missing", "missed"):
            proposer_stats[name]["missed"] += 1
        elif status == "orphaned":
            proposer_stats[name]["orphaned"] += 1

    # Per-client aggregation
    client_stats: dict[str, dict[str, int]] = {}
    for name, stats in proposer_stats.items():
        client = _client_from_proposer(name)
        if client not in client_stats:
            client_stats[client] = {"canonical": 0, "missed": 0, "orphaned": 0, "total": 0}
        for k in ("canonical", "missed", "orphaned", "total"):
            client_stats[client][k] += stats[k]

    # Build output rows with orphan rate
    proposers_out = []
    for name, stats in sorted(
        proposer_stats.items(),
        key=lambda kv: kv[1]["orphaned"] / kv[1]["total"] if kv[1]["total"] else 0,
        reverse=True,
    ):
        total = stats["total"]
        orphan_rate = stats["orphaned"] / total if total else 0.0
        client = _client_from_proposer(name)
        proposers_out.append({
            "name": name,
            "client": client,
            "is_ethrex": client == "ethrex",
            "canonical": stats["canonical"],
            "missed": stats["missed"],
            "orphaned": stats["orphaned"],
            "total": total,
            "orphan_rate": round(orphan_rate, 4),
        })

    clients_out = []
    for client, stats in sorted(
        client_stats.items(),
        key=lambda kv: kv[1]["orphaned"] / kv[1]["total"] if kv[1]["total"] else 0,
        reverse=True,
    ):
        total = stats["total"]
        orphan_rate = stats["orphaned"] / total if total else 0.0
        clients_out.append({
            "client": client,
            "is_ethrex": client == "ethrex",
            "canonical": stats["canonical"],
            "missed": stats["missed"],
            "orphaned": stats["orphaned"],
            "total": total,
            "orphan_rate": round(orphan_rate, 4),
        })

    total_slots = len(rows)
    max_slot_num = max(r["slot"] for r in rows)
    min_slot_num = min(r["slot"] for r in rows)
    window_label = f"slots {min_slot_num}-{max_slot_num} ({total_slots} slots)"

    return {
        "window_label": window_label,
        "min_slot": min_slot_num,
        "max_slot": max_slot_num,
        "total_slots": total_slots,
        "proposers": proposers_out,
        "clients": clients_out,
    }


def show_proposals(devnet: str, since: str | None = None) -> None:
    """Print per-proposer and per-client orphan summary."""
    data = get_proposals_data(devnet, since=since)
    if data is None:
        print(f"proposals({devnet}): no slot data. Run: dv collect {devnet} blobs")
        return

    print(f"\nProposals summary ({devnet})")
    print(f"Window: {data['window_label']}\n")

    # Client comparison
    print("EL client comparison:\n")
    print(f"{'CLIENT':<16} {'TOTAL':>7} {'CANONICAL':>10} {'MISSED':>7} {'ORPHANED':>9} {'ORPHAN%':>9}")
    print("-" * 65)
    for c in data["clients"]:
        marker = " <-- ethrex" if c["is_ethrex"] else ""
        print(
            f"{c['client']:<16} {c['total']:>7} {c['canonical']:>10} "
            f"{c['missed']:>7} {c['orphaned']:>9} {c['orphan_rate']:>8.1%}{marker}"
        )

    print(f"\nPer-proposer breakdown ({len(data['proposers'])} proposers):\n")
    col_w = max((len(p["name"]) for p in data["proposers"]), default=20) + 2
    print(
        f"{'PROPOSER':<{col_w}} {'CLIENT':<12} {'TOTAL':>6} "
        f"{'CANONICAL':>10} {'MISSED':>7} {'ORPHANED':>9} {'ORPHAN%':>9}"
    )
    print("-" * (col_w + 65))
    for p in data["proposers"]:
        print(
            f"{p['name']:<{col_w}} {p['client']:<12} {p['total']:>6} "
            f"{p['canonical']:>10} {p['missed']:>7} {p['orphaned']:>9} "
            f"{p['orphan_rate']:>8.1%}"
        )
    print()
