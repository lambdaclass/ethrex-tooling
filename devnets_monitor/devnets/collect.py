"""Orchestrator for dv collect: runs data collectors in order."""

from __future__ import annotations

import sys

from .store import connect, migrate

VALID_WHAT = (
    "blobs", "health", "hive", "forks", "network", "events",
    "clients", "spamoor", "assertoor",
    "bal", "epbs", "eiptrack", "deploygap", "slow", "all",
)


def collect(devnet: str, what: str) -> None:
    """
    Run data collectors for the given devnet.

    what: one of blobs | health | hive | forks | network | events |
          clients | spamoor | assertoor |
          bal | epbs | eiptrack | deploygap | slow | all

    'all'  runs the fast set: forks, blobs, hive, health, network,
           clients, spamoor, assertoor, then run_detectors last.
           Excludes slow collectors (bal, epbs, deploygap).
    'slow' runs the expensive collectors: bal + epbs + deploygap.
    'eiptrack' reloads eips.json status into fork_eips; cheap, included in
           'forks' (and therefore in 'all').

    The DB schema is always migrated before any collector runs.
    """
    if what not in VALID_WHAT:
        print(
            f"collect: unknown target '{what}'. "
            f"Valid: {', '.join(VALID_WHAT)}",
            file=sys.stderr,
        )
        sys.exit(1)

    # Ensure schema is up to date before any collector writes
    conn = connect()
    migrate(conn)
    conn.close()

    if what in ("forks", "all", "eiptrack"):
        from .forks import collect_forks
        collect_forks(devnet)

    if what in ("blobs", "all"):
        from .dora import collect_blobs
        collect_blobs(devnet)

    if what in ("hive", "all"):
        from .hive import collect_hive
        collect_hive(devnet)

    if what in ("health", "all"):
        from .health import collect_health
        collect_health(devnet)

    if what in ("network", "all"):
        from .network import collect_network
        collect_network(devnet)

    if what in ("clients", "all"):
        from .network import collect_clients
        collect_clients(devnet)

    if what in ("spamoor", "all"):
        from .spamoor import collect_spamoor
        collect_spamoor(devnet)

    if what in ("assertoor", "all"):
        from .assertoor import collect_assertoor
        collect_assertoor(devnet)

    if what in ("bal", "slow"):
        from .bal import collect_bal
        collect_bal(devnet)

    if what in ("epbs", "slow"):
        from .epbs import collect_epbs
        collect_epbs(devnet)

    if what in ("deploygap", "slow"):
        from .deploytl import collect_deploygap
        collect_deploygap(devnet)

    # run_detectors last: reads whatever data exists (including bal/epbs if
    # a prior 'slow' run populated those tables).
    if what in ("events", "all"):
        from .detect import run_detectors
        run_detectors(devnet)
