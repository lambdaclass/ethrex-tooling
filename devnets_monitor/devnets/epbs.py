"""ePBS (EIP-7732) panel: bid count and PTC vote data per slot.

Fetches /v1/slot/{slot}/bids and /v1/slot/{slot}/ptc_votes for recent slots
and persists into the epbs_slot table.

Confirmed field names from live glamsterdam-devnet-5 (slot 35537):
  /bids   -> data.{slot, block_root, count, bids[{is_self_built, is_winning, ...}]}
  /ptc_votes -> data.{slot, block_root, total_ptc_size, vote_count,
                      non_voter_count, non_voter_percent,
                      aggregates[{payload_present, vote_count}]}
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from .config import devnet_entry
from .dora import _get_with_backoff
from .store import connect, migrate, upsert

logger = logging.getLogger(__name__)

_DEFAULT_LIMIT = 50


def _max_epbs_slot(conn: Any, devnet: str) -> int | None:
    """Return the highest slot already in epbs_slot for this devnet."""
    row = conn.execute(
        "SELECT MAX(slot) FROM epbs_slot WHERE devnet = ?", (devnet,)
    ).fetchone()
    if row and row[0] is not None:
        return int(row[0])
    return None


def _payload_revealed(bids: list[dict]) -> int | None:
    """
    Derive whether a payload was revealed.

    A bid with is_winning=true that is NOT is_self_built indicates an external
    builder revealed a payload. If all winning bids are self-built (the local
    proposer built the block), ePBS payload reveal may not have been triggered.
    Returns 1 if an external winning bid exists, 0 if only self-built, None if
    no bids at all.
    """
    if not bids:
        return None
    for bid in bids:
        if bid.get("is_winning") and not bid.get("is_self_built"):
            return 1
    # No external winning bid; check if there is any winning bid at all
    for bid in bids:
        if bid.get("is_winning"):
            return 0
    return None


def collect_epbs(devnet: str, limit: int = _DEFAULT_LIMIT) -> None:
    """
    Collect ePBS bid and PTC vote data for recent slots.

    Fetches for ethrex-proposed canonical slots above the watermark, plus a
    sample of other-client slots for comparison (up to half the limit).
    Per-fetch try/except ensures one failure does not abort the chain.
    """
    entry = devnet_entry(devnet)
    dora_base = entry.get("dora_base", "").rstrip("/")
    if not dora_base:
        logger.error("collect_epbs: dora_base missing for devnet %s", devnet)
        return

    conn = connect()
    migrate(conn)

    watermark = _max_epbs_slot(conn, devnet)

    # Fetch ethrex canonical slots above the watermark
    query_ethrex = """
        SELECT slot, proposer_name
        FROM slots
        WHERE devnet = ?
          AND status = 'Canonical'
          AND proposer_name LIKE '%ethrex%'
    """
    params_ethrex: list[Any] = [devnet]
    if watermark is not None:
        query_ethrex += " AND slot > ?"
        params_ethrex.append(watermark)
    query_ethrex += " ORDER BY slot DESC LIMIT ?"
    params_ethrex.append(limit)

    ethrex_rows = conn.execute(query_ethrex, params_ethrex).fetchall()

    # Also pull a comparison sample of non-ethrex slots (up to limit // 2)
    comparison_limit = max(1, limit // 2)
    query_other = """
        SELECT slot, proposer_name
        FROM slots
        WHERE devnet = ?
          AND status = 'Canonical'
          AND proposer_name NOT LIKE '%ethrex%'
    """
    params_other: list[Any] = [devnet]
    if watermark is not None:
        query_other += " AND slot > ?"
        params_other.append(watermark)
    query_other += " ORDER BY slot DESC LIMIT ?"
    params_other.append(comparison_limit)

    other_rows = conn.execute(query_other, params_other).fetchall()

    all_rows = list(ethrex_rows) + list(other_rows)

    if not all_rows:
        print(f"collect_epbs({devnet}): no new slots above watermark")
        conn.close()
        return

    fetched = 0
    errors = 0
    now = int(time.time())

    for row in all_rows:
        slot = row["slot"]
        proposer_name = row["proposer_name"] or ""

        bids_url = f"{dora_base}/api/v1/slot/{slot}/bids"
        ptc_url = f"{dora_base}/api/v1/slot/{slot}/ptc_votes"

        bid_count: int | None = None
        ptc_size: int | None = None
        ptc_vote_count: int | None = None
        ptc_nonvoter_pct: float | None = None
        payload_rev: int | None = None
        block_root: str = ""
        bids_ok = False
        ptc_ok = False

        try:
            resp = _get_with_backoff(bids_url, {})
            if resp is not None and resp.status_code == 200:
                bdata = resp.json().get("data", {})
                block_root = bdata.get("block_root", "") or ""
                bid_count = bdata.get("count", 0) or 0
                bids_list = bdata.get("bids") or []
                payload_rev = _payload_revealed(bids_list)
                bids_ok = True
            else:
                logger.warning(
                    "collect_epbs: bids HTTP %s for slot %d",
                    resp.status_code if resp is not None else "None",
                    slot,
                )
                errors += 1
        except Exception as exc:
            logger.warning("collect_epbs: bids error on slot %d: %s", slot, exc)
            errors += 1

        try:
            resp2 = _get_with_backoff(ptc_url, {})
            if resp2 is not None and resp2.status_code == 200:
                pdata = resp2.json().get("data", {})
                if not block_root:
                    block_root = pdata.get("block_root", "") or ""
                ptc_size = pdata.get("total_ptc_size")
                ptc_vote_count = pdata.get("vote_count")
                ptc_nonvoter_pct = pdata.get("non_voter_percent")
                ptc_ok = True
            else:
                logger.warning(
                    "collect_epbs: ptc_votes HTTP %s for slot %d",
                    resp2.status_code if resp2 is not None else "None",
                    slot,
                )
                errors += 1
        except Exception as exc:
            logger.warning("collect_epbs: ptc_votes error on slot %d: %s", slot, exc)
            errors += 1

        # Don't persist (or advance past) a slot we got nothing for; leave it
        # below the watermark so the next run retries it.
        if not bids_ok and not ptc_ok:
            continue

        epbs_row: dict[str, Any] = {
            "devnet": devnet,
            "slot": slot,
            "block_root": block_root,
            "proposer_name": proposer_name,
            "bid_count": bid_count,
            "ptc_size": ptc_size,
            "ptc_vote_count": ptc_vote_count,
            "ptc_nonvoter_pct": ptc_nonvoter_pct,
            "payload_revealed": payload_rev,
            "fetched_at": now,
        }
        upsert(conn, "epbs_slot", epbs_row)
        fetched += 1

    conn.commit()
    conn.close()
    print(
        f"collect_epbs({devnet}): {fetched} slots fetched, "
        f"{errors} errors, watermark was {watermark}"
    )


def get_epbs_data(devnet: str, limit: int = 200) -> dict[str, Any] | None:
    """
    Return ePBS slot data for dashboard rendering.

    Keys:
      rows            list[dict]  recent epbs_slot rows (newest first)
      total           int         total rows
      ethrex_rows     list[dict]  ethrex-proposed rows only
      avg_ptc_size    float
      avg_vote_count  float
      avg_nonvoter_pct float
    """
    conn = connect()
    migrate(conn)

    rows = conn.execute(
        """
        SELECT slot, block_root, proposer_name, bid_count,
               ptc_size, ptc_vote_count, ptc_nonvoter_pct,
               payload_revealed, fetched_at
        FROM epbs_slot
        WHERE devnet = ?
        ORDER BY slot DESC
        LIMIT ?
        """,
        (devnet, limit),
    ).fetchall()
    conn.close()

    if not rows:
        return None

    def _fmt(ts: int | None) -> str:
        if ts is None:
            return "-"
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M UTC"
            )
        except Exception:
            return str(ts)

    out_rows = []
    ptc_sizes = []
    vote_counts = []
    nonvoter_pcts = []

    for r in rows:
        is_ethrex = "ethrex" in (r["proposer_name"] or "")
        ps = r["ptc_size"]
        vc = r["ptc_vote_count"]
        nvp = r["ptc_nonvoter_pct"]
        if ps is not None:
            ptc_sizes.append(ps)
        if vc is not None:
            vote_counts.append(vc)
        if nvp is not None:
            nonvoter_pcts.append(nvp)

        payload_rev = r["payload_revealed"]
        if payload_rev is None:
            payload_label = "unknown"
        elif payload_rev:
            payload_label = "yes"
        else:
            payload_label = "no (self-built)"

        out_rows.append({
            "slot": r["slot"],
            "block_root": r["block_root"] or "",
            "block_root_short": (r["block_root"] or "")[:14] + "...",
            "proposer_name": r["proposer_name"] or "",
            "bid_count": r["bid_count"] if r["bid_count"] is not None else 0,
            "ptc_size": ps,
            "ptc_vote_count": vc,
            "ptc_nonvoter_pct": round(nvp, 1) if nvp is not None else None,
            "payload_revealed": payload_label,
            "fetched_at": _fmt(r["fetched_at"]),
            "is_ethrex": is_ethrex,
        })

    def _avg(lst: list) -> float:
        return round(sum(lst) / len(lst), 1) if lst else 0.0

    return {
        "rows": out_rows,
        "total": len(out_rows),
        "ethrex_rows": [r for r in out_rows if r["is_ethrex"]],
        "avg_ptc_size": _avg(ptc_sizes),
        "avg_vote_count": _avg(vote_counts),
        "avg_nonvoter_pct": _avg(nonvoter_pcts),
    }


def show_epbs(devnet: str) -> None:
    """Print ePBS slot summary to stdout."""
    data = get_epbs_data(devnet)
    if data is None:
        print(
            f"epbs({devnet}): no ePBS data. "
            f"Run: dv collect {devnet} slow"
        )
        return

    print(f"\nePBS inspection ({devnet})")
    print(
        f"Total entries: {data['total']}, "
        f"avg PTC size: {data['avg_ptc_size']}, "
        f"avg vote count: {data['avg_vote_count']}, "
        f"avg non-voter%: {data['avg_nonvoter_pct']}\n"
    )

    print(
        f"{'SLOT':>8} {'BIDS':>5} {'PTC_SZ':>7} {'VOTES':>6} "
        f"{'NONVOTE%':>9} {'PAYLOAD':>14} {'PROPOSER':<28} {'ETHREX'}"
    )
    print("-" * 100)
    for r in data["rows"][:40]:
        ethrex_flag = "<--" if r["is_ethrex"] else ""
        ptc_sz = str(r["ptc_size"]) if r["ptc_size"] is not None else "-"
        votes = str(r["ptc_vote_count"]) if r["ptc_vote_count"] is not None else "-"
        nvpct = f"{r['ptc_nonvoter_pct']:.1f}" if r["ptc_nonvoter_pct"] is not None else "-"
        print(
            f"{r['slot']:>8} {r['bid_count']:>5} {ptc_sz:>7} {votes:>6} "
            f"{nvpct:>9} {r['payload_revealed']:>14} "
            f"{r['proposer_name']:<28} {ethrex_flag}"
        )
    print()
