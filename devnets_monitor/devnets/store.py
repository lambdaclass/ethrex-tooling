"""SQLite store: open, migrate, and helpers for the data collectors."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .config import repo_root


def db_path() -> Path:
    """Return path to the SQLite database; ensures data/ directory exists."""
    data_dir = repo_root() / "data"
    data_dir.mkdir(exist_ok=True)
    return data_dir / "ethrex-devnets.sqlite"


def connect() -> sqlite3.Connection:
    """Open (and create if absent) the SQLite database with sensible defaults."""
    conn = sqlite3.connect(str(db_path()))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


_CREATE_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS slots (
        devnet           TEXT    NOT NULL,
        slot             INTEGER NOT NULL,
        epoch            INTEGER,
        time             INTEGER,
        proposer         TEXT,
        proposer_name    TEXT,
        status           TEXT,
        blob_count       INTEGER,
        eth_block_number INTEGER,
        gas_used         INTEGER,
        PRIMARY KEY (devnet, slot)
    )""",
    """CREATE TABLE IF NOT EXISTS slot_exec_times (
        devnet      TEXT    NOT NULL,
        slot        INTEGER NOT NULL,
        client_type TEXT    NOT NULL,
        count       INTEGER,
        avg_time    REAL,
        min_time    REAL,
        max_time    REAL,
        PRIMARY KEY (devnet, slot, client_type)
    )""",
    """CREATE TABLE IF NOT EXISTS node_health (
        devnet        TEXT    NOT NULL,
        node          TEXT    NOT NULL,
        ts            INTEGER NOT NULL,
        image         TEXT,
        buildnum      TEXT,
        "commit"      TEXT,
        restart       INTEGER,
        head          INTEGER,
        peers         INTEGER,
        syncing       TEXT,
        state_at_head TEXT,
        watchtower    TEXT,
        cl_line       TEXT,
        PRIMARY KEY (devnet, node, ts)
    )""",
    """CREATE TABLE IF NOT EXISTS hive_runs (
        devnet          TEXT    NOT NULL,
        group_name      TEXT    NOT NULL,
        suite_id        TEXT    NOT NULL,
        ethrex_version  TEXT,
        fork_filter     TEXT,
        ntests          INTEGER,
        passes          INTEGER,
        fails           INTEGER,
        started_at      INTEGER,
        web_url         TEXT,
        PRIMARY KEY (devnet, group_name, suite_id)
    )""",
    """CREATE TABLE IF NOT EXISTS fork_schedule (
        devnet        TEXT    NOT NULL,
        fork          TEXT    NOT NULL,
        activation_ts INTEGER,
        blob_target   INTEGER,
        blob_max      INTEGER,
        PRIMARY KEY (devnet, fork)
    )""",
    """CREATE TABLE IF NOT EXISTS fork_eips (
        devnet TEXT    NOT NULL,
        fork   TEXT    NOT NULL,
        eip    INTEGER NOT NULL,
        title  TEXT,
        stage  TEXT,
        status TEXT,
        PRIMARY KEY (devnet, fork, eip)
    )""",
    """CREATE TABLE IF NOT EXISTS events (
        devnet       TEXT    NOT NULL,
        dedup_key    TEXT    NOT NULL,
        kind         TEXT    NOT NULL,
        severity     TEXT    NOT NULL,
        node         TEXT,
        message      TEXT    NOT NULL,
        details      TEXT,
        first_seen   INTEGER NOT NULL,
        last_seen    INTEGER NOT NULL,
        resolved_at  INTEGER,
        count        INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY (devnet, dedup_key)
    )""",
    """CREATE INDEX IF NOT EXISTS idx_events_devnet_lastseen
        ON events(devnet, last_seen DESC)""",
    """CREATE TABLE IF NOT EXISTS network_splits (
        devnet       TEXT    NOT NULL,
        ts           INTEGER NOT NULL,
        head_root    TEXT    NOT NULL,
        head_slot    INTEGER,
        head_count   INTEGER,
        is_canonical INTEGER,
        clients_json TEXT,
        fork_id      TEXT,
        PRIMARY KEY (devnet, ts, head_root)
    )""",
    """CREATE TABLE IF NOT EXISTS client_dist (
        devnet   TEXT    NOT NULL,
        ts       INTEGER NOT NULL,
        layer    TEXT    NOT NULL,
        client   TEXT    NOT NULL,
        version  TEXT    NOT NULL DEFAULT '',
        count    INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY (devnet, ts, layer, client, version)
    )""",
    """CREATE TABLE IF NOT EXISTS bal_inspect (
        devnet           TEXT    NOT NULL,
        slot             INTEGER NOT NULL,
        block_root       TEXT,
        proposer_name    TEXT,
        access_count     INTEGER,
        eth_block_number INTEGER,
        fetched_at       INTEGER,
        PRIMARY KEY (devnet, slot)
    )""",
    """CREATE TABLE IF NOT EXISTS epbs_slot (
        devnet           TEXT    NOT NULL,
        slot             INTEGER NOT NULL,
        block_root       TEXT,
        proposer_name    TEXT,
        bid_count        INTEGER,
        ptc_size         INTEGER,
        ptc_vote_count   INTEGER,
        ptc_nonvoter_pct REAL,
        payload_revealed INTEGER,
        fetched_at       INTEGER,
        PRIMARY KEY (devnet, slot)
    )""",
    """CREATE TABLE IF NOT EXISTS network_overview (
        devnet           TEXT    NOT NULL,
        ts               INTEGER NOT NULL,
        current_slot     INTEGER,
        current_epoch    INTEGER,
        finalized_epoch  INTEGER,
        justified_epoch  INTEGER,
        json             TEXT,
        PRIMARY KEY (devnet, ts)
    )""",
    """CREATE TABLE IF NOT EXISTS spamoor_status (
        devnet       TEXT    NOT NULL,
        ts           INTEGER NOT NULL,
        spammer_id   INTEGER NOT NULL,
        name         TEXT,
        scenario     TEXT,
        status       INTEGER,
        enabled      INTEGER,
        PRIMARY KEY (devnet, ts, spammer_id)
    )""",
    """CREATE TABLE IF NOT EXISTS assertoor_runs (
        devnet      TEXT    NOT NULL,
        run_id      INTEGER NOT NULL,
        test_id     TEXT,
        name        TEXT,
        status      TEXT,
        started_at  INTEGER,
        stopped_at  INTEGER,
        web_url     TEXT,
        PRIMARY KEY (devnet, run_id)
    )""",
    """CREATE TABLE IF NOT EXISTS deploy_gap (
        devnet            TEXT    NOT NULL,
        node              TEXT    NOT NULL,
        deployed_commit   TEXT,
        main_commit       TEXT,
        commits_behind    INTEGER,
        checked_at        INTEGER,
        PRIMARY KEY (devnet, node)
    )""",
    """CREATE TABLE IF NOT EXISTS gh_cache (
        key        TEXT PRIMARY KEY,
        value      TEXT,
        fetched_at INTEGER
    )""",
]


