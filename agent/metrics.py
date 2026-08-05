"""Collect runtime metrics from the CachePilot server."""

import math
from typing import Dict, List

import requests


def fetch_metrics(
    host: str = "localhost", port: int = 8080, timeout: float = 2.0
) -> dict:
    """Poll the CachePilot admin HTTP endpoint and return the metrics JSON."""
    url = f"http://{host}:{port}/metrics"
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def compute_zipf_coefficient(key_access_counts: Dict[str, int]) -> float:
    """Estimate the Zipf exponent s from per-key access counts.

    Fits log(count) = a - s * log(rank) by least squares over the ranked
    keys (most-frequent first). Returns 0.0 when fewer than 2 distinct
    keys are observed or the fit degenerates.
    """
    counts = sorted((int(c) for c in key_access_counts.values()), reverse=True)
    pairs = [
        (math.log(i + 1), math.log(c))
        for i, c in enumerate(counts)
        if c > 0
    ]
    if len(pairs) < 2:
        return 0.0
    n = len(pairs)
    sx = sum(x for x, _ in pairs)
    sy = sum(y for _, y in pairs)
    sxx = sum(x * x for x, _ in pairs)
    sxy = sum(x * y for x, y in pairs)
    denom = n * sxx - sx * sx
    if denom == 0:
        return 0.0
    return -(n * sxy - sx * sy) / denom


def detect_scan_pattern(access_history: List[str]) -> float:
    """Fraction of consecutive accesses that advance sequentially.

    A transition is sequential when both keys parse as integers and the
    next key equals the previous plus one (key N -> N+1). Returns 0.0 for
    histories with fewer than 2 entries.
    """
    if len(access_history) < 2:
        return 0.0
    sequential = 0
    total = 0
    for prev, cur in zip(access_history, access_history[1:]):
        total += 1
        try:
            if int(cur) == int(prev) + 1:
                sequential += 1
        except (TypeError, ValueError):
            pass
    return sequential / total
