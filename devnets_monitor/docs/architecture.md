# Architecture

How the pieces fit. For the working agreement and rules, see `CLAUDE.md`.

## Design goals

- One home for devnet ops + monitoring + incident history.
- Generic across ethpandaops devnets, parameterized by name.
- Read-only by default; mutations explicitly gated.
- Preserve the incident-tested remote sequences (the host-side shell).
- Minimal dependencies; the smallest thing that works.

## One codebase: Python

Everything local is Python, run via `uv`, behind a single `dv` console entry
point. There is no separate bash CLI layer (an earlier draft had one; it was
collapsed into Python to drop the two-language seam and the shell-injection /
fragile-parsing surface that came with it).

```
                        dv  (argparse dispatcher in devnets/cli.py)
                                       |
        +------------------------------+------------------------------+
        |                              |                              |
   host-touching                  data collection                analysis / view
   status/peers/logs/             collect (dora/hive/             blob/fork  +  serve
   cl/discover/wipe               health/forks)                   (FastAPI dashboard)
        |                              |                              |
        | subprocess: ssh / gh         | requests: Dora/Hive/gh       | reads SQLite
        v                              v                              v
   devnet hosts (over ssh)        data/ethrex-devnets.sqlite    data/ + config/
        |
        | the ONLY shell: snippets that run ON the host
        v
   ssh <host> bash -s  <<  remote.py constant  (docker / runlike / curl)
```

### The only shell is host-side

The commands that operate a node (docker inspect, runlike, the datadir wipe, RPC
curls) run ON the devnet host, so they are shell no matter what drives them
locally. They live in `devnets/remote.py` as named string constants and are sent
over `ssh <host> bash -s`, with values passed as positional args (not
interpolated) and any free-form input validated first. A host status probe emits
JSON on stdout; Python parses it in-process. So there is no local-bash
intermediary and no cross-language file seam; the JSON is produced on the host and
consumed directly.

### Why subprocess-ssh (not paramiko/fabric)

`subprocess` to the system `ssh` reuses the user's existing ssh config, keys, and
`known_hosts`, adds zero dependencies, and keeps the remote snippets identical to
what an operator would run by hand. A library would add a dependency and an auth
surface for no gain here.

## The `dv` CLI

`dv <subcommand> [devnet] [args]` (via `uv run dv ...`). It resolves the target
devnet (explicit arg > `$DEVNET` > `config/devnets.yaml` `default`), then dispatches:

| subcommand     | mutating | what |
|----------------|----------|------|
| `status`       | no  | per-node EL build/head/peers/state@head + CL line + watchtower (or `--json`) |
| `peers`        | no  | peer count, inbound/outbound, client mix, body-serving failures |
| `logs`         | no  | tail execution WARN/ERROR (validated `--since`) |
| `cl`           | no  | tail beacon sync lines (validated `--since`) |
| `discover`     | no  | refresh `config/devnets/<name>.yaml` roster/forks/image from the repo (via `gh`) |
| `wipe`         | YES | recover a wedged EL; requires `--yes` |
| `collect`      | no  | pull Dora/Hive/health/forks into SQLite |
| `blob`         | no  | blob inclusion per proposer over time; ethrex vs others |
| `fork`         | no  | fork schedule -> human time, blob target/max, EIP-per-fork, countdown |
| `hive`         | no  | summarize Hive group runs for the devnet |
| `eips-refresh` | no  | regenerate `config/eips.json` from eipmcp data |
| `serve`        | no  | read-only FastAPI dashboard on 127.0.0.1 |

## Config flow

`config/devnets.yaml` (static, hand-maintained) lists each devnet and its repo +
service URLs. `dv discover <name>` reads the ethpandaops devnet repo via `gh`
(subprocess) and writes `config/devnets/<name>.yaml` (roster, fork schedule, image
tag, `discovered_at`) parsed with `pyyaml` / `json`. Both the CLI and the data
layer read these files; neither hardcodes a roster. The cache lets the CLI work
offline and against shut-down devnets. See `config/schema.md` for fields.

## Data store

Single SQLite file `data/ethrex-devnets.sqlite`, every table keyed by a `devnet`
column so one store covers all devnets. Collectors are idempotent (upsert on
primary key) and incremental (watermark on max stored slot). Tables:

- `slots(devnet, slot, ...)` — per-slot proposer, blob_count, block number, status
- `slot_exec_times(devnet, slot, client_type, ...)` — per-client execution timing
- `node_health(devnet, node, ts, ...)` — point-in-time health snapshots
- `hive_runs(devnet, group_name, suite_id, ...)` — Hive pass/fail per run
- `fork_schedule(devnet, fork, activation_ts, blob_target, blob_max)`
- `fork_eips(devnet, fork, eip, title)` — EIP-per-fork enrichment

Incident history lives in `docs/history/<devnet>.md` (markdown), not the DB.

## Dashboard

`web/app.py` is a read-only FastAPI app over the SQLite store, bound to
`127.0.0.1`, no auth, no write endpoints. It is a view over already-collected
data; run `dv collect` first or it shows empty. Routes: `/`, `/blobs/<devnet>`,
`/forks/<devnet>`, `/hive/<devnet>`, `/incidents/<devnet>`. The index surfaces the
ethpandaops portal Services URLs (Dora, Forkmon, Assertoor, Checkpoint Sync,
Tracoor, Syncoor, Spamoor, Buildoor) per devnet.

## What lives where

```
devnets/cli.py          argparse dispatcher + `dv` console entry (the front door)
devnets/config.py       load registry + discovered cache; resolve target devnet
devnets/ssh.py          run a remote snippet via subprocess ssh (bash -s, positional args)
devnets/remote.py       host-side shell snippets as constants (status probe, peers probe, wipe)
devnets/status.py       status sweep: ssh-run the probe per node, parse JSON, format/store
devnets/peers.py        peer inspection
devnets/discover.py     gh-api roster/fork discovery -> config cache
devnets/wipe.py         mutating recovery driver (--yes gated; sends the wipe snippet)
devnets/store.py        sqlite open + schema migrations
devnets/dora.py         Dora slots fetch -> store
devnets/hive.py         Hive API fetch -> hive_runs
devnets/forks.py        fork schedule + eips.json -> store
devnets/collect.py      orchestrator for `dv collect`
devnets/blobtrack.py    `dv blob` analysis
devnets/forkview.py     `dv fork` analysis
config/devnets.yaml     static registry
config/devnets/<name>.yaml   discovered cache (committed, regenerable)
config/eips.json        fork -> EIP map (dv eips-refresh)
docs/devnet-ops.md      operational runbook
docs/history/<name>.md  per-devnet facts + incident log
web/app.py              FastAPI dashboard
pyproject.toml          uv project; `dv` console script -> devnets.cli:main
```
