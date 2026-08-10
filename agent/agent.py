"""AdaptiCache autonomous tuning agent.

Runs a LangGraph pipeline (fetch -> analyze -> decide -> act) on a loop.
Each cycle polls the admin HTTP endpoint for metrics, classifies the
workload, and -- subject to a cooldown and rollback guardrails -- issues
SWITCH_POLICY commands over the cache TCP port.
"""

import argparse
import json
import logging
import socket
import time
from collections import deque
from datetime import datetime, timezone
from typing import Dict, List, Optional, TypedDict

from langgraph.graph import END, StateGraph

from analyzer import WorkloadAnalyzer
from metrics import compute_zipf_coefficient, detect_scan_pattern, fetch_metrics

POLICY_FOR_WORKLOAD = {
    "skewed": "sieve",
    "scanning": "lru",
    "stable": "lfu",
    "bursty": "sieve",
}


class AgentState(TypedDict, total=False):
    current_policy: str
    metrics: Dict
    zipf: float
    scan_ratio: float
    workload: str
    desired_policy: str
    action: str
    reason: str
    decision: Dict


class TuningAgent:
    """Stateful agent that tunes a running AdaptiCache instance."""

    def __init__(
        self,
        cache_host: str = "localhost",
        cache_port: int = 6379,
        admin_host: str = "localhost",
        admin_port: int = 8080,
        cooldown_seconds: float = 30.0,
        rollback_drop: float = 0.10,
        log_path: Optional[str] = None,
        access_log_path: Optional[str] = None,
        decide_every_requests: int = 0,
        cooldown_requests: int = 0,
        no_preload_gate: bool = False,
    ) -> None:
        self.cache_host = cache_host
        self.cache_port = cache_port
        self.admin_host = admin_host
        self.admin_port = admin_port
        self.cooldown_seconds = cooldown_seconds
        self.rollback_drop = rollback_drop

        # Request-quantized cadence (deterministic benchmark mode): when
        # decide_every_requests > 0 the agent decides at fixed workload
        # positions (multiples of N requests as counted from the access
        # log) instead of wall-clock intervals.  This removes the run-to-run
        # timing jitter that made every compare sub-run measure differently.
        # cooldown_requests > 0 likewise turns the post-switch cooldown into
        # a request-distance instead of seconds.
        self.decide_every = max(0, int(decide_every_requests))
        self.cooldown_requests = max(0, int(cooldown_requests))
        self.no_preload_gate = bool(no_preload_gate)
        self._progress = 0
        # Negative so the FIRST decision fires as soon as decide_every
        # requests of traffic are observed after the preload gate opens
        # (see run(): decisions are skipped until the server reports
        # preload_complete, at which point the cadence restarts from the
        # current progress).  Firing at progress 0 mid-preload was the
        # 2026-08-10 artifact: a switch while the cache is still filling
        # rebuilds the policy on a half-loaded map and scrambles the
        # eviction frontier before any workload traffic.
        self._last_decide_progress = -self.decide_every
        self._switch_progress: Optional[int] = None

        self.analyzer = WorkloadAnalyzer()

        # Guardrail state.
        self.last_switch_ts = 0.0
        # (previous_policy, baseline_hit_rate, snapshot_count_at_switch)
        self.rollback_info: Optional[tuple] = None

        self.logger = logging.getLogger("adapticache-agent")
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(message)s")
            )
            self.logger.addHandler(handler)
        self.logger.setLevel(logging.INFO)

        self._decision_fh = None
        if log_path:
            self._decision_fh = open(log_path, "a", encoding="utf-8")
            self.logger.info("decision log: %s", log_path)

        # Access telemetry (JSONL written by the benchmark): drives the
        # zipf / scan_ratio signals that the analyzer classifies on.
        self._access_fh = None
        self._key_counts: Dict[str, int] = {}
        self._access_history: deque = deque(maxlen=5000)
        if access_log_path:
            try:
                self._access_fh = open(access_log_path, "r", encoding="utf-8")
                self.logger.info("access telemetry: %s", access_log_path)
            except OSError as exc:
                self.logger.error("cannot open access log %s: %s",
                                  access_log_path, exc)

        self._graph = self._build_graph()

    # ------------------------------------------------------------------ I/O

    def _switch_policy(self, policy: str) -> bool:
        """Send SWITCH_POLICY over TCP; return True on +OK."""
        try:
            with socket.create_connection(
                (self.cache_host, self.cache_port), timeout=2.0
            ) as sock:
                sock.sendall(f"SWITCH_POLICY {policy}\r\n".encode())
                resp = sock.recv(1024).decode(errors="replace").strip()
                return resp == "+OK"
        except OSError as exc:
            self.logger.error("SWITCH_POLICY failed: %s", exc)
            return False

    def _log_decision(self, decision: Dict) -> None:
        line = json.dumps(decision)
        self.logger.info("decision %s", line)
        if self._decision_fh:
            self._decision_fh.write(line + "\n")
            self._decision_fh.flush()

    def _ingest_access_log(self) -> int:
        """Read new lines from the benchmark's access telemetry file.

        An open file handle acts as a tail: each call picks up the lines
        appended since the previous call.  Lines are {"op": ..., "key": ...}
        -- exactly one line per workload request, so the count of ingested
        lines IS the workload position (request index).  Returns the number
        of lines read this call.
        """
        if self._access_fh is None:
            return 0
        read = 0
        try:
            for line in self._access_fh:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                key = entry.get("key")
                if not key:
                    continue
                self._key_counts[key] = self._key_counts.get(key, 0) + 1
                self._access_history.append(key)
                read += 1
        except (OSError, ValueError) as exc:
            self.logger.error("access log read failed: %s", exc)
        self._progress += read
        return read

    def _snapshot(self, state: AgentState) -> Dict:
        metrics = state.get("metrics", {})
        return {
            "_progress": self._progress,
            "hit_rate": metrics.get("hit_rate", 0.0),
            "miss_rate": metrics.get("miss_rate", 0.0),
            "hits": metrics.get("hits", 0),
            "misses": metrics.get("misses", 0),
            "evictions": metrics.get("evictions", 0),
            "memory_bytes": metrics.get("memory_bytes", 0),
            "policy": metrics.get("policy", ""),
            "total_keys": metrics.get("total_keys", 0),
        }

    # -------------------------------------------------------------- nodes

    def fetch(self, state: AgentState) -> Dict:
        metrics = fetch_metrics(self.admin_host, self.admin_port)
        self.analyzer.add_snapshot(metrics)
        counts = self._key_counts or {}
        history = list(self._access_history) or []
        return {
            "metrics": metrics,
            "current_policy": metrics.get("policy", ""),
            "zipf": compute_zipf_coefficient(counts),
            "scan_ratio": detect_scan_pattern(history),
        }

    def _analyze(self, state: AgentState) -> Dict:
        zipf = float(state.get("zipf", 0.0))
        scan_ratio = float(state.get("scan_ratio", 0.0))
        workload = self.analyzer.classify_workload(zipf, scan_ratio)
        latest = self.analyzer.latest() or {}
        self.logger.info(
            "analyze: workload=%s zipf=%.2f scan_ratio=%.2f trend=%.3f "
            "churn=%.1f hit_rate=%.3f",
            workload,
            zipf,
            scan_ratio,
            self.analyzer.compute_hit_rate_trend(),
            self.analyzer.compute_churn_rate(),
            float(latest.get("hit_rate", 0.0)),
        )
        return {"workload": workload}

    def _decide(self, state: AgentState) -> Dict:
        current = state.get("current_policy", "")
        metrics = state.get("metrics", {})
        hit_rate = float(metrics.get("hit_rate", 0.0))
        snapshots_seen = len(self.analyzer.snapshots())
        now = time.monotonic()
        if self.cooldown_requests > 0:
            cooldown_ok = (
                self._switch_progress is None
                or (self._progress - self._switch_progress)
                >= self.cooldown_requests
            )
        else:
            cooldown_ok = (now - self.last_switch_ts) >= self.cooldown_seconds

        # Rollback guardrail: revert if hit_rate dropped >10% after a switch.
        # One-shot: the baseline is cleared when the rollback fires, so a
        # stale baseline from an old switch cannot re-trigger rollbacks.
        if self.rollback_info is not None:
            previous, baseline, snap_at = self.rollback_info
            if (
                hit_rate < baseline * (1.0 - self.rollback_drop)
                and (snapshots_seen - snap_at) >= 3
                and cooldown_ok
            ):
                self.rollback_info = None
                return {
                    "action": "switch",
                    "desired_policy": previous,
                    "reason": (
                        f"rollback after switch: hit_rate {hit_rate:.3f} vs "
                        f"baseline {baseline:.3f} dropped > "
                        f"{self.rollback_drop * 100:.0f}%"
                    ),
                }

        target = POLICY_FOR_WORKLOAD.get(str(state.get("workload", "")), "")
        if target and target != current:
            if cooldown_ok:
                return {
                    "action": "switch",
                    "desired_policy": target,
                    "reason": (
                        f"workload classified as {state.get('workload')}, "
                        f"hit_rate={hit_rate:.3f}"
                    ),
                }
            return {
                "action": "wait",
                "desired_policy": target,
                "reason": (
                    f"cooldown active ({self.cooldown_seconds:.0f}s), "
                    f"would switch {current} -> {target}"
                ),
            }
        return {"action": "none", "desired_policy": current, "reason": ""}

    def _act(self, state: AgentState) -> Dict:
        if state.get("action") != "switch":
            return {"action": "none"}

        target = str(state.get("desired_policy", ""))
        current = state.get("current_policy", "")
        metrics = state.get("metrics", {})
        hit_rate = float(metrics.get("hit_rate", 0.0))
        reason = str(state.get("reason", ""))

        ok = self._switch_policy(target)
        if not ok:
            self.logger.error("switch to %s rejected by server", target)
            return {"action": "none"}

        # Record guardrail state for the rollback check.
        self.last_switch_ts = time.monotonic()
        self._switch_progress = self._progress
        self.rollback_info = (
            current if current else "lru",
            hit_rate,
            len(self.analyzer.snapshots()),
        )

        decision = {
            "kind": "switch",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "old_policy": current,
            "new_policy": target,
            "reason": reason,
            "metrics_snapshot": self._snapshot(state),
        }
        self._log_decision(decision)
        return {"action": "done", "current_policy": target, "decision": decision}

    # ---------------------------------------------------------------- graph

    def _build_graph(self):
        builder = StateGraph(AgentState)
        builder.add_node("fetch", self.fetch)
        builder.add_node("analyze", self._analyze)
        builder.add_node("decide", self._decide)
        builder.add_node("act", self._act)
        builder.set_entry_point("fetch")
        builder.add_edge("fetch", "analyze")
        builder.add_edge("analyze", "decide")
        builder.add_edge("decide", "act")
        builder.add_edge("act", END)
        return builder.compile()

    def _preload_complete(self) -> bool:
        """True when the cache server reports preload done.

        The benchmark sends MARK_PRELOADED right after the preload loop.
        A missing flag (stale binary) reads as False so the gate errs on
        the safe side; --no-preload-gate skips this check entirely.
        """
        try:
            metrics = fetch_metrics(self.admin_host, self.admin_port)
        except Exception:
            return False
        return bool(metrics.get("preload_complete", False))

    def run(self, interval_seconds: float = 5.0, max_cycles: int = -1) -> None:
        """Run the tuning loop until interrupted or max_cycles reached.

        With ``decide_every > 0`` (request-quantized cadence) the loop polls
        the access telemetry every ~0.1s and runs a decision cycle whenever
        the workload position advances by ``decide_every`` requests; the
        wall-clock ``interval_seconds`` only bounds the poll rate.  In the
        wall-clock mode (``decide_every == 0``) each cycle runs every
        ``interval_seconds`` as before.
        """
        cycle = 0
        self.logger.info(
            "agent started (cache=%s:%d admin=%s:%d)",
            self.cache_host,
            self.cache_port,
            self.admin_host,
            self.admin_port,
        )
        if self.decide_every > 0:
            self.logger.info(
                "request-quantized cadence: decide every %d requests "
                "(cooldown %d requests)",
                self.decide_every,
                self.cooldown_requests,
            )
        # Benchmark mode polls fast so the request-quantized cadence fires
        # within ~a poll interval of the grid position (ingest is a cheap
        # tail read; 20 polls/s is negligible).
        poll_delay = 0.05 if self.decide_every > 0 else interval_seconds
        preload_gate_done = self.no_preload_gate or self.decide_every == 0
        gated_cycles = 0
        while max_cycles < 0 or cycle < max_cycles:
            self._ingest_access_log()
            if not preload_gate_done:
                if not self._preload_complete():
                    gated_cycles += 1
                    if gated_cycles % 20 == 1:
                        self.logger.warning(
                            "waiting for cache preload to complete (send "
                            "MARK_PRELOADED once the cache is filled; "
                            "--no-preload-gate to skip this gate)")
                    time.sleep(poll_delay)
                    continue
                # Preload done: restart the decision cadence from here so a
                # fresh cache sees decide_every requests of real traffic
                # before the first decision can fire.  Quantize to the
                # decide grid: gate-open progress varies with poll timing
                # (0..~decide_every requests), and un-quantized that jitter
                # shifts the first switch position, which the policy-rebuild
                # scramble amplifies into 0.5-1.2pt of hit-rate noise (the
                # echo-control gap measured on the 2026-08-10 regen).
                self._last_decide_progress = (
                    self._progress - (self._progress % self.decide_every))
                preload_gate_done = True
            if (self.decide_every > 0
                    and self._progress - self._last_decide_progress
                    < self.decide_every):
                time.sleep(poll_delay)
                continue
            cycle += 1
            try:
                result = self._graph.invoke({})
                if result.get("action") == "done":
                    dec = result.get("decision", {})
                    self.logger.info(
                        "switched %s -> %s (%s)",
                        dec.get("old_policy", "?"),
                        dec.get("new_policy", "?"),
                        dec.get("reason", ""),
                    )
                cycle_entry = {
                    "kind": "cycle",
                    "action": str(result.get("action", "?")),
                    "progress": self._progress,
                    "desired_policy": str(result.get("desired_policy", "")),
                    "reason": str(result.get("reason", "")),
                }
                self._log_decision(cycle_entry)
            except Exception as exc:  # keep the loop alive
                self.logger.error("cycle failed: %s", exc)
            if self.decide_every > 0:
                self._last_decide_progress = self._progress
            if max_cycles >= 0 and cycle >= max_cycles:
                break
            time.sleep(poll_delay)

    def close(self) -> None:
        if self._decision_fh:
            self._decision_fh.close()
            self._decision_fh = None
        if self._access_fh:
            self._access_fh.close()
            self._access_fh = None


