"""FastAPI app: overview, leaderboard, coverage, compare, trends, test detail."""

from __future__ import annotations

import json
import time
from pathlib import Path

import plotly.graph_objects as go
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import config, db, queries
from .client import Client

# per-block telemetry fields worth charting if a client populates them
_BLOCKLOG_METRICS = [
    "timing_total_ms",
    "timing_execution_ms",
    "timing_state_read_ms",
    "timing_state_hash_ms",  # merkle
    "timing_commit_ms",  # store
    "throughput_mgas_per_sec",
    "cache_account_hit_rate",
    "cache_storage_hit_rate",
    "cache_code_hit_rate",
]


def _fetch_block_logs(run_id: str) -> list[dict]:
    """Live fetch (no storage) of per-block telemetry for one run."""
    with Client() as c:
        return c.paginate(
            "test_stats_block_logs",
            {"run_id": f"eq.{run_id}", "order": "block_number.asc"},
        )


ROOT = Path(__file__).resolve().parent
app = FastAPI(title="Benchmarkoor Dashboard")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
templates = Jinja2Templates(directory=ROOT / "templates")
HOME = config.HOME_CLIENT

# client -> color. ethrex (home) stays vivid; others are desaturated so they
# read as a calm comparison set against the dark panel.
COLORS = {
    "ethrex": "#e6007a",
    "geth": "#6e8fd6",
    "besu": "#d6a35c",
    "nethermind": "#4fb0a3",
    "erigon": "#9b86d3",
    "reth": "#cf8099",
}
MUTED_BAR = "#5f6b85"  # non-home bars on the leaderboard (ethrex is the only highlight)

# Dark theme matching the dashboard chrome (var(--panel)/(--txt)/(--line)).
DARK_LAYOUT = dict(
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(0,0,0,0)",
    font=dict(
        color="#e6e8ee",
        size=14,
        family='Inter, ui-sans-serif, system-ui, "Segoe UI", sans-serif',
    ),
    title_font=dict(color="#e6e8ee", size=16),
    xaxis=dict(gridcolor="#262a36", zerolinecolor="#262a36", linecolor="#262a36"),
    yaxis=dict(gridcolor="#262a36", zerolinecolor="#262a36", linecolor="#262a36"),
    legend=dict(font=dict(color="#8b90a0")),
)


def fig_json(fig: go.Figure) -> str:
    # Plotly's encoder handles numpy arrays / NaN correctly (json.dumps would not).
    fig.update_layout(template="plotly_dark", **DARK_LAYOUT)
    return fig.to_json()


def render(request: Request, name: str, ctx: dict) -> HTMLResponse:
    return templates.TemplateResponse(request=request, name=name, context=ctx)


def _ctx(request: Request, conn, suite_hash: str | None, **extra):
    suites = queries.active_suites(conn)
    last_sync = db.get_meta(conn, "last_sync")
    return {
        "suites": suites.to_dict("records") if not suites.empty else [],
        "suite_hash": suite_hash,
        "home": HOME,
        "last_sync": int(last_sync) if last_sync else None,
        "newest_run_ts": int(db.get_meta(conn, "newest_run_ts") or 0),
        "now": int(time.time()),
        **extra,
    }


def _suite_or_default(conn, suite: str | None, variant: str = "compute") -> str | None:
    if suite:
        return suite
    return queries.default_suite(conn, variant)


@app.get("/", response_class=HTMLResponse)
def overview(request: Request):
    conn = db.connect()
    db.init(conn)
    suites = queries.active_suites(conn)
    cards = []
    for s in suites.to_dict("records"):
        cov = queries.coverage(conn, s["suite_hash"])
        below = queries.below_summary(conn, s["suite_hash"])
        lb = queries.leaderboard(conn, s["suite_hash"])
        home_rank = None
        if not lb.empty and (lb["client"] == HOME).any():
            home_rank = int(lb[lb["client"] == HOME]["rank"].min())
        cards.append(
            {
                **s,
                "coverage": cov,
                "below": below,
                "home_rank": home_rank,
                "n_instances": 0 if lb.empty else len(lb),
                "headroom": queries.headroom(conn, s["suite_hash"]),
                "portfolio": queries.bottleneck_portfolio(conn, s["suite_hash"]),
                "failures": queries.failures(conn, s["suite_hash"]),
            }
        )
    live = []
    return render(
        request, "index.html", _ctx(request, conn, None, cards=cards, live=live)
    )


