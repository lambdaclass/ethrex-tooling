"""Pandas-backed analytics over the SQLite snapshot.

Comparisons use the *current* run per (suite, instance). For per-client tables we
pick a primary instance per client (prefer ``<client>-bal-full``) so the matrix
stays 6 columns; the leaderboard keeps every instance (mode) as its own row.
"""

from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd

from . import config
from .parse import extract_op

HOME = config.HOME_CLIENT


# ---- suites -------------------------------------------------------------


def active_suites(conn: sqlite3.Connection) -> pd.DataFrame:
    df = pd.read_sql(
        "SELECT * FROM suites WHERE is_active=1 ORDER BY variant, indexed_at DESC", conn
    )
    if df.empty:
        return df
    # rank old/new within the same name (0 = newest)
    df["age_rank"] = (
        df.groupby("name")["indexed_at"]
        .rank(method="first", ascending=False)
        .astype(int)
        - 1
    )
    df["age"] = df["age_rank"].map(lambda r: "new" if r == 0 else "old")
    return df


def default_suite(conn: sqlite3.Connection, variant: str = "compute") -> str | None:
    df = active_suites(conn)
    if df.empty:
        return None
    sub = df[(df["variant"] == variant) & (df.get("age") == "new")]
    if sub.empty:
        sub = df[df["variant"] == variant]
    if sub.empty:
        sub = df
    return sub.iloc[0]["suite_hash"]


# ---- runs ---------------------------------------------------------------


def current_runs(conn: sqlite3.Connection, suite_hash: str) -> pd.DataFrame:
    return pd.read_sql(
        "SELECT * FROM runs WHERE suite_hash=? AND is_current=1 ORDER BY instance_id",
        conn,
        params=(suite_hash,),
    )


def primary_run_per_client(conn: sqlite3.Connection, suite_hash: str) -> dict[str, str]:
    """client -> run_id, preferring the `<client>-bal-full` instance."""
    runs = current_runs(conn, suite_hash)
    out: dict[str, str] = {}
    for client, grp in runs.groupby("client"):
        full = grp[grp["instance_id"] == f"{client}-bal-full"]
        chosen = full if not full.empty else grp
        out[client] = chosen.iloc[0]["run_id"]
    return out


def _current_stats(conn: sqlite3.Connection, suite_hash: str) -> pd.DataFrame:
    return pd.read_sql(
        "SELECT * FROM test_stats WHERE suite_hash=?", conn, params=(suite_hash,)
    )


# ---- leaderboard --------------------------------------------------------


def leaderboard(conn: sqlite3.Connection, suite_hash: str) -> pd.DataFrame:
    """Per-instance ranking by gas-weighted aggregate Mgas/s (total gas / total time).

    This matches the headline benchmark throughput. Median/mean/wins are kept as
    secondary columns (median over-weights many small, fast tests).
    """
    ts = _current_stats(conn, suite_hash)
    if ts.empty:
        return ts
    agg = (
        ts.groupby(["instance_id", "client"])
        .agg(
            median_mgas=("test_mgas_s", "median"),
            mean_mgas=("test_mgas_s", "mean"),
            total_gas=("test_gas_used", "sum"),
            total_time_ns=("test_time_ns", "sum"),
            tests=("test_name", "nunique"),
        )
        .reset_index()
    )
    agg["agg_mgas"] = agg["total_gas"] * 1000.0 / agg["total_time_ns"]
    agg["total_time_s"] = agg["total_time_ns"] / 1e9
    # winner per test = instance with max per-test mgas; weight wins by the test's gas
    idx = ts.groupby("test_name")["test_mgas_s"].idxmax()
    winners = ts.loc[idx, ["instance_id", "test_gas_used"]]
    wins = winners["instance_id"].value_counts().rename("wins")
    gas_won = winners.groupby("instance_id")["test_gas_used"].sum()
    total_gas = float(
        winners["test_gas_used"].sum()
    )  # each test counted once (its winner)
    agg = agg.merge(wins, left_on="instance_id", right_index=True, how="left")
    agg["wins"] = agg["wins"].fillna(0).astype(int)
    agg["gas_won_pct"] = agg["instance_id"].map(gas_won).fillna(0) / total_gas * 100.0
    agg = agg.sort_values("agg_mgas", ascending=False).reset_index(drop=True)
    agg["rank"] = agg.index + 1
    agg["is_home"] = agg["client"] == HOME
    return agg


# ---- per-client matrix (compare) ---------------------------------------


