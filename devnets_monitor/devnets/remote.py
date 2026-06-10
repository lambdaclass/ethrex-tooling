"""
Host-side shell snippets, sent over 'ssh <host> bash -s'.

LINE PROTOCOL (TSV):
  STATUS_PROBE and PEERS_PROBE emit lines of the form:
      key<TAB>value
  One key-value pair per line. Python assembles the dict in-process.
  This avoids hand-rolled JSON quoting bugs (cl_line can contain quotes/ANSI).

  STATUS_PROBE keys: image, status, buildnum, commit, restart, head, peers,
                     syncing, state_at_head, watchtower, cl_line

  PEERS_PROBE keys: peercount, total, inbound, outbound,
                    then zero or more: client<TAB><name><TAB><count>
                    then: bodyfail<TAB><N>

WIPE_SEQUENCE takes: $1 = image tag (full tag, e.g. ethpandaops/ethrex:devnet-5)

LOGS_TAIL takes:    $1 = since value (e.g. 2m, 30s), pre-validated by Python
CL_TAIL takes:      $1 = since value, pre-validated by Python
"""

# ---------------------------------------------------------------------------
# STATUS_PROBE
# ---------------------------------------------------------------------------
# Emits TSV lines: key<TAB>value
# cl_line is truncated to 200 chars, tabs and newlines stripped.
STATUS_PROBE = r"""
set -uo pipefail

rpc(){ curl -s --max-time 4 localhost:8545 -H 'Content-Type: application/json' -d "$1"; }

hx(){
    v=$(grep -oE '0x[0-9a-f]+' <<<"$1" | head -1)
    [ -n "$v" ] && printf '%d' "$v" || echo "null"
}

img=$(docker inspect execution --format '{{.Config.Image}}' 2>/dev/null || echo "")
status=$(docker inspect execution --format '{{.State.Status}}' 2>/dev/null || echo "no-EL")
restart=$(docker inspect execution --format '{{.RestartCount}}' 2>/dev/null || echo 0)
buildnum=$(docker inspect execution --format '{{index .Config.Labels "buildnum"}}' 2>/dev/null || echo "")
commit=$(docker inspect execution --format '{{index .Config.Labels "commit"}}' 2>/dev/null || echo "")

bn_raw=$(rpc '{"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}')
head=$(hx "$bn_raw")

pc_raw=$(rpc '{"jsonrpc":"2.0","method":"net_peerCount","params":[],"id":1}')
peers=$(hx "$pc_raw")

sy_raw=$(rpc '{"jsonrpc":"2.0","method":"eth_syncing","params":[],"id":1}')

if grep -q '"result":false' <<<"$sy_raw"; then
    syncing="synced(false)"
else
    cur=$(grep -oE '"currentBlock":"0x[0-9a-f]+"' <<<"$sy_raw" | grep -oE '0x[0-9a-f]+' | head -1)
    hi=$(grep -oE '"highestBlock":"0x[0-9a-f]+"' <<<"$sy_raw" | grep -oE '0x[0-9a-f]+' | head -1)
    if [ -n "$cur" ] && [ -n "$hi" ]; then
        cur_d=$(printf '%d' "$cur")
        hi_d=$(printf '%d' "$hi")
        syncing="cur=${cur_d}->hi=${hi_d}"
    else
        syncing="unknown"
    fi
fi

gb_raw=$(rpc '{"jsonrpc":"2.0","method":"eth_getBalance","params":["0x0000000000000000000000000000000000000000","latest"],"id":1}')
if grep -q '"result"' <<<"$gb_raw"; then
    state_at_head="yes"
else
    state_at_head="no"
fi

wt=$(docker inspect ethereum-node-docker-watchtower --format '{{.State.Status}}' 2>/dev/null || echo "?")

# Bound by line count, not time window: a chatty CL (e.g. nimbus) can emit
# thousands of lines in 4m, making `docker logs --since 4m` take 30s+ and blow
# the probe timeout. `--tail` is O(N lines), fast regardless of verbosity.
cl_raw=$(docker logs --tail 400 beacon 2>&1 | grep -iE "Synced|Syncing|Slot start|head slot|exec-block|descendant|empty slots" | tail -1 || echo "")
# Strip tabs, newlines, and ANSI escape sequences; truncate to 200 chars
cl_line=$(printf '%s' "$cl_raw" | sed 's/\x1b\[[0-9;]*[mGKHF]//g' | tr '\t\n\r' '   ' | cut -c1-200)

printf 'image\t%s\n' "$img"
printf 'status\t%s\n' "$status"
printf 'buildnum\t%s\n' "$buildnum"
printf 'commit\t%s\n' "$commit"
printf 'restart\t%s\n' "$restart"
printf 'head\t%s\n' "$head"
printf 'peers\t%s\n' "$peers"
printf 'syncing\t%s\n' "$syncing"
printf 'state_at_head\t%s\n' "$state_at_head"
printf 'watchtower\t%s\n' "$wt"
printf 'cl_line\t%s\n' "$cl_line"
"""

