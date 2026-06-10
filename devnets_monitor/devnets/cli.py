"""
dv -- ethrex devnet ops CLI.

Usage: uv run dv <subcommand> [devnet] [args]

Devnet resolution priority: explicit arg > $DEVNET env > config/devnets.yaml default.
"""

from __future__ import annotations

import argparse
import re
import sys


_SINCE_RE = re.compile(r"^\d+[smh]$")


def _validate_since(value: str) -> str:
    """Validate --since matches \\d+[smh]; reject otherwise."""
    if not _SINCE_RE.match(value):
        raise argparse.ArgumentTypeError(
            f"invalid --since value '{value}': must match \\d+[smh] (e.g. 2m, 30s, 1h)"
        )
    return value


def _not_yet(name: str):
    """Return a handler that prints 'not yet implemented (later phase)'."""
    def _handler(args: argparse.Namespace) -> None:
        print(f"dv {name}: not yet implemented (later phase)")
        sys.exit(0)
    return _handler


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="dv",
        description="ethrex devnet ops CLI. Read-only by default; 'wipe' is MUTATING.",
    )
    sub = parser.add_subparsers(dest="command", metavar="subcommand")
    sub.required = True

    # --- status ---
    p_status = sub.add_parser(
        "status",
        help="per-node EL build/head/peers/sync/state@head + CL sync line + watchtower",
    )
    p_status.add_argument("devnet", nargs="?", default=None, help="devnet name (default: from config/env)")
    p_status.add_argument("node", nargs="?", default=None, help="specific node name (default: all)")
    p_status.add_argument("--json", dest="as_json", action="store_true", help="emit one JSON object per node")

    # --- peers ---
    p_peers = sub.add_parser(
        "peers",
        help="peer count, inbound/outbound, client mix, body-serving failures",
    )
    p_peers.add_argument("devnet", help="devnet name")
    p_peers.add_argument("node", help="node name")

    # --- logs ---
    p_logs = sub.add_parser(
        "logs",
        help="tail execution container WARN/ERROR lines",
    )
    p_logs.add_argument("devnet", help="devnet name")
    p_logs.add_argument("node", help="node name")
    p_logs.add_argument(
        "--since",
        default="2m",
        type=_validate_since,
        help="docker logs --since value (default: 2m); must match \\d+[smh]",
    )

    # --- cl ---
    p_cl = sub.add_parser(
        "cl",
        help="tail beacon sync lines",
    )
    p_cl.add_argument("devnet", help="devnet name")
    p_cl.add_argument("node", help="node name")
    p_cl.add_argument(
        "--since",
        default="3m",
        type=_validate_since,
        help="docker logs --since value (default: 3m); must match \\d+[smh]",
    )

    # --- discover ---
    p_discover = sub.add_parser(
        "discover",
        help="refresh config/devnets/<name>.yaml roster/forks/image from the ethpandaops repo",
    )
    p_discover.add_argument("devnet", nargs="?", default=None, help="devnet name (default: from config/env)")

    # --- wipe (MUTATING) ---
    p_wipe = sub.add_parser(
        "wipe",
        help="[MUTATING] recover a wedged EL node; requires --yes",
    )
    p_wipe.add_argument("devnet", help="devnet name")
    p_wipe.add_argument("node", help="node name")
    p_wipe.add_argument(
        "--yes",
        action="store_true",
        help="confirm the MUTATING wipe operation (required)",
    )

    # --- collect ---
    p_collect = sub.add_parser(
        "collect",
        help="pull Dora/Hive/health/forks/network into SQLite",
    )
    p_collect.add_argument("devnet", nargs="?", default=None, help="devnet name (default: from config/env)")
    p_collect.add_argument(
        "what",
        nargs="?",
        default="all",
        choices=[
            "blobs", "health", "hive", "forks", "network", "events",
            "clients", "spamoor", "assertoor",
            "bal", "epbs", "eiptrack", "deploygap", "slow", "all",
        ],
        help=(
            "which collector to run (default: all); "
            "'slow' runs bal + epbs + deploygap"
        ),
    )

    # --- blob ---
    p_blob = sub.add_parser(
        "blob",
        help="blob inclusion per proposer over time; ethrex vs others",
    )
    p_blob.add_argument("devnet", nargs="?", default=None, help="devnet name (default: from config/env)")
    p_blob.add_argument("--proposer", default=None, help="filter to proposer_name substring")
    p_blob.add_argument("--since", default=None, help="slot count or duration (e.g. 500, 2h, 30m)")

    # --- fork ---
    p_fork = sub.add_parser(
        "fork",
        help="fork schedule with human times, blob target/max, EIP-per-fork, countdown",
    )
    p_fork.add_argument("devnet", nargs="?", default=None, help="devnet name (default: from config/env)")

    # --- hive ---
    p_hive = sub.add_parser(
        "hive",
        help="summarize Hive group runs for the devnet",
    )
    p_hive.add_argument("devnet", nargs="?", default=None, help="devnet name (default: from config/env)")

    # --- events ---
    p_events = sub.add_parser(
        "events",
        help="show detected events (anomalies, wedges, splits, etc.)",
    )
    p_events.add_argument("devnet", nargs="?", default=None, help="devnet name (default: from config/env)")
    p_events.add_argument("--kind", default=None, help="filter by event kind")
    p_events.add_argument("--severity", default=None, help="filter by severity (crit/warn/info)")
    p_events.add_argument("--active", dest="active_only", action="store_true", help="show only active (unresolved) events")
    p_events.add_argument("--all", dest="include_resolved", action="store_true", default=True, help="include resolved events (default)")

    # --- proposals ---
    p_proposals = sub.add_parser(
        "proposals",
        help="per-proposer canonical/missed/orphaned slot summary",
    )
    p_proposals.add_argument("devnet", nargs="?", default=None, help="devnet name (default: from config/env)")
    p_proposals.add_argument("--since", default=None, help="slot count or duration (e.g. 500, 2h, 30m)")

    # --- backfill ---
    p_backfill = sub.add_parser(
        "backfill",
        help="range-collect slots [--from .. --to ..] into the slots table",
    )
    p_backfill.add_argument("devnet", nargs="?", default=None, help="devnet name (default: from config/env)")
    p_backfill.add_argument("--from", dest="from_slot", type=int, required=True, metavar="SLOT", help="first slot (inclusive)")
    p_backfill.add_argument("--to", dest="to_slot", type=int, required=True, metavar="SLOT", help="last slot (inclusive)")

    # --- bal ---
    p_bal = sub.add_parser(
        "bal",
        help="show BAL (EIP-7928) access-count inspection for ethrex slots",
    )
    p_bal.add_argument("devnet", nargs="?", default=None, help="devnet name (default: from config/env)")

    # --- epbs ---
    p_epbs = sub.add_parser(
        "epbs",
        help="show ePBS (EIP-7732) bid and PTC vote data per slot",
    )
    p_epbs.add_argument("devnet", nargs="?", default=None, help="devnet name (default: from config/env)")

    # --- eip-track ---
    p_eiptrack = sub.add_parser(
        "eip-track",
        help="show EIP implementation-status summary for the devnet fork",
    )
    p_eiptrack.add_argument("devnet", nargs="?", default=None, help="devnet name (default: from config/env)")

    # --- clients ---
    p_clients = sub.add_parser(
        "clients",
        help="EL+CL client diversity, ethrex versions live, fork agreement",
    )
    p_clients.add_argument("devnet", nargs="?", default=None, help="devnet name (default: from config/env)")

    # --- spamoor ---
    p_spamoor = sub.add_parser(
        "spamoor",
        help="spamoor status: active spammers and blob load state",
    )
    p_spamoor.add_argument("devnet", nargs="?", default=None, help="devnet name (default: from config/env)")

    # --- deploy ---
    p_deploy = sub.add_parser(
        "deploy",
        help="deploy timeline per node and GitHub gap vs ethrex main",
    )
    p_deploy.add_argument("devnet", nargs="?", default=None, help="devnet name (default: from config/env)")

    # --- exectime ---
    p_exectime = sub.add_parser(
        "exectime",
        help="per-client execution-time comparison table and verdict",
    )
    p_exectime.add_argument("devnet", nargs="?", default=None, help="devnet name (default: from config/env)")

    # --- assertoor ---
    p_assertoor = sub.add_parser(
        "assertoor",
        help="assertoor test run results",
    )
    p_assertoor.add_argument("devnet", nargs="?", default=None, help="devnet name (default: from config/env)")

    # --- eips-refresh ---
    p_eips = sub.add_parser(
        "eips-refresh",
        help="regenerate config/eips.json from eipmcp data",
    )
    p_eips.add_argument("devnet", nargs="?", default=None, help="devnet name (default: from config/env)")

    # --- serve ---
    p_serve = sub.add_parser(
        "serve",
        help=(
            "read-only FastAPI dashboard on 127.0.0.1 (localhost only, no auth, no write endpoints). "
            "Requires `dv collect` to have been run first."
        ),
    )
    p_serve.add_argument(
        "--port",
        type=int,
        default=8099,
        metavar="N",
        help="port to listen on (default: 8099)",
    )
    p_serve.add_argument(
        "--host",
        default="127.0.0.1",
        metavar="ADDR",
        help="bind address (default: 127.0.0.1 -- localhost only)",
    )

    args = parser.parse_args()

    # Dispatch stubs
    if hasattr(args, "_handler"):
        args._handler(args)
        return

    from .config import resolve_devnet

    if args.command == "status":
        devnet = resolve_devnet(args.devnet)
        from .status import status
        status(devnet, args.node, args.as_json)

    elif args.command == "peers":
        devnet = resolve_devnet(args.devnet)
        from .peers import peers
        peers(devnet, args.node)

    elif args.command == "logs":
        devnet = resolve_devnet(args.devnet)
        from .config import host_of
        from .remote import LOGS_TAIL
        from .ssh import run_remote
        host = host_of(devnet, args.node)
        result = run_remote(host, LOGS_TAIL, args=[args.since], timeout=30)
        if result.stdout:
            print(result.stdout, end="")
        if result.returncode != 0:
            print(result.stderr.strip(), file=sys.stderr)
            sys.exit(result.returncode)

    elif args.command == "cl":
        devnet = resolve_devnet(args.devnet)
        from .config import host_of
        from .remote import CL_TAIL
        from .ssh import run_remote
        host = host_of(devnet, args.node)
        result = run_remote(host, CL_TAIL, args=[args.since], timeout=30)
        if result.stdout:
            print(result.stdout, end="")
        if result.returncode != 0:
            print(result.stderr.strip(), file=sys.stderr)
            sys.exit(result.returncode)

    elif args.command == "discover":
        devnet = resolve_devnet(args.devnet)
        from .discover import discover
        discover(devnet)

    elif args.command == "wipe":
        devnet = resolve_devnet(args.devnet)
        from .wipe import wipe
        wipe(devnet, args.node, args.yes)

    elif args.command == "collect":
        devnet = resolve_devnet(args.devnet)
        from .collect import collect
        collect(devnet, args.what)

    elif args.command == "blob":
        devnet = resolve_devnet(args.devnet)
        from .blobtrack import show_blobs
        show_blobs(devnet, proposer=args.proposer, since=args.since)

    elif args.command == "fork":
        devnet = resolve_devnet(args.devnet)
        from .forkview import show_forks
        show_forks(devnet)

    elif args.command == "hive":
        devnet = resolve_devnet(args.devnet)
        from .hive import show_hive
        show_hive(devnet)

    elif args.command == "events":
        devnet = resolve_devnet(args.devnet)
        from .detect import show_events
        show_events(
            devnet,
            kind=args.kind,
            severity=args.severity,
            active_only=args.active_only,
            include_resolved=args.include_resolved,
        )

    elif args.command == "proposals":
        devnet = resolve_devnet(args.devnet)
        from .proposals import show_proposals
        show_proposals(devnet, since=args.since)

    elif args.command == "backfill":
        devnet = resolve_devnet(args.devnet)
        from .dora import backfill
        backfill(devnet, args.from_slot, args.to_slot)

    elif args.command == "bal":
        devnet = resolve_devnet(args.devnet)
        from .bal import show_bal
        show_bal(devnet)

    elif args.command == "epbs":
        devnet = resolve_devnet(args.devnet)
        from .epbs import show_epbs
        show_epbs(devnet)

    elif args.command == "eip-track":
        devnet = resolve_devnet(args.devnet)
        from .eiptrack import show_eiptrack
        show_eiptrack(devnet)

    elif args.command == "clients":
        devnet = resolve_devnet(args.devnet)
        from .network import show_clients
        show_clients(devnet)

    elif args.command == "spamoor":
        devnet = resolve_devnet(args.devnet)
        from .spamoor import show_spamoor
        show_spamoor(devnet)

    elif args.command == "deploy":
        devnet = resolve_devnet(args.devnet)
        from .deploytl import show_deploy
        show_deploy(devnet)

    elif args.command == "exectime":
        devnet = resolve_devnet(args.devnet)
        from .exectime import show_exectime
        show_exectime(devnet)

    elif args.command == "assertoor":
        devnet = resolve_devnet(args.devnet)
        from .assertoor import show_assertoor
        show_assertoor(devnet)

    elif args.command == "eips-refresh":
        devnet = resolve_devnet(args.devnet)
        from .collect import collect
        collect(devnet, "forks")

    elif args.command == "serve":
        import uvicorn
        host = args.host
        port = args.port
        print(f"Dashboard: http://{host}:{port}/  (read-only, localhost-only, no auth)")
        uvicorn.run("web.app:app", host=host, port=port)


if __name__ == "__main__":
    main()