def compare_matrix(conn: sqlite3.Connection, suite_hash: str) -> pd.DataFrame:
    """One row per test, one Mgas/s column per client (primary instance)."""
    primary = primary_run_per_client(conn, suite_hash)
    ts = _current_stats(conn, suite_hash)
    if ts.empty:
        return ts
    ts = ts[ts["run_id"].isin(primary.values())]
    mat = ts.pivot_table(
        index=["test_name", "file", "fork", "benchmark_mgas"],
        columns="client",
        values="test_mgas_s",
    ).reset_index()
    clients = [c for c in config.CLIENTS if c in mat.columns]
    others = [c for c in clients if c != HOME]
    if HOME in mat.columns and others:
        mat["median_others"] = mat[others].median(axis=1, skipna=True)
        mat["best_other"] = mat[others].max(axis=1, skipna=True)
        mat["ratio"] = mat[HOME] / mat["median_others"]
        # rank of home among clients (1 = fastest), higher mgas better
        ranks = mat[clients].rank(axis=1, ascending=False, method="min")
        mat["home_rank"] = ranks[HOME]
        mat["n_clients"] = mat[clients].notna().sum(axis=1)
    return mat


def below_summary(conn: sqlite3.Connection, suite_hash: str) -> dict:
    mat = compare_matrix(conn, suite_hash)
    if mat.empty or "ratio" not in mat:
        return {"total": 0, "below": 0, "below_pct": 0.0, "by_file": []}
    have = mat.dropna(subset=["ratio"])
    below = have[have["ratio"] < 1.0]
    by_file = (
        below.groupby("file")
        .agg(n=("test_name", "count"), median_ratio=("ratio", "median"))
        .reset_index()
        .sort_values("n", ascending=False)
    )
    return {
        "total": int(len(have)),
        "below": int(len(below)),
        "below_pct": round(100.0 * len(below) / max(len(have), 1), 1),
        "median_rank": float(have["home_rank"].median())
        if "home_rank" in have
        else None,
        "by_file": by_file.to_dict("records"),
    }


# ---- coverage gaps ------------------------------------------------------


def coverage(conn: sqlite3.Connection, suite_hash: str) -> dict:
    ts = _current_stats(conn, suite_hash)
    if ts.empty:
        return {"union": 0, "home": 0, "missing": [], "missing_count": 0, "by_file": []}
    union = set(ts["test_name"].unique())
    home = set(ts[ts["client"] == HOME]["test_name"].unique())
    missing = sorted(union - home)
    miss_df = pd.DataFrame({"test_name": missing})
    if not miss_df.empty:
        miss_df["file"] = miss_df["test_name"].str.extract(r"^(.*?\.py)__")
        by_file = (
            miss_df.groupby("file")
            .size()
            .reset_index(name="n")
            .sort_values("n", ascending=False)
        )
        by_file = by_file.to_dict("records")
    else:
        by_file = []
    return {
        "union": len(union),
        "home": len(home),
        "missing_count": len(missing),
        "coverage_pct": round(100.0 * len(home) / max(len(union), 1), 1),
        "missing": missing[:500],
        "by_file": by_file,
    }


# ---- optimization targets (agent-facing) -------------------------------


def _row_by_client(
    pivot: pd.DataFrame, clients: list[str], pick: np.ndarray
) -> np.ndarray:
    """For each row, the value of the column named in `pick` (best-other client)."""
    vals = pivot.reindex(columns=clients).to_numpy(dtype=float)
    idx = {c: i for i, c in enumerate(clients)}
    out = np.full(len(pick), np.nan)
    for i, c in enumerate(pick):
        j = idx.get(c)
        if j is not None:
            out[i] = vals[i, j]
    return out


