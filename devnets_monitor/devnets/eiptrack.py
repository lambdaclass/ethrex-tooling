"""EIP implementation-status tracker.

Reads fork_eips (eip, title, stage, status) and groups by implementation status
to give a quick summary of ethrex's coverage for an upcoming fork.

Status values (set by operator in config/eips.json):
  done        - implemented and passing tests
  in_progress - work underway
  missing     - not yet started
  n/a         - not applicable to the EL (e.g. pure CL EIPs)
  unknown     - not yet assessed (default when absent)
"""

from __future__ import annotations

from typing import Any

from .store import connect, migrate


_STATUS_ORDER = ["done", "in_progress", "missing", "n/a", "unknown"]
_STATUS_LABELS = {
    "done": "Done",
    "in_progress": "In Progress",
    "missing": "Missing",
    "n/a": "N/A - CL-only",
    "unknown": "Unknown",
}


def get_eiptrack_data(
    devnet: str, fork: str = "amsterdam"
) -> dict[str, Any] | None:
    """
    Return EIP implementation-status data for dashboard rendering.

    Keys:
      fork           str         fork name queried
      groups         list[dict]  one entry per status value, ordered by _STATUS_ORDER
        status       str         status key
        label        str         display label
        count        int         number of EIPs with this status
        eips         list[dict]  {eip, title, stage, status}
      total          int         total EIPs for this fork
      stage_counts   dict        {SFI: N, CFI: N, PFI: N, None: N}
    """
    conn = connect()
    migrate(conn)

    rows = conn.execute(
        """
        SELECT eip, title, stage, status
        FROM fork_eips
        WHERE devnet = ? AND fork = ?
        ORDER BY eip ASC
        """,
        (devnet, fork),
    ).fetchall()
    conn.close()

    if not rows:
        return None

    # Group by status
    groups_map: dict[str, list[dict]] = {s: [] for s in _STATUS_ORDER}
    stage_counts: dict[str | None, int] = {}

    for r in rows:
        status = r["status"] or "unknown"
        if status not in groups_map:
            groups_map[status] = []
        groups_map[status].append({
            "eip": r["eip"],
            "title": r["title"] or "",
            "stage": r["stage"] or "",
            "status": status,
        })
        stage = r["stage"] or "none"
        stage_counts[stage] = stage_counts.get(stage, 0) + 1

    groups = []
    for status in _STATUS_ORDER:
        eips = groups_map.get(status, [])
        groups.append({
            "status": status,
            "label": _STATUS_LABELS.get(status, status),
            "count": len(eips),
            "eips": eips,
        })

    return {
        "fork": fork,
        "groups": groups,
        "total": len(rows),
        "stage_counts": stage_counts,
    }


def show_eiptrack(devnet: str, fork: str = "amsterdam") -> None:
    """Print EIP status summary to stdout."""
    data = get_eiptrack_data(devnet, fork=fork)
    if data is None:
        print(
            f"eip-track({devnet}): no fork_eips data for fork '{fork}'. "
            f"Run: dv collect {devnet} forks"
        )
        return

    print(f"\nEIP implementation status -- {devnet} / {data['fork']}")
    print(f"Total EIPs: {data['total']}")

    stage_parts = [f"{s}={n}" for s, n in sorted(data["stage_counts"].items())]
    print(f"Stages: {', '.join(stage_parts)}\n")

    for group in data["groups"]:
        if not group["eips"] and group["status"] == "unknown":
            # Show unknown even if empty so the table is always complete
            pass
        count_label = f"{group['label']} ({group['count']})"
        print(f"  {count_label}")
        for e in group["eips"]:
            stage = f"[{e['stage']}]" if e["stage"] else ""
            print(f"    EIP-{e['eip']:5d} {stage:<6} {e['title']}")
        print()
