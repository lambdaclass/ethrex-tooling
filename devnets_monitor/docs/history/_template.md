# <devnet> — history & facts

Devnet-specific facts and the running log of problems (what / why / when / how recovered). Generic
access/inspection procedures live in `docs/devnet-ops.md`.

Devnet branch: `<devnet>`. Builder target: `lambdaclass/ethrex@<devnet>`.
Devnet repo: `ethpandaops/<devnets-repo>`. ethrex image: `ethpandaops/ethrex:<devnet>`.

## Node roster (from inventory, group `[ethrex:children]`)

All intended as `ethpandaops/ethrex:<devnet>`. Populate from `dv discover <devnet>` output.

- <!-- list nodes here, e.g. lighthouse-ethrex-1, prysm-ethrex-1 ... -->

Live divergences from inventory (verify before trusting a name):
- <!-- dated entries: e.g. 2026-XX-XX: node-name was running <other-client> due to manual swap -->

## Genesis / fork schedule

From `ethpandaops/<devnets-repo>`, `network-configs/<repo_path>/metadata/genesis.json`. Populate
from `dv discover <devnet>` output (also written to `config/devnets/<devnet>.yaml`).

- chainId: <value>
- Fork activations: cancun/prague/osaka @0; <!-- other forks @ ts -->
- blobSchedule (target/max): <!-- cancun 3/6, prague 6/9, ... -->
- <!-- any other noteworthy genesis/config facts -->

## Commit map

Notable commits / PRs that affected this devnet:

- <!-- sha  description (PR #N) -->

## Known issues / learnings

Numbered, dated entries. Format: symptom description, root cause, recovery steps.

1. <!-- YYYY-MM-DD: SHORT TITLE.
   Root cause: ...
   Recovery: ...
   Notes: ... -->
