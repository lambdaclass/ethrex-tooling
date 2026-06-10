---
name: devnet-ops
description: Operate, inspect, and debug ethrex EL nodes on ethpandaops devnets (SSH access, finding nodes, reading logs, deploy, wipe/resync, incident history). Use whenever the user asks to ssh into a devnet node, check/inspect a devnet node, find or read logs on glamsterdam/bal/any ethpandaops devnet, investigate a devnet error report, deploy or wipe a node, or asks about past devnet problems.
---

# devnet-ops

Before doing any ethrex devnet operations or answering questions about a devnet node/incident, READ both
reference files:

1. `docs/devnet-ops.md` — generic access & inspection procedures (SSH, finding nodes via inventory,
   container layout, inspection curl/docker commands, build & deploy, debug logging, wipe & resync, Dora
   API). Substitute `<devnet>` with the target network (e.g. `glamsterdam-devnet-5`).

2. `docs/history/<devnet>.md` — per-devnet facts and incident history (node roster, fork schedule, commit
   map, known issues with root cause and recovery). For glamsterdam-devnet-5 this is
   `docs/history/glamsterdam-devnet-5.md`. If a file for the requested devnet doesn't exist yet, use the
   generic doc and create a new `docs/history/<devnet>.md` from `docs/history/_template.md` once you learn
   devnet-specific facts.

## CLI

The `dv` dispatcher wraps all common SSH/curl/docker checks (read-only by default; `wipe` is mutating and
gated behind `--yes`). Prefer it over hand-typed one-liners:
```
dv status [devnet] [node|all]     # EL build/head/peers/state@head + CL sync line + watchtower
dv peers  [devnet] <node>         # peer count, inbound/outbound, client mix, body-serving failures
dv logs   [devnet] <node> [since] # tail execution WARN/ERROR (default since 2m)
dv cl     [devnet] <node> [since] # tail beacon sync lines (default since 3m)
dv wipe   [devnet] <node> --yes   # MUTATING: full wipe + resync sequence
dv discover [devnet]              # refresh config/devnets/<devnet>.yaml from the devnet repo
```
Details (subcommands, the mandatory snooper-engine restart on wipe) are in `docs/devnet-ops.md`.

## Workflow

1. Read `docs/devnet-ops.md` for HOW (procedures, commands).
2. Read `docs/history/<devnet>.md` for WHAT/WHY (known issues, divergences, facts) — check whether the
   current symptom matches a known issue before investigating from scratch.
3. ALWAYS verify the live host before trusting a node name: `docker inspect execution --format "{{.Config.Image}}"`.
   A `*-ethrex-*` node may have been manually swapped to another client.
4. Default to read-only (logs/inspect/curl). Recreate/wipe/deploy are mutating — confirm with the user first.

## Maintenance

When you discover a new incident, divergence, or devnet fact during a session, append it to
`docs/history/<devnet>.md` (and `docs/devnet-ops.md` if a procedure changed). Keep entries dated.
These files are committed in this repo and form the long-term ops record.