@app.get("/leaderboard", response_class=HTMLResponse)
def leaderboard(request: Request):
    conn = db.connect()
    db.init(conn)
    suites = queries.active_suites(conn)
    # newest-created first (indexed_at = when the suite was generated/indexed)
    if not suites.empty:
        suites = suites.sort_values("indexed_at", ascending=False)
    sections = []
    for s in suites.to_dict("records"):
        rc = conn.execute(
            "SELECT COUNT(*) t, SUM(status='completed') c FROM runs WHERE suite_hash=?",
            (s["suite_hash"],),
        ).fetchone()
        s = {**s, "runs_total": rc["t"], "runs_completed": rc["c"] or 0}
        lb = queries.leaderboard(conn, s["suite_hash"])
        fig = None
        if not lb.empty:
            fig = go.Figure(
                go.Bar(
                    x=lb["agg_mgas"],
                    y=lb["instance_id"],
                    orientation="h",
                    marker_color=[
                        COLORS["ethrex"] if c == HOME else MUTED_BAR
                        for c in lb["client"]
                    ],
                    text=[f"{v:,.0f}" for v in lb["agg_mgas"]],
                    textposition="auto",
                )
            )
            fig.update_layout(
                title=None,
                height=34 * len(lb) + 90,
                margin=dict(l=10, r=10, t=10, b=10),
                yaxis=dict(autorange="reversed"),
            )
        sections.append(
            {
                "suite": s,
                "rows": lb.to_dict("records") if not lb.empty else [],
                "fig": fig_json(fig) if fig is not None else None,
                "commit": queries.current_commit(conn, s["suite_hash"]),
            }
        )
    return render(
        request, "leaderboard.html", _ctx(request, conn, None, sections=sections)
    )


@app.get("/coverage", response_class=HTMLResponse)
def coverage(request: Request, suite: str | None = None):
    conn = db.connect()
    db.init(conn)
    sh = _suite_or_default(conn, suite)
    cov = queries.coverage(conn, sh) if sh else {}
    return render(request, "coverage.html", _ctx(request, conn, sh, cov=cov))


@app.get("/compare", response_class=HTMLResponse)
def compare(
    request: Request,
    suite: str | None = None,
    file: str | None = None,
    fork: str | None = None,
    below: int = 0,
    sort: str = "ratio",
):
    conn = db.connect()
    db.init(conn)
    sh = _suite_or_default(conn, suite)
    mat = queries.compare_matrix(conn, sh) if sh else None
    clients = [
        c
        for c in config.CLIENTS
        if mat is not None and not mat.empty and c in mat.columns
    ]
    rows, files, forks = [], [], []
    if mat is not None and not mat.empty:
        files = sorted(x for x in mat["file"].dropna().unique())
        forks = sorted(x for x in mat["fork"].dropna().unique())
        view = mat.copy()
        if file:
            view = view[view["file"] == file]
        if fork:
            view = view[view["fork"] == fork]
        if below and "ratio" in view:
            view = view[view["ratio"] < 1.0]
        if sort in view.columns:
            view = view.sort_values(sort, ascending=(sort in ("ratio", "home_rank")))
        rows = view.head(800).to_dict("records")
    ctx = _ctx(
        request,
        conn,
        sh,
        rows=rows,
        clients=clients,
        files=files,
        forks=forks,
        file=file,
        fork=fork,
        below=below,
        sort=sort,
    )
    tmpl = (
        "_compare_table.html" if request.headers.get("HX-Request") else "compare.html"
    )
    return render(request, tmpl, ctx)


