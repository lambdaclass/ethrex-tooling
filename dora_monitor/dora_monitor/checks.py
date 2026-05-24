import logging
import time
from collections import Counter
from dataclasses import dataclass, field

from dora_monitor.config import Config
from dora_monitor.dora import DoraClient
from dora_monitor.notify import Notifier
from dora_monitor.state import State

log = logging.getLogger(__name__)


def _matches(name: str | None, needle: str) -> bool:
    if not name:
        return False
    return needle.lower() in name.lower()


def check_missed_blocks(
    dora: DoraClient,
    notifier: Notifier,
    cfg: Config,
    state: State,
) -> None:
    slots = dora.slots(limit=cfg.slot_scan_limit, with_orphaned=1, with_missing=1)
    for s in slots:
        proposer_name = s.get("proposer_name") or ""
        if not _matches(proposer_name, cfg.client_match):
            continue
        slot_num = int(s.get("slot", 0))
        status = (s.get("status") or "").lower()
        if status == "missing" and slot_num not in state.reported_missed_slots:
            state.reported_missed_slots.add(slot_num)
            notifier.send(
                f":warning: *Missed block* — slot `{slot_num}` "
                f"(epoch {s.get('epoch')}) proposer `{proposer_name}` (idx {s.get('proposer')})"
            )
        elif status == "orphaned" and slot_num not in state.reported_orphan_slots:
            state.reported_orphan_slots.add(slot_num)
            notifier.send(
                f":warning: *Orphaned block* — slot `{slot_num}` "
                f"(epoch {s.get('epoch')}) proposer `{proposer_name}` (idx {s.get('proposer')})"
            )


def check_client_head_forks(
    dora: DoraClient,
    notifier: Notifier,
    cfg: Config,
    state: State,
) -> None:
    """Drives the forks, sync_lag, and offline checks.

    All three derive from /v1/network/client_head_forks, where each matched
    entry is a CL/beacon client paired with the EL named in the suffix
    (e.g. lighthouse-ethrex-1 = lighthouse beacon paired with ethrex EL).
    Dora's per-client `status` here is the real online/synchronizing/optimistic
    /offline value, NOT the misleading "connected/disconnected" from
    /v1/clients/execution (which only reflects Dora's devp2p crawler).
    """
    payload = dora.client_head_forks()
    forks = payload.get("forks") or []
    if not forks:
        return

    # Canonical = fork followed by the most clients. Using head_slot would
    # mis-identify a minority fork that's briefly ahead during a split.
    # Tiebreak on highest head_slot just to be deterministic.
    canonical_fork = max(
        forks,
        key=lambda f: (len(f.get("clients") or []), int(f.get("head_slot", 0))),
    )
    canonical_slot = int(canonical_fork.get("head_slot", 0))
    canonical_root = canonical_fork.get("head_root", "")
    state.last_known_head = max(state.last_known_head, canonical_slot)

    current_forked: set[str] = set()
    forked_candidates: set[str] = set()
    current_lagging: set[str] = set()
    current_offline: set[str] = set()
    matched_clients: dict[str, dict] = {}

    for fork in forks:
        head_slot = int(fork.get("head_slot", 0))
        head_root = fork.get("head_root", "")
        is_canonical = head_root == canonical_root
        for client in fork.get("clients") or []:
            name = client.get("name") or ""
            if not _matches(name, cfg.client_match):
                continue
            status = (client.get("status") or "").lower()
            client_head = int(client.get("head_slot") or head_slot)
            matched_clients[name] = {
                "head_slot": client_head,
                "head_root": head_root,
                "distance": client.get("distance", 0),
                "status": status,
                "last_error": client.get("last_error"),
                "is_canonical_fork": is_canonical,
            }

            # Only `offline` is an actionable alert. `synchronizing` and
            # `optimistic` are normal transient states (esp. at startup); we
            # don't want to page on them. Use sync_lag for stuck-syncing nodes.
            if cfg.checks.offline and status == "offline":
                current_offline.add(name)

            # Skip fork/lag judgement when the client isn't fully online;
            # head_slot is stale and would produce noisy alerts.
            if status != "online":
                continue

            if cfg.checks.forks and not is_canonical:
                forked_candidates.add(name)

            if cfg.checks.sync_lag:
                distance = canonical_slot - client_head
                if distance >= cfg.sync_lag_threshold:
                    current_lagging.add(name)

    if cfg.checks.offline:
        new_offline = current_offline - state.offline_clients
        recovered_offline = state.offline_clients - current_offline
        for name in sorted(new_offline):
            info = matched_clients.get(name, {})
            extra = f" (last_error: {info['last_error']})" if info.get("last_error") else ""
            notifier.send(
                f":red_circle: *Client offline* — `{name}` status=`{info.get('status') or 'missing'}`{extra}"
            )
        for name in sorted(recovered_offline):
            notifier.send(f":large_green_circle: *Client back online* — `{name}` is online again")
        state.offline_clients = current_offline

    if cfg.checks.forks:
        # Bump the consecutive-tick counter for clients seen on a
        # non-canonical fork this tick; reset for everyone else. Only treat
        # a client as truly forked once the counter crosses fork_confirm_ticks
        # to filter propagation-timing noise (1-2 slot jitter that resolves
        # within a poll or two).
        threshold = max(cfg.fork_confirm_ticks, 1)
        seen_matched = set(matched_clients.keys())
        for name in list(state.pending_fork_ticks.keys()):
            if name not in seen_matched:
                # Client vanished from the payload (lost from network view);
                # drop the pending counter so it doesn't persist forever.
                del state.pending_fork_ticks[name]
        for name in forked_candidates:
            state.pending_fork_ticks[name] = state.pending_fork_ticks.get(name, 0) + 1
        for name in seen_matched - forked_candidates:
            state.pending_fork_ticks.pop(name, None)
        current_forked = {n for n, c in state.pending_fork_ticks.items() if c >= threshold}

        new_forked = current_forked - state.forked_clients
        resolved_forked = state.forked_clients - current_forked
        for name in sorted(new_forked):
            info = matched_clients.get(name, {})
            notifier.send(
                f":fork_and_knife: *Fork detected* — `{name}` head "
                f"`{info.get('head_root', '?')[:14]}…` at slot `{info.get('head_slot')}` "
                f"is not on canonical (`{canonical_root[:14]}…` at `{canonical_slot}`)"
            )
        for name in sorted(resolved_forked):
            notifier.send(f":white_check_mark: *Fork resolved* — `{name}` is back on canonical head")
        state.forked_clients = current_forked

    if cfg.checks.sync_lag:
        new_lagging = current_lagging - state.lagging_clients
        recovered_lagging = state.lagging_clients - current_lagging
        for name in sorted(new_lagging):
            info = matched_clients.get(name, {})
            distance = canonical_slot - int(info.get("head_slot") or 0)
            notifier.send(
                f":turtle: *Sync lag* — `{name}` is `{distance}` slots behind "
                f"(client head `{info.get('head_slot')}`, canonical `{canonical_slot}`)"
            )
        for name in sorted(recovered_lagging):
            notifier.send(f":zap: *Sync caught up* — `{name}` is back in range of head")
        state.lagging_clients = current_lagging


