"""Execution-time analysis: per-client comparison and trend chart data.

Reads slot_exec_times (already collected by dora.py); no new collection.
"""

from __future__ import annotations

from typing import Any

from .store import connect, migrate


# Number of chart buckets (mirrors blobs.html)
_CHART_BUCKETS = 60


def get_exectime_data(devnet: str, window_slots: int = 1500) -> dict[str, Any] | None:
    """Return per-client exec-time summary and a binned time-series for charting.

    Queries slot_exec_times for the most recent window_slots slots.

    Returned dict:
      window_label  str
      clients       list[dict]  per client: client_type, samples, avg_ms, min_ms,
                                max_ms, is_ethrex
      series        dict[str, list[[bucket_index, avg_ms]]]  binned per client
      min_slot      int
      max_slot      int
    """
    conn = connect()
    migrate(conn)

    max_row = conn.execute(
        "SELECT MAX(slot) FROM slot_exec_times WHERE devnet=?", (devnet,)
    ).fetchone()
    if not max_row or max_row[0] is None:
        conn.close()
        return None

    max_slot = int(max_row[0])
    min_slot = max_slot - window_slots

    rows = conn.execute(
        """SELECT slot, client_type, count, avg_time, min_time, max_time
           FROM slot_exec_times
           WHERE devnet=? AND slot > ?
           ORDER BY slot ASC""",
        (devnet, min_slot),
    ).fetchall()
    conn.close()

    if not rows:
        return None

    # Per-client aggregation
    client_agg: dict[str, dict[str, Any]] = {}
    # Per-client per-slot raw points for the time-series
    client_pts: dict[str, list[tuple[int, float]]] = {}

    actual_min = min(r["slot"] for r in rows)
    actual_max = max(r["slot"] for r in rows)

    for r in rows:
        ct = r["client_type"] or "unknown"
        if ct not in client_agg:
            client_agg[ct] = {
                "sum_avg": 0.0,
                "count": 0,
                "min_ms": float("inf"),
                "max_ms": float("-inf"),
            }
        if r["avg_time"] is not None and r["count"] and r["count"] > 0:
            client_agg[ct]["sum_avg"] += r["avg_time"]
            client_agg[ct]["count"] += r["count"]
            if r["min_time"] is not None:
                client_agg[ct]["min_ms"] = min(client_agg[ct]["min_ms"], r["min_time"])
            if r["max_time"] is not None:
                client_agg[ct]["max_ms"] = max(client_agg[ct]["max_ms"], r["max_time"])
            client_pts.setdefault(ct, []).append((r["slot"], r["avg_time"]))

    if not client_agg:
        return None

    clients_out: list[dict[str, Any]] = []
    for ct, agg in sorted(client_agg.items()):
        n = agg["count"]
        if n == 0:
            continue
        # avg_ms = mean of per-slot avg_time values (not weighted by count,
        # matching the "avg of avg_time" spec)
        pts = client_pts.get(ct, [])
        avg_ms = sum(p[1] for p in pts) / len(pts) if pts else 0.0
        min_ms = agg["min_ms"] if agg["min_ms"] != float("inf") else None
        max_ms = agg["max_ms"] if agg["max_ms"] != float("-inf") else None
        clients_out.append({
            "client_type": ct,
            "samples": n,
            "avg_ms": round(avg_ms, 1),
            "min_ms": round(min_ms, 1) if min_ms is not None else None,
            "max_ms": round(max_ms, 1) if max_ms is not None else None,
            "is_ethrex": ct == "ethrex",
        })

    # Sort: ethrex first, then by avg_ms ascending
    clients_out.sort(key=lambda c: (0 if c["is_ethrex"] else 1, c["avg_ms"]))

    # Binned time-series per client (~60 buckets)
    span = (actual_max - actual_min) or 1
    series: dict[str, list[list]] = {}
    for ct, pts in client_pts.items():
        bucket_sum: list[float] = [0.0] * _CHART_BUCKETS
        bucket_cnt: list[int] = [0] * _CHART_BUCKETS
        for slot, avg_t in pts:
            b = min(_CHART_BUCKETS - 1, int((slot - actual_min) / span * _CHART_BUCKETS))
            bucket_sum[b] += avg_t
            bucket_cnt[b] += 1
        binned = []
        for i in range(_CHART_BUCKETS):
            if bucket_cnt[i] > 0:
                binned.append([i, round(bucket_sum[i] / bucket_cnt[i], 1)])
        series[ct] = binned

    window_label = f"slots {actual_min}-{actual_max} (last {window_slots} slots)"

    return {
        "window_label": window_label,
        "clients": clients_out,
        "series": series,
        "min_slot": actual_min,
        "max_slot": actual_max,
    }


def show_exectime(devnet: str) -> None:
    """Print per-client exec-time comparison table and a one-line verdict."""
    from .analyze import peer_ratio

    data = get_exectime_data(devnet)
    if data is None:
        print(f"exectime({devnet}): no data in slot_exec_times. Run: dv collect {devnet} blobs")
        return

    clients = data["clients"]
    print(f"\nExec time per client -- {devnet}")
    print(f"Window: {data['window_label']}\n")

    col_w = max(len(c["client_type"]) for c in clients) + 2 if clients else 16
    print(
        f"{'CLIENT':<{col_w}} {'SAMPLES':>8} {'AVG ms':>8} {'MIN ms':>8} {'MAX ms':>8}"
    )
    print("-" * (col_w + 36))
    for c in clients:
        marker = " <-- ethrex" if c["is_ethrex"] else ""
        min_s = f"{c['min_ms']:.1f}" if c["min_ms"] is not None else "-"
        max_s = f"{c['max_ms']:.1f}" if c["max_ms"] is not None else "-"
        print(
            f"{c['client_type']:<{col_w}} {c['samples']:>8} {c['avg_ms']:>8.1f} "
            f"{min_s:>8} {max_s:>8}{marker}"
        )

    # Verdict: ethrex vs peer median
    ethrex_row = next((c for c in clients if c["is_ethrex"]), None)
    peer_avgs = [c["avg_ms"] for c in clients if not c["is_ethrex"]]
    print()
    if ethrex_row is None:
        print("verdict: no ethrex data in this window")
    elif not peer_avgs:
        print(f"verdict: ethrex avg {ethrex_row['avg_ms']:.1f} ms (no peer baseline)")
    else:
        ratio = peer_ratio(ethrex_row["avg_ms"], peer_avgs)
        from .analyze import median as _median
        peer_med = _median(peer_avgs)
        if ratio is None:
            print(f"verdict: ethrex avg {ethrex_row['avg_ms']:.1f} ms, peer median unavailable")
        else:
            direction = "faster" if ratio < 1.0 else "slower"
            print(
                f"verdict: ethrex {ethrex_row['avg_ms']:.1f} ms avg; "
                f"peer median {peer_med:.1f} ms; "
                f"ratio {ratio:.2f}x ({direction})"
            )
    print()