def _trend_fig(df, markers: list[dict] | None = None) -> go.Figure | None:
    """Multi-line Mgas/s over time, home client emphasized, partial-run spikes removed.
    `markers` draws a dashed vertical deploy line per home-client commit."""
    import pandas as pd

    if df is None or df.empty:
        return None
    df = df.copy()
    df["dt"] = pd.to_datetime(df["timestamp"], unit="s")
    fig = go.Figure()
    # order: home first so it draws on top; then other -bal-full, then variants
    insts = sorted(
        df["instance_id"].unique(),
        key=lambda i: (not i.startswith(HOME), "-bal-full" not in i, i),
    )
    for inst in insts:
        grp = df[df["instance_id"] == inst].sort_values("dt").tail(400)
        client = grp["client"].iloc[0]
        med = grp["mgas_s"].median()
        # drop index-lag / partial-run spikes (e.g. a truncated run reading 3x normal)
        grp = grp[(grp["mgas_s"] <= med * 2.5) & (grp["mgas_s"] >= med * 0.4)]
        is_home = client == HOME
        is_full = inst == f"{client}-bal-full"
        fig.add_trace(
            go.Scatter(
                x=grp["dt"],
                y=grp["mgas_s"],
                name=inst,
                mode="lines",
                legendgroup=client,
                line=dict(
                    color=COLORS.get(client, "#999"),
                    width=3.5 if is_home else (2 if is_full else 1.2),
                    dash="solid" if is_full else "dot",
                ),
                opacity=1.0 if (is_home or is_full) else 0.65,
            )
        )
    for m in markers or []:
        x = pd.to_datetime(m["committed_at"], unit="s")
        if not (df["dt"].min() <= x <= df["dt"].max()):
            continue
        fig.add_vline(
            x=x,
            line_width=1,
            line_dash="dash",
            line_color="#8b90a0",
            annotation_text=m["sha"][:7],
            annotation_position="top",
            annotation_font_size=10,
            annotation_font_color="#8b90a0",
        )
    fig.update_layout(
        height=460,
        hovermode="x unified",
        margin=dict(l=10, r=10, t=10, b=10),
        legend=dict(orientation="h", y=-0.18, font=dict(size=12)),
        yaxis_title="Mgas/s (aggregate)",
    )
    # ethrex joined later than other clients; default the view to its lifetime
    # (with a small pad) and offer a one-click toggle back to the full range.
    home_dt = df[df["client"] == HOME]["dt"]
    if not home_dt.empty:
        lo, hi = home_dt.min(), home_dt.max()
        pad = (hi - lo) * 0.03 or pd.Timedelta(hours=12)
        rng = [(lo - pad).isoformat(), (hi + pad).isoformat()]
        fig.update_xaxes(range=rng)
        fig.update_layout(
            updatemenus=[
                dict(
                    type="buttons",
                    direction="left",
                    showactive=False,
                    x=0,
                    xanchor="left",
                    y=1.12,
                    yanchor="top",
                    pad=dict(t=0, b=0),
                    bgcolor="#1b1e27",
                    bordercolor="#262a36",
                    font=dict(size=11, color="#c7ccd8"),
                    buttons=[
                        dict(
                            label=f"{HOME} lifetime",
                            method="relayout",
                            args=[{"xaxis.range": rng, "xaxis.autorange": False}],
                        ),
                        dict(
                            label="Full range",
                            method="relayout",
                            args=[{"xaxis.autorange": True}],
                        ),
                    ],
                )
            ]
        )
    return fig


@app.get("/trends", response_class=HTMLResponse)
def trends(request: Request):
    conn = db.connect()
    db.init(conn)
    suites = queries.active_suites(conn)
    if not suites.empty:
        suites = suites.sort_values("indexed_at", ascending=False)
    sections = []
    for s in suites.to_dict("records"):
        df = queries.trends(conn, s["suite_hash"])
        markers = queries.deploy_markers(conn, s["suite_hash"])
        fig = _trend_fig(df, markers)
        n_runs = 0 if df is None else len(df)
        sections.append(
            {
                "suite": s,
                "fig": fig_json(fig) if fig is not None else None,
                "n_runs": n_runs,
            }
        )
    return render(request, "trends.html", _ctx(request, conn, None, sections=sections))


@app.get("/test", response_class=HTMLResponse)
def test_detail(request: Request, suite: str, name: str):
    conn = db.connect()
    db.init(conn)
    mat = queries.compare_matrix(conn, suite)
    row = None
    if mat is not None and not mat.empty:
        sub = mat[mat["test_name"] == name]
        if not sub.empty:
            row = sub.iloc[0].to_dict()
    fig = None
    if row:
        clients = [
            c for c in config.CLIENTS if c in mat.columns and row.get(c) == row.get(c)
        ]
        vals = [row[c] for c in clients]
        fig = go.Figure(
            go.Bar(
                x=clients,
                y=vals,
                marker_color=[COLORS.get(c, "#999") for c in clients],
                text=[f"{v:,.0f}" for v in vals],
                textposition="auto",
            )
        )
        fig.update_layout(
            title=f"Mgas/s — {name}",
            template="plotly_white",
            height=420,
            margin=dict(l=10, r=10, t=60, b=10),
        )
    return render(
        request,
        "test_detail.html",
        _ctx(
            request, conn, suite, name=name, row=row, fig=fig_json(fig) if fig else None
        ),
    )