def check_version_drift(
    dora: DoraClient,
    notifier: Notifier,
    cfg: Config,
    state: State,
) -> None:
    """Alert when an ethrex EL's version string changes.

    Reads versions from the /clients/execution HTML page (the v1 JSON API
    does not return the EL version for ethrex). First-time observations are
    recorded silently; subsequent changes post a Slack alert.
    """
    versions = dora.execution_versions()
    for name, version in versions.items():
        if not _matches(name, cfg.client_match):
            continue
        prev = state.client_versions.get(name)
        if prev is None:
            state.client_versions[name] = version
            continue
        if prev != version:
            notifier.send(
                f":package: *Version change* — `{name}`\n"
                f"  was: `{prev}`\n"
                f"  now: `{version}`"
            )
            state.client_versions[name] = version


_STATUS_EMOJI_SLACK = {
    "online": ":large_green_circle:",
    "synchronizing": ":large_yellow_circle:",
    "optimistic": ":large_orange_circle:",
    "offline": ":red_circle:",
}

_STATUS_EMOJI_UNICODE = {
    "online": "\U0001f7e2",
    "synchronizing": "\U0001f7e1",
    "optimistic": "\U0001f7e0",
    "offline": "\U0001f534",
}


def _status_emoji_slack(status: str) -> str:
    return _STATUS_EMOJI_SLACK.get(status, ":white_circle:")


def _status_emoji_unicode(status: str) -> str:
    return _STATUS_EMOJI_UNICODE.get(status, "⚪")


def _health_rank(entry: dict) -> int:
    """Lower rank = surface higher. Sort key for ordering clients."""
    status = entry["status"]
    if status == "offline":
        return 0
    if status in ("synchronizing", "optimistic"):
        return 1
    if not entry["is_canonical_fork"]:
        return 2
    if entry["distance"] > 0:
        return 3
    return 4