# ---------------------------------------------------------------------------
# PEERS_PROBE
# ---------------------------------------------------------------------------
# Emits TSV lines:
#   peercount<TAB><N>
#   total<TAB><N>
#   inbound<TAB><N>
#   outbound<TAB><N>
#   client<TAB><name><TAB><count>   (one per distinct client name, zero or more)
#   bodyfail<TAB><N>
#
# Uses mktemp for a safe temp file; cleans up on exit.
PEERS_PROBE = r"""
set -uo pipefail

rpc(){ curl -s --max-time 4 localhost:8545 -H 'Content-Type: application/json' -d "$1"; }

tmpfile=$(mktemp)
trap 'rm -f "$tmpfile"' EXIT

pc_raw=$(rpc '{"jsonrpc":"2.0","method":"net_peerCount","params":[],"id":1}')
peercount=$(grep -oE '0x[0-9a-f]+' <<<"$pc_raw" | head -1 | xargs -I{} printf '%d' {} 2>/dev/null || echo 0)

rpc '{"jsonrpc":"2.0","method":"admin_peers","params":[],"id":1}' > "$tmpfile"

total=$(grep -oE '"enode"' "$tmpfile" | wc -l)
inbound=$(grep -c '"inbound":true' "$tmpfile" || true)
outbound=$(grep -c '"inbound":false' "$tmpfile" || true)

printf 'peercount\t%s\n' "$peercount"
printf 'total\t%s\n' "$total"
printf 'inbound\t%s\n' "$inbound"
printf 'outbound\t%s\n' "$outbound"

# Client name histogram: extract "name":"<value>" fields
grep -oE '"name":"[^"]*"' "$tmpfile" | grep -oE '"[^"]*"$' | tr -d '"' | sort | uniq -c | \
    awk '{count=$1; $1=""; name=substr($0,2); printf "client\t%s\t%s\n", name, count}'

# Body-serving failures in the last 60s
bodyfail=$(docker logs --since 60s execution 2>&1 | grep -c "Didn.t receive block bodies" || true)
printf 'bodyfail\t%s\n' "$bodyfail"
"""

# ---------------------------------------------------------------------------
# WIPE_SEQUENCE
# ---------------------------------------------------------------------------
# Args: $1 = image tag (full, e.g. ethpandaops/ethrex:devnet-5)
# Incident-tested sequence; do not break.
WIPE_SEQUENCE = r"""
set -euo pipefail

IMAGE="$1"

if [ -z "$IMAGE" ]; then
    echo "ABORT: no image tag supplied as \$1" >&2
    exit 1
fi

echo "==> Pausing watchtower..."
docker pause ethereum-node-docker-watchtower || true

echo "==> Capturing current execution container config with runlike..."
runfile=$(mktemp)
trap 'rm -f "$runfile"' EXIT

runlike execution > "$runfile" 2>&1
captured=$(cat "$runfile")

if ! grep -q '^docker run ' "$runfile"; then
    echo "ABORT: runlike capture does not start with 'docker run'. Contents:" >&2
    cat "$runfile" >&2
    echo "==> Unpausing watchtower due to abort..."
    docker unpause ethereum-node-docker-watchtower || true
    exit 1
fi

if grep -q '\-\-nat\.extip' "$runfile"; then
    echo "(note: --nat.extip is present in the run command)"
fi

echo "==> Pulling image: $IMAGE ..."
docker pull "$IMAGE"

echo "==> Removing execution container..."
docker rm -f execution

echo "==> Wiping datadir via root container..."
docker run --rm --user 0 --entrypoint sh -v /data/ethrex:/d "$IMAGE" \
    -c 'rm -rf /d/* /d/.[!.]*'

echo "==> Recreating execution container (detached)..."
recreate=$(sed 's#^docker run #docker run -d #' "$runfile")
if ! echo "$recreate" | grep -q '^docker run -d '; then
    echo "ABORT: recreate command does not start with 'docker run -d '" >&2
    echo "==> Unpausing watchtower due to abort..."
    docker unpause ethereum-node-docker-watchtower || true
    exit 1
fi
bash -c "$recreate"

echo "==> Restarting snooper-engine (REQUIRED: reconnects engine proxy to new EL)..."
docker restart snooper-engine || echo "(no snooper-engine; restart beacon manually if needed)"

echo "==> Unpausing watchtower..."
docker unpause ethereum-node-docker-watchtower || true

echo "==> Waiting 8s for EL to start..."
sleep 8

echo "==> Post-wipe status:"
curl -s --max-time 4 localhost:8545 -H 'Content-Type: application/json' \
    -d '{"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}' || true
echo ""
docker logs --since 12s execution 2>&1 | \
    grep -iE "Storing genesis|Unknown state|SYNCING|Sync target" | tail -2 || true
"""

# ---------------------------------------------------------------------------
# LOGS_TAIL
# ---------------------------------------------------------------------------
# Args: $1 = since value (e.g. 2m, 30s); pre-validated by Python before sending.
LOGS_TAIL = r"""
set -uo pipefail
SINCE="$1"
docker logs --since "$SINCE" execution 2>&1 | grep -iE 'WARN|ERROR' | tail -30
"""

# ---------------------------------------------------------------------------
# CL_TAIL
# ---------------------------------------------------------------------------
# Args: $1 = since value (e.g. 3m); pre-validated by Python before sending.
CL_TAIL = r"""
set -uo pipefail
SINCE="$1"
docker logs --since "$SINCE" beacon 2>&1 | \
    grep -iE 'Synced|Syncing|Slot start|head|exec-block|descendant|finaliz' | tail -15
"""