@app.get("/op", response_class=HTMLResponse)
def op_scaling_page(request: Request, suite: str | None = None, op: str | None = None):
    """Gas-scaling. Default: a heatmap of every op × gas level (home ÷ best other)
    so all data shows at once. `?op=` drills into one op's per-client lines."""
    conn = db.connect()
    db.init(conn)
    sh = _suite_or_default(conn, suite)

    if op:  # drill-down: one op, per-client lines across gas
        df = queries.op_scaling(conn, sh, op) if sh else None
        fig = None
        if df is not None and not df.empty:
            fig = go.Figure()
            series = [c for c in config.CLIENTS if c in df.columns] + (
                ["best_other"] if "best_other" in df.columns else []
            )
            for c in series:
                fig.add_trace(
                    go.Scatter(
                        x=df["benchmark_mgas"], y=df[c], name=c, mode="lines+markers",
                        line=dict(
                            color=COLORS.get(c, "#8b90a0") if c != "best_other" else "#cdd3df",
                            width=3 if c == HOME else 1.6,
                            dash="dot" if c == "best_other" else "solid",
                        ),
                    )
                )
            fig.update_layout(
                height=460, hovermode="x unified", margin=dict(l=10, r=10, t=10, b=40),
                xaxis_title="benchmark gas (M)", yaxis_title="Mgas/s",
                legend=dict(orientation="h", y=-0.2, font=dict(size=12)),
            )
        return render(request, "op.html",
                      _ctx(request, conn, sh, op=op, fig=fig_json(fig) if fig else None))

    # default: all-ops heatmap of ratio (home ÷ best other) across gas levels
    m = queries.scaling_matrix(conn, sh) if sh else None
    fig = None
    if m and m["ops"]:
        fig = go.Figure(
            go.Heatmap(
                z=m["z"], x=[f"{g}M" for g in m["gas"]], y=m["ops"],
                zmid=1.0, zmin=0.4, zmax=1.6,
                colorscale=[[0.0, "#b3263a"], [0.5, "#222633"], [1.0, "#2e9e6a"]],
                colorbar=dict(title=f"{HOME} ÷ best", thickness=12),
                hovertemplate="%{y} @ %{x}: %{z}×<extra></extra>",
                xgap=1, ygap=1,
            )
        )
        fig.update_layout(
            height=18 * len(m["ops"]) + 150, margin=dict(l=10, r=10, t=10, b=40),
            xaxis_title="benchmark gas", yaxis=dict(autorange="reversed"),
        )
    return render(request, "op.html",
                  _ctx(request, conn, sh, op=None, fig=fig_json(fig) if fig else None))


@app.get("/merkle", response_class=HTMLResponse)
def merkle_page(request: Request, suite: str | None = None):
    """Merkle parallelism: tests whose merkleization runs largely serial."""
    conn = db.connect()
    db.init(conn)
    sh = _suite_or_default(conn, suite)
    df = queries.merkle_opportunities(conn, sh) if sh else None
    rows = [] if df is None or df.empty else df.to_dict("records")
    return render(request, "merkle.html", _ctx(request, conn, sh, rows=rows))


# soft, analogous cool palette that sits calmly on the dark panel
_PHASE_COLORS = {"exec_ms": "#6e8fd6", "merkle_ms": "#8c7bd0", "store_ms": "#4fb0a3"}


@app.get("/run/{run_id}", response_class=HTMLResponse)
def run_logs(request: Request, run_id: str):
    """Per-run phase view.

    If we ingested the run log (home client), show the per-test exec/merkle/store
    split (stacked, top tests by total). Otherwise fall back to the benchmarkoor
    block-logs API, which for ethrex only carries `timing_total_ms` (one line).
    """
    conn = db.connect()
    db.init(conn)
    ph = queries.run_phases(conn, run_id)
    fig, populated, n, source = None, [], 0, ""

    if not ph.empty:
        source = "parsed phase log"
        n = len(ph)
        # one bar per operation = its worst (max total_ms) block, so y-labels are
        # unique (many tests share an op at different gas) and each bar is a clean
        # exec/merkle/store split.
        ph["op"] = ph["op"].fillna(ph["test_name"])
        worst = ph.loc[ph.groupby("op")["total_ms"].idxmax()]
        top = worst.sort_values("total_ms", ascending=False).head(30).iloc[::-1]
        labels = [f"{r['op'][:36]}" for _, r in top.iterrows()]
        populated = ["exec_ms", "merkle_ms", "store_ms"]
        fig = go.Figure()
        for m in populated:
            fig.add_trace(
                go.Bar(
                    y=labels,
                    x=top[m],
                    name=m.removesuffix("_ms"),
                    orientation="h",
                    marker=dict(
                        color=_PHASE_COLORS[m],
                        line=dict(color="#181b24", width=1),  # panel-colored separators
                    ),
                )
            )
        fig.update_layout(
            barmode="stack",
            bargap=0.55,
            height=20 * len(top) + 110,
            hovermode="y unified",
            margin=dict(l=10, r=10, t=10, b=40),
            xaxis_title="ms (per test block)",
            legend=dict(orientation="h", y=-0.12, font=dict(size=12)),
        )
    else:
        # fallback: live block-logs from the API (no phase split for ethrex)
        import pandas as pd

        rows = _fetch_block_logs(run_id)
        n = len(rows)
        if rows:
            df = (
                pd.DataFrame(rows)
                .sort_values(["block_number", "id"])
                .reset_index(drop=True)
            )
            df["seq"] = range(1, len(df) + 1)
            populated = [
                m
                for m in _BLOCKLOG_METRICS
                if df.get(m) is not None and df[m].abs().sum() > 0
            ]
            fig = go.Figure()
            for m in populated:
                fig.add_trace(go.Scatter(x=df["seq"], y=df[m], name=m, mode="lines"))
            fig.update_layout(
                height=480,
                hovermode="x unified",
                margin=dict(l=10, r=10, t=10, b=40),
                xaxis_title="block / test sequence",
                yaxis_title="ms",
                legend=dict(orientation="h", y=-0.2, font=dict(size=12)),
            )
    return render(
        request,
        "run.html",
        _ctx(
            request,
            conn,
            suite_hash=None,
            run_id=run_id,
            n_blocks=n,
            populated=populated,
            source=source,
            fig=fig_json(fig) if fig is not None else None,
        ),
    )


