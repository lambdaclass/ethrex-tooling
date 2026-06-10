"""Deploy timeline + GitHub gap collector."""

from __future__ import annotations

import logging
import subprocess
import time
from datetime import datetime, timezone
from typing import Any

from .store import connect, migrate, upsert

logger = logging.getLogger(__name__)

# Cache TTL for the GitHub gap check: 6 hours
_GH_CACHE_TTL = 6 * 3600


def _gh_api_json(path: str, jq: str | None = None) -> Any:
    """
    Run 'gh api <path>' (optionally with --jq) and return parsed JSON.
    Returns None on error.
    """
    cmd = ["gh", "api", path]
    if jq:
        cmd += ["--jq", jq]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            logger.warning("gh api %s failed: %s", path, result.stderr.strip()[:200])
            return None
        text = result.stdout.strip()
        if not text:
            return None
        import json
        if jq:
            # jq output may be a bare value (int, string) or JSON
            try:
                return json.loads(text)
            except Exception:
                return text
        return json.loads(text)
    except FileNotFoundError:
        logger.error("'gh' CLI not found; cannot query GitHub")
        return None
    except subprocess.TimeoutExpired:
        logger.warning("gh api %s timed out", path)
        return None
    except Exception as exc:
        logger.warning("gh api %s error: %s", path, exc)
        return None


def collect_deploygap(devnet: str) -> None:
    """
    For each node in node_health, compare the deployed commit against
    lambdaclass/ethrex main HEAD and store the gap into deploy_gap.

    Uses a gh_cache entry (key='ethrex_main_commit') with a 6h TTL to avoid
    hammering the GitHub API on every slow run.

    Only re-queries gh if the cache is absent or older than 6h.
    """
    conn = connect()
    migrate(conn)
    now = int(time.time())

    # Fetch latest deployed commit per node from node_health
    node_commits = conn.execute(
        """
        SELECT nh.node, nh."commit"
        FROM node_health nh
        INNER JOIN (
            SELECT node, MAX(ts) AS max_ts
            FROM node_health
            WHERE devnet = ?
            GROUP BY node
        ) latest ON nh.node = latest.node AND nh.ts = latest.max_ts
        WHERE nh.devnet = ?
        """,
        (devnet, devnet),
    ).fetchall()

    if not node_commits:
        conn.close()
        print(f"collect_deploygap({devnet}): no node_health data; run dv collect {devnet} health first")
        return

    # Check / refresh the main HEAD commit from cache
    cache_row = conn.execute(
        "SELECT value, fetched_at FROM gh_cache WHERE key='ethrex_main_commit'",
    ).fetchone()

    main_commit: str | None = None
    if cache_row and cache_row["fetched_at"] and (now - cache_row["fetched_at"]) < _GH_CACHE_TTL:
        main_commit = cache_row["value"]
        logger.info("collect_deploygap: using cached main commit %s", main_commit)
    else:
        # Fetch from GitHub
        sha = _gh_api_json("repos/lambdaclass/ethrex/commits/main", jq=".sha")
        if sha and isinstance(sha, str):
            main_commit = sha.strip()
            upsert(conn, "gh_cache", {"key": "ethrex_main_commit", "value": main_commit, "fetched_at": now})
            conn.commit()
            logger.info("collect_deploygap: fetched main commit %s", main_commit)
        else:
            logger.warning("collect_deploygap: could not fetch main commit from GitHub")

    inserted = 0
    for nc in node_commits:
        node = nc["node"]
        deployed = (nc["commit"] or "").strip()
        if not deployed:
            continue

        commits_behind: int | None = None
        if main_commit and deployed != main_commit:
            # Only query compare if not cache-TTL blocked
            # Check if we have a recent deploy_gap entry for this node
            existing = conn.execute(
                "SELECT commits_behind, checked_at FROM deploy_gap WHERE devnet=? AND node=?",
                (devnet, node),
            ).fetchone()
            if (
                existing
                and existing["checked_at"]
                and (now - existing["checked_at"]) < _GH_CACHE_TTL
                and existing["commits_behind"] is not None
            ):
                # Use cached gap value
                commits_behind = existing["commits_behind"]
            else:
                result = _gh_api_json(
                    f"repos/lambdaclass/ethrex/compare/{deployed}...main",
                    jq=".ahead_by",
                )
                if result is not None:
                    try:
                        commits_behind = int(result)
                    except (ValueError, TypeError):
                        pass
        elif main_commit and deployed == main_commit:
            commits_behind = 0

        row: dict[str, Any] = {
            "devnet": devnet,
            "node": node,
            "deployed_commit": deployed or None,
            "main_commit": main_commit,
            "commits_behind": commits_behind,
            "checked_at": now,
        }
        upsert(conn, "deploy_gap", row)
        inserted += 1

    conn.commit()
    conn.close()
    print(f"collect_deploygap({devnet}): {inserted} nodes processed at ts={now}")


