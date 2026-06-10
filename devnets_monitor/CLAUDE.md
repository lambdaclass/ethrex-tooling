# CLAUDE.md — ethrex-devnets

Working agreement for any agent operating in this repo. Read this first, then
`docs/architecture.md` for the component map and `docs/devnet-ops.md` for the
operational procedures.

## What this repo is

A standalone ops + monitoring toolkit for ethrex on ethpandaops devnets. NOT part
of the ethrex codebase; it operates ethrex nodes from the outside (SSH + docker +
JSON-RPC) and pulls public devnet data (Dora, Hive, the ethpandaops devnet repo).
The owner uses it to track ethrex behavior across forks ("forks news").

It replaces git-excluded notes that used to live scattered inside the ethrex
working copies. This is now the single home for the devnet ops runbook and
per-devnet incident history.

## Golden rules

1. **Read-only by default.** Inspection (status/peers/logs/curl/collect) never
   mutates. Every mutating action (wipe, deploy, recreate) MUST be gated behind an
   explicit flag (`--yes`) and refuse to run without it. When in doubt, do not mutate.
2. **No secrets in git, ever.** SSH keys, JWTs, and Xatu/ClickHouse credentials
   live only on hosts. Before any `git add` of new/edited bash or docs, run:
   `rg -n 'xatu|jwt|Bearer|password|secret|-----BEGIN' lib/bash/ bin/ docs/ config/`
   and require empty output. The `.gitignore` guards by filename; this audit
   guards by content. Captured `runlike`/`run-*.sh` files (which embed host
   internals) are gitignored, never commit them.
3. **Generic over devnets.** Nothing is hardcoded to one devnet. New devnet =
   one entry in `config/devnets.yaml` + `dv discover <name>`. Never add a
   `NODES=()`-style hardcoded roster; read it from `config/devnets/<name>.yaml`.
4. **Verify the live host before trusting a name.** Inventory is INTENDED config;
   a `*-ethrex-*` node may have been manually swapped to another client. The
   status path always reads the live `docker inspect execution` image.
5. **Preserve the incident-tested remote sequences.** The shell snippets that run
   ON the devnet host (the wipe sequence, the status/peers probes) are generalized
   from a working, incident-tested helper. They run on the host over SSH and stay
   shell; the `wipe` sequence in particular is load-bearing (see "wipe" below).
   Change them only with a clear reason and preserve every step.

## Architecture in one paragraph

One codebase: **Python**, run via `uv`, with a single `dv` console entry point.
Python does everything local: config/YAML/JSON parsing, SSH orchestration
(subprocess to the system `ssh`), the SQLite store, analysis, and the dashboard.
The only shell involved is the snippets that execute ON the devnet host (docker,
runlike, curl, the wipe sequence); those are sent over `ssh ... bash -s` as
heredoc strings because they run on the host regardless of local language. A host
status probe emits JSON that Python parses in-process; there is no local-bash
intermediary and no cross-language file seam. Earlier drafts used a separate bash
CLI layer; it was collapsed into Python to remove the two-language seam and the
shell-injection / fragile-parsing surface. Full detail in `docs/architecture.md`.

## Conventions

- **Commits:** conventional commits, short. Types: `feat`, `fix`, `docs`, `chore`,
  `refactor`. No Co-Authored-By lines. Run the secret audit before committing.
- **Prose / user-facing text:** use `;` or `,`, never the double-hyphen dash.
- **Python:** run via `uv` (`uv run dv ...`); stdlib first; deps minimal
  (`pyyaml`, `requests`, and `fastapi`/`uvicorn`/`jinja2` only for the dashboard).
  No premature abstraction. One module per concern (`config`, `ssh`, `remote`,
  `status`, `discover`, `wipe`, `store`, `dora`, `hive`, `forks`, `collect`,
  `blobtrack`, `forkview`). Idempotent, re-runnable collectors (upsert on primary
  key, watermark incremental fetch).
- **Remote shell snippets:** keep them in `devnets/remote.py` as named string
  constants; send via `ssh ... bash -s`. Validate any value interpolated into a
  remote command (durations, node names) before sending; never f-string raw user
  input into a shell command. Prefer passing values as positional args to
  `bash -s` over interpolation.
- **Mutations:** only `dv wipe` mutates, gated behind `--yes`. Everything else is
  read-only.
