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


def physics_proposal(
    evictions_so_far: int,
    churn_per_window: float,
    capacity_keys: int,
    burst_keys: int,
    decide_every: int = 5000,
) -> tuple:
    """Deterministic eviction-physics signal: is the burst pool at risk?

    Under cold churn every policy's eviction frontier walks the preload
    insertion order, so the burst pool (the last ``burst_keys`` inserted
    keys) is doomed once ``evictions_so_far`` approaches
    ``capacity_keys - burst_keys``.  This computes how far the frontier is
    from the pool (in evictions) and when it will get there (in requests,
    from the churn rate), turning that into a leading preemptive proposal.

    Returns ``(proposal, eta_requests, frontier_progress)`` where
    ``proposal`` is ``"sieve"`` when the pool will be breached before the
    next decision window (``eta < decide_every``) and ``None`` otherwise
    (no signal / too late to matter / physics unavailable).
    """
    if (capacity_keys <= 0 or burst_keys <= 0
            or burst_keys >= capacity_keys or churn_per_window <= 0
            or evictions_so_far < 0):
        return None, None, None
    pool_offset = capacity_keys - burst_keys
    frontier = evictions_so_far / capacity_keys
    remaining = pool_offset - evictions_so_far
    if remaining <= 0:
        # Frontier already in the pool: switching now cannot save it.
        return None, 0.0, round(frontier, 3)
    eta_requests = remaining * decide_every / churn_per_window
    proposal = "sieve" if eta_requests < decide_every else None
    return proposal, round(eta_requests, 1), round(frontier, 3)


class WorkloadAnalyzer:
    """Sliding-window analysis of AdaptiCache metric snapshots.

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

    def rule_confidence(self, workload: str, zipf: float = 0.0,
                        scan_ratio: float = 0.0) -> float:
        """How far the dominant signal sits from its threshold (0..1).

        1.0 = far past the threshold (confident classification), ~0.5 =
        right at the threshold (noisy).  Used to gate LLM consults in
        hybrid mode: the LLM only weighs in on uncertain proposals.
        """
        churn = self.compute_churn_rate()
        if workload == "scanning":
            return min(1.0, scan_ratio / self.SCAN_RATIO)
        if workload == "skewed":
            return min(1.0, zipf / self.SKEW_ZIPF)
        if workload == "bursty":
            if churn <= 0:
                return 0.5
            return min(1.0, churn / self.HIGH_CHURN_PER_WINDOW)
        # stable: confident when NOT near any other classification threshold.
        return max(0.0, 1.0 - max(
            scan_ratio / self.SCAN_RATIO,
            zipf / self.SKEW_ZIPF,
            (churn / self.HIGH_CHURN_PER_WINDOW if churn > 0 else 0.0),
        ))
