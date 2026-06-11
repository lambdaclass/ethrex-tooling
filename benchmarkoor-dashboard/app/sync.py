"""Sync the Benchmarkoor API into the local SQLite snapshot.

Run with:  uv run python -m app.sync

Steps:
  1. List suites; find each suite's latest run timestamp (cheap 1-row queries).
  2. Active set = suites with a run within ACTIVE_WINDOW_DAYS of the newest run.
  3. Pull all runs for active suites; derive suite-level Mgas/s.
  4. Mark the latest *completed* run per (suite, instance) as current.
  5. Pull per-test stats for each current run.
Idempotent: re-running refreshes everything.
"""

from __future__ import annotations

import time

from . import config, db
from .client import Client
from .parse import parse_suite_name, parse_test_name

WINDOW = config.ACTIVE_WINDOW_DAYS * 86400


def _mgas_s(gas_used: int, dur_ns: int) -> float | None:
    if not gas_used or not dur_ns:
        return None
    return gas_used * 1000.0 / dur_ns  # gas/(ns) * 1e3 == Mgas/s


def sync(verbose: bool = True) -> dict[str, int]:
    conn = db.connect()
    db.init(conn)
    counts = {"suites": 0, "active": 0, "runs": 0, "current_runs": 0, "test_rows": 0}

    with Client() as c:
        # 1. suites + latest run ts each
        suites = c.query("suites", {"limit": 500})
        latest: dict[str, int] = {}
        for s in suites:
            h = s["suite_hash"]
            rows = c.query(
                "runs",
                {"suite_hash": f"eq.{h}", "order": "timestamp.desc", "limit": 1},
            )
            latest[h] = rows[0]["timestamp"] if rows else 0
        counts["suites"] = len(suites)

        # 2. active set = the *newest* suite per name, among suites with a recent run.
        # Old regenerated duplicates (same name, earlier indexed_at) are dropped: they're
        # historical and not forward-looking, and the big ones make sync slow.
        global_max = max(latest.values(), default=0)
        recent = [
            s for s in suites if latest.get(s["suite_hash"], 0) >= global_max - WINDOW
        ]
        by_name: dict[str, tuple[str, str]] = {}
        for s in recent:
            name, ia = s.get("name", ""), s.get("indexed_at", "") or ""
            if name not in by_name or ia > by_name[name][1]:
                by_name[name] = (s["suite_hash"], ia)
        active = {h for h, _ in by_name.values()}
        counts["active"] = len(active)
        if verbose:
            print(
                f"{len(suites)} suites, {len(active)} active "
                f"(newest-per-name, window {config.ACTIVE_WINDOW_DAYS}d)"
            )

        # upsert suite rows
        for s in suites:
            h = s["suite_hash"]
            parsed = parse_suite_name(s.get("name", ""))
            conn.execute(
                """INSERT INTO suites
                   (suite_hash,name,network,block,fork,variant,tests_total,indexed_at,latest_run_ts,is_active)
                   VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(suite_hash) DO UPDATE SET
                     name=excluded.name, network=excluded.network, block=excluded.block,
                     fork=excluded.fork, variant=excluded.variant, tests_total=excluded.tests_total,
                     indexed_at=excluded.indexed_at, latest_run_ts=excluded.latest_run_ts,
                     is_active=excluded.is_active""",
                (
                    h,
                    s.get("name"),
                    parsed["network"],
                    parsed["block"],
                    parsed["fork"],
                    parsed["variant"],
                    s.get("tests_total"),
                    s.get("indexed_at"),
                    latest.get(h, 0),
                    1 if h in active else 0,
                ),
            )
        conn.commit()

        # 3. runs for active suites
        conn.execute(
            "DELETE FROM runs WHERE suite_hash IN (%s)" % ",".join("?" * len(active)),
            tuple(active),
        ) if active else None
        for h in active:
            runs = c.paginate(
                "runs", {"suite_hash": f"eq.{h}", "order": "timestamp.desc"}
            )
            for r in runs:
                step = (r.get("steps_json") or {}).get("test") or {}
                gas, dur = step.get("gas_used"), step.get("gas_used_duration")
                conn.execute(
                    """INSERT INTO runs
                       (run_id,suite_hash,timestamp,timestamp_end,status,client,instance_id,image,
                        rollback_strategy,tests_total,tests_passed,tests_failed,
                        test_gas_used,test_gas_used_duration,mgas_s,is_current)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)
                       ON CONFLICT(run_id) DO UPDATE SET
                         status=excluded.status, tests_passed=excluded.tests_passed,
                         tests_failed=excluded.tests_failed, mgas_s=excluded.mgas_s""",
                    (
                        r["run_id"],
                        h,
                        r.get("timestamp"),
                        r.get("timestamp_end"),
                        r.get("status"),
                        r.get("client"),
                        r.get("instance_id"),
                        r.get("image"),
                        r.get("rollback_strategy"),
                        r.get("tests_total"),
                        r.get("tests_passed"),
                        r.get("tests_failed"),
                        gas,
                        dur,
                        _mgas_s(gas, dur),
                    ),
                )
            counts["runs"] += len(runs)
        conn.commit()

        # 4+5. current run per (suite, instance) = newest *full* completed run.
        # A run is "full" only when its test_stats row count == the suite's tests_total.
        # The newest completed run can report tests_passed=N yet have truncated per-test
        # data (API index lag), which skews aggregates — so we fall back to the newest
        # run that is actually complete, scanning at most MAX_CANDIDATES per instance.
        MAX_CANDIDATES = 6
        conn.execute("UPDATE runs SET is_current=0, is_full=0")
        conn.execute("DELETE FROM test_stats")
        expected = (
            dict(
                conn.execute(
                    "SELECT suite_hash, tests_total FROM suites WHERE suite_hash IN (%s)"
                    % ",".join("?" * len(active)),
                    tuple(active),
                ).fetchall()
            )
            if active
            else {}
        )
        pairs = (
            conn.execute(
                "SELECT DISTINCT suite_hash, instance_id FROM runs "
                "WHERE status='completed' AND suite_hash IN (%s)"
                % ",".join("?" * len(active)),
                tuple(active),
            ).fetchall()
            if active
            else []
        )
        counts["current_runs"] = 0
        counts["partial_current"] = 0
        for i, pr in enumerate(pairs, 1):
            sh, inst = pr["suite_hash"], pr["instance_id"]
            need = expected.get(sh)
            cands = conn.execute(
                "SELECT run_id, client FROM runs WHERE suite_hash=? AND instance_id=? "
                "AND status='completed' ORDER BY timestamp DESC LIMIT ?",
                (sh, inst, MAX_CANDIDATES),
            ).fetchall()
            best = None  # (run_id, client, rows)
            for cand in cands:
                rows = c.paginate(
                    "test_stats", {"run_id": f"eq.{cand['run_id']}", "order": "id.asc"}
                )
                if best is None or len(rows) > len(best[2]):
                    best = (cand["run_id"], cand["client"], rows)
                if need and len(rows) == need:
                    break
            if best is None:
                continue
            rid, client, rows = best
            is_full = 1 if (need and len(rows) == need) else 0
            if not is_full:
                counts["partial_current"] += 1
            for r in rows:
                p = parse_test_name(r["test_name"])
                conn.execute(
                    """INSERT OR REPLACE INTO test_stats
                       (run_id,suite_hash,client,instance_id,test_name,file,fork,benchmark_mgas,
                        test_mgas_s,test_time_ns,test_gas_used,rpc_calls,cpu_usec,memory_bytes,
                        disk_read_bytes,disk_write_bytes)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        rid,
                        sh,
                        client,
                        inst,
                        r["test_name"],
                        p["file"],
                        p["fork"],
                        p["benchmark_mgas"],
                        r.get("test_mgas_s"),
                        r.get("test_time_ns"),
                        r.get("test_gas_used"),
                        r.get("test_rpc_calls_count"),
                        r.get("test_resource_cpu_usec"),
                        r.get("test_resource_memory_bytes"),
                        r.get("test_resource_disk_read_bytes"),
                        r.get("test_resource_disk_write_bytes"),
                    ),
                )
            conn.execute(
                "UPDATE runs SET is_current=1, is_full=? WHERE run_id=?", (is_full, rid)
            )
            counts["current_runs"] += 1
            counts["test_rows"] += len(rows)
            if verbose:
                flag = "" if is_full else f"  ⚠ PARTIAL ({len(rows)}/{need})"
                print(f"  [{i}/{len(pairs)}] {inst}: {len(rows)} tests{flag}")
        conn.commit()

        db.set_meta(conn, "last_sync", str(int(time.time())))
        db.set_meta(conn, "counts", str(counts))
        conn.commit()

    if verbose:
        print("done:", counts)
    return counts


if __name__ == "__main__":
    sync()