@app.get("/api/runs/{run_id}/block_logs")
def api_block_logs(run_id: str, limit: int = 0):
    """Live per-block telemetry for a run. `limit=0` returns all blocks."""
    rows = _fetch_block_logs(run_id)
    populated = sorted({m for m in _BLOCKLOG_METRICS for r in rows if (r.get(m) or 0)})
    return JSONResponse(
        {
            "run_id": run_id,
            "blocks": len(rows),
            "populated_metrics": populated,
            "note": "fkv (FlatKeyValue) catch-up is logged to ethrex stdout "
            '("Generation of FlatKeyValue started/finished"); benchmarkoor only '
            "stores parsed per-block telemetry, so for ethrex usually just timing_total_ms.",
            "rows": rows if not limit else rows[:limit],
        }
    )


# --------------------------------------------------------------------------
# Agent-facing API: JSON endpoints + a single Markdown report to point Claude at
# --------------------------------------------------------------------------


def _records(df) -> list:
    """DataFrame -> JSON-safe list of dicts (NaN -> null)."""
    if df is None or df.empty:
        return []
    return json.loads(df.to_json(orient="records"))


def _active(conn):
    s = queries.active_suites(conn)
    if not s.empty:
        s = s.sort_values("indexed_at", ascending=False)
    return s


@app.get("/api/suites")
def api_suites():
    conn = db.connect()
    db.init(conn)
    cols = [
        "suite_hash",
        "name",
        "variant",
        "tests_total",
        "indexed_at",
        "latest_run_ts",
    ]
    return JSONResponse(
        _records(_active(conn)[cols]) if not _active(conn).empty else []
    )


@app.get("/api/leaderboard")
def api_leaderboard(suite: str | None = None):
    conn = db.connect()
    db.init(conn)
    sh = _suite_or_default(conn, suite)
    lb = queries.leaderboard(conn, sh) if sh else None
    return JSONResponse({"suite_hash": sh, "ranking": _records(lb)})


@app.get("/api/targets")
def api_targets(
    suite: str | None = None, limit: int = 100, min_time_lost_ms: float = 0.0
):
    """Ranked optimization targets for the home client, by recoverable time."""
    conn = db.connect()
    db.init(conn)
    sh = _suite_or_default(conn, suite)
    df = queries.opt_targets(conn, sh) if sh else None
    if df is not None and not df.empty:
        df = df[df["time_lost_ms"] >= min_time_lost_ms].head(limit)
    return JSONResponse({"suite_hash": sh, "home": HOME, "targets": _records(df)})


@app.get("/api/targets/by_file")
def api_targets_by_file(suite: str | None = None):
    conn = db.connect()
    db.init(conn)
    sh = _suite_or_default(conn, suite)
    df = queries.targets_by_file(conn, sh) if sh else None
    return JSONResponse({"suite_hash": sh, "home": HOME, "by_file": _records(df)})


@app.get("/api/coverage")
def api_coverage(suite: str | None = None):
    conn = db.connect()
    db.init(conn)
    sh = _suite_or_default(conn, suite)
    return JSONResponse(queries.coverage(conn, sh) if sh else {})


