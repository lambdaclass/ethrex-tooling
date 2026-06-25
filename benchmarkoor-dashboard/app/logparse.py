"""Parse ethrex per-test phase timings + fkv catch-up from benchmarkoor run logs.

`benchmarkoor.log` interleaves the executor markers (`Running test step ... test=X`)
with the client's own stdout (`[METRIC] BLOCK ...` headers + `|- exec/merkle/store`
breakdown lines, and the FlatKeyValue generator lines). So a single stream gives
us, per benchmark test, the phase split of its block, plus a per-run fkv summary.

Nothing is stored to disk: callers stream the log line-by-line through `parse`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

import httpx

from . import config
from .client import RETRY_STATUS

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_TEST_STEP = re.compile(r"Running test step .*?\btest=(.+\.txt)\s*$")
_SETUP_STEP = re.compile(r"Running (?:setup|pre_run) step ")
_HEADER = re.compile(
    r"\[METRIC\] BLOCK (\d+)(?:\s+0x[0-9a-fA-F]+)? \| "
    r"([0-9.]+) Ggas/s \| ([0-9.]+) ms \| (\d+) txs \| ([0-9.]+) Mgas"
)
_PHASE = re.compile(r"\|-\s*(exec|merkle|store):\s*([0-9.]+)\s*ms")
_MERKLE_DETAIL = re.compile(r"drain:\s*([0-9.]+)\s*ms.*?overlap:\s*(\d+)%")
_FKV_STARTED = "Generation of FlatKeyValue started."
_FKV_SKIP = "FlatKeyValue already generated. Skipping."
_FKV_DONE = "FlatKeyValue generation finished."


@dataclass
class TestPhase:
    test_name: str
    total_ms: float = 0.0
    exec_ms: float = 0.0
    merkle_ms: float = 0.0
    store_ms: float = 0.0
    merkle_drain_ms: float = 0.0
    merkle_overlap_pct: float = 0.0
    bottleneck: str = ""

    def finalize_bottleneck(self) -> None:
        phases = {
            "exec": self.exec_ms,
            "merkle": self.merkle_ms,
            "store": self.store_ms,
        }
        if not self.bottleneck and any(phases.values()):
            self.bottleneck = max(phases, key=phases.get)


@dataclass
class ParseResult:
    tests: dict[str, TestPhase] = field(default_factory=dict)
    fkv_started: int = 0
    fkv_skipping: int = 0
    fkv_finished: int = 0


def parse(lines: Iterable[str]) -> ParseResult:
    res = ParseResult()
    current_test: str | None = None
    in_test_step = False
    pending: TestPhase | None = None

    def flush() -> None:
        nonlocal pending
        if pending is not None:
            pending.finalize_bottleneck()
            res.tests[pending.test_name] = pending
            pending = None

    for raw in lines:
        line = _ANSI.sub("", raw)

        if _FKV_STARTED in line:
            res.fkv_started += 1
            continue
        if _FKV_SKIP in line:
            res.fkv_skipping += 1
            continue
        if _FKV_DONE in line:
            res.fkv_finished += 1
            continue

        m = _TEST_STEP.search(line)
        if m:
            flush()
            current_test = m.group(1).strip()
            in_test_step = True
            continue
        if _SETUP_STEP.search(line):
            flush()
            in_test_step = False
            continue

        if not in_test_step or current_test is None:
            continue

        m = _HEADER.search(line)
        if m:
            # one benchmark block per test step; start fresh on its header
            pending = TestPhase(
                test_name=current_test,
                total_ms=float(m.group(3)),
            )
            continue

        if pending is not None:
            m = _PHASE.search(line)
            if m:
                ms = float(m.group(2))
                phase = m.group(1)
                setattr(pending, f"{phase}_ms", ms)
                if "<< BOTTLENECK" in line:
                    pending.bottleneck = phase
                if phase == "merkle":
                    d = _MERKLE_DETAIL.search(line)
                    if d:
                        pending.merkle_drain_ms = float(d.group(1))
                        pending.merkle_overlap_pct = float(d.group(2))
                # store is the last mapped phase -> this block is done
                if phase == "store":
                    flush()

    flush()
    return res


def stream_run_log(run_id: str, name: str = "benchmarkoor.log") -> Iterable[str]:
    """Yield lines of a run's log, streamed from the API (never held on disk)."""
    url = (
        f"{config.API_BASE}/api/v1/files/repricings/results/runs/"
        f"{run_id}/{name}?redirect=true"
    )
    headers = {"Authorization": f"Bearer {config.require_key()}"}
    with httpx.Client(timeout=120.0, follow_redirects=True) as c:
        with c.stream("GET", url, headers=headers) as resp:
            if resp.status_code in RETRY_STATUS or resp.status_code == 404:
                resp.read()
                return
            resp.raise_for_status()
            yield from resp.iter_lines()
