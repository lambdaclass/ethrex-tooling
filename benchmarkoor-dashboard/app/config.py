"""Configuration: API credentials + dashboard tuning.

The API key/base are loaded from the process env, then from a local .env
(benchmarkoor-dashboard/.env, gitignored — keep the key out of version control).
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent  # benchmarkoor-dashboard/

# Load local .env (does not override variables already set in the real environment).
load_dotenv(ROOT / ".env")

API_BASE = os.getenv(
    "BENCHMARKOOR_API_BASE", "https://benchmarkoor-api.core.ethpandaops.io"
).rstrip("/")
API_KEY = os.getenv("BENCHMARKOOR_API_KEY", "")

DB_PATH = Path(os.getenv("DASH_DB_PATH", ROOT / "data" / "benchmarkoor.db"))
ACTIVE_WINDOW_DAYS = int(os.getenv("DASH_ACTIVE_WINDOW_DAYS", "7"))
HOME_CLIENT = os.getenv("DASH_HOME_CLIENT", "ethrex")

# Display order / known clients.
CLIENTS = ["besu", "geth", "nethermind", "ethrex", "erigon", "reth"]


def require_key() -> str:
    if not API_KEY:
        raise SystemExit(
            "BENCHMARKOOR_API_KEY not set. Put it in dashboard/.env or ../.env "
            "(see .env.example)."
        )
    return API_KEY