@app.get("/api/fkv")
def api_fkv(suite: str | None = None):
    """FlatKeyValue catch-up summary for the home client's current run."""
    conn = db.connect()
    db.init(conn)
    sh = _suite_or_default(conn, suite)
    return JSONResponse(
        {
            "suite_hash": sh,
            "home": HOME,
            "fkv": queries.fkv_summary(conn, sh) if sh else None,
        }
    )


@app.get("/api/headroom")
def api_headroom(suite: str | None = None):
    conn = db.connect()
    db.init(conn)
    sh = _suite_or_default(conn, suite)
    return JSONResponse(
        {
            "suite_hash": sh,
            "home": HOME,
            "headroom": queries.headroom(conn, sh) if sh else {},
            "portfolio": queries.bottleneck_portfolio(conn, sh) if sh else {},
        }
    )


@app.get("/api/merkle")
def api_merkle(suite: str | None = None, limit: int = 40):
    conn = db.connect()
    db.init(conn)
    sh = _suite_or_default(conn, suite)
    df = queries.merkle_opportunities(conn, sh, limit) if sh else None
    return JSONResponse({"suite_hash": sh, "home": HOME, "tests": _records(df)})


@app.get("/api/failures")
def api_failures(suite: str | None = None):
    conn = db.connect()
    db.init(conn)
    sh = _suite_or_default(conn, suite)
    return JSONResponse(
        {"suite_hash": sh, "failures": queries.failures(conn, sh) if sh else []}
    )


@app.get("/api/regressions")
def api_regressions(suite: str | None = None, threshold_pct: float = 3.0):
    """Home-client commit-over-commit aggregate regressions beyond threshold_pct.
    Detection only — delivery (Slack/webhook) is external."""
    conn = db.connect()
    db.init(conn)
    sh = _suite_or_default(conn, suite)
    regs = []
    if sh:
        tl = queries.commit_timeline(conn, sh)
        for r in tl.to_dict("records"):
            d = r.get("delta_vs_prev")
            if (
                d == d
                and r["mean_mgas"]
                and (d / r["mean_mgas"] * 100) <= -threshold_pct
            ):
                regs.append(
                    {
                        "sha": r["sha"][:9],
                        "message": r["message"],
                        "mean_mgas": round(r["mean_mgas"], 1),
                        "delta_pct": round(d / r["mean_mgas"] * 100, 1),
                    }
                )
    return JSONResponse(
        {"suite_hash": sh, "threshold_pct": threshold_pct, "regressions": regs}
    )


@app.get("/api/freshness")
def api_freshness():
    """Snapshot age + live check of the API's newest run (is the snapshot behind?)."""
    conn = db.connect()
    db.init(conn)
    last_sync = int(db.get_meta(conn, "last_sync") or 0)
    snap_newest = int(db.get_meta(conn, "newest_run_ts") or 0)
    api_newest = None
    try:
        with Client() as c:
            rows = c.query("runs", {"order": "timestamp.desc", "limit": 1})
            api_newest = rows[0]["timestamp"] if rows else None
    except Exception:
        pass
    behind = (api_newest - snap_newest) if (api_newest and snap_newest) else None
    return JSONResponse(
        {
            "last_sync": last_sync,
            "snapshot_newest_run": snap_newest,
            "api_newest_run": api_newest,
            "behind_seconds": behind,
            "stale": bool(behind and behind > 0),
        }
    )


@app.get("/api/commits")
def api_commits(suite: str | None = None):
    """Current home-client build + per-commit aggregate throughput timeline."""
    conn = db.connect()
    db.init(conn)
    sh = _suite_or_default(conn, suite)
    return JSONResponse(
        {
            "suite_hash": sh,
            "home": HOME,
            "current": queries.current_commit(conn, sh) if sh else None,
            "timeline": _records(queries.commit_timeline(conn, sh)) if sh else [],
        }
    )


def _cur_build_line(conn) -> str:
    c = queries.current_commit(conn)
    if not c:
        return (
            f"{config.HOME_CLIENT} build: unknown "
            f"(no commits mapped; needs `gh` access to {config.ETHREX_REPO})."
        )
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(int(c["committed_at"])))
    return (
        f"Current {config.HOME_CLIENT} build: **`{c['sha'][:9]}`** — {c['message']} "
        f"(committed {when}, {config.ETHREX_REPO}@{config.ETHREX_BRANCH}). "
        "Runs are mapped to the branch commit that was HEAD at run time."
    )


def _md_table(headers: list[str], rows: list[list]) -> str:
    out = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


