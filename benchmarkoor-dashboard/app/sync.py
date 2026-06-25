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

import calendar
import json
import subprocess
import time

from . import config, db
from .client import Client
from .parse import parse_suite_name, parse_test_name

WINDOW = config.ACTIVE_WINDOW_DAYS * 86400


def _iso_to_unix(s: str) -> int:
    return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ"))


def _store_commit(
    conn, sha: str, date_iso: str, msg: str, url: str, branch: str
) -> None:
    conn.execute(
        "INSERT INTO commits(sha,committed_at,message,branch,url) VALUES(?,?,?,?,?) "
        "ON CONFLICT(sha) DO UPDATE SET committed_at=excluded.committed_at, "
        "message=excluded.message, branch=excluded.branch, url=excluded.url",
        (sha, _iso_to_unix(date_iso), msg, branch, url),
    )


def _commits_via_gh(conn, repo: str, branch: str, verbose: bool) -> int | None:
    """Fetch via `gh` (authed, 5000/hr). Returns count, or None if gh unusable."""
    try:
        out = subprocess.run(
            [
                "gh",
                "api",
                "--paginate",
                f"repos/{repo}/commits?sha={branch}&per_page=100",
                "--jq",
                ".[] | {sha:.sha, date:.commit.committer.date, "
                'msg:(.commit.message|split("\\n")[0]), url:.html_url}',
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    n = 0
    for line in out.stdout.splitlines():
        if line.strip():
            c = json.loads(line)
            _store_commit(conn, c["sha"], c["date"], c["msg"], c["url"], branch)
            n += 1
    conn.commit()
    if verbose:
        print(f"  commits: {n} via gh from {repo}@{branch}")
    return n


def _commits_via_http(conn, repo: str, branch: str, verbose: bool) -> int:
    """Unauthenticated GitHub REST fallback. Heavily cached: stops at the first
    commit already in the table (history is append-only), so steady state is one
    request; only the initial empty-table sync pages further. Bounded + handles
    the 60/hr rate limit gracefully."""
    import httpx

    known = {
        r[0] for r in conn.execute("SELECT sha FROM commits WHERE branch=?", (branch,))
    }
    n, stop = 0, False
    with httpx.Client(timeout=30.0) as c:
        for page in range(1, 41):  # cap: ~4000 commits on a cold start
            try:
                resp = c.get(
                    f"https://api.github.com/repos/{repo}/commits",
                    params={"sha": branch, "per_page": 100, "page": page},
                    headers={"Accept": "application/vnd.github+json"},
                )
            except httpx.TransportError:
                break
            if resp.status_code == 403:  # rate limited
                if verbose:
                    print("  commits: github http rate-limited; using cached commits")
                break
            if resp.status_code != 200:
                break
            batch = resp.json()
            if not batch:
                break
            for c_ in batch:
                if c_["sha"] in known:  # reached cached history
                    stop = True
                    break
                _store_commit(
                    conn,
                    c_["sha"],
                    c_["commit"]["committer"]["date"],
                    c_["commit"]["message"].split("\n")[0],
                    c_["html_url"],
                    branch,
                )
                n += 1
            if stop or len(batch) < 100:
                break
    conn.commit()
    if verbose:
        print(f"  commits: {n} new via http (unauth) from {repo}@{branch}")
    return n


def fetch_commits(conn, verbose: bool = True) -> int:
    """Pull the home-client branch's commit history (sha + date + message).

    Prefers `gh` (authed); falls back to the unauthenticated GitHub REST API with
    incremental caching when gh is missing/unauthenticated. Best-effort: on total
    failure, commits stay as-is and runs are simply left without association.
    """
    repo, branch = config.ETHREX_REPO, config.ETHREX_BRANCH
    n = _commits_via_gh(conn, repo, branch, verbose)
    if n is None:
        n = _commits_via_http(conn, repo, branch, verbose)
    return n


def ingest_phase_logs(conn, verbose: bool = True) -> dict[str, int]:
    """Stream each current home-client run's benchmarkoor.log, parse per-test
    phase timings (exec/merkle/store) + fkv catch-up summary, store the derived
    rows. The raw log is never written to disk."""
    from .logparse import parse, stream_run_log

    counts = {"phase_runs": 0, "phase_rows": 0}
    rows = conn.execute(
        "SELECT run_id, suite_hash FROM runs WHERE is_current=1 AND client=?",
        (config.HOME_CLIENT,),
    ).fetchall()
    conn.execute("DELETE FROM test_phases")
    conn.execute("DELETE FROM fkv_summary")
    for r in rows:
        rid, sh = r["run_id"], r["suite_hash"]
        try:
            res = parse(stream_run_log(rid))
        except Exception as e:  # network/parse hiccup: skip this run, keep going
            if verbose:
                print(f"  phases: {rid} skipped ({type(e).__name__}: {e})")
            continue
        for tp in res.tests.values():
            conn.execute(
                """INSERT OR REPLACE INTO test_phases
                   (run_id,suite_hash,test_name,total_ms,exec_ms,merkle_ms,store_ms,
                    merkle_drain_ms,merkle_overlap_pct,bottleneck)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    rid,
                    sh,
                    tp.test_name,
                    tp.total_ms,
                    tp.exec_ms,
                    tp.merkle_ms,
                    tp.store_ms,
                    tp.merkle_drain_ms,
                    tp.merkle_overlap_pct,
                    tp.bottleneck,
                ),
            )
        conn.execute(
            "INSERT OR REPLACE INTO fkv_summary(run_id,suite_hash,started,skipping,finished) "
            "VALUES(?,?,?,?,?)",
            (rid, sh, res.fkv_started, res.fkv_skipping, res.fkv_finished),
        )
        counts["phase_runs"] += 1
        counts["phase_rows"] += len(res.tests)
        if verbose:
            print(
                f"  phases: {rid} -> {len(res.tests)} tests "
                f"(fkv started={res.fkv_started} skipping={res.fkv_skipping} "
                f"finished={res.fkv_finished})"
            )
    conn.commit()
    return counts


def _append_phase_history(conn) -> None:
    """Aggregate the current run's per-test phases into per-(commit, suite, op)
    rows and upsert into phase_history, so regressions accrue across syncs."""
    from collections import defaultdict

    from .parse import extract_op

    rows = conn.execute(
        """SELECT r.ethrex_commit AS sha, p.suite_hash, p.test_name,
                  p.exec_ms, p.merkle_ms, p.store_ms, p.total_ms
           FROM test_phases p JOIN runs r ON r.run_id=p.run_id
           WHERE r.ethrex_commit IS NOT NULL"""
    ).fetchall()
    agg: dict[tuple, list] = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0, 0])
    for r in rows:
        op = extract_op(r["test_name"]) or r["test_name"]
        a = agg[(r["sha"], r["suite_hash"], op)]
        a[0] += r["exec_ms"] or 0
        a[1] += r["merkle_ms"] or 0
        a[2] += r["store_ms"] or 0
        a[3] += r["total_ms"] or 0
        a[4] += 1
    for (sha, sh, op), (e, m, s, t, n) in agg.items():
        conn.execute(
            """INSERT OR REPLACE INTO phase_history
               (commit_sha, suite_hash, op, exec_ms, merkle_ms, store_ms, total_ms)
               VALUES(?,?,?,?,?,?,?)""",
            (sha, sh, op, e / n, m / n, s / n, t / n),
        )
    conn.commit()


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
            # order by id (PK, indexed) not timestamp: ORDER BY timestamp + OFFSET
            # forces an unindexed sort that hits the API's statement timeout and 500s
            # on large run sets. id is unique and stable, so pagination is complete;
            # row order doesn't matter here (every page is stored, current run is
            # derived separately).
            runs = c.paginate("runs", {"suite_hash": f"eq.{h}", "order": "id.desc"})
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

        # 6. associate home-client runs to the branch commit live at their timestamp.
        counts["commits"] = fetch_commits(conn, verbose)
        lag = config.DEPLOY_LAG_MIN * 60
        conn.execute("UPDATE runs SET ethrex_commit=NULL")
        conn.execute(
            "UPDATE runs SET ethrex_commit=("
            "  SELECT sha FROM commits c WHERE c.committed_at + ? <= runs.timestamp"
            "  ORDER BY c.committed_at DESC LIMIT 1) "
            "WHERE client=?",
            (lag, config.HOME_CLIENT),
        )
        mapped = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE client=? AND ethrex_commit IS NOT NULL",
            (config.HOME_CLIENT,),
        ).fetchone()[0]
        counts["runs_with_commit"] = mapped
        conn.commit()

        # 7. per-test phase timings + fkv summary from the run logs (home client)
        counts.update(ingest_phase_logs(conn, verbose))

        # 8. append per-(commit, op) phase aggregates so a regression history builds
        # up over time (each snapshot holds only the current commit's run).
        _append_phase_history(conn)

        # staleness: newest run timestamp across active suites (for age display)
        newest = (
            conn.execute(
                "SELECT MAX(timestamp) FROM runs WHERE suite_hash IN (%s)"
                % ",".join("?" * len(active)),
                tuple(active),
            ).fetchone()[0]
            if active
            else None
        )
        db.set_meta(conn, "newest_run_ts", str(newest or 0))

        db.set_meta(conn, "last_sync", str(int(time.time())))
        db.set_meta(conn, "counts", str(counts))
        conn.commit()

    if verbose:
        print("done:", counts)
    return counts


if __name__ == "__main__":
    sync()
