---
name: devnet-ops
description: Operate, inspect, monitor, and debug ethrex EL nodes on ethpandaops devnets (status sweeps, peers/logs, blob & fork tracking, Hive results, wipe/resync, incident history). Use whenever the user asks to check/inspect a devnet node, ssh into one, read logs, investigate a devnet error, track blob inclusion or fork schedule, see Hive conformance, deploy/wipe a node, or ask about past devnet problems.
---

# devnet-ops

This repo (`ethrex-devnets`) is the single home for ethrex devnet ops + monitoring.
Procedures, per-devnet history, and the `dv` CLI all live here. Paths below are
relative to the repo root.

Before any devnet operation or answering a question about a node/incident, READ:

1. `CLAUDE.md` — the working agreement (golden rules, exact data sources, the wipe
   sequence).
2. `docs/devnet-ops.md` — generic access & inspection procedures (SSH, inventory,
   container layout, build & deploy, debug logging, wipe & resync, Dora API).
   Substitute `<devnet>` with the target network.
3. `docs/history/<devnet>.md` — per-devnet facts and incident history (roster, fork
   schedule, commit map, known issues with root cause + recovery). For
   glamsterdam-devnet-5 this is `docs/history/glamsterdam-devnet-5.md`. If a devnet
   has no history file, create one from `docs/history/_template.md` as you learn facts.

## The `dv` CLI

Run from the repo root via `uv run dv ...`. Read-only by default; only `dv wipe`
mutates (gated behind `--yes`). Target devnet resolves: explicit arg > `$DEVNET`
env > `config/devnets.yaml` `default`.

```
uv run dv discover <devnet>            # refresh roster/forks/image from the ethpandaops repo (gh)
uv run dv status <devnet> [node]       # EL build/head/peers/state@head + CL + watchtower (--json)
uv run dv peers  <devnet> <node>       # peer count, inbound/outbound, client mix, body-serving fails
uv run dv logs   <devnet> <node> [--since 2m]
uv run dv cl     <devnet> <node> [--since 3m]
uv run dv collect <devnet> [all|blobs|health|hive|forks]   # into data/ethrex-devnets.sqlite
uv run dv blob   <devnet>              # blob inclusion per proposer + ethrex-vs-others (decay lens)
uv run dv fork   <devnet>              # fork schedule + EIPs + countdown
uv run dv hive   <devnet>              # Hive conformance summary (groups from config/devnets.yaml)
uv run dv serve                        # read-only dashboard at http://127.0.0.1:8099
uv run dv wipe   <devnet> <node> --yes # MUTATING: recover a wedged EL
```

## Workflow

1. Read `docs/devnet-ops.md` for HOW (procedures).
2. Read `docs/history/<devnet>.md` for WHAT/WHY — check whether the current symptom
   matches a known issue before investigating from scratch; many recur.
3. ALWAYS verify the live host before trusting a node name. `dv status` reads the
   live `docker inspect execution` image; a `*-ethrex-*` node may have been swapped
   to another client.
4. Default to read-only. `dv wipe` and any deploy/recreate are mutating; confirm
   with the user first.

## Adding a devnet

1. Add an entry under `devnets:` in `config/devnets.yaml` (see `config/schema.md`).
2. `uv run dv discover <devnet>` to populate `config/devnets/<devnet>.yaml`.
3. Create `docs/history/<devnet>.md` from `docs/history/_template.md`.

## Maintenance

When you discover a new incident, divergence, or devnet fact, append a dated entry
to `docs/history/<devnet>.md` (and `docs/devnet-ops.md` if a procedure changed) and
commit it. The fork -> EIP map is `config/eips.json` (sourced via eipmcp; re-run
`get_hardfork` to refresh).
