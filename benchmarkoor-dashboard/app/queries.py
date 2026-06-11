"""Pandas-backed analytics over the SQLite snapshot.

Comparisons use the *current* run per (suite, instance). For per-client tables we
pick a primary instance per client (prefer ``<client>-bal-full``) so the matrix
stays 6 columns; the leaderboard keeps every instance (mode) as its own row.
"""

from __future__ import annotations

import sqlite3

import pandas as pd

from . import config

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


def opt_targets(conn: sqlite3.Connection, suite_hash: str) -> pd.DataFrame:
    """Per-test view for picking optimization targets for the home client.

    Priority is `time_lost_ms` = home test time − fastest competitor's test time
    (how much wall-clock the home client could recover on that test), not the raw
    Mgas/s ratio (which over-weights tiny tests).
    """
    primary = primary_run_per_client(conn, suite_hash)
    ts = _current_stats(conn, suite_hash)
    if ts.empty:
        return ts
    ts = ts[ts["run_id"].isin(primary.values())]
    mg = ts.pivot_table(
        index=["test_name", "file", "fork", "benchmark_mgas"],
        columns="client",
        values="test_mgas_s",
    )
    tm = ts.pivot_table(
        index=["test_name", "file", "fork", "benchmark_mgas"],
        columns="client",
        values="test_time_ns",
    )
    clients = [c for c in config.CLIENTS if c in mg.columns]
    others = [c for c in clients if c != HOME]
    if HOME not in mg.columns or not others:
        return pd.DataFrame()
    df = mg.index.to_frame(index=False)
    df["ethrex_mgas"] = mg[HOME].to_numpy()
    df["median_other_mgas"] = mg[others].median(axis=1).to_numpy()
    df["best_other_mgas"] = mg[others].max(axis=1).to_numpy()
    df["ethrex_time_ns"] = tm[HOME].to_numpy()
    df["best_other_time_ns"] = tm[others].min(axis=1).to_numpy()
    df["best_other_client"] = tm[others].idxmin(axis=1).to_numpy()
    df["ratio"] = df["ethrex_mgas"] / df["median_other_mgas"]
    df["time_lost_ms"] = (df["ethrex_time_ns"] - df["best_other_time_ns"]) / 1e6
    df["rank"] = (
        mg[clients].rank(axis=1, ascending=False, method="min")[HOME].to_numpy()
    )
    df["n_clients"] = mg[clients].notna().sum(axis=1).to_numpy()
    return df.sort_values("time_lost_ms", ascending=False).reset_index(drop=True)


def targets_by_file(conn: sqlite3.Connection, suite_hash: str) -> pd.DataFrame:
    """Aggregate targets per file/opcode — the 'which subsystem to attack' view."""
    df = opt_targets(conn, suite_hash)
    if df.empty:
        return df
    g = (
        df.groupby("file")
        .agg(
            tests=("test_name", "count"),
            below=("ratio", lambda s: int((s < 1.0).sum())),
            time_lost_ms=("time_lost_ms", "sum"),
            median_rank=("rank", "median"),
            median_ratio=("ratio", "median"),
        )
        .reset_index()
        .sort_values("time_lost_ms", ascending=False)
    )
    return g


# ---- trends -------------------------------------------------------------


def trends(conn: sqlite3.Connection, suite_hash: str) -> pd.DataFrame:
    return pd.read_sql(
        """SELECT run_id, instance_id, client, timestamp, mgas_s, tests_passed, tests_failed
           FROM runs WHERE suite_hash=? AND status='completed' AND mgas_s IS NOT NULL
           ORDER BY timestamp""",
        conn,
        params=(suite_hash,),
    )