_migrated = False


def migrate(conn: sqlite3.Connection) -> None:
    """
    Create all tables if they do not exist. Idempotent, and runs the DDL at most
    once per process (a long-running dashboard calls this on many requests; the
    guard avoids re-issuing CREATE/ALTER + commit on every call).
    """
    global _migrated
    if _migrated:
        return
    for stmt in _CREATE_STATEMENTS:
        conn.execute(stmt)
    # Additive migrations: add columns to tables created before these fields existed.
    fork_cols = {r[1] for r in conn.execute("PRAGMA table_info(fork_eips)")}
    if "stage" not in fork_cols:
        conn.execute("ALTER TABLE fork_eips ADD COLUMN stage TEXT")
    if "status" not in fork_cols:
        conn.execute("ALTER TABLE fork_eips ADD COLUMN status TEXT")
    conn.commit()
    _migrated = True


def upsert(conn: sqlite3.Connection, table: str, row: dict[str, Any]) -> None:
    """
    Insert or update a row identified by its primary key columns.
    Uses INSERT ... ON CONFLICT DO UPDATE SET for all non-PK columns.
    Column names are double-quoted to handle reserved words (e.g. 'commit').
    """
    cols = list(row.keys())
    placeholders = ", ".join("?" for _ in cols)
    col_names = ", ".join(f'"{c}"' for c in cols)
    updates = ", ".join(f'"{c}" = excluded."{c}"' for c in cols)
    sql = (
        f"INSERT INTO {table} ({col_names}) VALUES ({placeholders}) "
        f"ON CONFLICT DO UPDATE SET {updates}"
    )
    conn.execute(sql, list(row.values()))


def max_slot(conn: sqlite3.Connection, devnet: str) -> int | None:
    """Return the highest stored slot for a devnet, or None if the table is empty."""
    row = conn.execute(
        "SELECT MAX(slot) FROM slots WHERE devnet = ?", (devnet,)
    ).fetchone()
    if row and row[0] is not None:
        return int(row[0])
    return None