def _client_line_slack(entry: dict) -> str:
    parts = [
        _status_emoji_slack(entry["status"]),
        f"`{entry['name']}`",
        f"head `{entry['head_slot']}`",
    ]
    if entry["distance"] > 0:
        parts.append(f"·  *{entry['distance']} behind*")
    if not entry["is_canonical_fork"]:
        parts.append("·  :fork_and_knife: non-canonical")
    return "  ".join(parts)


def _client_line_discord(entry: dict) -> str:
    parts = [
        _status_emoji_unicode(entry["status"]),
        f"`{entry['name']}`",
        f"head `{entry['head_slot']}`",
    ]
    if entry["distance"] > 0:
        parts.append(f"·  **{entry['distance']} behind**")
    if not entry["is_canonical_fork"]:
        parts.append("·  \U0001f374 non-canonical")
    return "  ".join(parts)


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


@dataclass
class HeartbeatData:
    forks: list = field(default_factory=list)
    canonical_slot: int = 0
    canonical_root: str = ""
    matched: list[dict] = field(default_factory=list)
    others: list[dict] = field(default_factory=list)
    status_counts: Counter = field(default_factory=Counter)
    window: int = 0
    missed: int = 0
    orphaned: int = 0
    total_matched_proposals: int = 0


def _gather_heartbeat(dora: DoraClient, cfg: Config) -> HeartbeatData:
    """Fetch the data behind the periodic heartbeat digest.

    Two separate HTTP requests (client_head_forks + slots), so head slot and
    missed/orphan counts are sampled at slightly different instants. They may
    disagree by a slot or two; that's by design.
    """
    payload = dora.client_head_forks()
    forks = payload.get("forks") or []
    data = HeartbeatData(forks=forks)
    if forks:
        canonical = max(
            forks,
            key=lambda f: (len(f.get("clients") or []), int(f.get("head_slot", 0))),
        )
        data.canonical_slot = int(canonical.get("head_slot", 0))
        data.canonical_root = canonical.get("head_root", "")

    for fork in forks:
        for client in fork.get("clients") or []:
            name = client.get("name") or ""
            status = (client.get("status") or "unknown").lower()
            entry = {
                "name": name,
                "status": status,
                "head_slot": int(client.get("head_slot") or 0),
                "distance": data.canonical_slot - int(client.get("head_slot") or 0),
                "is_canonical_fork": fork.get("head_root", "") == data.canonical_root,
            }
            data.status_counts[status] += 1
            if _matches(name, cfg.client_match):
                data.matched.append(entry)
            else:
                data.others.append(entry)

    data.window = max(cfg.heartbeat_slot_window, 1)
    slots = dora.slots(limit=data.window, with_orphaned=1, with_missing=1)
    for s in slots:
        if not _matches(s.get("proposer_name") or "", cfg.client_match):
            continue
        data.total_matched_proposals += 1
        st = (s.get("status") or "").lower()
        if st == "missing":
            data.missed += 1
        elif st == "orphaned":
            data.orphaned += 1
    return data


def _build_fallback(data: HeartbeatData, cfg: Config) -> str:
    fb_status_mix = ", ".join(f"{k}:{v}" for k, v in sorted(data.status_counts.items())) or "no clients"
    lines = [
        f"Heartbeat — canonical head {data.canonical_slot} ({len(data.forks)} fork(s), {fb_status_mix})",
    ]
    if data.matched:
        unhealthy = sum(1 for e in data.matched if _health_rank(e) != 4)
        if unhealthy == 0:
            lines.append(
                f"{cfg.client_match}: {len(data.matched)} client(s) all healthy @ {data.canonical_slot}; "
                f"{data.total_matched_proposals} proposals (missed {data.missed}, orphan {data.orphaned})"
            )
        else:
            lines.append(
                f"{cfg.client_match}: {unhealthy}/{len(data.matched)} unhealthy; "
                f"{data.total_matched_proposals} proposals (missed {data.missed}, orphan {data.orphaned})"
            )
    if data.others:
        unhealthy_others = sum(1 for e in data.others if _health_rank(e) != 4)
        lines.append(
            f"others: {len(data.others) - unhealthy_others}/{len(data.others)} healthy"
        )
    return "\n".join(lines)


