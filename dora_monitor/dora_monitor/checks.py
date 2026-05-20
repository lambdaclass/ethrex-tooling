import logging
import time
from collections import Counter

from dora_monitor.config import Config
from dora_monitor.dora import DoraClient
from dora_monitor.slack import SlackNotifier
from dora_monitor.state import State

log = logging.getLogger(__name__)


def _matches(name: str | None, needle: str) -> bool:
    if not name:
        return False
    return needle.lower() in name.lower()


def check_missed_blocks(
    dora: DoraClient,
    slack: SlackNotifier,
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
            slack.send(
                f":warning: *Missed block* — slot `{slot_num}` "
                f"(epoch {s.get('epoch')}) proposer `{proposer_name}` (idx {s.get('proposer')})"
            )
        elif status == "orphaned" and slot_num not in state.reported_orphan_slots:
            state.reported_orphan_slots.add(slot_num)
            slack.send(
                f":warning: *Orphaned block* — slot `{slot_num}` "
                f"(epoch {s.get('epoch')}) proposer `{proposer_name}` (idx {s.get('proposer')})"
            )


def check_client_head_forks(
    dora: DoraClient,
    slack: SlackNotifier,
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
                current_forked.add(name)

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
            slack.send(
                f":red_circle: *Client offline* — `{name}` status=`{info.get('status') or 'missing'}`{extra}"
            )
        for name in sorted(recovered_offline):
            slack.send(f":large_green_circle: *Client back online* — `{name}` is online again")
        state.offline_clients = current_offline

    if cfg.checks.forks:
        new_forked = current_forked - state.forked_clients
        resolved_forked = state.forked_clients - current_forked
        for name in sorted(new_forked):
            info = matched_clients.get(name, {})
            slack.send(
                f":fork_and_knife: *Fork detected* — `{name}` head "
                f"`{info.get('head_root', '?')[:14]}…` at slot `{info.get('head_slot')}` "
                f"is not on canonical (`{canonical_root[:14]}…` at `{canonical_slot}`)"
            )
        for name in sorted(resolved_forked):
            slack.send(f":white_check_mark: *Fork resolved* — `{name}` is back on canonical head")
        state.forked_clients = current_forked

    if cfg.checks.sync_lag:
        new_lagging = current_lagging - state.lagging_clients
        recovered_lagging = state.lagging_clients - current_lagging
        for name in sorted(new_lagging):
            info = matched_clients.get(name, {})
            distance = canonical_slot - int(info.get("head_slot") or 0)
            slack.send(
                f":turtle: *Sync lag* — `{name}` is `{distance}` slots behind "
                f"(client head `{info.get('head_slot')}`, canonical `{canonical_slot}`)"
            )
        for name in sorted(recovered_lagging):
            slack.send(f":zap: *Sync caught up* — `{name}` is back in range of head")
        state.lagging_clients = current_lagging


def check_version_drift(
    dora: DoraClient,
    slack: SlackNotifier,
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
            slack.send(
                f":package: *Version change* — `{name}`\n"
                f"  was: `{prev}`\n"
                f"  now: `{version}`"
            )
            state.client_versions[name] = version


_STATUS_EMOJI = {
    "online": ":large_green_circle:",
    "synchronizing": ":large_yellow_circle:",
    "optimistic": ":large_orange_circle:",
    "offline": ":red_circle:",
}


def _status_emoji(status: str) -> str:
    return _STATUS_EMOJI.get(status, ":white_circle:")


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


def _client_line(entry: dict) -> str:
    """One-line mrkdwn rendering of a single client outlier row."""
    parts = [
        _status_emoji(entry["status"]),
        f"`{entry['name']}`",
        f"head `{entry['head_slot']}`",
    ]
    if entry["distance"] > 0:
        parts.append(f"·  *{entry['distance']} behind*")
    if not entry["is_canonical_fork"]:
        parts.append("·  :fork_and_knife: non-canonical")
    return "  ".join(parts)


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _build_heartbeat(
    dora: DoraClient,
    cfg: Config,
) -> tuple[list[dict], str]:
    """Build a Block Kit heartbeat plus a plain-text fallback.

    Makes two separate HTTP requests (client_head_forks + slots), so the
    head slot and the missed/orphaned counts are sampled at slightly
    different instants. They may disagree by a slot or two; that's by
    design, not a bug.
    """
    payload = dora.client_head_forks()
    forks = payload.get("forks") or []
    canonical_slot = 0
    canonical_root = ""
    if forks:
        canonical = max(
            forks,
            key=lambda f: (len(f.get("clients") or []), int(f.get("head_slot", 0))),
        )
        canonical_slot = int(canonical.get("head_slot", 0))
        canonical_root = canonical.get("head_root", "")

    matched: list[dict] = []
    others: list[dict] = []
    status_counts: Counter[str] = Counter()
    for fork in forks:
        for client in fork.get("clients") or []:
            name = client.get("name") or ""
            status = (client.get("status") or "unknown").lower()
            entry = {
                "name": name,
                "status": status,
                "head_slot": int(client.get("head_slot") or 0),
                "distance": canonical_slot - int(client.get("head_slot") or 0),
                "is_canonical_fork": fork.get("head_root", "") == canonical_root,
            }
            status_counts[status] += 1
            if _matches(name, cfg.client_match):
                matched.append(entry)
            else:
                others.append(entry)

    # Missed / orphan counts within the recent slot window for client_match.
    window = max(cfg.heartbeat_slot_window, 1)
    slots = dora.slots(limit=window, with_orphaned=1, with_missing=1)
    missed = 0
    orphaned = 0
    total_matched_proposals = 0
    for s in slots:
        if not _matches(s.get("proposer_name") or "", cfg.client_match):
            continue
        total_matched_proposals += 1
        st = (s.get("status") or "").lower()
        if st == "missing":
            missed += 1
        elif st == "orphaned":
            orphaned += 1

    label = f" — {cfg.network_label}" if cfg.network_label else ""
    blocks: list[dict] = []
    blocks.append({
        "type": "header",
        "text": {"type": "plain_text", "text": f"\U0001F4CA Heartbeat{label}"},
    })

    # Network summary section.
    status_mix = "  ".join(
        f"{_status_emoji(k)} {v}" for k, v in sorted(status_counts.items())
    ) or "no clients"
    root_short = f"`{canonical_root[:14]}…`" if canonical_root else "`?`"
    summary_text = (
        f"Canonical head: slot `{canonical_slot}`  ·  root {root_short}\n"
        f"Active forks: *{len(forks)}*  ·  Status mix: {status_mix}"
    )
    blocks.append(_section(summary_text))
    blocks.append({"type": "divider"})

    # Matched (client_match) section.
    if matched:
        matched_sorted = sorted(matched, key=lambda x: (_health_rank(x), x["name"]))
        matched_lines = [
            f":rocket: *{cfg.client_match}* ({len(matched_sorted)} matched)"
        ]
        # Collapse the healthy bucket if every matched client is healthy.
        healthy = [e for e in matched_sorted if _health_rank(e) == 4]
        outliers = [e for e in matched_sorted if _health_rank(e) != 4]
        for e in outliers:
            matched_lines.append(_client_line(e))
        if healthy:
            if len(healthy) == len(matched_sorted):
                names = ", ".join(f"`{e['name']}`" for e in healthy)
                matched_lines.append(
                    f"{_status_emoji('online')} *all online @ canonical* "
                    f"({len(healthy)}): {names}"
                )
            else:
                for e in healthy:
                    matched_lines.append(_client_line(e))
        matched_lines.append("")
        matched_lines.append(
            f"Proposals in last {window} slots: *{total_matched_proposals}*  "
            f"(missed *{missed}*, orphaned *{orphaned}*)"
        )
        blocks.append(_section("\n".join(matched_lines)))
    else:
        blocks.append(_section(f":mag: No clients matching `{cfg.client_match}` found."))

    # Other clients section (collapsed healthy bucket + per-client outliers).
    mode = (cfg.heartbeat_other_clients or "summary").lower()
    if others and mode != "off":
        blocks.append({"type": "divider"})
        others_sorted = sorted(others, key=lambda x: (_health_rank(x), x["name"]))
        healthy = [e for e in others_sorted if _health_rank(e) == 4]
        outliers = [e for e in others_sorted if _health_rank(e) != 4]

        lines = [f":desktop_computer: *Other clients* ({len(others_sorted)})"]
        for e in outliers:
            lines.append(_client_line(e))
        if healthy:
            if mode == "detailed":
                names = ", ".join(f"`{e['name']}`" for e in healthy)
                lines.append(
                    f"{_status_emoji('online')} *online @ canonical* "
                    f"({len(healthy)}): {names}"
                )
            else:  # summary
                lines.append(
                    f"{_status_emoji('online')} *online @ canonical*: "
                    f"{len(healthy)} client(s)"
                )
        blocks.append(_section("\n".join(lines)))

    # Footer context block (small grey).
    footer = (
        f"_Polling `{cfg.dora_url}` every {cfg.poll_interval}s · "
        f"matching `{cfg.client_match}`_"
    )
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": footer}]})

    # Plain-text fallback for notifications / non-Block-Kit clients.
    fb_status_mix = ", ".join(f"{k}:{v}" for k, v in sorted(status_counts.items())) or "no clients"
    fb_lines = [
        f"Heartbeat — canonical head {canonical_slot} ({len(forks)} fork(s), {fb_status_mix})",
    ]
    if matched:
        unhealthy_matched = sum(1 for e in matched if _health_rank(e) != 4)
        if unhealthy_matched == 0:
            fb_lines.append(
                f"{cfg.client_match}: {len(matched)} client(s) all healthy @ {canonical_slot}; "
                f"{total_matched_proposals} proposals (missed {missed}, orphan {orphaned})"
            )
        else:
            fb_lines.append(
                f"{cfg.client_match}: {unhealthy_matched}/{len(matched)} unhealthy; "
                f"{total_matched_proposals} proposals (missed {missed}, orphan {orphaned})"
            )
    if others:
        unhealthy_others = sum(1 for e in others if _health_rank(e) != 4)
        fb_lines.append(
            f"others: {len(others) - unhealthy_others}/{len(others)} healthy"
        )
    fallback = "\n".join(fb_lines)

    return blocks, fallback


def maybe_heartbeat(
    dora: DoraClient,
    slack: SlackNotifier,
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
        blocks, fallback = _build_heartbeat(dora, cfg)
    except Exception as e:
        log.exception("heartbeat compose failed: %s", e)
        return
    slack.send_blocks(blocks, fallback)
    state.last_heartbeat_ts = now


def run_checks(
    dora: DoraClient,
    slack: SlackNotifier,
    cfg: Config,
    state: State,
) -> None:
    if cfg.checks.missed_blocks:
        try:
            check_missed_blocks(dora, slack, cfg, state)
        except Exception as e:
            log.exception("missed_blocks check failed: %s", e)
    if cfg.checks.forks or cfg.checks.sync_lag or cfg.checks.offline:
        try:
            check_client_head_forks(dora, slack, cfg, state)
        except Exception as e:
            log.exception("client_head_forks check failed: %s", e)
    if cfg.checks.version_drift:
        try:
            check_version_drift(dora, slack, cfg, state)
        except Exception as e:
            log.exception("version_drift check failed: %s", e)

    try:
        maybe_heartbeat(dora, slack, cfg, state)
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