def opt_targets(conn: sqlite3.Connection, suite_hash: str) -> pd.DataFrame:
    """Per-test view for picking optimization targets for the home client.

    Priority is `time_lost_ms` = home test time − fastest competitor's test time
    (how much wall-clock the home client could recover on that test), not the raw
    Mgas/s ratio (which over-weights tiny tests). Also surfaces the operation under
    test (`op`), resource use vs the best competitor, and a `bottleneck` guess
    (cpu / io / memory) for tests where the home client is slower.
    """
    primary = primary_run_per_client(conn, suite_hash)
    ts = _current_stats(conn, suite_hash)
    if ts.empty:
        return ts
    ts = ts[ts["run_id"].isin(primary.values())]
    idx = ["test_name", "file", "fork", "benchmark_mgas"]

    def piv(col: str) -> pd.DataFrame:
        return ts.pivot_table(index=idx, columns="client", values=col)

    mg, tm = piv("test_mgas_s"), piv("test_time_ns")
    cpu, mem = piv("cpu_usec"), piv("memory_bytes")
    dr, dw = piv("disk_read_bytes"), piv("disk_write_bytes")
    clients = [c for c in config.CLIENTS if c in mg.columns]
    others = [c for c in clients if c != HOME]
    if HOME not in mg.columns or not others:
        return pd.DataFrame()

    df = mg.index.to_frame(index=False)
    df["op"] = df["test_name"].map(extract_op)
    df["ethrex_mgas"] = mg[HOME].to_numpy()
    df["median_other_mgas"] = mg[others].median(axis=1).to_numpy()
    df["best_other_mgas"] = mg[others].max(axis=1).to_numpy()
    df["ethrex_time_ns"] = tm[HOME].to_numpy()
    boc = tm[others].idxmin(axis=1).to_numpy()  # fastest competitor by time
    df["best_other_client"] = boc
    df["best_other_time_ns"] = tm[others].min(axis=1).to_numpy()
    df["ratio"] = df["ethrex_mgas"] / df["median_other_mgas"]
    df["time_lost_ms"] = (df["ethrex_time_ns"] - df["best_other_time_ns"]) / 1e6
    df["rank"] = (
        mg[clients].rank(axis=1, ascending=False, method="min")[HOME].to_numpy()
    )
    df["n_clients"] = mg[clients].notna().sum(axis=1).to_numpy()

    # resources: home vs the fastest competitor on that test
    e_cpu, o_cpu = cpu[HOME].to_numpy(dtype=float), _row_by_client(cpu, clients, boc)
    e_mem, o_mem = mem[HOME].to_numpy(dtype=float), _row_by_client(mem, clients, boc)
    e_io = np.nan_to_num(dr[HOME].to_numpy(dtype=float)) + np.nan_to_num(
        dw[HOME].to_numpy(dtype=float)
    )
    o_io = np.nan_to_num(_row_by_client(dr, clients, boc)) + np.nan_to_num(
        _row_by_client(dw, clients, boc)
    )
    df["ethrex_cpu_usec"], df["other_cpu_usec"] = e_cpu, o_cpu
    df["ethrex_io_bytes"], df["other_io_bytes"] = e_io, o_io
    df["ethrex_mem_bytes"], df["other_mem_bytes"] = e_mem, o_mem
    with np.errstate(divide="ignore", invalid="ignore"):
        df["cpu_ratio"] = e_cpu / o_cpu
        df["io_ratio"] = np.where(o_io > 0, e_io / o_io, np.nan)
        df["mem_ratio"] = e_mem / o_mem

    def bottleneck(r) -> str:
        if not r["time_lost_ms"] > 0:
            return "even"  # home already at/ahead of best competitor
        cands = {"cpu": r["cpu_ratio"], "io": r["io_ratio"], "memory": r["mem_ratio"]}
        cands = {k: v for k, v in cands.items() if pd.notna(v) and v > 0}
        if not cands:
            return "unknown"
        k = max(cands, key=cands.get)
        return k if cands[k] > 1.15 else "even"

    df["bottleneck"] = df.apply(bottleneck, axis=1)
    return df.sort_values("time_lost_ms", ascending=False).reset_index(drop=True)


def _scaling(sub: pd.DataFrame) -> str:
    """Does the home client's gap widen with gas? corr(gas, ratio) over the file's
    tests. ratio<1 = behind; negative corr => relatively worse at high gas."""
    s = sub.dropna(subset=["benchmark_mgas", "ratio"])
    if len(s) < 4 or s["benchmark_mgas"].nunique() < 3:
        return "n/a"
    c = s["benchmark_mgas"].corr(s["ratio"])
    if pd.isna(c):
        return "n/a"
    if c <= -0.3:
        return "worse-at-high-gas"
    if c >= 0.3:
        return "better-at-high-gas"
    return "flat"


def targets_by_file(conn: sqlite3.Connection, suite_hash: str) -> pd.DataFrame:
    """Aggregate targets per file/opcode — the 'which subsystem to attack' view.

    `time_lost_ms` counts only tests where the home client is behind (ratio < 1),
    so it's purely recoverable deficit. Adds the dominant `bottleneck` among those
    deficits and a `scaling` hint (does the gap grow with gas?).
    """
    df = opt_targets(conn, suite_hash)
    if df.empty:
        return df
    below = df[df["ratio"] < 1.0]
    g = (
        df.groupby("file")
        .agg(
            tests=("test_name", "count"),
            below=("ratio", lambda s: int((s < 1.0).sum())),
            median_rank=("rank", "median"),
            median_ratio=("ratio", "median"),
        )
        .reset_index()
    )
    rec = below.groupby("file")["time_lost_ms"].sum().rename("time_lost_ms")
    g = g.merge(rec, on="file", how="left")
    g["time_lost_ms"] = g["time_lost_ms"].fillna(0.0)
    bn = (
        below.groupby("file")["bottleneck"]
        .agg(lambda s: s.mode().iloc[0] if not s.mode().empty else "even")
        .rename("bottleneck")
    )
    g = g.merge(bn, on="file", how="left")
    g["bottleneck"] = g["bottleneck"].fillna("even")
    sc = (
        df.groupby("file")[["benchmark_mgas", "ratio"]]
        .apply(_scaling)
        .rename("scaling")
    )
    g = g.merge(sc, on="file", how="left")
    return g.sort_values("time_lost_ms", ascending=False)


# ---- trends -------------------------------------------------------------


def trends(conn: sqlite3.Connection, suite_hash: str) -> pd.DataFrame:
    return pd.read_sql(
        """SELECT run_id, instance_id, client, timestamp, mgas_s, tests_passed, tests_failed
           FROM runs WHERE suite_hash=? AND status='completed' AND mgas_s IS NOT NULL
           ORDER BY timestamp""",
        conn,
        params=(suite_hash,),
    )