def _build_slack_heartbeat(data: HeartbeatData, cfg: Config) -> list[dict]:
    label = f" — {cfg.network_label}" if cfg.network_label else ""
    blocks: list[dict] = []
    blocks.append({
        "type": "header",
        "text": {"type": "plain_text", "text": f"\U0001F4CA Heartbeat{label}"},
    })

    status_mix = "  ".join(
        f"{_status_emoji_slack(k)} {v}" for k, v in sorted(data.status_counts.items())
    ) or "no clients"
    root_short = f"`{data.canonical_root[:14]}…`" if data.canonical_root else "`?`"
    summary_text = (
        f"Canonical head: slot `{data.canonical_slot}`  ·  root {root_short}\n"
        f"Active forks: *{len(data.forks)}*  ·  Status mix: {status_mix}"
    )
    blocks.append(_section(summary_text))
    blocks.append({"type": "divider"})

    if data.matched:
        matched_sorted = sorted(data.matched, key=lambda x: (_health_rank(x), x["name"]))
        matched_lines = [
            f":rocket: *{cfg.client_match}* ({len(matched_sorted)} matched)"
        ]
        healthy = [e for e in matched_sorted if _health_rank(e) == 4]
        outliers = [e for e in matched_sorted if _health_rank(e) != 4]
        for e in outliers:
            matched_lines.append(_client_line_slack(e))
        if healthy:
            if len(healthy) == len(matched_sorted):
                names = ", ".join(f"`{e['name']}`" for e in healthy)
                matched_lines.append(
                    f"{_status_emoji_slack('online')} *all online @ canonical* "
                    f"({len(healthy)}): {names}"
                )
            else:
                for e in healthy:
                    matched_lines.append(_client_line_slack(e))
        matched_lines.append("")
        matched_lines.append(
            f"Proposals in last {data.window} slots: *{data.total_matched_proposals}*  "
            f"(missed *{data.missed}*, orphaned *{data.orphaned}*)"
        )
        blocks.append(_section("\n".join(matched_lines)))
    else:
        blocks.append(_section(f":mag: No clients matching `{cfg.client_match}` found."))

    mode = (cfg.heartbeat_other_clients or "summary").lower()
    if data.others and mode != "off":
        blocks.append({"type": "divider"})
        others_sorted = sorted(data.others, key=lambda x: (_health_rank(x), x["name"]))
        healthy = [e for e in others_sorted if _health_rank(e) == 4]
        outliers = [e for e in others_sorted if _health_rank(e) != 4]

        lines = [f":desktop_computer: *Other clients* ({len(others_sorted)})"]
        for e in outliers:
            lines.append(_client_line_slack(e))
        if healthy:
            if mode == "detailed":
                names = ", ".join(f"`{e['name']}`" for e in healthy)
                lines.append(
                    f"{_status_emoji_slack('online')} *online @ canonical* "
                    f"({len(healthy)}): {names}"
                )
            else:
                lines.append(
                    f"{_status_emoji_slack('online')} *online @ canonical*: "
                    f"{len(healthy)} client(s)"
                )
        blocks.append(_section("\n".join(lines)))

    footer = (
        f"_Polling `{cfg.dora_url}` every {cfg.poll_interval}s · "
        f"matching `{cfg.client_match}`_"
    )
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": footer}]})
    return blocks


def _heartbeat_color(data: HeartbeatData) -> int:
    """Embed sidebar color: red = matched-offline, yellow = matched-degraded,
    green = all matched healthy, blue = no matched clients."""
    if not data.matched:
        return 0x3498DB
    ranks = [_health_rank(e) for e in data.matched]
    if any(r == 0 for r in ranks):
        return 0xE74C3C
    if any(r < 4 for r in ranks):
        return 0xF1C40F
    return 0x2ECC71


def _field(name: str, value: str) -> dict:
    # Discord caps field value at 1024 chars; truncate rather than 400-ing.
    if len(value) > 1024:
        value = value[:1020] + "…"
    return {"name": name, "value": value, "inline": False}


