"""Status sweep: run STATUS_PROBE per node, parse TSV, format output."""

from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .config import host_of, node_list
from .remote import STATUS_PROBE
from .ssh import run_remote


def _parse_tsv(output: str) -> dict[str, str]:
    """Parse TSV key<TAB>value lines into a dict."""
    result: dict[str, str] = {}
    for line in output.splitlines():
        if "\t" in line:
            key, _, value = line.partition("\t")
            result[key.strip()] = value
    return result


# Per-node probe timeout. A healthy probe (ssh + docker inspect + RPC + log tail)
# is ~1.5s; 5s leaves headroom while failing a wedged node fast.
PROBE_TIMEOUT = 5


def _probe_node(devnet: str, node: str) -> dict[str, Any]:
    """Run STATUS_PROBE on one node and return a parsed dict (with 'node' added)."""
    host = host_of(devnet, node)
    try:
        result = run_remote(host, STATUS_PROBE, timeout=PROBE_TIMEOUT)
        if result.returncode != 0:
            return {
                "node": node,
                "_error": f"ssh exit {result.returncode}: {result.stderr.strip()[:200]}",
            }
        data = _parse_tsv(result.stdout)
        data["node"] = node
        return data
    except Exception as exc:  # includes TimeoutExpired, OSError
        return {"node": node, "_error": str(exc)[:200]}


def _human_line(d: dict[str, Any]) -> str:
    """Format a probe result as the human-readable status line."""
    node = d.get("node", "?")
    if "_error" in d:
        return f"### {node}\n  ERROR: {d['_error']}"

    image = d.get("image", "")
    status = d.get("status", "?")
    restart = d.get("restart", "0")
    buildnum = d.get("buildnum", "")
    commit = d.get("commit", "")
    head = d.get("head", "?")
    peers = d.get("peers", "?")
    syncing = d.get("syncing", "?")
    state_at_head = d.get("state_at_head", "?")
    watchtower = d.get("watchtower", "?")
    cl_line = d.get("cl_line", "")

    # Warn if the live image is not ethrex
    image_note = ""
    if image and "ethrex" not in image.lower():
        image_note = f"  [WARNING: image is not ethrex: {image}]\n"

    build_str = f"bn{buildnum}/{commit}" if buildnum or commit else ""
    el_line = (
        f"  EL: {status}/r{restart} {build_str}  "
        f"head={head}  peers={peers}  "
        f"state@head={state_at_head}  {syncing}  wt={watchtower}"
    )
    cl_disp = f"  CL: {cl_line}" if cl_line else "  CL: (no recent sync lines)"

    return f"### {node}\n{image_note}{el_line}\n{cl_disp}"


def gather(devnet: str, node_arg: str | None) -> list[dict[str, Any]]:
    """
    Run the status probe across nodes and return a list of per-node dicts.
    Does not print anything; callers use the returned data directly.
    This is the clean seam for the health collector.
    """
    nodes = node_list(devnet, node_arg)
    results: list[dict[str, Any]] = [None] * len(nodes)  # type: ignore[list-item]

    # Fire the whole roster at once (capped at 16) so one wedged node can't
    # serialize behind a worker slot; the slow node's timeout no longer gates
    # nodes waiting for a free worker.
    with ThreadPoolExecutor(max_workers=min(16, max(1, len(nodes)))) as pool:
        futures = {pool.submit(_probe_node, devnet, n): i for i, n in enumerate(nodes)}
        for fut in as_completed(futures):
            idx = futures[fut]
            results[idx] = fut.result()

    return results


def status(devnet: str, node_arg: str | None, as_json: bool) -> None:
    """Run status probe across nodes and print results."""
    # Check cache age
    from .config import load_cache
    cache = load_cache(devnet)
    if cache:
        discovered_at = cache.get("discovered_at", 0)
        age_hours = (time.time() - discovered_at) / 3600
        if age_hours > 24:
            print(
                f"warning: cache for '{devnet}' is {age_hours:.0f}h old; "
                f"run 'dv discover {devnet}' to refresh",
                file=sys.stderr,
            )

    results = gather(devnet, node_arg)

    for d in results:
        if as_json:
            print(json.dumps(d))
        else:
            print(_human_line(d))
