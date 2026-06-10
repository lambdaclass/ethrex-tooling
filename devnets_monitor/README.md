# Devnets Monitor

Ops + monitoring toolkit for [ethrex](https://github.com/lambdaclass/ethrex) on
[ethpandaops](https://ethpandaops.io) devnets, living in the `devnets_monitor/`
directory of `ethrex-tooling` (ported from the standalone `ethrex-devnets` repo).
Single home for the devnet ops runbook, per-devnet incident history, and the
tooling that watches ethrex as new forks (glamsterdam, BAL, fusaka, ...) roll out.

Self-contained: it has its own Python package (`uv`-managed) and `dv` CLI; run all
commands from this directory (`cd devnets_monitor`).

Generic across devnets: everything is parameterized by devnet name
(`glamsterdam-devnet-5`, `bal-devnet-3`, ...). Read-only by default; every
mutating action (wipe, deploy) is gated behind an explicit flag.

## What it does

Four capability areas, one front door (`dv`):

1. **Node health monitoring** — multi-node, multi-client status sweeps (EL
   build/commit/head/peers/sync/state-at-head, CL sync line, watchtower);
   peer mix; log tails.
2. **Blob & fork tracking** — blob inclusion per proposer over time, ethrex vs
   other clients; fork schedule + EIP-per-fork; next-fork countdown.
3. **Hive / conformance** — pull and summarize Hive group runs (bal, bal-quick,
   future fork groups) and pass rates for ethrex.
4. **Ops automation** — wipe/resync, debug-log capture, watchtower control,
   wrapped as safe (mutation-gated) commands.

## Layout

```
devnets/        # the Python package: CLI, SSH orchestration, collectors, analysis, store
  cli.py        #   `dv` argparse dispatcher (the daily driver)
  remote.py     #   shell snippets that run ON the devnet host (sent over ssh bash -s)
config/         # devnets.yaml (registry) + devnets/<name>.yaml (discovered cache) + eips.json
docs/           # devnet-ops runbook, per-devnet history, architecture/agent guide
data/           # SQLite store (gitignored, regenerable)
web/            # FastAPI dashboard (read-only, localhost)
pyproject.toml  # uv-managed; exposes the `dv` console script
```

## Quick start

Everything runs through `uv run dv` (or just `dv` if the venv is on PATH):

```bash
# discover a devnet's roster + fork schedule from the ethpandaops repo
uv run dv discover glamsterdam-devnet-5

# live health sweep across all ethrex nodes
uv run dv status glamsterdam-devnet-5

# collect historical data into SQLite, then analyze
uv run dv collect glamsterdam-devnet-5 all
uv run dv blob glamsterdam-devnet-5      # blob inclusion per proposer over time
uv run dv fork glamsterdam-devnet-5      # fork schedule + EIPs + countdown

# pull Hive conformance results
uv run dv hive glamsterdam-devnet-5

# read-only dashboard at http://127.0.0.1:8099
uv run dv serve

uv run dv --help                          # full subcommand list (read-only vs mutating)
```

The default devnet (`config/devnets.yaml` `default:`) is used when no `<devnet>`
argument is given. Override per-invocation with `DEVNET=<name>`.

## Requirements

- `uv` (drives the whole CLI; manages the Python env)
- `ssh` (access to `devops@<node>.srv.<devnet>.ethpandaops.io`)
- `gh` authenticated with read access to the ethpandaops devnet repos (for `dv discover`)
- On the devnet hosts: `docker`, `runlike` (for the wipe path), `curl`

## Conventions

- Read-only by default. Mutating commands (`dv wipe`) refuse to run without `--yes`.
- No secrets in git. SSH keys, JWTs, Xatu credentials live only on hosts; a
  content audit gates commits (see CLAUDE.md).
- User-facing text uses `;` or `,`, not the double-hyphen dash.

See `CLAUDE.md` for the full working agreement and `docs/architecture.md` for how
the pieces fit together.
