import json
import logging
import os
from dataclasses import dataclass, field, asdict

log = logging.getLogger(__name__)


@dataclass
class State:
    reported_missed_slots: set[int] = field(default_factory=set)
    reported_orphan_slots: set[int] = field(default_factory=set)
    offline_clients: set[str] = field(default_factory=set)
    lagging_clients: set[str] = field(default_factory=set)
    forked_clients: set[str] = field(default_factory=set)
    # Per-client consecutive non-canonical observations. Used to require
    # N ticks of confirmation before firing a fork alert.
    pending_fork_ticks: dict[str, int] = field(default_factory=dict)
    last_known_head: int = 0
    last_heartbeat_ts: float = 0.0
    client_versions: dict[str, str] = field(default_factory=dict)
    # Sliding window of recent missed-block timestamps + slot numbers per
    # proposer. Used to decide when a client's miss rate crosses the
    # storm-mode threshold.
    missed_recent: dict[str, list[list[float]]] = field(default_factory=dict)
    # Active missed-block burst per proposer:
    #   {"started_ts", "last_update_ts", "first_slot", "last_slot", "total_misses"}.
    burst_state: dict[str, dict] = field(default_factory=dict)

    def to_json(self) -> dict:
        d = asdict(self)
        for k in (
            "reported_missed_slots",
            "reported_orphan_slots",
            "offline_clients",
            "lagging_clients",
            "forked_clients",
        ):
            d[k] = sorted(d[k])
        return d

    @classmethod
    def from_json(cls, d: dict) -> "State":
        return cls(
            reported_missed_slots=set(d.get("reported_missed_slots", [])),
            reported_orphan_slots=set(d.get("reported_orphan_slots", [])),
            offline_clients=set(d.get("offline_clients", [])),
            lagging_clients=set(d.get("lagging_clients", [])),
            forked_clients=set(d.get("forked_clients", [])),
            last_known_head=int(d.get("last_known_head", 0)),
            last_heartbeat_ts=float(d.get("last_heartbeat_ts", 0.0)),
            client_versions=dict(d.get("client_versions", {})),
            pending_fork_ticks={k: int(v) for k, v in (d.get("pending_fork_ticks") or {}).items()},
            missed_recent={
                k: [[float(t), int(s)] for t, s in v]
                for k, v in (d.get("missed_recent") or {}).items()
            },
            burst_state={k: dict(v) for k, v in (d.get("burst_state") or {}).items()},
        )


def load_state(path: str | None) -> State:
    if not path or not os.path.exists(path):
        return State()
    try:
        with open(path, "r") as f:
            return State.from_json(json.load(f))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("could not read state file %s: %s; starting fresh", path, e)
        return State()


def save_state(path: str | None, state: State) -> None:
    if not path:
        return
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(state.to_json(), f, indent=2)
        os.replace(tmp, path)
    except OSError as e:
        log.warning("could not write state file %s: %s", path, e)
