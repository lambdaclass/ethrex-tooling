"""
Discover devnet roster, image tag, and fork schedule from the ethpandaops devnet repo.
Writes config/devnets/<name>.yaml via config.write_cache.
Uses 'gh api' (subprocess) to fetch file contents from GitHub.
"""

from __future__ import annotations

import base64
import configparser
import io
import json
import subprocess
import sys
import time
from typing import Any

from .config import devnet_entry, write_cache


def _gh_api_content(repo: str, path: str) -> bytes | None:
    """
    Fetch file contents from GitHub via 'gh api repos/<repo>/contents/<path>'.
    Returns the decoded bytes, or None on 404/error (with a warning printed).
    """
    api_path = f"repos/{repo}/contents/{path}"
    try:
        result = subprocess.run(
            ["gh", "api", api_path, "--jq", ".content"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        print("error: 'gh' CLI not found; install GitHub CLI to use 'dv discover'", file=sys.stderr)
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print(f"warning: gh api timeout fetching {api_path}", file=sys.stderr)
        return None

    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "404" in stderr or "Not Found" in stderr:
            print(f"warning: 404 fetching {api_path}", file=sys.stderr)
        else:
            print(f"warning: gh api error for {api_path}: {stderr[:200]}", file=sys.stderr)
        return None

    # GitHub API returns base64-encoded content (with newlines); decode it
    encoded = result.stdout.strip()
    # Remove embedded newlines that GitHub adds to the base64 output
    encoded_clean = encoded.replace("\\n", "").replace("\n", "")
    try:
        return base64.b64decode(encoded_clean)
    except Exception as exc:
        print(f"warning: failed to decode base64 for {api_path}: {exc}", file=sys.stderr)
        return None


def _parse_inventory(ini_bytes: bytes) -> list[str]:
    """
    Parse an Ansible inventory.ini and extract ethrex node names.

    Looks for [ethrex:children] group to get child group names, then collects
    host entries from each child group.
    """
    text = ini_bytes.decode("utf-8", errors="replace")

    # configparser doesn't handle Ansible's [group:children] sections well,
    # so we parse manually.
    children_groups: list[str] = []
    current_section: str | None = None
    group_members: dict[str, list[str]] = {}

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(";") or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current_section = line[1:-1]
            if current_section not in group_members:
                group_members[current_section] = []
            continue
        if current_section is not None:
            # For [ethrex:children], each entry is a child group name
            if current_section == "ethrex:children":
                # line may be just a group name (possibly with ansible vars)
                group_name = line.split()[0]
                children_groups.append(group_name)
            else:
                # For regular groups, each entry is a hostname (optionally with vars)
                host = line.split()[0]
                group_members.setdefault(current_section, []).append(host)

    # Collect all hosts from the child groups
    nodes: list[str] = []
    seen: set[str] = set()
    for grp in children_groups:
        for host in group_members.get(grp, []):
            if host not in seen:
                seen.add(host)
                nodes.append(host)

    return nodes


def _parse_images_yaml(yaml_bytes: bytes) -> str | None:
    """
    Extract the ethrex image tag from images.yaml.
    Looks for the 'ethrex:' key under default_ethereum_client_images.
    Returns the tag string or None.
    """
    text = yaml_bytes.decode("utf-8", errors="replace")
    # Simple line-based search: find a line containing 'ethrex:' (with optional spaces)
    # after stripping quotes
    in_client_images = False
    for line in text.splitlines():
        stripped = line.strip()
        if "default_ethereum_client_images" in stripped:
            in_client_images = True
            continue
        if in_client_images:
            # If we hit another top-level key (no leading spaces), stop
            if stripped and not line.startswith(" ") and not line.startswith("\t"):
                in_client_images = False
                continue
            if stripped.startswith("ethrex:"):
                value = stripped[len("ethrex:"):].strip().strip('"').strip("'")
                if value:
                    return value
    return None


def _parse_fork_schedule(genesis_bytes: bytes) -> tuple[int | None, dict[str, dict]]:
    """
    Parse genesis.json to extract chainId and fork schedule.

    Returns (chain_id, fork_schedule) where fork_schedule maps fork name to
    {activation_ts, blob_target, blob_max}.
    """
    text = genesis_bytes.decode("utf-8", errors="replace")
    try:
        genesis = json.loads(text)
    except json.JSONDecodeError as exc:
        print(f"warning: failed to parse genesis.json: {exc}", file=sys.stderr)
        return None, {}

    config = genesis.get("config", {})

    # chain_id
    chain_id = config.get("chainId") or genesis.get("chainId")
    if chain_id is not None:
        chain_id = int(chain_id)

    # Fork timestamps: config keys ending in "Time" (excluding terminalTotalDifficultyPassed etc.)
    fork_ts: dict[str, int] = {}
    for key, val in config.items():
        if key.endswith("Time") and isinstance(val, (int, str)):
            fork_name = key[: -len("Time")]
            # Normalize to lower camel -> lowercase (e.g. cancunTime -> cancun)
            # Just lowercase the first char for simple cases
            name = fork_name[0].lower() + fork_name[1:] if fork_name else fork_name
            try:
                fork_ts[name] = int(val)
            except (ValueError, TypeError):
                pass

    # blobSchedule: maps fork name to {target, max}
    blob_schedule: dict[str, dict[str, int]] = {}
    raw_blob = config.get("blobSchedule", {})
    for fork, vals in raw_blob.items():
        blob_schedule[fork] = {
            "target": int(vals.get("target", 0)),
            "max": int(vals.get("max", 0)),
        }

    # Build fork_schedule dict
    fork_schedule: dict[str, dict] = {}
    for fork, ts in fork_ts.items():
        entry: dict[str, Any] = {"activation_ts": ts}
        if fork in blob_schedule:
            entry["blob_target"] = blob_schedule[fork]["target"]
            entry["blob_max"] = blob_schedule[fork]["max"]
        fork_schedule[fork] = entry

    # Add any blob-schedule forks that didn't have a *Time key
    for fork, blob in blob_schedule.items():
        if fork not in fork_schedule:
            fork_schedule[fork] = {
                "activation_ts": 0,
                "blob_target": blob["target"],
                "blob_max": blob["max"],
            }

    return chain_id, fork_schedule


def discover(devnet: str) -> None:
    """
    Fetch roster, image tag, and fork schedule from the ethpandaops devnet repo.
    Writes config/devnets/<devnet>.yaml.
    """
    entry = devnet_entry(devnet)
    devnets_repo: str = entry["devnets_repo"]
    repo_path: str = entry["repo_path"]

    print(f"Discovering {devnet} from {devnets_repo} (path: {repo_path})...")

    # --- Fetch inventory.ini ---
    ini_path = f"ansible/inventories/{repo_path}/inventory.ini"
    ini_bytes = _gh_api_content(devnets_repo, ini_path)

    nodes: list[str] = []
    if ini_bytes is None:
        # Try with full devnet name as fallback
        ini_path_fallback = f"ansible/inventories/{devnet}/inventory.ini"
        print(f"  Trying fallback path: {ini_path_fallback}", file=sys.stderr)
        ini_bytes = _gh_api_content(devnets_repo, ini_path_fallback)

    if ini_bytes:
        nodes = _parse_inventory(ini_bytes)
        print(f"  Found {len(nodes)} ethrex nodes: {', '.join(nodes)}")
    else:
        print("warning: could not fetch inventory.ini; roster will be empty", file=sys.stderr)

    # --- Fetch images.yaml ---
    images_path = f"ansible/inventories/{repo_path}/group_vars/all/images.yaml"
    images_bytes = _gh_api_content(devnets_repo, images_path)

    image_tag: str = ""
    if images_bytes:
        image_tag = _parse_images_yaml(images_bytes) or ""
        if image_tag:
            print(f"  Image tag: {image_tag}")
        else:
            print("warning: ethrex image tag not found in images.yaml", file=sys.stderr)
    else:
        print("warning: could not fetch images.yaml; image_tag will be empty", file=sys.stderr)

    # --- Fetch genesis.json ---
    genesis_path = f"network-configs/{repo_path}/metadata/genesis.json"
    genesis_bytes = _gh_api_content(devnets_repo, genesis_path)

    chain_id: int | None = None
    fork_schedule: dict[str, dict] = {}
    if genesis_bytes:
        chain_id, fork_schedule = _parse_fork_schedule(genesis_bytes)
        if chain_id is not None:
            print(f"  Chain ID: {chain_id}")
        forks_found = list(fork_schedule.keys())
        print(f"  Fork schedule keys: {', '.join(forks_found)}")
    else:
        print("warning: could not fetch genesis.json; fork_schedule will be empty", file=sys.stderr)

    # --- Build cache data ---
    cache: dict[str, Any] = {
        "discovered_at": int(time.time()),
        "image_tag": image_tag,
        "roster": [{"name": n, "verified": False} for n in nodes],
        "fork_schedule": fork_schedule,
    }
    if chain_id is not None:
        cache["chain_id"] = chain_id

    write_cache(devnet, cache)
    print(f"Written: config/devnets/{devnet}.yaml")
