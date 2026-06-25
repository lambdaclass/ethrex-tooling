# Benchmarkoor Dashboard — Plan

Interactive dashboard over the ethPandaOps **Benchmarkoor API** (EL client BAL benchmarks).
Goal: find where **ethrex** is *lacking* (missing/failing tests) and *benching below* the other
clients, with comparison panels, leaderboard, and trends over time.

Decisions (confirmed):
- **Home client:** `ethrex` — highlighted everywhere; "lacking/below" computed relative to ethrex.
- **Data strategy:** local **SQLite snapshot** populated by a sync job (handles API 500/524 flakiness).
- **History scope:** **active suites only** (latest suites with recent runs; keeps the old/new regenerated pair).
- **Frontend:** **FastAPI + Jinja + HTMX + Plotly**.

---

## What the API actually exposes (verified live, 2026-06-11)

- **6 clients:** `besu, geth, nethermind, ethrex, erigon, reth`.
- **Instances (client + mode):** `besu-bal-full`, `besu-bal-full-aot`, `nethermind-bal-full`,
  `nethermind-bal-nobatchio`, `nethermind-bal-sequential`, `ethrex-bal-full`, `geth-bal-full`,
  `erigon-bal-full`, `reth-bal-full`. Mode (full/aot/nobatchio/sequential) is encoded in `instance_id`.
- **Suites** (`/index/query/suites`): `name` = `<network>-<block>[-<fork>]-<variant>`, variant ∈
  `{compute, stateful, stateful-bloat}`. Currently active: `jochemnet-24402727-amsterdam` in
  compute (4871 tests) + stateful (550 tests). Each exists as an **old/new pair** — same name,
  identical test list, different `suite_hash`, different `indexed_at` (suite was regenerated →
  new hash). Discriminator is `indexed_at`/latest-run-timestamp, **not** a tag.
- **Runs** (`/index/query/runs`, wrapped in `{data,limit,offset}`): flat columns + `steps_json`
  with `setup`/`test` step aggregates (gas_used, gas_used_duration ns, resource_totals).
- **test_stats** (`/index/query/test_stats`): the gold table — one row per (run, test):
  `test_name, client, suite_hash, run_id, test_mgas_s` (throughput, **higher = better**),
  `test_time_ns, test_gas_used, test_rpc_calls_count`, and `*_resource_*` (cpu_usec,
  memory_bytes, disk_read/write_bytes/iops). `setup_*` mirror exists. **No `passed` column.**
  Filtering `?run_id=eq.<id>` returns exactly the suite's test rows (e.g. 4871) — efficient.
- **live_runs** (`/index/live_runs`): in-progress runs incl. per-test `{passed,gas_used,...}`.
- API is **intermittently 500/524** under load → client needs retry/backoff; sync into SQLite.

### test_name shape
`test_single_opcode.py__test_log_benchmark[fork_Amsterdam-benchmark_test-...-benchmark_180M].txt`
→ parse `file` (`test_*.py`), `fork` (`fork_<X>`), `benchmark_mgas` (`benchmark_<N>M`), keep raw.

---

## Metrics & definitions

- **Primary metric:** `test_mgas_s` (Mgas/s, higher better). Secondary: `test_time_ns`,
  `test_gas_used`, cpu/mem/disk resources, rpc calls.
- **Current comparison** uses the **latest completed run per (suite, instance)**.
- **Suite-level throughput** (for trends, cheap): from `runs.steps_json.test`,
  `mgas_s = gas_used * 1000 / gas_used_duration_ns`.
- **Lacking (coverage gap):** `tests run by any client in suite` − `tests run by ethrex`
  (missing), plus tests where ethrex ran but other clients didn't (asymmetry), plus suites
  where ethrex isn't running at all.
- **Benching below:** per test, `ethrex_mgas / median(other_clients_mgas)`; flag `< 1.0`,
  rank ethrex among 6, severity by gap %. Aggregated by test `file`/`fork`.
