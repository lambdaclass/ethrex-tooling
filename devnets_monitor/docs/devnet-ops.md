# ethrex devnet ops — access & inspection runbook (generic)

Generic procedures for operating/inspecting ethrex EL nodes on any ethpandaops devnet. Substitute
`<devnet>` with the network name, e.g. `glamsterdam-devnet-5`. Per-devnet facts (fork schedule, node
roster, incidents) live in `docs/history/<devnet>.md`.

Read-only by default (logs/inspect/curl). Recreate/wipe are the only mutating actions.

## CLI: `dv`

The `dv` dispatcher wraps all SSH+curl+docker patterns. Read-only by default; `wipe` is the only
mutating subcommand, gated behind `--yes`. Set `DEVNET` env to target another network, or pass it
as the first argument (default taken from `config/devnets.yaml`).
```
dv status [devnet] [node|all]          # EL build/head/peers/syncing/state@head + CL sync line + watchtower
dv peers  [devnet] <node>              # peer count, inbound/outbound, client mix, body-serving failures
dv logs   [devnet] <node> [since]      # tail execution WARN/ERROR (default since 2m)
dv cl     [devnet] <node> [since]      # tail beacon sync lines (default since 3m)
dv wipe   [devnet] <node> --yes        # MUTATING: pause wt->runlike->rm->wipe DB->recreate->snooper restart->unpause
dv discover [devnet]                   # refresh config/devnets/<devnet>.yaml from the devnet repo
```
`wipe` bakes in the full procedure below INCLUDING the mandatory `docker restart snooper-engine` (see "Wipe
& resync"). `status`'s `state@head` uses `eth_getBalance(@latest)` (yes = canonical head has state on disk;
distinguishes a real stateless wedge from a node that simply trails its CL). Prefer this over hand-typed
one-liners; if the node roster changes, run `dv discover <devnet>` to refresh `config/devnets/<devnet>.yaml`.

## Find the nodes (inventory / instances)

The ethpandaops instances page (`https://ethpandaops.io/networks/<devnet>/?tab=instances`) is a
client-rendered SPA; it 404s to any plain fetcher/WebFetch. Source of truth = the devnet repo:
```
# repo varies per devnet family; e.g. ethpandaops/glamsterdam-devnets for glamsterdam-devnet-N
gh api repos/ethpandaops/<devnets-repo>/contents/ansible/inventories/<devnet>/inventory.ini --jq .content | base64 -d
gh api repos/ethpandaops/<devnets-repo>/contents/ansible/inventories/<devnet>/group_vars/all/images.yaml --jq .content | base64 -d
```
- `inventory.ini` groups nodes by `[<cl>_<el>]`; the `[ethrex:children]` group lists all ethrex nodes.
- `images.yaml` `default_ethereum_client_images.ethrex` = the deployed ethrex image tag.

CAUTION: inventory = INTENDED config, not live. A node named `*-ethrex-*` may have been manually
swapped to another client. ALWAYS verify on the host:
```
docker inspect execution --format "{{.Config.Image}}"
```

## SSH access

```
ssh -o StrictHostKeyChecking=accept-new devops@<node>.srv.<devnet>.ethpandaops.io
```
Node naming: `<cl>-<el>-<n>` and `buildoor-<cl>-<el>-<n>` (local block builder nodes).
All hosts accept the same `devops@` key.

## Containers per node

- `execution`  → ethrex (EL). Entrypoint `./ethrex`, workdir `/usr/local/bin`.
- `beacon`     → the CL (lighthouse/grandine/prysm/...).
- `validator`  → validator client.
- `snooper-engine` → engine-API proxy between CL and EL (`http://snooper-engine:8561`); logs FCU/newPayload.
- `ethereum-node-docker-watchtower` → auto-updates containers on new image.

