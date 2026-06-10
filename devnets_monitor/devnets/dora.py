"""Dora slots collector: fetch slot data and persist into SQLite."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

import requests

from .config import devnet_entry
from .store import connect, max_slot, migrate, upsert

logger = logging.getLogger(__name__)

_PAGE_SIZE = 100
_MAX_RETRIES = 6
_BASE_BACKOFF = 2.0  # seconds; doubled each retry, capped


def _get_with_backoff(url: str, params: dict[str, Any]) -> requests.Response | None:
    """
    GET with retry/backoff for HTTP 429 and transient 5xx. Honors a Retry-After
    header when present, else exponential backoff (2,4,8,... capped at 60s).
    Returns the Response on success, None if all retries are exhausted.
    """
    for attempt in range(_MAX_RETRIES):
        try:
            resp = requests.get(url, params=params, timeout=30)
        except requests.RequestException as exc:
            wait = min(_BASE_BACKOFF * (2 ** attempt), 60.0)
            logger.warning("dora request error (%s); retry in %.0fs", exc, wait)
            time.sleep(wait)
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            retry_after = resp.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                wait = float(retry_after)
            else:
                wait = min(_BASE_BACKOFF * (2 ** attempt), 60.0)
            logger.warning(
                "dora HTTP %d (rate-limited?); backing off %.0fs",
                resp.status_code,
                wait,
            )
            time.sleep(wait)
            continue
        return resp
    logger.error("dora: giving up after %d retries", _MAX_RETRIES)
    return None


def _parse_time(value: Any) -> int | None:
    """Convert ISO-8601 or unix-int time to a unix int. Returns None on failure."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            # Handle ISO-8601 with Z suffix
            s = value.rstrip("Z")
            dt = datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            pass
    return None


def _status_str(value: Any) -> str:
    """Normalise slot status to a string."""
    if isinstance(value, int):
        mapping = {0: "Missing", 1: "Canonical", 2: "Orphaned"}
        return mapping.get(value, str(value))
    return str(value) if value is not None else "Unknown"


def _upsert_slot(conn: Any, devnet: str, s: dict[str, Any]) -> int | None:
    """
    Upsert one raw Dora slot dict into slots + slot_exec_times.
    Returns the slot number on success, None if slot is absent.
    """
    slot_num = s.get("slot")
    if slot_num is None:
        return None

    row: dict[str, Any] = {
        "devnet": devnet,
        "slot": slot_num,
        "epoch": s.get("epoch"),
        "time": _parse_time(s.get("time")),
        "proposer": str(s["proposer"]) if s.get("proposer") is not None else None,
        "proposer_name": s.get("proposer_name"),
        "status": _status_str(s.get("status")),
        "blob_count": s.get("blob_count"),
        "eth_block_number": s.get("eth_block_number"),
        "gas_used": s.get("gas_used"),
    }
    upsert(conn, "slots", row)

    for et in s.get("execution_times") or []:
        ct = et.get("client_type")
        if not ct:
            continue
        et_row: dict[str, Any] = {
            "devnet": devnet,
            "slot": slot_num,
            "client_type": ct,
            "count": et.get("count"),
            "avg_time": et.get("avg_time"),
            "min_time": et.get("min_time"),
            "max_time": et.get("max_time"),
        }
        upsert(conn, "slot_exec_times", et_row)

    return slot_num