@app.get("/agent.md", response_class=PlainTextResponse)
@app.get("/llm.md", response_class=PlainTextResponse)
def agent_md(request: Request):
    """Self-contained Markdown brief for an LLM agent: where to optimize ethrex."""
    conn = db.connect()
    db.init(conn)
    last_sync = db.get_meta(conn, "last_sync")
    base = str(request.base_url).rstrip("/")
    L = [
        f"# Benchmarkoor — {HOME} optimization brief",
        "",
        f"Source: {base} · data snapshot synced "
        f"{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(int(last_sync))) if last_sync else 'unknown'}.",
        f"Home client: **{HOME}**. Clients compared: {', '.join(config.CLIENTS)}.",
        _cur_build_line(conn),
        "",
        "## How to read this",
        "- Benchmarks are EL-client BAL execution tests; metric is **Mgas/s** (higher = faster).",
        "- A client's suite ranking uses **gas-weighted aggregate Mgas/s** = Σ(test gas) / Σ(test time) "
        "(real end-to-end throughput), not the per-test median.",
        f"- **`time_lost_ms`** = {HOME}'s time on a test − the fastest competitor's time on that test. "
        "It is how much wall-clock could be recovered. Targets are ranked by it (not by Mgas/s ratio, "
        "which over-weights tiny tests).",
        f"- **`rank`** = {HOME}'s position among clients on that test (1 = fastest).",
        "- **`op`** = the operation under test (opcode or scenario), parsed from the test name.",
        f"- **`phase`** = which pipeline stage dominates {HOME}'s block (`exec` / `merkle` / "
        "`store`), parsed from the run log. For `merkle`, `%ov` is the exec/merkle overlap "
        "(low overlap = merkle runs serially after exec). This is the most actionable signal.",
        f"- **`resource`** (a.k.a. bottleneck) = where {HOME} most exceeds the fastest competitor's "
        "resource use: `cpu`, `io`, `memory`, or `even`. Orthogonal hint at the kind of fix.",
        f"- **fkv** line per suite = whether {HOME}'s FlatKeyValue store had to (re)generate during "
        "the run (`finished>0`) or was already caught up (all `skipping`).",
        "- **`scaling`** (per file) = does the gap grow with gas? `worse-at-high-gas` suggests "
        "per-gas/algorithmic overhead; `flat` suggests fixed setup cost.",
        "- In the by-file table, `time_lost` counts **only tests where "
        f"{HOME} is behind** (ratio<1) — pure recoverable deficit. Optimize per **file/op**.",
        "",
        "Machine-readable equivalents: "
        f"`{base}/api/targets?suite=<hash>`, `{base}/api/targets/by_file?suite=<hash>`, "
        f"`{base}/api/leaderboard?suite=<hash>`, `{base}/api/coverage?suite=<hash>`, `{base}/api/suites`.",
    ]
    for s in _active(conn).to_dict("records"):
        sh = s["suite_hash"]
        L += [
            "",
            "---",
            "",
            f"## Suite: {s['name']}  (`{sh}`)",
            f"{s['variant']} · {s['tests_total']} tests",
            "",
        ]

        lb = queries.leaderboard(conn, sh)
        if not lb.empty:
            L += [
                "### Leaderboard (aggregate Mgas/s)",
                _md_table(
                    ["#", "instance", "agg Mgas/s", "median", "rank vs " + HOME],
                    [
                        [
                            int(r["rank"]),
                            r["instance_id"],
                            f"{r['agg_mgas']:.0f}",
                            f"{r['median_mgas']:.0f}",
                            "← home" if r["client"] == HOME else "",
                        ]
                        for r in lb.to_dict("records")
                    ],
                ),
                "",
            ]

        hr = queries.headroom(conn, sh)
        if hr and hr.get("gain_pct"):
            L += [
                f"**Headroom:** if {HOME} matched the fastest client on every test, aggregate "
                f"would go {hr['current_mgas']:.0f} → {hr['potential_mgas']:.0f} Mgas/s "
                f"(+{hr['gain_pct']:.0f}%, {hr['recoverable_s']:.1f}s recoverable).",
                "",
            ]
        port = queries.bottleneck_portfolio(conn, sh)
        if port.get("phase"):
            phase_str = ", ".join(f"{k} {v}" for k, v in port["phase"].items())
            res_str = ", ".join(f"{k} {v}" for k, v in port["resource"].items())
            L += [f"Deficits by phase: {phase_str}. By resource: {res_str}.", ""]

        fkv = queries.fkv_summary(conn, sh)
        if fkv:
            if fkv["caught_up"]:
                L += [
                    f"fkv: already caught up (DB pre-populated; {fkv['skipping']} "
                    "container starts, 0 regenerations).",
                    "",
                ]
            else:
                L += [
                    f"fkv: regenerated on {fkv['finished']} of {fkv['started']} "
                    "container starts (catch-up cost incurred).",
                    "",
                ]

        cov = queries.coverage(conn, sh)
        L += [
            f"### Coverage: {cov['coverage_pct']}% "
            f"({cov['home']}/{cov['union']} tests; {cov['missing_count']} not run by {HOME})",
            "",
        ]
        if cov["by_file"]:
            L += [
                _md_table(
                    ["file", "missing tests"],
                    [[f["file"], f["n"]] for f in cov["by_file"][:15]],
                ),
                "",
            ]

        bf = queries.targets_by_file(conn, sh)
        if not bf.empty:
            top = bf.head(15).to_dict("records")
            L += [
                "### Top subsystems to optimize (recoverable time from deficits)",
                _md_table(
                    [
                        "file",
                        "tests",
                        f"{HOME} below",
                        "time_lost (ms)",
                        "phase",
                        "resource",
                        "scaling",
                        "median rank",
                    ],
                    [
                        [
                            r["file"],
                            int(r["tests"]),
                            int(r["below"]),
                            f"{r['time_lost_ms']:.1f}",
                            r.get("phase") or "-",
                            r["bottleneck"],
                            r["scaling"],
                            f"{r['median_rank']:.0f}",
                        ]
                        for r in top
                    ],
                ),
                "",
            ]

        tg = queries.opt_targets(conn, sh)
        if not tg.empty:
            top = tg.head(25).to_dict("records")
            L += [
                "### Top individual test targets (by recoverable time)",
                _md_table(
                    [
                        "op",
                        "fork",
                        "gas(M)",
                        f"{HOME} Mgas/s",
                        "best other",
                        "by",
                        "ratio",
                        "rank",
                        "phase",
                        "resource",
                        "time_lost(ms)",
                    ],
                    [
                        [
                            r["op"] or "",
                            r["fork"] or "",
                            r["benchmark_mgas"] or "",
                            f"{r['ethrex_mgas']:.0f}",
                            f"{r['best_other_mgas']:.0f}",
                            r["best_other_client"],
                            f"{r['ratio']:.2f}",
                            int(r["rank"]),
                            (r.get("phase_bottleneck") or "-")
                            + (
                                f" {r['merkle_overlap_pct']:.0f}%ov"
                                if r.get("phase_bottleneck") == "merkle"
                                and r.get("merkle_overlap_pct")
                                == r.get("merkle_overlap_pct")
                                else ""
                            ),
                            r["bottleneck"],
                            f"{r['time_lost_ms']:.1f}",
                        ]
                        for r in top
                    ],
                ),
                "",
            ]

        mk = queries.merkle_opportunities(conn, sh, limit=10)
        if not mk.empty:
            L += [
                "### Merkle parallelism opportunities (serial merkleization)",
                "High merkle time with low exec/merkle overlap = merkle running serially. "
                "`serial_merkle_ms` = merkle × (1 − overlap%).",
                _md_table(
                    ["op", "gas(M)", "merkle ms", "overlap %", "serial merkle ms"],
                    [
                        [
                            r["op"] or "",
                            r["benchmark_mgas"] or "",
                            f"{r['merkle_ms']:.1f}",
                            f"{r['merkle_overlap_pct']:.0f}"
                            if r.get("merkle_overlap_pct")
                            == r.get("merkle_overlap_pct")
                            else "-",
                            f"{r['serial_merkle_ms']:.1f}",
                        ]
                        for r in mk.to_dict("records")
                    ],
                ),
                "",
            ]

        fails = queries.failures(conn, sh)
        if fails:
            L += [
                "**Failing tests:** "
                + ", ".join(f"{f['instance_id']} ({f['tests_failed']})" for f in fails),
                "",
            ]

        tl = queries.commit_timeline(conn, sh)
        if not tl.empty and len(tl) > 1:
            rows = tl.tail(10).to_dict("records")
            L += [
                f"### {HOME} commit timeline (aggregate Mgas/s, this suite)",
                "How throughput moved across deployed commits (Δ vs previous):",
                _md_table(
                    ["commit", "message", "runs", "mean Mgas/s", "Δ vs prev"],
                    [
                        [
                            r["sha"][:9],
                            (r["message"] or "")[:48],
                            int(r["runs"]),
                            f"{r['mean_mgas']:.0f}",
                            "—"
                            if r["delta_vs_prev"] != r["delta_vs_prev"]
                            else f"{r['delta_vs_prev']:+.0f}",
                        ]
                        for r in rows
                    ],
                ),
                "",
            ]
    return "\n".join(L)
