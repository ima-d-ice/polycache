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
    zipf: float
    scan_ratio: float
    workload: str
    desired_policy: str
    action: str
    reason: str
    decision: Dict
    hybrid_detail: Optional[Dict]


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
        decision_mode: str = "rule",
        hybrid_disagreement: str = "stay",
        mock_llm: Optional[str] = None,
        decide_every_requests: int = 0,
        cooldown_requests: int = 0,
    ) -> None:
        self.cache_host = cache_host
        self.cache_port = cache_port
        self.admin_host = admin_host
        self.admin_port = admin_port
        self.cooldown_seconds = cooldown_seconds
        self.rollback_drop = rollback_drop
        self.hybrid_disagreement = hybrid_disagreement

        # Request-quantized cadence (deterministic benchmark mode): when
        # decide_every_requests > 0 the agent decides at fixed workload
        # positions (multiples of N requests as counted from the access
        # log) instead of wall-clock intervals.  This removes the run-to-run
        # timing jitter that made every compare sub-run measure differently.
        # cooldown_requests > 0 likewise turns the post-switch cooldown into
        # a request-distance instead of seconds.
        self.decide_every = max(0, int(decide_every_requests))
        self.cooldown_requests = max(0, int(cooldown_requests))
        self._progress = 0
        # Negative so the FIRST decision fires immediately (progress 0,
        # before any workload traffic): the agent gets one chance to set
        # the right policy from the preload state, matching the original
        # wall-clock behavior where the first cycle ran pre-workload.
        self._last_decide_progress = -self.decide_every
        self._switch_progress: Optional[int] = None

        # Recent switch history fed to the LLM ("tried this and it failed
        # last time") and a per-(workload,from,to) consult cache so the same
        # proposal is decided ONCE per context instead of re-litigated
        # every decide point (the old 5000-request flip-flop).
        self._switch_history: deque = deque(maxlen=5)
        self._llm_cache: Dict[tuple, Dict] = {}

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

        # Optional LLM layer.  Strictly opt-in: with no Groq keys in the
        # environment the agent warns once and stays on the rules.
        # --mock-llm echo substitutes a ~0ms client that echoes the rule
        # classifier's proposal (timing-control experiments, fully offline).
        # llm mode is DEPRECATED (experiment result: its decisions beat the
        # rule only by luck on individual seeds and regressed on others);
        # hybrid is diagnostic-only (the LLM annotates proposals, the rule
        # always decides).
        self.decision_mode = decision_mode if decision_mode in DECISION_MODES \
            else "rule"
        if self.decision_mode == "llm":
            self.logger.warning(
                "decision mode 'llm' is DEPRECATED: it performed no better "
                "than the rule classifier in controlled experiments and "
                "adds per-cycle latency + cost; use 'rule' or 'hybrid'")
        if decision_mode == "hybrid" and hybrid_disagreement != "stay":
            self.logger.warning(
                "hybrid_disagreement is deprecated: the LLM is "
                "diagnostic-only and can no longer veto the rule")
        self._llm: Optional[RoundRobinLLMClient] = None
        if mock_llm == "echo":
            from mock_echo_llm_client import EchoLLMClient
            self._llm = EchoLLMClient(policy_for_workload=dict(POLICY_FOR_WORKLOAD))
            self.logger.info(
                "echo LLM client active (mode=%s, ~0ms consult latency)",
                decision_mode,
            )
        elif decision_mode in ("llm", "hybrid"):
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
            "signals": self._llm_signals(state),
            "metrics_snapshot": self._snapshot(state),
        }
        try:
            pick = llm.decide_policy(
                state.get("metrics", {}), workload, current,
                signals=payload["signals"],
                switch_history=self._recent_switch_history())
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
        self._log_decision(payload)
        return pick

    # -------------------------------------------------- LLM signal wiring

    def _llm_signals(self, state: AgentState) -> Dict:
        """Raw rule-classifier inputs, so the LLM is not reasoning from the
        lossy workload label alone."""
        zipf = float(state.get("zipf", 0.0))
        scan_ratio = float(state.get("scan_ratio", 0.0))
        workload = str(state.get("workload", ""))
        return {
            "zipf": round(zipf, 3),
            "scan_ratio": round(scan_ratio, 3),
            "churn_rate": round(self.analyzer.compute_churn_rate(), 2),
            "hit_rate_trend": round(self.analyzer.compute_hit_rate_trend(), 4),
            "volatility": round(self.analyzer.compute_hit_rate_volatility(), 3),
            "rule_confidence": round(
                self.analyzer.rule_confidence(workload, zipf, scan_ratio), 2),
        }

    def _recent_switch_history(self) -> List[Dict]:
        return list(self._switch_history)[-3:]

    def _backfill_switch_history(self, hit_rate: float) -> None:
        """Stamp the previous switch's post-switch hit rate (first observed
        after the switch) so the LLM sees before/after pairs."""
        if self._switch_history:
            last = self._switch_history[-1]
            if last.get("after") is None:
                last["after"] = round(float(hit_rate), 4)

    def _llm_evaluate_switch(
        self, state: AgentState, current: str, proposed: str
    ) -> Optional[Dict]:
        """Hybrid diagnostic consult: ask the LLM to assess ONE proposed
        switch (approve/confidence/reason).  The result is logged and the
        rule proposal always executes -- the LLM cannot veto.

        Cached per (workload_class, current, proposed) so a recurring
        proposal is consulted once per context.  Returns the LLM eval dict
        or None on failure.  Always logs a ``kind:"llm"`` entry with the
        eval fields.
        """
        llm = self._llm
        if llm is None:
            return None
        workload = str(state.get("workload", ""))
        cache_key = (workload, current, proposed)
        if cache_key in self._llm_cache:
            return self._llm_cache[cache_key]
        signals = self._llm_signals(state)
        payload = {
            "kind": "llm",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "current_policy": current,
            "workload_class": workload,
            "rule_policy": proposed,
            "signals": signals,
            "metrics_snapshot": self._snapshot(state),
        }
        try:
            eval_res = llm.evaluate_switch(
                state.get("metrics", {}), workload, current, proposed,
                signals=signals,
                switch_history=self._recent_switch_history())
        except LLMError as exc:
            payload.update({"llm_policy": None, "fallback": True})
            self._log_decision(payload)
            self.logger.warning(
                "LLM switch eval failed, rule proposal proceeds (%s)", exc)
            return None
        approve = bool(eval_res.get("approve", False))
        payload.update({
            "llm_approve": approve,
            "llm_confidence": eval_res.get("confidence", 0.0),
            "llm_policy": proposed if approve else current,
            "llm_reason": eval_res.get("reason", ""),
            "llm_model": eval_res["model_used"],
            "llm_latency_ms": eval_res["latency_ms"],
            "llm_tokens_prompt": eval_res["tokens_prompt"],
            "llm_tokens_completion": eval_res["tokens_completion"],
            "agreement": int(approve),
            "fallback": False,
        })
        self._log_decision(payload)
        self._llm_cache[cache_key] = eval_res
        return eval_res

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
        self._backfill_switch_history(hit_rate)
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
        llm_pick: Optional[Dict] = None
        hybrid_detail: Optional[Dict] = None
        # Consult the LLM only when the agent has observed real traffic.
        # Pre-workload (empty access history) the classifier reads an empty
        # cache and any LLM "no activity" veto just preserves the default
        # policy -- on a skewed phase that costs the whole first phase.
        has_evidence = bool(self._access_history)
        if self.decision_mode == "llm":
            if has_evidence:
                llm_pick = self._llm_decide(state)
            if llm_pick is not None:
                target = llm_pick["policy"]
        elif self.decision_mode == "hybrid":
            # Diagnostic fusion (experiment outcome: the LLM's veto never
            # beat the rule on a majority of seeds, so it no longer decides
            # anything).  The rule proposes; the LLM assesses the proposal
            # as a risk annotator; the rule ALWAYS decides.  The eval is
            # logged as a kind:"llm" entry plus hybrid_detail on the switch
            # decision, but can never veto.  Consult only when a switch is
            # on the table, the cooldown allows it, and the agent has seen
            # traffic (pre-workload the classifier reads an empty cache and
            # a consult is pure noise).
            if (target and target != current and cooldown_ok
                    and has_evidence):
                llm_eval = self._llm_evaluate_switch(state, current, target)
                if llm_eval is not None:
                    hybrid_detail = {
                        "rule_proposal": target,
                        "llm_approval": bool(llm_eval.get("approve", False)),
                        "llm_confidence": llm_eval.get("confidence", 0.0),
                        "resolution": "rule",
                    }
                # else: consult failed -> rule proposal executes unchanged.
        if target and target != current:
            if cooldown_ok:
                if self.decision_mode == "llm" and llm_pick is not None:
                    reason = ("llm[%s] chose %s: %s"
                              % (llm_pick["model_used"], target,
                                 llm_pick.get("reason", "") or "no reason"))
                elif hybrid_detail is not None:
                    reason = (
                        "hybrid[%s]: rule->%s llm_approve=%s conf=%.2f "
                        "final=%s"
                        % (hybrid_detail["resolution"],
                           hybrid_detail["rule_proposal"],
                           hybrid_detail["llm_approval"],
                           hybrid_detail["llm_confidence"], target)
                    )
                else:
                    reason = (
                        f"workload classified as {state.get('workload')}, "
                        f"hit_rate={hit_rate:.3f}"
                    )
                return {
                    "action": "switch",
                    "desired_policy": target,
                    "reason": reason,
                    "hybrid_detail": hybrid_detail,
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

        # Switch history for the LLM prompt ("tried X and it cost Y").
        # 'after' is backfilled at the next decision cycle.
        self._switch_history.append({
            "from": current if current else "lru",
            "to": target,
            "at": self._progress,
            "before": round(float(hit_rate), 4),
            "after": None,
        })

        decision = {
            "kind": "switch",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "old_policy": current,
            "new_policy": target,
            "reason": reason,
            "metrics_snapshot": self._snapshot(state),
        }
        hybrid_detail = state.get("hybrid_detail")
        if hybrid_detail:
            decision["hybrid_detail"] = hybrid_detail
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
        poll_delay = 0.1 if self.decide_every > 0 else interval_seconds
        while max_cycles < 0 or cycle < max_cycles:
            self._ingest_access_log()
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
                self._log_decision({
                    "kind": "cycle",
                    "action": str(result.get("action", "?")),
                    "progress": self._progress,
                    "desired_policy": str(result.get("desired_policy", "")),
                    "reason": str(result.get("reason", "")),
                })
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
    parser.add_argument("--decision-mode", default="rule",
                        choices=DECISION_MODES,
                        help="rule = heuristic only (default); hybrid = rule "
                             "decides, LLM consulted on proposals as a "
                             "diagnostic annotator (recommended for "
                             "experimentation; ~0 steady-state latency); "
                             "llm = LLM chooses the policy (DEPRECATED: "
                             "lost to rule in controlled experiments)")
    parser.add_argument("--hybrid-disagreement", default="stay",
                        choices=("stay", "rule", "llm"),
                        help="DEPRECATED no-op: hybrid is diagnostic-only, "
                             "the rule always decides; kept for CLI "
                             "compatibility")
    parser.add_argument("--mock-llm", default=None, choices=("echo",),
                        help="replace the LLM with a ~0ms echo client that "
                             "always agrees with the rule classifier "
                             "(timing-control runs; works without Groq keys)")
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
        hybrid_disagreement=args.hybrid_disagreement,
        mock_llm=args.mock_llm,
        decide_every_requests=args.decide_every,
        cooldown_requests=args.cooldown_req,
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