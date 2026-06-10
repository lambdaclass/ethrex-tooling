"""Peer inspection: peer count, inbound/outbound split, client mix, body-serving failures."""

from __future__ import annotations

import sys

from .config import host_of
from .remote import PEERS_PROBE
from .ssh import run_remote


def _parse_peers_tsv(output: str) -> dict:
    """
    Parse PEERS_PROBE TSV output into a structured dict.

    Expected lines:
      peercount<TAB><N>
      total<TAB><N>
      inbound<TAB><N>
      outbound<TAB><N>
      client<TAB><name><TAB><count>   (zero or more)
      bodyfail<TAB><N>
    """
    data: dict = {
        "peercount": "?",
        "total": "?",
        "inbound": "?",
        "outbound": "?",
        "clients": {},
        "bodyfail": "?",
    }
    for line in output.splitlines():
        parts = line.split("\t")
        if not parts:
            continue
        key = parts[0].strip()
        if key == "client" and len(parts) >= 3:
            name = parts[1]
            count = parts[2].strip()
            data["clients"][name] = count
        elif key in ("peercount", "total", "inbound", "outbound", "bodyfail") and len(parts) >= 2:
            data[key] = parts[1].strip()
    return data


def peers(devnet: str, node: str) -> None:
    """Run PEERS_PROBE on a specific node and print the peer report."""
    host = host_of(devnet, node)
    try:
        result = run_remote(host, PEERS_PROBE, timeout=30)
    except Exception as exc:
        print(f"error: SSH to {host} failed: {exc}", file=sys.stderr)
        sys.exit(1)

    if result.returncode != 0:
        print(
            f"error: peers probe failed (exit {result.returncode}):\n{result.stderr.strip()}",
            file=sys.stderr,
        )
        sys.exit(1)

    d = _parse_peers_tsv(result.stdout)

    print(f"### {node}")
    print(f"  peercount (eth_peerCount): {d['peercount']}")
    print(f"  admin_peers total:         {d['total']}")
    print(f"  inbound:                   {d['inbound']}")
    print(f"  outbound:                  {d['outbound']}")
    print()
    if d["clients"]:
        print("  Client mix:")
        for name, count in sorted(d["clients"].items(), key=lambda kv: -int(kv[1]) if kv[1].isdigit() else 0):
            print(f"    {count:>4}  {name}")
    else:
        print("  Client mix: (no admin_peers data)")
    print()
    print(f"  Body-serving failures (last 60s): {d['bodyfail']}")
