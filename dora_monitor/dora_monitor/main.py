import argparse
import logging
import signal
import sys
import time

from dora_monitor.checks import run_checks
from dora_monitor.config import load_config
from dora_monitor.dora import DoraClient
from dora_monitor.slack import SlackNotifier
from dora_monitor.state import load_state, save_state

log = logging.getLogger("dora_monitor")


def _setup_logging(debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def cli() -> None:
    parser = argparse.ArgumentParser(
        prog="dora-monitor",
        description="Monitor a Dora explorer for client-specific issues and alert to Slack.",
    )
    parser.add_argument("-c", "--config", required=True, help="Path to YAML config file")
    parser.add_argument("--once", action="store_true", help="Run a single check tick and exit")
    parser.add_argument("--dry-run", action="store_true", help="Print alerts instead of posting to Slack (no webhook needed)")
    parser.add_argument("--reset-state", action="store_true", help="Ignore the persisted state file (force re-alerting)")
    parser.add_argument("--debug", action="store_true", help="Verbose logging")
    parser.add_argument("--force-heartbeat", action="store_true", help="Post a heartbeat digest on the first tick regardless of interval")
    args = parser.parse_args()

    cfg = load_config(args.config, require_slack=not args.dry_run)
    if args.debug:
        cfg.debug = True
    _setup_logging(cfg.debug)

    dora = DoraClient(cfg.dora_url, timeout=cfg.http_timeout)
    slack = SlackNotifier(cfg.slack_webhook_url, cfg.network_label, timeout=cfg.http_timeout)
    if args.dry_run:
        import json as _json
        prefix = slack._prefix()
        def _dry_send(text: str) -> None:
            print(f"[DRY-RUN] {prefix}{text}")
        def _dry_send_blocks(blocks: list, fallback: str) -> None:
            print(f"[DRY-RUN] {prefix}{fallback}")
            if args.debug:
                print("[DRY-RUN blocks JSON]")
                print(_json.dumps(blocks, indent=2))
        slack.send = _dry_send  # type: ignore[assignment]
        slack.send_blocks = _dry_send_blocks  # type: ignore[assignment]

    state = load_state(None if args.reset_state else cfg.state_file)
    if args.dry_run:
        # Don't persist state during dry-run so the next run shows the same
        # alerts again instead of being silenced by dedup.
        cfg.state_file = None
    if args.force_heartbeat:
        state.last_heartbeat_ts = 0.0
        if cfg.heartbeat_interval_minutes <= 0:
            cfg.heartbeat_interval_minutes = 1

    stop = {"flag": False}

    def _signal(_signum, _frame):
        stop["flag"] = True
        log.info("shutdown requested")

    signal.signal(signal.SIGINT, _signal)
    signal.signal(signal.SIGTERM, _signal)

    log.info(
        "monitoring %s for clients matching %r every %ds",
        cfg.dora_url,
        cfg.client_match,
        cfg.poll_interval,
    )

    while not stop["flag"]:
        try:
            run_checks(dora, slack, cfg, state)
        except Exception as e:
            log.exception("tick failed: %s", e)
        save_state(cfg.state_file, state)
        if args.once:
            break
        for _ in range(cfg.poll_interval):
            if stop["flag"]:
                break
            time.sleep(1)

    log.info("exiting")
    sys.exit(0)


if __name__ == "__main__":
    cli()
