"""FastAPI app: overview, leaderboard, coverage, compare, trends, test detail."""

from __future__ import annotations

import time
from pathlib import Path

import plotly.graph_objects as go
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import config, db, queries

ROOT = Path(__file__).resolve().parent
app = FastAPI(title="Benchmarkoor Dashboard")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
templates = Jinja2Templates(directory=ROOT / "templates")
HOME = config.HOME_CLIENT

# client -> color (ethrex highlighted)
COLORS = {
    "ethrex": "#e6007a",
    "geth": "#4aa3ff",
    "besu": "#ff9f43",
    "nethermind": "#2ecc71",
    "erigon": "#b07cff",
    "reth": "#ff6b9d",
}

# Dark theme matching the dashboard chrome (var(--panel)/(--txt)/(--line)).
DARK_LAYOUT = dict(
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(0,0,0,0)",
    font=dict(color="#e6e8ee", size=14),
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
                    marker_color=[COLORS.get(c, "#999") for c in lb["client"]],
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


def _trend_fig(df) -> go.Figure | None:
    """Multi-line Mgas/s over time, home client emphasized, partial-run spikes removed."""
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
    fig.update_layout(
        height=460,
        hovermode="x unified",
        margin=dict(l=10, r=10, t=10, b=10),
        legend=dict(orientation="h", y=-0.18, font=dict(size=12)),
        yaxis_title="Mgas/s (aggregate)",
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
        fig = _trend_fig(df)
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
