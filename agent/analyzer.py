"""Analyze metrics and recommend cache tuning."""

import math
import statistics
from collections import deque
from typing import Deque, Dict, List, Optional


def _least_squares_slope(y: List[float]) -> float:
    """Slope of y over indices 0..n-1 (per-index units)."""
    n = len(y)
    if n < 2:
        return 0.0
    xs = list(range(n))
    x_mean = (n - 1) / 2.0
    y_mean = sum(y) / n
    denom = sum((x - x_mean) ** 2 for x in xs)
    if denom == 0:
        return 0.0
    return sum((x - x_mean) * (yi - y_mean) for x, yi in zip(xs, y)) / denom


class WorkloadAnalyzer:
    """Sliding-window analysis of CachePilot metric snapshots.

    Keeps the last ``window_size`` snapshots (default 20) and derives
    hit-rate trend, churn, volatility, and a workload classification.
    """

    #: Churn (evictions per window) at or above this is considered "high".
    HIGH_CHURN_PER_WINDOW = 5.0
    #: Coefficient of variation of hit_rate above this is "volatile".
    VOLATILITY_THRESHOLD = 0.2
    #: Zipf exponent above this (with a healthy hit rate) implies skew.
    SKEW_ZIPF = 1.2
    #: Minimum hit rate required to trust the skew signal.
    SKEW_MIN_HIT_RATE = 0.5
    #: Sequential-transition ratio above this implies a scan.
    SCAN_RATIO = 0.4

    def __init__(self, window_size: int = 20) -> None:
        self._window: Deque[Dict] = deque(maxlen=window_size)

    def add_snapshot(self, metrics: dict) -> None:
        """Append one metrics snapshot to the sliding window."""
        self._window.append(dict(metrics))

    def snapshots(self) -> List[Dict]:
        """Copy of the current window, oldest first."""
        return list(self._window)

    def latest(self) -> Optional[Dict]:
        """Most recent snapshot, or None while the window is empty."""
        return self._window[-1] if self._window else None

    def compute_hit_rate_trend(self) -> float:
        """Slope of hit_rate over the last 5 windows (per-window units)."""
        rates = [float(m.get("hit_rate", 0.0)) for m in self._window]
        return _least_squares_slope(rates[-5:])

    def compute_churn_rate(self) -> float:
        """Evictions per window: mean delta of the cumulative counter."""
        evictions = [int(m.get("evictions", 0)) for m in self._window]
        if len(evictions) < 2:
            return 0.0
        deltas = [b - a for a, b in zip(evictions, evictions[1:])]
        return sum(deltas) / len(deltas)

    def compute_hit_rate_volatility(self) -> float:
        """Coefficient of variation of hit_rate across the window."""
        rates = [float(m.get("hit_rate", 0.0)) for m in self._window]
        if len(rates) < 2:
            return 0.0
        mean = statistics.fmean(rates)
        if mean == 0:
            return 0.0
        return statistics.pstdev(rates) / mean

    def classify_workload(
        self, zipf: float = 0.0, scan_ratio: float = 0.0
    ) -> str:
        """Classify the workload as skewed|scanning|stable|bursty.

        Rules, in priority order:
          scanning -> sequential_key_ratio > 0.4
          skewed   -> zipf > 1.2 AND hit_rate > 0.5
          bursty   -> high churn AND volatile hit_rate
          stable   -> low churn AND steady hit_rate (also the fallback)
        """
        latest = self.latest() or {}
        hit_rate = float(latest.get("hit_rate", 0.0))
        churn = self.compute_churn_rate()
        volatile = self.compute_hit_rate_volatility()
        high_churn = churn >= self.HIGH_CHURN_PER_WINDOW
        steady = volatile <= self.VOLATILITY_THRESHOLD

        if scan_ratio > self.SCAN_RATIO:
            return "scanning"
        if zipf > self.SKEW_ZIPF and hit_rate > self.SKEW_MIN_HIT_RATE:
            return "skewed"
        if high_churn and not steady:
            return "bursty"
        return "stable"