def get_deploy_data(devnet: str) -> dict[str, Any] | None:
    """
    Return deploy timeline + gap data for template rendering.

    Timeline: per-node version-over-time from existing node_health rows
    (groups by node, shows commit/buildnum/image changes over ts).
    Gap: current deploy_gap table rows.
    Events: events table rows for this devnet ordered by ts, overlaid.

    Returns None if no node_health data exists.
    """
    conn = connect()
    migrate(conn)

    # Check data exists
    cnt = conn.execute(
        "SELECT COUNT(*) AS c FROM node_health WHERE devnet=?", (devnet,)
    ).fetchone()
    if not cnt or cnt["c"] == 0:
        conn.close()
        return None

    # --- Timeline: per-node rows ordered by ts ---
    # We return all node_health rows; the template groups by node and shows
    # only rows where commit/buildnum/image changed (version transitions).
    timeline_rows = conn.execute(
        """
        SELECT node, ts, image, buildnum, "commit", head, peers
        FROM node_health
        WHERE devnet=?
        ORDER BY node, ts DESC
        """,
        (devnet,),
    ).fetchall()

    # Build per-node version transitions (collapse consecutive identical
    # versions). Rows arrive newest-first (ts DESC), so each node's list stays
    # newest-first by appending and comparing against the last appended entry.
    from datetime import datetime, timezone

    def _ts_str(ts: int | None) -> str:
        if ts is None:
            return "-"
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        except Exception:
            return str(ts)

    nodes: dict[str, list[dict]] = {}
    for r in timeline_rows:
        node = r["node"]
        entry = {
            "ts": r["ts"],
            "ts_str": _ts_str(r["ts"]),
            "image": r["image"],
            "buildnum": r["buildnum"],
            "commit": (r["commit"] or "")[:12],
            "commit_full": r["commit"] or "",
            "head": r["head"],
            "peers": r["peers"],
        }
        if node not in nodes:
            nodes[node] = [entry]
        else:
            last = nodes[node][-1]  # oldest-so-far == previous (older) version
            if (
                entry["commit_full"] != last["commit_full"]
                or entry["buildnum"] != last["buildnum"]
                or entry["image"] != last["image"]
            ):
                nodes[node].append(entry)

    # --- Deploy gap ---
    gap_rows = conn.execute(
        """
        SELECT node, deployed_commit, main_commit, commits_behind, checked_at
        FROM deploy_gap
        WHERE devnet=?
        ORDER BY node
        """,
        (devnet,),
    ).fetchall()

    gap_data = []
    for r in gap_rows:
        checked_str = ""
        if r["checked_at"]:
            try:
                checked_str = datetime.fromtimestamp(r["checked_at"], tz=timezone.utc).strftime(
                    "%Y-%m-%d %H:%M UTC"
                )
            except Exception:
                checked_str = str(r["checked_at"])
        gap_data.append({
            "node": r["node"],
            "deployed_commit": (r["deployed_commit"] or "")[:12],
            "deployed_commit_full": r["deployed_commit"] or "",
            "main_commit": (r["main_commit"] or "")[:12],
            "commits_behind": r["commits_behind"],
            "checked_at": checked_str,
        })

    # --- Events overlay (last 50 events for this devnet, ordered by last_seen) ---
    event_rows = conn.execute(
        """
        SELECT kind, severity, node, message, first_seen, last_seen
        FROM events
        WHERE devnet=?
        ORDER BY last_seen DESC
        LIMIT 50
        """,
        (devnet,),
    ).fetchall()

    events = []
    for e in event_rows:
        ts_str = ""
        if e["last_seen"]:
            try:
                ts_str = datetime.fromtimestamp(e["last_seen"], tz=timezone.utc).strftime(
                    "%Y-%m-%d %H:%M UTC"
                )
            except Exception:
                ts_str = str(e["last_seen"])
        events.append({
            "kind": e["kind"],
            "severity": e["severity"],
            "node": e["node"] or "",
            "message": e["message"],
            "ts_str": ts_str,
        })

    conn.close()

    return {
        "nodes": nodes,
        "gap": gap_data,
        "events": events,
        "gh_repo": "https://github.com/lambdaclass/ethrex",
    }


def show_deploy(devnet: str) -> None:
    """Print deploy timeline and GitHub gap summary to stdout."""
    data = get_deploy_data(devnet)
    if data is None:
        print(f"deploy({devnet}): no data. Run: dv collect {devnet} health")
        return

    print(f"\nDeploy timeline for {devnet}\n")

    for node, versions in sorted(data["nodes"].items()):
        print(f"  {node}:")
        for v in versions[:5]:
            ts_str = ""
            if v["ts"]:
                try:
                    ts_str = datetime.fromtimestamp(v["ts"], tz=timezone.utc).strftime(
                        "%Y-%m-%d %H:%M"
                    )
                except Exception:
                    ts_str = str(v["ts"])
            print(
                f"    [{ts_str}] commit={v['commit']} build={v.get('buildnum') or '?'}"
            )

    if data["gap"]:
        print("\nGitHub gap (deployed vs main):\n")
        print(f"  {'NODE':<36} {'DEPLOYED':>12} {'BEHIND':>8} {'CHECKED'}")
        print("  " + "-" * 72)
        for g in data["gap"]:
            behind = g["commits_behind"]
            behind_str = str(behind) if behind is not None else "?"
            print(
                f"  {g['node']:<36} {g['deployed_commit']:>12} {behind_str:>8}  {g['checked_at']}"
            )
    else:
        print("\nGitHub gap: no data. Run: dv collect slow")

    print()