def collect_blobs(
    devnet: str,
    since_slot: int | None = None,
    min_slot: int | None = None,
    max_slot_param: int | None = None,
    limit_pages: int | None = None,
) -> None:
    """
    Page through Dora slots and upsert into the `slots` and `slot_exec_times` tables.

    Modes:
    - Incremental (default, no args): stops once slot numbers fall to or below
      the stored watermark (max_slot). since_slot overrides the watermark.
    - Bounded range: if min_slot and/or max_slot_param are given, passes them
      as min_slot/max_slot query params to Dora and pages through that range
      without applying the incremental watermark.

    Resilient to missing fields in the Dora response.
    """
    entry = devnet_entry(devnet)
    dora_base = entry.get("dora_base", "").rstrip("/")
    if not dora_base:
        logger.error("dv collect blobs: dora_base missing for devnet %s", devnet)
        return

    conn = connect()
    migrate(conn)

    bounded = min_slot is not None or max_slot_param is not None
    if bounded:
        watermark = None  # range mode: no incremental stop
    else:
        watermark = since_slot if since_slot is not None else max_slot(conn, devnet)

    url = f"{dora_base}/api/v1/slots"
    params: dict[str, Any] = {
        "limit": _PAGE_SIZE,
        "with_missing": 1,
        "with_orphaned": 1,
    }
    if min_slot is not None:
        params["min_slot"] = min_slot
    if max_slot_param is not None:
        params["max_slot"] = max_slot_param

    new_slots = 0
    min_seen: int | None = None
    max_seen: int | None = None
    page = 0

    while True:
        if limit_pages is not None and page >= limit_pages:
            break

        params["page"] = page
        resp = _get_with_backoff(url, params)
        if resp is None:
            logger.error("dora fetch failed at page %d; stopping", page)
            break

        data = resp.json().get("data", {})
        slots = data.get("slots") or []
        if not slots:
            break

        stop = False
        for s in slots:
            slot_num = s.get("slot")
            if slot_num is None:
                continue

            # Incremental stop (only in non-bounded mode)
            if not bounded and watermark is not None and slot_num <= watermark:
                stop = True
                break

            inserted = _upsert_slot(conn, devnet, s)
            if inserted is not None:
                new_slots += 1
                if min_seen is None or inserted < min_seen:
                    min_seen = inserted
                if max_seen is None or inserted > max_seen:
                    max_seen = inserted

        conn.commit()

        if stop:
            break

        next_page = data.get("next_page")
        if next_page is None or not slots:
            break

        page = int(next_page)

    conn.close()

    slot_range = f"{min_seen}-{max_seen}" if min_seen is not None else "none"
    logger.info(
        "collect_blobs(%s): %d new slots inserted, slot range %s",
        devnet,
        new_slots,
        slot_range,
    )
    print(
        f"collect_blobs({devnet}): {new_slots} new slots, "
        f"slot range {slot_range}"
    )


def backfill(devnet: str, from_slot: int, to_slot: int) -> None:
    """
    Range-collect slots [from_slot, to_slot] into the slots + slot_exec_times
    tables using min_slot/max_slot paging. Prints progress.
    """
    entry = devnet_entry(devnet)
    dora_base = entry.get("dora_base", "").rstrip("/")
    if not dora_base:
        logger.error("backfill: dora_base missing for devnet %s", devnet)
        return

    conn = connect()
    migrate(conn)

    url = f"{dora_base}/api/v1/slots"
    params: dict[str, Any] = {
        "limit": _PAGE_SIZE,
        "with_missing": 1,
        "with_orphaned": 1,
        "min_slot": from_slot,
        "max_slot": to_slot,
    }

    new_slots = 0
    min_seen: int | None = None
    max_seen: int | None = None
    page = 0

    print(f"backfill({devnet}): fetching slots {from_slot}-{to_slot} ...")

    while True:
        params["page"] = page
        resp = _get_with_backoff(url, params)
        if resp is None:
            logger.error("backfill: dora fetch failed at page %d; stopping", page)
            break

        data = resp.json().get("data", {})
        slots = data.get("slots") or []
        if not slots:
            break

        for s in slots:
            inserted = _upsert_slot(conn, devnet, s)
            if inserted is not None:
                new_slots += 1
                if min_seen is None or inserted < min_seen:
                    min_seen = inserted
                if max_seen is None or inserted > max_seen:
                    max_seen = inserted

        conn.commit()
        print(f"  page {page}: {len(slots)} slots (total so far: {new_slots})")

        next_page = data.get("next_page")
        if next_page is None or not slots:
            break

        page = int(next_page)

    conn.close()

    slot_range = f"{min_seen}-{max_seen}" if min_seen is not None else "none"
    print(
        f"backfill({devnet}): done. {new_slots} slots inserted, range {slot_range}"
    )