def _build_discord_heartbeat(data: HeartbeatData, cfg: Config) -> dict:
    label = f" — {cfg.network_label}" if cfg.network_label else ""

    status_mix = "  ".join(
        f"{_status_emoji_unicode(k)} {v}" for k, v in sorted(data.status_counts.items())
    ) or "no clients"
    root_short = f"`{data.canonical_root[:14]}…`" if data.canonical_root else "`?`"
    description = (
        f"Canonical head: slot `{data.canonical_slot}`  ·  root {root_short}\n"
        f"Active forks: **{len(data.forks)}**  ·  Status mix: {status_mix}"
    )

    fields: list[dict] = []
    if data.matched:
        matched_sorted = sorted(data.matched, key=lambda x: (_health_rank(x), x["name"]))
        healthy = [e for e in matched_sorted if _health_rank(e) == 4]
        outliers = [e for e in matched_sorted if _health_rank(e) != 4]
        lines: list[str] = []
        for e in outliers:
            lines.append(_client_line_discord(e))
        if healthy:
            if len(healthy) == len(matched_sorted):
                names = ", ".join(f"`{e['name']}`" for e in healthy)
                lines.append(
                    f"{_status_emoji_unicode('online')} **all online @ canonical** "
                    f"({len(healthy)}): {names}"
                )
            else:
                for e in healthy:
                    lines.append(_client_line_discord(e))
        lines.append("")
        lines.append(
            f"Proposals in last {data.window} slots: **{data.total_matched_proposals}**  "
            f"(missed **{data.missed}**, orphaned **{data.orphaned}**)"
        )
        fields.append(_field(
            f"\U0001f680 {cfg.client_match} ({len(matched_sorted)} matched)",
            "\n".join(lines),
        ))
    else:
        fields.append(_field(
            "\U0001f50d Matched",
            f"No clients matching `{cfg.client_match}` found.",
        ))

    mode = (cfg.heartbeat_other_clients or "summary").lower()
    if data.others and mode != "off":
        others_sorted = sorted(data.others, key=lambda x: (_health_rank(x), x["name"]))
        healthy = [e for e in others_sorted if _health_rank(e) == 4]
        outliers = [e for e in others_sorted if _health_rank(e) != 4]
        lines = []
        for e in outliers:
            lines.append(_client_line_discord(e))
        if healthy:
            if mode == "detailed":
                names = ", ".join(f"`{e['name']}`" for e in healthy)
                lines.append(
                    f"{_status_emoji_unicode('online')} **online @ canonical** "
                    f"({len(healthy)}): {names}"
                )
            else:
                lines.append(
                    f"{_status_emoji_unicode('online')} **online @ canonical**: "
                    f"{len(healthy)} client(s)"
                )
        fields.append(_field(
            f"\U0001f5a5️ Other clients ({len(others_sorted)})",
            "\n".join(lines) or "—",
        ))

    footer_text = (
        f"Polling {cfg.dora_url} every {cfg.poll_interval}s · matching {cfg.client_match}"
    )
    return {
        "title": f"\U0001F4CA Heartbeat{label}",
        "description": description,
        "color": _heartbeat_color(data),
        "fields": fields,
        "footer": {"text": footer_text},
    }


def maybe_heartbeat(
    dora: DoraClient,
    notifier: Notifier,
    cfg: Config,
    state: State,
) -> None:
    if cfg.heartbeat_interval_minutes <= 0:
        return
    now = time.time()
    interval_s = cfg.heartbeat_interval_minutes * 60
    if state.last_heartbeat_ts > 0 and (now - state.last_heartbeat_ts) < interval_s:
        return
    try:
        data = _gather_heartbeat(dora, cfg)
        blocks = _build_slack_heartbeat(data, cfg)
        embed = _build_discord_heartbeat(data, cfg)
        fallback = _build_fallback(data, cfg)
    except Exception as e:
        log.exception("heartbeat compose failed: %s", e)
        return
    notifier.send_heartbeat(blocks, embed, fallback)
    state.last_heartbeat_ts = now


def run_checks(
    dora: DoraClient,
    notifier: Notifier,
    cfg: Config,
    state: State,
) -> None:
    if cfg.checks.missed_blocks:
        try:
            check_missed_blocks(dora, notifier, cfg, state)
        except Exception as e:
            log.exception("missed_blocks check failed: %s", e)
    if cfg.checks.forks or cfg.checks.sync_lag or cfg.checks.offline:
        try:
            check_client_head_forks(dora, notifier, cfg, state)
        except Exception as e:
            log.exception("client_head_forks check failed: %s", e)
    if cfg.checks.version_drift:
        try:
            check_version_drift(dora, notifier, cfg, state)
        except Exception as e:
            log.exception("version_drift check failed: %s", e)

    try:
        maybe_heartbeat(dora, notifier, cfg, state)
    except Exception as e:
        log.exception("heartbeat failed: %s", e)

    # Trim reported-slots sets to keep state file from growing forever.
    # Guard against last_known_head being 0 (e.g. all client_head_forks
    # checks disabled or the check threw on every tick): without the guard,
    # cutoff would go negative and the trim would silently be a no-op.
    if state.last_known_head > 10_000:
        cutoff = state.last_known_head - 10_000
        state.reported_missed_slots = {s for s in state.reported_missed_slots if s >= cutoff}
        state.reported_orphan_slots = {s for s in state.reported_orphan_slots if s >= cutoff}
