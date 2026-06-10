"""Read-only FastAPI dashboard over the SQLite store.

All routes are GET only; the SQLite connection is opened read-only.
Bound to 127.0.0.1 by default (set in cli.py / uvicorn.run call).
No authentication, no write endpoints.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import markdown
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

_HERE = Path(__file__).parent
_TEMPLATES_DIR = _HERE / "templates"

app = FastAPI(title="ethrex-devnets dashboard", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _ethrex_commit_url(commit: str | None) -> str:
    """GitHub URL for an ethrex commit (short hashes resolve). Empty if no commit."""
    c = (commit or "").strip()
    return f"https://github.com/lambdaclass/ethrex/commit/{c}" if c else ""


# Expose to all templates so any commit can render as a link to ethrex source.
templates.env.globals["ethrex_commit_url"] = _ethrex_commit_url


def _humanize(s: Any) -> str:
    """snake_case / kebab-case -> 'Sentence case' for display (e.g.
    version_change -> 'Version change', from_commit -> 'From commit')."""
    return str(s or "").replace("_", " ").replace("-", " ").strip().capitalize()


templates.env.filters["humanize"] = _humanize

# ---------------------------------------------------------------------------
# Read-only DB helpers
# ---------------------------------------------------------------------------


def _db_path() -> Path:
    """Locate the SQLite database file."""
    return _HERE.parent / "data" / "ethrex-devnets.sqlite"


def _connect_ro() -> sqlite3.Connection | None:
    """Open the SQLite DB read-only. Returns None if the file does not exist."""
    path = _db_path()
    if not path.exists():
        return None
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Config helpers (no side effects -- registry read only)
# ---------------------------------------------------------------------------


def _load_registry() -> dict[str, Any]:
    import yaml
    reg_path = _HERE.parent / "config" / "devnets.yaml"
    if not reg_path.exists():
        return {}
    with reg_path.open() as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


def _devnet_names() -> list[str]:
    reg = _load_registry()
    devnets = reg.get("devnets", {})
    return list(devnets.keys()) if isinstance(devnets, dict) else []


def _devnet_entry(name: str) -> dict[str, Any]:
    reg = _load_registry()
    devnets = reg.get("devnets", {}) or {}
    return dict(devnets.get(name, {}))


# ---------------------------------------------------------------------------
# URL derivation helpers
# ---------------------------------------------------------------------------

_SERVICES = [
    ("Dora",             "dora"),
    ("Forkmon",          "forkmon"),
    ("Assertoor",        "assertoor"),
    ("Checkpoint Sync",  "checkpoint-sync"),
    ("Tracoor",          "tracoor"),
    ("Syncoor",          "syncoor"),
    ("Spamoor",          "spamoor"),
    ("Buildoor",         "buildoor"),
    ("JSON RPC",         "rpc"),
    ("Beacon RPC",       "beacon"),
]


def _service_urls(devnet: str, entry: dict[str, Any]) -> list[dict[str, str]]:
    """
    Build the list of ethpandaops service URLs for a devnet.
    Use dora_base / config_base from the registry where available;
    derive the rest from the <service>.<devnet>.ethpandaops.io pattern.
    """
    dora_base = entry.get("dora_base", "")
    urls = []
    for label, slug in _SERVICES:
        if slug == "dora" and dora_base:
            url = dora_base.rstrip("/")
        else:
            url = f"https://{slug}.{devnet}.ethpandaops.io"
        urls.append({"label": label, "url": url})
    return urls


# ---------------------------------------------------------------------------
# Index data helper
# ---------------------------------------------------------------------------


def _latest_health(conn: sqlite3.Connection, devnet: str) -> list[dict[str, Any]]:
    """Return the most recent node_health row per node for a devnet."""
    rows = conn.execute(
        """
        SELECT nh.*
        FROM node_health nh
        INNER JOIN (
            SELECT node, MAX(ts) AS max_ts
            FROM node_health
            WHERE devnet = ?
            GROUP BY node
        ) latest ON nh.node = latest.node AND nh.ts = latest.max_ts
        WHERE nh.devnet = ?
        ORDER BY nh.node
        """,
        (devnet, devnet),
    ).fetchall()
    result = []
    for r in rows:
        syncing = r["syncing"]
        if isinstance(syncing, str):
            syncing_disp = "yes" if syncing.lower() in ("true", "1", "yes") else "no"
        else:
            syncing_disp = "yes" if syncing else "no"
        ts_disp = ""
        if r["ts"]:
            try:
                ts_disp = datetime.fromtimestamp(r["ts"], tz=timezone.utc).strftime(
                    "%Y-%m-%d %H:%M UTC"
                )
            except Exception:
                ts_disp = str(r["ts"])
        result.append({
            "node": r["node"],
            "head": r["head"],
            "peers": r["peers"],
            "state_at_head": r["state_at_head"],
            "syncing": syncing_disp,
            "buildnum": r["buildnum"],
            "commit": r["commit"],
            "ts": ts_disp,
        })
    return result


def _latest_hive_per_group(conn: sqlite3.Connection, devnet: str) -> list[dict[str, Any]]:
    """Return the most recent hive run per group_name for a devnet."""
    rows = conn.execute(
        """
        SELECT group_name, passes, fails, ntests, started_at, web_url
        FROM hive_runs
        WHERE devnet = ?
        ORDER BY group_name, started_at DESC
        """,
        (devnet,),
    ).fetchall()
    seen: set[str] = set()
    result = []
    for r in rows:
        g = r["group_name"]
        if g in seen:
            continue
        seen.add(g)
        started = ""
        if r["started_at"]:
            try:
                started = datetime.fromtimestamp(r["started_at"], tz=timezone.utc).strftime(
                    "%Y-%m-%d %H:%M UTC"
                )
            except Exception:
                started = str(r["started_at"])
        passes = r["passes"] if r["passes"] is not None else "?"
        fails = r["fails"] if r["fails"] is not None else "?"
        ntests = r["ntests"] if r["ntests"] is not None else "?"
        result.append({
            "group": g,
            "passes": passes,
            "fails": fails,
            "ntests": ntests,
            "started": started,
            "web_url": r["web_url"] or "",
        })
    return result


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    from web.aggregate import command_center_data

    devnet_names = _devnet_names()
    devnets_data: list[dict[str, Any]] = []
    conn = _connect_ro()

    for name in devnet_names:
        entry = _devnet_entry(name)
        cc: dict[str, Any] = {}
        hive: list[dict[str, Any]] = []
        if conn is not None:
            try:
                cc = command_center_data(conn, name)
            except Exception:
                cc = {"nodes": [], "finality": None, "next_fork": None, "blob_flow": None, "events": []}
            hive = _latest_hive_per_group(conn, name)
        devnets_data.append({
            "name": name,
            "cc": cc,
            "hive": hive,
            "services": _service_urls(name, entry),
        })

    if conn is not None:
        conn.close()

    return templates.TemplateResponse(
        request,
        "command_center.html",
        {"devnets": devnets_data, "devnet_names": devnet_names},
    )


@app.get("/blobs/{devnet}", response_class=HTMLResponse)
async def blobs(request: Request, devnet: str) -> HTMLResponse:
    import json as _json

    blob_data: dict[str, Any] | None = None
    error: str | None = None

    conn = _connect_ro()
    if conn is None:
        error = "No data yet. Run: dv collect"
    else:
        conn.close()
        # Use the data-returning helper from blobtrack
        from devnets.blobtrack import get_blob_data
        blob_data = get_blob_data(devnet)
        if blob_data is None:
            error = f"No slot data for {devnet}. Run: dv collect {devnet} blobs"

    # Serialize slot_series for inline JS chart
    chart_json = "{}"
    if blob_data and blob_data.get("slot_series"):
        chart_json = _json.dumps(blob_data["slot_series"])

    return templates.TemplateResponse(
        request,
        "blobs.html",
        {
            "devnet": devnet,
            "blob_data": blob_data,
            "chart_json": chart_json,
            "error": error,
        },
    )


@app.get("/forks/{devnet}", response_class=HTMLResponse)
async def forks(request: Request, devnet: str) -> HTMLResponse:
    fork_data: dict[str, Any] | None = None
    error: str | None = None

    conn = _connect_ro()
    if conn is None:
        error = "No data yet. Run: dv collect"
    else:
        conn.close()
        from devnets.forkview import get_fork_data
        fork_data = get_fork_data(devnet)
        if fork_data is None:
            error = f"No fork data for {devnet}. Run: dv collect {devnet} forks"

    return templates.TemplateResponse(
        request,
        "forks.html",
        {
            "devnet": devnet,
            "fork_data": fork_data,
            "error": error,
        },
    )


@app.get("/hive/{devnet}", response_class=HTMLResponse)
async def hive(request: Request, devnet: str) -> HTMLResponse:
    rows: list[dict[str, Any]] = []
    error: str | None = None

    conn = _connect_ro()
    if conn is None:
        error = "No data yet. Run: dv collect"
    else:
        raw = conn.execute(
            """
            SELECT group_name, fork_filter, ethrex_version,
                   passes, fails, ntests, started_at, web_url
            FROM hive_runs
            WHERE devnet = ?
            ORDER BY group_name, fork_filter, started_at DESC
            """,
            (devnet,),
        ).fetchall()
        conn.close()

        seen: set[tuple[str, str]] = set()
        for r in raw:
            key = (r["group_name"], r["fork_filter"] or "")
            if key in seen:
                continue
            seen.add(key)
            started = ""
            if r["started_at"]:
                try:
                    started = datetime.fromtimestamp(r["started_at"], tz=timezone.utc).strftime(
                        "%Y-%m-%d %H:%M UTC"
                    )
                except Exception:
                    started = str(r["started_at"])
            rows.append({
                "group": r["group_name"] or "",
                "suite": r["fork_filter"] or "",
                "version": r["ethrex_version"] or "",
                "passes": r["passes"] if r["passes"] is not None else "?",
                "fails": r["fails"] if r["fails"] is not None else "?",
                "ntests": r["ntests"] if r["ntests"] is not None else "?",
                "started": started,
                "web_url": r["web_url"] or "",
            })

        if not rows:
            error = f"No Hive runs for {devnet}. Run: dv collect {devnet} hive"

    return templates.TemplateResponse(
        request,
        "hive.html",
        {
            "devnet": devnet,
            "rows": rows,
            "error": error,
        },
    )


@app.get("/events/{devnet}", response_class=HTMLResponse)
async def events(request: Request, devnet: str) -> HTMLResponse:
    events_data: list[dict[str, Any]] = []
    error: str | None = None

    conn = _connect_ro()
    if conn is None:
        error = "No data yet. Run: dv collect"
    else:
        conn.close()
        from devnets.detect import get_events_data
        events_data = get_events_data(devnet, include_resolved=True)
        if not events_data:
            error = f"No events for {devnet}. Run: dv collect {devnet} events"

    active = [e for e in events_data if e.get("active")]
    resolved = [e for e in events_data if not e.get("active")]

    return templates.TemplateResponse(
        request,
        "events.html",
        {
            "devnet": devnet,
            "active_events": active,
            "resolved_events": resolved,
            "error": error,
            "current_devnet": devnet,
        },
    )


@app.get("/network/{devnet}", response_class=HTMLResponse)
async def network(request: Request, devnet: str) -> HTMLResponse:
    network_data: dict[str, Any] | None = None
    error: str | None = None

    conn = _connect_ro()
    if conn is None:
        error = "No data yet. Run: dv collect"
    else:
        conn.close()
        from devnets.network import get_network_data
        network_data = get_network_data(devnet)
        if network_data is None:
            error = f"No network data for {devnet}. Run: dv collect {devnet} network"

    return templates.TemplateResponse(
        request,
        "network.html",
        {
            "devnet": devnet,
            "network_data": network_data,
            "error": error,
            "current_devnet": devnet,
        },
    )


@app.get("/proposals/{devnet}", response_class=HTMLResponse)
async def proposals(request: Request, devnet: str) -> HTMLResponse:
    proposals_data: dict[str, Any] | None = None
    error: str | None = None

    conn = _connect_ro()
    if conn is None:
        error = "No data yet. Run: dv collect"
    else:
        conn.close()
        from devnets.proposals import get_proposals_data
        proposals_data = get_proposals_data(devnet, since=None)
        if proposals_data is None:
            error = f"No slot data for {devnet}. Run: dv collect {devnet} blobs"

    return templates.TemplateResponse(
        request,
        "proposals.html",
        {
            "devnet": devnet,
            "proposals_data": proposals_data,
            "error": error,
            "current_devnet": devnet,
        },
    )


@app.get("/bal/{devnet}", response_class=HTMLResponse)
async def bal(request: Request, devnet: str) -> HTMLResponse:
    bal_data = None
    error: str | None = None

    conn = _connect_ro()
    if conn is None:
        error = "No data yet. Run: dv collect"
    else:
        conn.close()
        from devnets.bal import get_bal_data
        bal_data = get_bal_data(devnet)
        if bal_data is None:
            error = f"No BAL data for {devnet}. Run: dv collect {devnet} slow"

    return templates.TemplateResponse(
        request,
        "bal.html",
        {
            "devnet": devnet,
            "bal_data": bal_data,
            "error": error,
            "current_devnet": devnet,
        },
    )


@app.get("/epbs/{devnet}", response_class=HTMLResponse)
async def epbs(request: Request, devnet: str) -> HTMLResponse:
    epbs_data = None
    error: str | None = None

    conn = _connect_ro()
    if conn is None:
        error = "No data yet. Run: dv collect"
    else:
        conn.close()
        from devnets.epbs import get_epbs_data
        epbs_data = get_epbs_data(devnet)
        if epbs_data is None:
            error = f"No ePBS data for {devnet}. Run: dv collect {devnet} slow"

    return templates.TemplateResponse(
        request,
        "epbs.html",
        {
            "devnet": devnet,
            "epbs_data": epbs_data,
            "error": error,
            "current_devnet": devnet,
        },
    )


@app.get("/eips/{devnet}", response_class=HTMLResponse)
async def eips(request: Request, devnet: str) -> HTMLResponse:
    eiptrack_data = None
    error: str | None = None

    conn = _connect_ro()
    if conn is None:
        error = "No data yet. Run: dv collect"
    else:
        conn.close()
        from devnets.eiptrack import get_eiptrack_data
        eiptrack_data = get_eiptrack_data(devnet)
        if eiptrack_data is None:
            error = (
                f"No EIP-track data for {devnet}. "
                f"Run: dv collect {devnet} forks"
            )

    return templates.TemplateResponse(
        request,
        "eiptrack.html",
        {
            "devnet": devnet,
            "eiptrack_data": eiptrack_data,
            "error": error,
            "current_devnet": devnet,
        },
    )


@app.get("/clients/{devnet}", response_class=HTMLResponse)
async def clients(request: Request, devnet: str) -> HTMLResponse:
    clients_data: dict[str, Any] | None = None
    error: str | None = None

    conn = _connect_ro()
    if conn is None:
        error = "No data yet. Run: dv collect"
    else:
        conn.close()
        from devnets.network import get_clients_data
        clients_data = get_clients_data(devnet)
        if clients_data is None:
            error = f"No client data for {devnet}. Run: dv collect {devnet} clients"

    forkmon_url = f"https://forkmon.{devnet}.ethpandaops.io"

    return templates.TemplateResponse(
        request,
        "clients.html",
        {
            "devnet": devnet,
            "clients_data": clients_data,
            "forkmon_url": forkmon_url,
            "error": error,
            "current_devnet": devnet,
        },
    )


@app.get("/spamoor/{devnet}", response_class=HTMLResponse)
async def spamoor(request: Request, devnet: str) -> HTMLResponse:
    spamoor_data: dict[str, Any] | None = None
    error: str | None = None

    conn = _connect_ro()
    if conn is None:
        error = "No data yet. Run: dv collect"
    else:
        conn.close()
        from devnets.spamoor import get_spamoor_data
        spamoor_data = get_spamoor_data(devnet)
        if spamoor_data is None:
            error = f"No spamoor data for {devnet}. Run: dv collect {devnet} spamoor"

    spamoor_url = f"https://spamoor.{devnet}.ethpandaops.io"

    return templates.TemplateResponse(
        request,
        "spamoor.html",
        {
            "devnet": devnet,
            "spamoor_data": spamoor_data,
            "spamoor_url": spamoor_url,
            "error": error,
            "current_devnet": devnet,
        },
    )


@app.get("/deploy/{devnet}", response_class=HTMLResponse)
async def deploy(request: Request, devnet: str) -> HTMLResponse:
    deploy_data: dict[str, Any] | None = None
    error: str | None = None

    conn = _connect_ro()
    if conn is None:
        error = "No data yet. Run: dv collect"
    else:
        conn.close()
        from devnets.deploytl import get_deploy_data
        deploy_data = get_deploy_data(devnet)
        if deploy_data is None:
            error = f"No deploy data for {devnet}. Run: dv collect {devnet} health"

    return templates.TemplateResponse(
        request,
        "deploy.html",
        {
            "devnet": devnet,
            "deploy_data": deploy_data,
            "error": error,
            "current_devnet": devnet,
        },
    )


@app.get("/assertoor/{devnet}", response_class=HTMLResponse)
async def assertoor(request: Request, devnet: str) -> HTMLResponse:
    assertoor_data: dict[str, Any] | None = None
    error: str | None = None

    conn = _connect_ro()
    if conn is None:
        error = "No data yet. Run: dv collect"
    else:
        conn.close()
        from devnets.assertoor import get_assertoor_data
        assertoor_data = get_assertoor_data(devnet)
        if assertoor_data is None:
            error = f"No assertoor data for {devnet}. Run: dv collect {devnet} assertoor"

    assertoor_url = f"https://assertoor.{devnet}.ethpandaops.io"

    return templates.TemplateResponse(
        request,
        "assertoor.html",
        {
            "devnet": devnet,
            "assertoor_data": assertoor_data,
            "assertoor_url": assertoor_url,
            "error": error,
            "current_devnet": devnet,
        },
    )


@app.get("/node/{devnet}/{node}", response_class=HTMLResponse)
async def node_drilldown(request: Request, devnet: str, node: str) -> HTMLResponse:
    """Per-node drill-down: health history, version history, events, proposals.
    DB-only: no SSH, no HTTP, no subprocess calls in this handler.
    """
    from datetime import datetime, timezone as _tz

    node_data: dict[str, Any] | None = None
    error: str | None = None

    conn = _connect_ro()
    if conn is None:
        error = "No data yet. Run: dv collect"
    else:
        try:
            def _ts_str(ts: int | None) -> str:
                if ts is None:
                    return "-"
                try:
                    return datetime.fromtimestamp(ts, tz=_tz.utc).strftime("%Y-%m-%d %H:%M UTC")
                except Exception:
                    return str(ts)

            # --- Latest snapshot ---
            latest_row = conn.execute(
                """SELECT * FROM node_health WHERE devnet=? AND node=?
                   ORDER BY ts DESC LIMIT 1""",
                (devnet, node),
            ).fetchone()

            # --- Health history (last 20 snapshots) ---
            history_rows = conn.execute(
                """SELECT ts, head, peers, state_at_head, syncing, buildnum, "commit"
                   FROM node_health WHERE devnet=? AND node=?
                   ORDER BY ts DESC LIMIT 20""",
                (devnet, node),
            ).fetchall()
            health_history = []
            for r in history_rows:
                syncing_raw = r["syncing"] or ""
                if isinstance(syncing_raw, str):
                    syncing_disp = "yes" if syncing_raw.lower() in ("true", "1", "yes") else "no"
                else:
                    syncing_disp = "yes" if syncing_raw else "no"
                health_history.append({
                    "ts": _ts_str(r["ts"]),
                    "head": r["head"],
                    "peers": r["peers"],
                    "state_at_head": r["state_at_head"],
                    "syncing": syncing_disp,
                    "buildnum": r["buildnum"],
                    "commit": r["commit"] or "",
                })

            # --- Version history: distinct commit/buildnum transitions ---
            all_rows = conn.execute(
                """SELECT ts, "commit", buildnum, image, head, peers
                   FROM node_health WHERE devnet=? AND node=?
                     AND "commit" IS NOT NULL AND "commit" != ''
                   ORDER BY ts DESC LIMIT 500""",
                (devnet, node),
            ).fetchall()
            version_history: list[dict[str, Any]] = []
            for r in all_rows:
                entry = {
                    "ts_str": _ts_str(r["ts"]),
                    "commit": (r["commit"] or "")[:12],
                    "commit_full": r["commit"] or "",
                    "buildnum": r["buildnum"],
                    "image": r["image"],
                    "head": r["head"],
                    "peers": r["peers"],
                }
                if not version_history:
                    version_history.append(entry)
                else:
                    last = version_history[-1]
                    if (
                        entry["commit_full"] != last["commit_full"]
                        or entry["buildnum"] != last["buildnum"]
                        or entry["image"] != last["image"]
                    ):
                        version_history.append(entry)

            # --- This node's events (active + recently resolved) ---
            _sev_order = {"crit": 0, "warn": 1, "info": 2}
            ev_rows = conn.execute(
                """SELECT kind, severity, node, message, details,
                          first_seen, last_seen, resolved_at, count
                   FROM events WHERE devnet=? AND (node=? OR node IS NULL)
                   ORDER BY last_seen DESC LIMIT 40""",
                (devnet, node),
            ).fetchall()
            import json as _json
            node_events = []
            for e in ev_rows:
                details: dict = {}
                if e["details"]:
                    try:
                        details = _json.loads(e["details"])
                    except Exception:
                        details = {"raw": e["details"]}
                # include only if node matches or no node (network-wide events)
                node_events.append({
                    "kind": e["kind"],
                    "severity": e["severity"],
                    "node": e["node"] or "",
                    "message": e["message"],
                    "details": details,
                    "first_seen_str": _ts_str(e["first_seen"]),
                    "last_seen_str": _ts_str(e["last_seen"]),
                    "resolved_at_str": _ts_str(e["resolved_at"]),
                    "count": e["count"],
                    "active": e["resolved_at"] is None,
                })
            node_events.sort(
                key=lambda e: (0 if e["active"] else 1, _sev_order.get(e["severity"], 9))
            )

            # --- This node's proposals ---
            counts_row = conn.execute(
                """SELECT
                     SUM(CASE WHEN LOWER(status)='canonical' THEN 1 ELSE 0 END) AS canonical,
                     SUM(CASE WHEN LOWER(status) IN ('missing','missed') THEN 1 ELSE 0 END) AS missed,
                     SUM(CASE WHEN LOWER(status)='orphaned' THEN 1 ELSE 0 END) AS orphaned,
                     COUNT(*) AS total
                   FROM slots WHERE devnet=? AND proposer_name=?""",
                (devnet, node),
            ).fetchone()
            recent_slots_rows = conn.execute(
                """SELECT slot, status, blob_count, time FROM slots
                   WHERE devnet=? AND proposer_name=?
                   ORDER BY slot DESC LIMIT 20""",
                (devnet, node),
            ).fetchall()
            proposals = {
                "canonical": (counts_row["canonical"] or 0) if counts_row else 0,
                "missed": (counts_row["missed"] or 0) if counts_row else 0,
                "orphaned": (counts_row["orphaned"] or 0) if counts_row else 0,
                "total": (counts_row["total"] or 0) if counts_row else 0,
                "recent": [
                    {
                        "slot": r["slot"],
                        "status": r["status"],
                        "blob_count": r["blob_count"],
                        "time": _ts_str(r["time"]),
                    }
                    for r in recent_slots_rows
                ],
            }

            # Format latest snapshot for display
            latest: dict[str, Any] | None = None
            if latest_row:
                syncing_raw = latest_row["syncing"] or ""
                if isinstance(syncing_raw, str):
                    syncing_disp = "yes" if syncing_raw.lower() in ("true", "1", "yes") else "no"
                else:
                    syncing_disp = "yes" if syncing_raw else "no"
                latest = {
                    "ts": _ts_str(latest_row["ts"]),
                    "head": latest_row["head"],
                    "peers": latest_row["peers"],
                    "state_at_head": latest_row["state_at_head"],
                    "syncing": syncing_disp,
                    "buildnum": latest_row["buildnum"],
                    "commit": latest_row["commit"] or "",
                    "image": latest_row["image"] or "",
                    "restart": latest_row["restart"],
                    "cl_line": latest_row["cl_line"] or "",
                }

            node_data = {
                "node": node,
                "devnet": devnet,
                "latest": latest,
                "health_history": health_history,
                "version_history": version_history,
                "events": node_events,
                "proposals": proposals,
            }
        except Exception as exc:
            error = f"Error loading node data: {exc}"
        finally:
            conn.close()

    return templates.TemplateResponse(
        request,
        "node.html",
        {
            "devnet": devnet,
            "node": node,
            "node_data": node_data,
            "error": error,
            "current_devnet": devnet,
        },
    )


@app.get("/exectime/{devnet}", response_class=HTMLResponse)
async def exectime(request: Request, devnet: str) -> HTMLResponse:
    import json as _json

    exectime_data: dict[str, Any] | None = None
    error: str | None = None

    conn = _connect_ro()
    if conn is None:
        error = "No data yet. Run: dv collect"
    else:
        conn.close()
        from devnets.exectime import get_exectime_data
        exectime_data = get_exectime_data(devnet)
        if exectime_data is None:
            error = f"No exec-time data for {devnet}. Run: dv collect {devnet} blobs"

    series_json = "{}"
    if exectime_data and exectime_data.get("series"):
        series_json = _json.dumps(exectime_data["series"])

    return templates.TemplateResponse(
        request,
        "exectime.html",
        {
            "devnet": devnet,
            "exectime_data": exectime_data,
            "series_json": series_json,
            "error": error,
            "current_devnet": devnet,
        },
    )


@app.get("/incidents/{devnet}", response_class=HTMLResponse)
async def incidents(request: Request, devnet: str) -> HTMLResponse:
    history_path = _HERE.parent / "docs" / "history" / f"{devnet}.md"
    content: str | None = None
    error: str | None = None

    content_html: str | None = None
    if history_path.exists():
        content = history_path.read_text(encoding="utf-8")
        # History docs are trusted (we author them), so rendering their HTML is safe.
        content_html = markdown.markdown(
            content,
            extensions=["fenced_code", "tables", "sane_lists", "nl2br"],
        )
    else:
        error = f"No incident history found for {devnet} at docs/history/{devnet}.md"

    return templates.TemplateResponse(
        request,
        "incidents.html",
        {
            "devnet": devnet,
            "content_html": content_html,
            "error": error,
        },
    )