ethrex container details:
- datadir bind mount: host `/data/ethrex` → container `/data` (owned by uid **1004**)
- JWT: `/data/execution-auth.secret` → `/execution-auth.jwt`
- network config: `/data/ethereum-network-config/metadata` → `/network-config`
- ports: 30303 tcp/udp (p2p, public), `127.0.0.1:8545` (http rpc), `127.0.0.1:8551` (authrpc), `127.0.0.1:6060` (metrics)
- network: docker network `shared`; restart=always; log-opt max-size=500m max-file=8
- image: `ethpandaops/ethrex:<devnet>`; labels `buildnum`, `commit`

## Common inspection (on a node)

```
# what build/commit is deployed + uptime + which client image
docker inspect execution --format "image={{.Config.Image}} buildnum={{index .Config.Labels \"buildnum\"}} commit={{index .Config.Labels \"commit\"}} started={{.State.StartedAt}}"

# head / peers / syncing
curl -s localhost:8545 -H content-type:application/json -d '{"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}'
curl -s localhost:8545 -H content-type:application/json -d '{"jsonrpc":"2.0","method":"net_peerCount","params":[],"id":1}'
curl -s localhost:8545 -H content-type:application/json -d '{"jsonrpc":"2.0","method":"eth_syncing","params":[],"id":1}'
# blob base fee + ratio
curl -s localhost:8545 -H content-type:application/json -d '{"jsonrpc":"2.0","method":"eth_feeHistory","params":["0xa","latest",[]],"id":1}'

# metrics (Prometheus): tx tracker by type, p2p msg counts, getBlobs, tx errors
curl -s localhost:6060/metrics | grep -E "transactions_tracker|ethrex_p2p_(in|out)going_messages|getBlobsV3.+count|transaction_errors"

# CL-not-driving-EL symptom
docker logs --since 90s execution 2>&1 | grep -c "No messages from the consensus"

# sync-loop health (full-sync stalls)
docker logs --since 30m execution 2>&1 | grep -iE "Sync cycle|No bodies|state root missing|fetch headers|penalizing"

# beacon peers (CL p2p) — watch for peers=[0/200] (isolated) or headSlot<<currentSlot (CL behind)
docker logs --since 4m beacon 2>&1 | grep -iE "peers=|headSlot|forkchoice|execution"

# engine traffic (FCU/newPayload + VALID/INVALID/SYNCING)
docker logs --since 3m snooper-engine 2>&1 | grep -iE "forkchoiceUpdated|newPayload|SYNCING|VALID|INVALID"
```

Note: metrics counters are cumulative (since restart); there is NO current-pool-size-by-type gauge, and
`txpool` RPC namespace is not enabled (only `eth,net,web3,admin,debug`). So you cannot read current
blob-mempool count without debug logs.

## Build & deploy workflow

1. Commit + push to the devnet branch (`git push origin <devnet>`).
2. Image build (NOT automatic on commit — must be triggered): GitHub Actions on
   `ethpandaops/eth-client-docker-image-builder`, target `lambdaclass/ethrex@<devnet>`.
   - check runs: `gh run list --repo ethpandaops/eth-client-docker-image-builder --limit 10`
   - it builds branch HEAD at checkout time → push BEFORE triggering, or later commits won't be included.
   - run's `headSha` is the BUILDER repo's commit, not ethrex's — identify the built ethrex commit by
     timing / buildnum bump.
   - Rust build takes ~15-20 min (amd64 + arm64 + manifest).
3. Deploy = `ethereum-node-docker-watchtower` (15-min poll, `--include-restarting`) auto-pulls the new tag
   and recreates `execution` fleet-wide.
   - watchtower recreate PRESERVES the running container's config (env/cmd) — a manual config change carries
     forward until the next image.
   - all ethrex nodes' watchtowers fire ~same cycle → simultaneous fleet restart (transient missed slots).

To deploy a specific build with custom config (e.g. info/debug logging), pull + recreate manually instead
of waiting for watchtower (see below), and pause watchtower so it doesn't fight you.

## Debug logging (requires container recreate)

ethrex log level: `--log.level <info|debug|trace>` / env `ETHREX_LOG_LEVEL`, OR `RUST_LOG`.
On these devnets `RUST_LOG=info` is set as env. To enable debug you must recreate the container.

