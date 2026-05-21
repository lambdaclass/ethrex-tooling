import os
from dataclasses import dataclass, field

import yaml


@dataclass
class Checks:
    missed_blocks: bool = True
    forks: bool = True
    offline: bool = True
    sync_lag: bool = True
    version_drift: bool = True


@dataclass
class Config:
    dora_url: str
    client_match: str = "ethrex"
    slack_webhook_url: str = ""
    network_label: str = ""
    poll_interval: int = 30
    slot_scan_limit: int = 64
    sync_lag_threshold: int = 16
    # Number of consecutive polls a matched client must be on a non-canonical
    # head before we fire a fork alert. Filters out propagation-timing noise
    # where one client briefly leads or lags by a slot or two.
    fork_confirm_ticks: int = 3
    state_file: str | None = "./dora_monitor_state.json"
    http_timeout: int = 10
    debug: bool = False
    heartbeat_interval_minutes: int = 360  # 6 hours; set 0 to disable
    heartbeat_slot_window: int = 256
    # "off" (skip), "summary" (one-line aggregate), "detailed" (per-client list)
    heartbeat_other_clients: str = "detailed"
    checks: Checks = field(default_factory=Checks)


def load_config(path: str, require_slack: bool = True) -> Config:
    with open(path, "r") as f:
        raw = yaml.safe_load(f) or {}

    checks_raw = raw.pop("checks", {}) or {}
    try:
        checks = Checks(**checks_raw)
    except TypeError as e:
        raise ValueError(f"config: unknown key under `checks:` ({e})") from e

    try:
        cfg = Config(checks=checks, **raw)
    except TypeError as e:
        raise ValueError(f"config: unknown top-level key ({e})") from e

    env_hook = os.environ.get("SLACK_WEBHOOK_URL")
    if env_hook:
        cfg.slack_webhook_url = env_hook

    if not cfg.dora_url:
        raise ValueError("config: dora_url is required")
    cfg.dora_url = cfg.dora_url.rstrip("/")
    if require_slack and not cfg.slack_webhook_url:
        raise ValueError("config: slack_webhook_url is required (set in config, in SLACK_WEBHOOK_URL env, or pass --dry-run)")
    if not cfg.client_match:
        raise ValueError("config: client_match is required")

    return cfg
