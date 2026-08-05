"""CachePilot autonomous tuning agent.

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
    key_access_counts: Dict[str, int]
    access_history: List[str]
    zipf: float
    scan_ratio: float
    workload: str
    desired_policy: str
    action: str
    reason: str
    decision: Dict


class TuningAgent:
    """Stateful agent that tunes a running CachePilot instance."""

    def __init__(
        self,
        cache_host: str = "localhost",
        cache_port: int = 6379,
        admin_host: str = "localhost",
        admin_port: int = 8080,
        cooldown_seconds: float = 30.0,
        rollback_drop: float = 0.10,
        log_path: Optional[str] = None,
    ) -> None:
        self.cache_host = cache_host
        self.cache_port = cache_port
        self.admin_host = admin_host
        self.admin_port = admin_port
        self.cooldown_seconds = cooldown_seconds
        self.rollback_drop = rollback_drop

        self.analyzer = WorkloadAnalyzer()

        # Guardrail state.
        self.last_switch_ts = 0.0
        # (previous_policy, baseline_hit_rate, snapshot_count_at_switch)
        self.rollback_info: Optional[tuple] = None

        self.logger = logging.getLogger("cachepilot-agent")
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

    def _snapshot(self, state: AgentState) -> Dict:
        metrics = state.get("metrics", {})
        return {
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
        counts = state.get("key_access_counts") or {}
        history = state.get("access_history") or []
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
        cooldown_ok = (now - self.last_switch_ts) >= self.cooldown_seconds

        # Rollback guardrail: revert if hit_rate dropped >10% after a switch.
        if self.rollback_info is not None:
            previous, baseline, snap_at = self.rollback_info
            if (
                hit_rate < baseline * (1.0 - self.rollback_drop)
                and (snapshots_seen - snap_at) >= 3
                and cooldown_ok
            ):
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
        self.rollback_info = (
            current if current else "lru",
            hit_rate,
            len(self.analyzer.snapshots()),
        )

        decision = {
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

    def run(self, interval_seconds: float = 5.0, max_cycles: int = -1) -> None:
        """Run the tuning loop until interrupted or max_cycles reached."""
        cycle = 0
        self.logger.info(
            "agent started (cache=%s:%d admin=%s:%d)",
            self.cache_host,
            self.cache_port,
            self.admin_host,
            self.admin_port,
        )
        while max_cycles < 0 or cycle < max_cycles:
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
            except Exception as exc:  # keep the loop alive
                self.logger.error("cycle failed: %s", exc)
            if max_cycles >= 0 and cycle >= max_cycles:
                break
            time.sleep(interval_seconds)

    def close(self) -> None:
        if self._decision_fh:
            self._decision_fh.close()
            self._decision_fh = None


def main() -> None:
    parser = argparse.ArgumentParser(description="CachePilot tuning agent")
    parser.add_argument("--cache-host", default="localhost")
    parser.add_argument("--cache-port", type=int, default=6379)
    parser.add_argument("--admin-host", default="localhost")
    parser.add_argument("--admin-port", type=int, default=8080)
    parser.add_argument("--interval", type=float, default=5.0,
                        help="seconds between tuning cycles")
    parser.add_argument("--cooldown", type=float, default=30.0,
                        help="minimum seconds between policy switches")
    parser.add_argument("--log", default=None,
                        help="JSON-lines file for every decision")
    parser.add_argument("--cycles", type=int, default=-1,
                        help="run N cycles then exit (-1 = forever)")
    args = parser.parse_args()

    agent = TuningAgent(
        cache_host=args.cache_host,
        cache_port=args.cache_port,
        admin_host=args.admin_host,
        admin_port=args.admin_port,
        cooldown_seconds=args.cooldown,
        log_path=args.log,
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
    run()