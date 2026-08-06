"""Kybernetes autonomous tuning agent.

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
from llm_client import LLMError, RoundRobinLLMClient

POLICY_FOR_WORKLOAD = {
    "skewed": "sieve",
    "scanning": "lru",
    "stable": "lfu",
    "bursty": "sieve",
}

DECISION_MODES = ("rule", "llm", "hybrid")


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
    """Stateful agent that tunes a running Kybernetes instance."""

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
        decision_mode: str = "rule",
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

        self.logger = logging.getLogger("kybernetes-agent")
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

        # Optional LLM decision layer.  Strictly opt-in: with no Groq keys
        # in the environment the agent warns once and stays on the rules.
        self.decision_mode = decision_mode if decision_mode in DECISION_MODES \
            else "rule"
        self._llm: Optional[RoundRobinLLMClient] = None
        if decision_mode in ("llm", "hybrid"):
            client = RoundRobinLLMClient()
            if not client.ready:
                self.logger.warning(
                    "WARNING: Groq keys (GROQ_API_KEY_1..N) not found in "
                    "environment. Falling back to rule mode."
                )
                self.decision_mode = "rule"
            else:
                self.decision_mode = decision_mode
                self._llm = client
                self.logger.info(
                    "LLM decision layer active (mode=%s, %d model(s), "
                    "%d key(s))",
                    decision_mode,
                    len(client.models),
                    client.key_count(),
                )

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

    def _ingest_access_log(self) -> None:
        """Read new lines from the benchmark's access telemetry file.

        An open file handle acts as a tail: each call picks up the lines
        appended since the previous call.  Lines are {"op": ..., "key": ...}.
        """
        if self._access_fh is None:
            return
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
        except (OSError, ValueError) as exc:
            self.logger.error("access log read failed: %s", exc)

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

    def set_llm_client(self, client: Optional[RoundRobinLLMClient]) -> None:
        """Testing hook: inject a (mock) LLM client and switch to llm mode."""
        self._llm = client
        self.decision_mode = "llm" if client is not None else "rule"

    def _llm_decide(self, state: AgentState) -> Optional[Dict]:
        """One LLM consult per cycle; writes a per-cycle ``kind:"llm"`` log.

        Returns the client's pick dict on success, or None when no LLM is
        configured or the consult failed (rule-based fallback).  The log
        carries model/latency/tokens so the benchmark can report LLM stats
        even when no switch is made.
        """
        llm = self._llm
        if llm is None:
            return None
        current = state.get("current_policy", "")
        workload = str(state.get("workload", ""))
        payload = {
            "kind": "llm",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "current_policy": current,
            "workload_class": workload,
            "rule_policy": POLICY_FOR_WORKLOAD.get(workload, ""),
            "metrics_snapshot": self._snapshot(state),
        }
        try:
            pick = llm.decide_policy(state.get("metrics", {}),
                                     workload, current)
        except LLMError as exc:
            payload.update({"llm_policy": None, "fallback": True})
            self._log_decision(payload)
            self.logger.warning(
                "LLM decision failed, falling back to rule-based (%s)", exc)
            return None
        payload.update({
            "llm_policy": pick["policy"],
            "llm_reason": pick.get("reason", ""),
            "llm_model": pick["model_used"],
            "llm_latency_ms": pick["latency_ms"],
            "llm_tokens_prompt": pick["tokens_prompt"],
            "llm_tokens_completion": pick["tokens_completion"],
            "fallback": False,
        })
        if self.decision_mode == "hybrid":
            payload["agreement"] = int(pick["policy"] == payload["rule_policy"])
        self._log_decision(payload)
        return pick

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
        if self.decision_mode == "llm":
            llm_pick = self._llm_decide(state)
            if llm_pick is not None:
                target = llm_pick["policy"]
        elif self.decision_mode == "hybrid":
            self._llm_decide(state)  # second opinion, logged only
        if target and target != current:
            if cooldown_ok:
                if self.decision_mode == "llm" and llm_pick is not None:
                    reason = ("llm[%s] chose %s: %s"
                              % (llm_pick["model_used"], target,
                                 llm_pick.get("reason", "") or "no reason"))
                else:
                    reason = (
                        f"workload classified as {state.get('workload')}, "
                        f"hit_rate={hit_rate:.3f}"
                    )
                return {
                    "action": "switch",
                    "desired_policy": target,
                    "reason": reason,
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
                self._ingest_access_log()
                initial: Dict = {}
                if self._key_counts or self._access_history:
                    initial = {
                        "key_access_counts": dict(self._key_counts),
                        "access_history": list(self._access_history),
                    }
                result = self._graph.invoke(initial)
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
        if self._access_fh:
            self._access_fh.close()
            self._access_fh = None


def main() -> None:
    parser = argparse.ArgumentParser(description="Kybernetes tuning agent")
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
    parser.add_argument("--access-log", default=None,
                        help="JSONL access telemetry written by benchmark.py "
                             "(drives zipf/scan detection)")
    parser.add_argument("--cycles", type=int, default=-1,
                        help="run N cycles then exit (-1 = forever)")
    parser.add_argument("--decision-mode", default="rule",
                        choices=DECISION_MODES,
                        help="rule = heuristic only (default); llm = LLM "
                             "chooses the policy; hybrid = rule decides, "
                             "LLM logged as a second opinion")
    args = parser.parse_args()

    agent = TuningAgent(
        cache_host=args.cache_host,
        cache_port=args.cache_port,
        admin_host=args.admin_host,
        admin_port=args.admin_port,
        cooldown_seconds=args.cooldown,
        log_path=args.log,
        access_log_path=args.access_log,
        decision_mode=args.decision_mode,
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