def main() -> None:
    parser = argparse.ArgumentParser(description="AdaptiCache tuning agent")
    parser.add_argument("--cache-host", default="localhost")
    parser.add_argument("--cache-port", type=int, default=6379)
    parser.add_argument("--admin-host", default="localhost")
    parser.add_argument("--admin-port", type=int, default=8080)
    parser.add_argument("--interval", type=float, default=5.0,
                        help="seconds between tuning cycles (wall-clock "
                             "cadence; only bounds the poll rate when "
                             "--decide-every is set)")
    parser.add_argument("--decide-every", type=int, default=0,
                        help="request-quantized cadence: decide whenever the "
                             "access log shows N new requests (0 = wall-clock "
                             "interval cadence, the default)")
    parser.add_argument("--cooldown-req", type=int, default=0,
                        help="post-switch cooldown in requests instead of "
                             "seconds (0 = use --cooldown seconds)")
    parser.add_argument("--cooldown", type=float, default=30.0,
                        help="minimum seconds between policy switches")
    parser.add_argument("--log", default=None,
                        help="JSON-lines file for every decision")
    parser.add_argument("--access-log", default=None,
                        help="JSONL access telemetry written by benchmark.py "
                             "(drives zipf/scan detection)")
    parser.add_argument("--cycles", type=int, default=-1,
                        help="run N cycles then exit (-1 = forever)")
    parser.add_argument("--no-preload-gate", action="store_true",
                        help="skip the preload-complete gate (decide even "
                             "while the cache is still filling; only for "
                             "standalone runs that cannot send "
                             "MARK_PRELOADED)")
    args = parser.parse_args()

    agent = TuningAgent(
        cache_host=args.cache_host,
        cache_port=args.cache_port,
        admin_host=args.admin_host,
        admin_port=args.admin_port,
        cooldown_seconds=args.cooldown,
        log_path=args.log,
        access_log_path=args.access_log,
        decide_every_requests=args.decide_every,
        cooldown_requests=args.cooldown_req,
        no_preload_gate=args.no_preload_gate,
    )
    try:
        agent.run(args.interval, args.cycles)
    except KeyboardInterrupt:
        print()
        agent.logger.info("interrupted, shutting down")
    finally:
        agent.close()


def run() -> None:
    """Programmatic entry point."""
    main()


if __name__ == "__main__":
    main()
