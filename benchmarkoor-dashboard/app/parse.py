"""Parsing helpers for suite names and test names."""

from __future__ import annotations

import re

_FORK_RE = re.compile(r"fork_([A-Za-z0-9]+)")
_BENCH_RE = re.compile(r"benchmark_(\d+)M")
_FILE_RE = re.compile(r"^(.*?\.py)__")
# suite name: <network>-<block>[-<fork>]-<variant>
_KNOWN_VARIANTS = ("stateful-bloat", "compute", "stateful")
_KNOWN_FORKS = ("amsterdam", "osaka", "prague", "cancun")


def parse_suite_name(name: str) -> dict[str, str | None]:
    """`jochemnet-24402727-amsterdam-compute` -> network/block/fork/variant."""
    variant = next((v for v in _KNOWN_VARIANTS if name.endswith("-" + v)), None)
    rest = name[: -(len(variant) + 1)] if variant else name
    fork = next((f for f in _KNOWN_FORKS if rest.endswith("-" + f)), None)
    if fork:
        rest = rest[: -(len(fork) + 1)]
    parts = rest.rsplit("-", 1)
    network = parts[0] if len(parts) == 2 else rest
    block = parts[1] if len(parts) == 2 else None
    return {"network": network, "block": block, "fork": fork, "variant": variant}


def parse_test_name(name: str) -> dict[str, object]:
    """Extract file, fork, and benchmark gas (M) from a test_stats test_name."""
    file_m = _FILE_RE.match(name)
    fork_m = _FORK_RE.search(name)
    bench_m = _BENCH_RE.search(name)
    return {
        "file": file_m.group(1) if file_m else None,
        "fork": fork_m.group(1) if fork_m else None,
        "benchmark_mgas": int(bench_m.group(1)) if bench_m else None,
    }