Safe recreate using `runlike` (installed at `/usr/local/bin/runlike`):
```
docker pause ethereum-node-docker-watchtower         # so it can't bounce mid-capture
runlike execution > /tmp/run-execution-orig.txt      # capture exact run cmd
# edit: add `-d` (runlike omits detach!), set RUST_LOG to debug filter:
#   RUST_LOG=info,ethrex_blockchain=debug,ethrex_p2p=debug,ethrex_common=debug,ethrex_vm=debug,ethrex_levm=debug
sed -e 's#--env=RUST_LOG=info#--env=RUST_LOG=<filter>#' -e 's#^docker run #docker run -d #' /tmp/run-execution-orig.txt > /tmp/run-debug.sh
docker rm -f execution && bash /tmp/run-debug.sh
# ... capture ...
# revert: recreate with RUST_LOG=info (orig cmd + -d), then:
docker unpause ethereum-node-docker-watchtower
```

Build-path debug lines (in `ethrex_blockchain`): `Creating a new payload`, `fails 2D inclusion check`
(`payload.rs` 2D skip), `Failed to execute transaction` (apply fail), `No more blob gas`, `max data blobs`,
`Adding transaction: X to payload`.

CAUTION: a restart resets the in-memory mempool (clears any decay state) AND can break an in-progress
snap sync. So debug-capturing a slow-accumulating bug means letting it RE-decay with debug on.

## Wipe & resync (recover a wedged EL)

datadir is owned by uid 1004, so `devops` can't `rm` it; wipe via a root container:
```
docker pause ethereum-node-docker-watchtower
runlike execution > /tmp/run.txt                     # before removing
docker rm -f execution
docker run --rm --user 0 --entrypoint sh -v /data/ethrex:/d ethpandaops/ethrex:<devnet> \
  -c 'rm -rf /d/* /d/.[!.]*'
sed 's#^docker run #docker run -d #' /tmp/run.txt | bash   # fresh start from genesis
docker restart snooper-engine                        # REQUIRED: reconnect engine proxy to the new EL
# let sync COMPLETE uninterrupted, confirm head reaches tip, then:
docker unpause ethereum-node-docker-watchtower
```
IMPORTANT: after `docker rm -f execution` + recreate, the `snooper-engine` proxy still holds a stale
connection to the OLD execution container, so the CL can't drive the fresh EL — the EL logs `No messages
from the consensus layer` and `eth_syncing` stays at currentBlock=0/highestBlock=0 with peers but no FCU.
Fix: `docker restart snooper-engine`. After that the CL's FCU/newPayload reach the EL and full sync starts
(EL logs `[SYNCING] N% of batch processed`, currentBlock climbs toward the CL's exec head). Observed
2026-06-08 wiping lodestar-ethrex-2. (If a node talks to the EL directly without a snooper, restart the
beacon instead.)
For a wedged CL (e.g. grandine halted) a plain CL restart (`docker restart beacon`) is often enough — no
EL wipe needed.

## Dora API (devnet explorer)

Base: `https://dora.<devnet>.ethpandaops.io/api`  (swagger at `/api/swagger/doc.json`)
```
# recent slots; filter by proposer_name = "<cl>-<el>-<n>"; fields: blob_count, eth_block_number, status, time
curl -s "https://dora.<devnet>.ethpandaops.io/api/v1/slots?limit=400&with_missing=1&with_orphaned=1"
# blob inclusion per ethrex pair (post a time): split proposer_name on '-'
# status: Canonical / Missing / Orphaned. chain tip = latest slot's eth_block_number
```

## Find genesis / fork schedule (per-devnet)

```
gh api repos/ethpandaops/<devnets-repo>/contents/network-configs/<devnet>/metadata/genesis.json --jq .content | base64 -d
```
Fields: chainId, fork activation timestamps (cancun/prague/osaka/bpoN/amsterdam @ ts), `blobSchedule`
(target/max per fork). Record the concrete values for a given devnet in `docs/history/<devnet>.md`.
