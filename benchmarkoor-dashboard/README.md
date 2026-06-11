# Benchmarkoor Dashboard

Interactive dashboard over the ethPandaOps **Benchmarkoor API** (EL client BAL benchmarks),
focused on **ethrex**: where it's *lacking* (missing tests), where it *benches below* the
other clients, plus a leaderboard and trends over time.

See [`PLAN.md`](PLAN.md) for the full design and the verified API shape.

## Setup

Copy `.env.example` to `.env` and set your Benchmarkoor API key. The `.env` is gitignored —
keep the key out of version control.

```bash
cd benchmarkoor-dashboard
cp .env.example .env             # then edit .env: set BENCHMARKOOR_API_KEY
uv sync                          # create venv + install deps
uv run python -m app.sync        # pull active suites into data/benchmarkoor.db (SQLite)
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
# open http://localhost:8000  (or http://<lan-ip>:8000 from another machine)
```

Re-run `uv run python -m app.sync` to refresh the snapshot (idempotent).

## Pages

- **Overview** — active-suite cards: ethrex coverage %, leaderboard rank, # tests below median.
- **Leaderboard** — per-instance ranking by gas-weighted aggregate Mgas/s; median/mean/wins/gas-won as secondary columns.
- **Coverage** — ethrex's missing tests vs the union run by all clients, grouped by file.
- **Compare** — per-test Mgas/s matrix (ethrex vs each client), ratio vs median, rank; filterable.
- **Trends** — suite-level Mgas/s per instance across runs over time.
- **Test detail** — per-client bar for a single test.

## Agent API

Point an LLM agent at **`/agent.md`** (alias `/llm.md`) — a self-contained Markdown brief:
metric definitions, per-suite leaderboard, coverage gaps, and ranked optimization targets
for the home client (by `time_lost_ms` = home time − fastest competitor's time, per test,
aggregated per file/opcode). No scraping or MCP needed.

Machine-readable JSON (all take `?suite=<hash>`, default = latest compute suite):

- `GET /api/suites` — active suites + hashes.
- `GET /api/leaderboard` — per-instance aggregate ranking.
- `GET /api/targets?limit=&min_time_lost_ms=` — ranked per-test targets.
- `GET /api/targets/by_file` — targets aggregated per file/opcode.
- `GET /api/coverage` — tests not run by the home client.
- `GET /api/commits` — current home-client build + per-commit aggregate-throughput timeline.

`time_lost_ms` ranks by recoverable wall-clock, not Mgas/s ratio (which over-weights tiny tests).

### Commit association

The benchmark image uses a mutable tag (`ethpandaops/ethrex:bal-devnet-7`) rebuilt on each push
to its branch, so a run's commit = the branch HEAD at the run's timestamp. Sync fetches the
branch commit history via `gh` (`DASH_ETHREX_REPO`/`DASH_ETHREX_BRANCH`) and maps each home-client
run to its commit by time (optionally offset by `DASH_DEPLOY_LAG_MIN` for build+push lag). This
powers the build line + commit timeline in `/agent.md`, `/api/commits`, the leaderboard build
label, and the deploy markers on the Trends chart. Needs `gh` authenticated; skipped gracefully
if unavailable.

## Data model

- Source of truth is the API; we snapshot the **active suites** (suites with a run within
  `DASH_ACTIVE_WINDOW_DAYS` of the newest run) into SQLite.
- Comparisons use the **latest completed run per (suite, instance)**.
- Primary metric is `test_mgas_s` (Mgas/s, higher is better).
- Tuning via env: `DASH_DB_PATH`, `DASH_ACTIVE_WINDOW_DAYS`, `DASH_HOME_CLIENT`,
  `DASH_ETHREX_REPO`, `DASH_ETHREX_BRANCH`, `DASH_DEPLOY_LAG_MIN`.