- **Leaderboard score per client/suite:** median `test_mgas_s` (robust), plus **win count**
  (# tests where client is fastest) and # tests covered.

---

## Architecture

```
dashboard/
  pyproject.toml            # uv-managed; fastapi, uvicorn, httpx, jinja2, plotly, pandas, tenacity, python-dotenv
  .env.example              # points at ../.env for the key by default
  .gitignore                # .env, .venv, *.db, __pycache__
  README.md
  PLAN.md
  app/
    config.py               # load key/base from env or ../.env; settings (active-window days, db path)
    client.py               # httpx client: get(), query() ({data} unwrap), paginate(); tenacity retry on 5xx/524/timeout
    db.py                   # sqlite schema + connection helpers
    sync.py                 # CLI: pull suites -> active set -> runs -> current test_stats; upsert
    parse.py                # test_name + suite name parsing helpers
    queries.py              # pandas-backed analytics: leaderboard, coverage, below-median, trends
    main.py                 # FastAPI routes + Jinja + HTMX partials; Plotly JSON -> client
    templates/              # base.html, index, leaderboard, coverage, compare, trends, test_detail + _partials
    static/                 # styles.css; plotly.js + htmx via CDN
```

### SQLite schema
- `suites(suite_hash PK, name, network, block, fork, variant, tests_total, indexed_at, is_active)`
- `runs(run_id PK, suite_hash, timestamp, timestamp_end, status, client, instance_id, image,
   rollback_strategy, tests_total, tests_passed, tests_failed, test_gas_used,
   test_gas_used_duration, mgas_s, is_current)`
- `test_stats(run_id, suite_hash, client, instance_id, test_name, file, fork, benchmark_mgas,
   test_mgas_s, test_time_ns, test_gas_used, rpc_calls, cpu_usec, memory_bytes,
   disk_read_bytes, disk_write_bytes, PRIMARY KEY(run_id, test_name))`
- `sync_meta(key PK, value)` — last_sync, counts.

### Sync algorithm
1. `GET /index/query/suites` → parse, group by `name`, compute latest run ts per suite.
2. **Active set** = suites whose latest run ts ≥ (global max run ts − `ACTIVE_WINDOW_DAYS`).
   Keeps current jochemnet old+new pairs.
3. For each active suite: paginate `runs` → upsert; derive `mgas_s`.
4. Mark `is_current` = latest **completed** run per `(suite_hash, instance_id)`.
5. For each current run: `GET /index/query/test_stats?run_id=eq.<id>` → upsert parsed rows.
6. Write `sync_meta`. Idempotent; safe to re-run. (Optional later: APScheduler/cron.)

---

## Pages

1. **`/` Overview** — active-suite cards (name, old/new, #clients, freshness), ethrex headline:
   coverage %, leaderboard rank per suite, # tests below median, last sync time, live-run banner.
2. **`/leaderboard`** — per suite: client ranking table (median Mgas/s, win count, coverage),
   bar chart; metric + suite toggles (HTMX). ethrex row highlighted. (bal-dashboard inspired.)
3. **`/coverage`** — ethrex gaps: missing tests, suites not running ethrex, asymmetric coverage;
   grouped by file/fork; counts + drill list.
4. **`/compare`** — per-test table: ethrex vs each client Mgas/s, delta% vs median, rank;
   filters (suite, file, fork, "only where ethrex below"); sortable; severity coloring.
5. **`/trends`** — timeline (Plotly) of suite-level Mgas/s per instance across runs; per-metric;
   regression markers (run-over-run drop for ethrex).
6. **`/test/{run_or_name}`** — single-test drill-down: per-client bar of Mgas/s + resources for
   the current runs; history if available.

---

## Phases

- **Phase 1 — Foundation & data layer:** pyproject/uv, config, `client.py` (retry), `db.py`,
  `parse.py`, `sync.py`. Deliver: `uv run python -m app.sync` populates a local SQLite snapshot.
- **Phase 2 — Analytics:** `queries.py` (pandas) — leaderboard, coverage gaps, below-median,
  trends series. Unit-checked against live snapshot.
- **Phase 3 — App shell + Overview + Leaderboard:** FastAPI, base template, HTMX toggles,
  Plotly rendering, Overview + Leaderboard pages.
- **Phase 4 — Compare + Coverage:** per-test comparison table with filters; coverage gap page.
- **Phase 5 — Trends + Test detail:** timeline charts, regression markers, drill-down.
- **Phase 6 — Polish:** README run instructions, sync freshness banner, error states for API down,
  optional scheduled sync.

---

## Open questions / things you might add
- **Modes:** treat nethermind `full/nobatchio/sequential` and besu `full/aot` as separate
  entries in leaderboard/compare, or collapse to one row per client (best mode)? Default: show
  per-instance, with a "primary instance per client" = `*-bal-full`.
- **Old vs new suite pair:** default views use the **new** (latest) suite; add a toggle to view
  the old one and a "regeneration diff" (did ethrex's numbers change between hashes)?
- **Regression alerting:** just visual markers, or a threshold list ("ethrex dropped >10% vs
  previous run")?
- **Stateful-bloat / other networks (mainnet, perf-devnet-3):** include if they reappear as
  active, or hard-scope to jochemnet for now? (Sync auto-includes any suite with recent runs.)
- **Failing tests:** test_stats has no pass/fail; use `runs.tests_failed` + `live_runs` per-test
  `passed`. Pull a failing-tests list from live_runs/last run? Worth a dedicated panel?
- **Refresh cadence:** manual `sync` only, or background scheduler every N min?
