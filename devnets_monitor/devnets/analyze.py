"""Comparative / baseline math helpers.

Pure Python, no dependencies. Used by detectors and any relative analysis
that needs peer ratios, self-regression ratios, or z-scores.
"""

from __future__ import annotations

import math


def median(xs: list[float]) -> float | None:
    """Return the median of xs, or None if xs is empty."""
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return float(s[mid])
    return (s[mid - 1] + s[mid]) / 2.0


def peer_ratio(target: float, peers: list[float]) -> float | None:
    """Return target / median(peers).

    None if peers is empty or median is 0.
    """
    m = median(peers)
    if m is None or m == 0:
        return None
    return target / m


def baseline_shift(recent: list[float], prior: list[float]) -> float | None:
    """Return mean(recent) / mean(prior): the self-regression ratio.

    None if prior is empty or mean(prior) is 0.
    """
    if not prior or not recent:
        return None
    prior_mean = sum(prior) / len(prior)
    if prior_mean == 0:
        return None
    return (sum(recent) / len(recent)) / prior_mean


def zscore(value: float, sample: list[float]) -> float | None:
    """Return (value - mean(sample)) / stdev(sample).

    None if fewer than 2 samples or stdev is 0.
    """
    if len(sample) < 2:
        return None
    n = len(sample)
    mean = sum(sample) / n
    variance = sum((x - mean) ** 2 for x in sample) / (n - 1)
    std = math.sqrt(variance)
    if std == 0:
        return None
    return (value - mean) / std