- **Simplicity over complexity.** This is a personal/small-team ops tool, not a
  platform. Prefer the smallest thing that works.

## Data sources (exact)

- **SSH:** `devops@<node>.srv.<devnet>.ethpandaops.io`. Per-node docker
  containers: `execution` (ethrex), `beacon` (CL), `validator`, `snooper-engine`
  (engine proxy CL<->EL, logs FCU/newPayload), `xatu-sentry`, `vector`,
  `ethereum-node-docker-watchtower`, `prometheus`, `node_exporter`.
- **EL RPC** on node: `localhost:8545`, namespaces `eth,net,web3,admin,debug`
  (NO `txpool`). Metrics `localhost:6060/metrics` (Prometheus; cumulative
  counters, no current-pool-size gauge).
- **Dora** (blob inclusion over time, the practical source):
  `<dora_base>/api/v1/slots?limit=N&with_missing=1&with_orphaned=1` ->
  `data.slots[]` each with `slot`, `proposer_name`, `blob_count`,
  `eth_block_number`, `status`, `time`, `epoch`, `gas_used`, `execution_times[]`;
  paginate via `data.next_page` (also supports `min_slot`/`max_slot` range params).
- **ethpandaops config API:** `<config_base>/api/v1/nodes/inventory` (enodes/ENRs).
- **Devnet repo (source of truth)** via `gh api`:
  `repos/<devnets_repo>/contents/ansible/inventories/<repo_path>/inventory.ini`
  (group `[ethrex:children]` = roster), `.../group_vars/all/images.yaml`
  (`default_ethereum_client_images.ethrex` = image tag),
  `network-configs/<repo_path>/metadata/genesis.json` (chainId, fork timestamps,
  blobSchedule).
- **Hive:** `https://hive.ethpandaops.io` group listings; per-devnet groups in
  `devnets.yaml` `hive_groups`. Fetch the API directly; do not depend on any
  external/Claude-only wrapper script.
- **eipmcp** (MCP tool, not HTTP): EIP/fork data for `dv fork` enrichment;
  `dv eips-refresh` regenerates `config/eips.json`. Not auto-fetched at runtime.
- **Xatu / ClickHouse (FUTURE, credential-gated):** each node's `xatu-sentry`
  ships CL events (incl. `blob_sidecar`) via gRPC/TLS to
  `server.xatu-experimental.ethpandaops.io:443`. The public query endpoint
  `clickhouse.xatu.ethpandaops.io` needs credentials we do NOT have, and devnet
  data may live only in the experimental instance. Treat as optional; the core
  must NOT depend on it. Dora is the available substitute.

## The `wipe` sequence (do not break)

Recover a wedged EL. The datadir is owned by uid 1004, so wipe via a root
container. The full, incident-tested sequence:
pause watchtower -> `runlike execution` capture (ABORT if capture is not a valid
`docker run`) -> note if `--nat.extip` already present -> `docker pull` ->
`docker rm -f execution` -> root-container `rm -rf` of the datadir -> recreate
with `-d` (runlike omits detach) -> **`docker restart snooper-engine`** (REQUIRED:
the proxy holds a stale connection to the old EL; without this the CL can't drive
the fresh EL and it logs "No messages from the consensus layer") -> unpause
watchtower -> print post-status. If a node talks to the EL directly without a
snooper, restart `beacon` instead. Gated behind `--yes`.

## Adding a devnet

1. Add an entry under `devnets:` in `config/devnets.yaml` (see `config/schema.md`).
2. `dv discover <name>` to populate `config/devnets/<name>.yaml` from the repo.
3. Optionally `dv collect <name> all` to start a history.
4. Create `docs/history/<name>.md` from `docs/history/_template.md` and log
   facts/incidents there as you learn them (dated entries; what / why / recovery).

## Incident history is part of the job

`docs/history/<devnet>.md` is hard-won knowledge: root causes and recovery steps
for real wedges (blob decay, snap-leftover state wedges, 0-inbound-peers, etc.).
When you discover a new incident or devnet fact, append a dated entry. Check the
history before investigating a symptom from scratch; many recur.

## Where to look

- `docs/architecture.md` — components, the bash/Python seam, data flow.
- `docs/devnet-ops.md` — operational procedures (SSH, inspection, build/deploy,
  debug logging, wipe/resync, Dora API).
- `docs/history/<devnet>.md` — per-devnet facts + dated incident log.
- `config/schema.md` — config file field reference.
