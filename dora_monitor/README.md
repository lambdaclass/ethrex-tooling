# dora_monitor

Polls a [Dora explorer](https://github.com/ethpandaops/dora) API and posts Slack alerts when a specific client (e.g. `ethrex`) misses a block, orphans a block, drifts onto a fork, falls behind the canonical head, or drops offline.

## Install

```bash
cd dora_monitor
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

## Configure

Copy `config.example.yaml` and edit it; the only mandatory fields are `dora_url`, `client_match`, and the Slack webhook (which can come from `SLACK_WEBHOOK_URL` instead).

```bash
cp config.example.yaml config.yaml
$EDITOR config.yaml
```

`client_match` is a case-insensitive substring matched against Dora's `proposer_name` and client `name` fields (e.g. `lighthouse-ethrex-1`), so `ethrex` catches every CL/EL pair that runs the ethrex execution layer.

## Run

```bash
# foreground
dora-monitor -c config.yaml

# one tick, no Slack
SLACK_WEBHOOK_URL=unused dora-monitor -c config.yaml --once --dry-run
```

The process holds dedup state in `state_file` so restarts don't re-alert on already-reported missed slots / open conditions.

## Alerts

| Trigger | Source endpoint |
|---|---|
| Missed slot by `client_match` proposer | `/api/v1/slots?with_missing=1` |
| Orphaned block by `client_match` proposer | `/api/v1/slots?with_orphaned=1` |
| `client_match` node on a non-canonical fork | `/api/v1/network/client_head_forks` |
| `client_match` node lagging `>= sync_lag_threshold` slots | `/api/v1/network/client_head_forks` |
| `client_match` node `status != online` | `/api/v1/network/client_head_forks` |
| `client_match` EL `version` string changes (deploy/rollback) | scraped from `/clients/execution` HTML |

Recoveries (fork resolved, caught up, back online) are posted as well.

The periodic heartbeat digest uses Slack Block Kit (`{"blocks": [...]}`) with a plain-text fallback for notifications. Action alerts (offline / fork / lag / version change / missed-block) use plain mrkdwn `text` posts. Clients with status `online`, on the canonical fork, and at `distance == 0` from canonical head collapse into a single "online @ canonical" bucket so the digest highlights outliers instead of repeating identical rows; use `heartbeat_other_clients: detailed` (default) to list the healthy names, `summary` for just a count, or `off` to drop the section entirely.

## A note on what "client" means here

`/api/v1/network/client_head_forks` lists Dora's **beacon (CL)** clients; their names embed the paired EL (e.g. `lighthouse-ethrex-1` is the Lighthouse beacon paired with an ethrex EL). So the offline / fork / lag signals are observed on the beacon side. An ethrex-EL crash shows up indirectly: the paired beacon's head stops advancing (sync_lag) or its status flips to non-online (offline).

Dora's `/api/v1/clients/execution` is deliberately NOT used. Its `status` field only reflects whether Dora's devp2p crawler could fetch `admin_nodeInfo` from the node, not whether the EL is healthy; ethrex EL nodes typically show `disconnected` there even when the UI shows them as `Ready` and following the chain. The execution clients page's real status (`Ready`/`Synchronizing`/`Offline`) is not exposed via any JSON API as of Dora master.
