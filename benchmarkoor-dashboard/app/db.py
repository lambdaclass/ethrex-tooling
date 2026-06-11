"""SQLite snapshot: schema + connection helpers."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS suites (
    suite_hash   TEXT PRIMARY KEY,
    name         TEXT,
    network      TEXT,
    block        TEXT,
    fork         TEXT,
    variant      TEXT,
    tests_total  INTEGER,
    indexed_at   TEXT,
    latest_run_ts INTEGER,
    is_active    INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    suite_hash    TEXT,
    timestamp     INTEGER,
    timestamp_end INTEGER,
    status        TEXT,
    client        TEXT,
    instance_id   TEXT,
    image         TEXT,
    rollback_strategy TEXT,
    tests_total   INTEGER,
    tests_passed  INTEGER,
    tests_failed  INTEGER,
    test_gas_used INTEGER,
    test_gas_used_duration INTEGER,
    mgas_s        REAL,
    is_current    INTEGER DEFAULT 0,
    is_full       INTEGER DEFAULT 0,
    ethrex_commit TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_suite ON runs(suite_hash);
CREATE INDEX IF NOT EXISTS idx_runs_current ON runs(is_current);

CREATE TABLE IF NOT EXISTS commits (
    sha          TEXT PRIMARY KEY,
    committed_at INTEGER,
    message      TEXT,
    branch       TEXT,
    url          TEXT
);

CREATE TABLE IF NOT EXISTS test_stats (
    run_id        TEXT,
    suite_hash    TEXT,
    client        TEXT,
    instance_id   TEXT,
    test_name     TEXT,
    file          TEXT,
    fork          TEXT,
    benchmark_mgas INTEGER,
    test_mgas_s   REAL,
    test_time_ns  INTEGER,
    test_gas_used INTEGER,
    rpc_calls     INTEGER,
    cpu_usec      INTEGER,
    memory_bytes  INTEGER,
    disk_read_bytes  INTEGER,
    disk_write_bytes INTEGER,
    PRIMARY KEY (run_id, test_name)
);
CREATE INDEX IF NOT EXISTS idx_ts_suite ON test_stats(suite_hash);
CREATE INDEX IF NOT EXISTS idx_ts_client ON test_stats(client);

CREATE TABLE IF NOT EXISTS sync_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path or config.DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO sync_meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def get_meta(
    conn: sqlite3.Connection, key: str, default: str | None = None
) -> str | None:
    row = conn.execute("SELECT value FROM sync_meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default
