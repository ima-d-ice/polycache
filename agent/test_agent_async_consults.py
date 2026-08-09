"""Offline regression test for the FIRE-AND-FORGET LLM consult path.

The synchronous consult artifact (2.6s blocking calls shifting the switch
grid ~6.5K requests, swinging single-seed compare results +-13..20pt for
every consult mode) is fixed by executing the rule decision at grid time
and running consults on background threads.  This test guards that
behavior without a running server or network (stub LLM client with
configurable latency):

  1. hybrid: _decide returns immediately; the rule executes at grid time
     with a "deferred" reason; the kind:"llm" eval lands asynchronously.
  2. hybrid cache-hit path still annotates synchronously (zero latency).
  3. hybrid_conflict: rule executes at grid time; the arbiter's differing
     pick becomes a pending override applied at the NEXT decide point.
  4. failed consult: fail-safe, rule stands, no override queued.
  5. llm mode: grid-time rule execution; the LLM's differing pick applies
     as an override next cycle (and an agreeing pick queues nothing).
  6. no LLM configured: unchanged behavior.

Run with the project venv (needs langgraph):
    venv/bin/python3 agent/test_agent_async_consults.py
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import TuningAgent  # noqa: E402
from llm_client import LLMError  # noqa: E402


class StubLLM:
    def __init__(self, latency=0.05, pick="sieve", approve=True,
                 fail=False):
        self.latency = latency
        self.pick = pick
        self.approve = approve
        self.fail = fail
        self.model = "stub"

    def _maybe_fail(self):
        if self.fail:
            raise LLMError("stub failure")

    def decide_policy(self, metrics, workload, current, **kw):
        self._maybe_fail()
        time.sleep(self.latency)
        return {"policy": self.pick, "reason": "stub", "model_used": self.model,
                "latency_ms": int(self.latency * 1000),
                "tokens_prompt": 1, "tokens_completion": 1}

    def evaluate_switch(self, metrics, workload, current, proposed, **kw):
        self._maybe_fail()
        time.sleep(self.latency)
        return {"approve": self.approve, "confidence": 0.9, "reason": "stub",
                "model_used": self.model,
                "latency_ms": int(self.latency * 1000),
                "tokens_prompt": 1, "tokens_completion": 1}

    def arbitrate_conflict(self, metrics, workload, current, rule_target,
                           physics_target, **kw):
        self._maybe_fail()
        time.sleep(self.latency)
        return {"policy": self.pick, "trust": "stub", "confidence": 0.9,
                "reason": "stub", "model_used": self.model,
                "latency_ms": int(self.latency * 1000),
                "tokens_prompt": 1, "tokens_completion": 1}


def make_agent(decision_mode, llm, log_path):
    if os.path.exists(log_path):
        os.remove(log_path)
    a = TuningAgent(decision_mode=decision_mode, log_path=log_path)
    a._llm = llm  # inject without set_llm_client (that forces llm mode)
    # __init__ auto-fell back to rule mode (no Groq keys in env): restore.
    a.decision_mode = decision_mode
    return a


def base_state(policy="lru"):
    return {"workload": "bursty", "current_policy": policy,
            "metrics": {"hit_rate": 0.7, "policy": policy, "evictions": 5000}}


def entries(path):
    with open(path) as fh:
        return [json.loads(line) for line in fh]


def poll(path, want, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if len(entries(path)) >= want:
                return True
        except OSError:
            pass
        time.sleep(0.02)
    return False


def main():
    log = "/tmp/ff_hybrid.jsonl"
    a = make_agent("hybrid", StubLLM(latency=0.2), log)
    a._access_history.append("k1")  # has_evidence
    t0 = time.monotonic()
    dec = a._decide(base_state())
    dt = time.monotonic() - t0
    assert dec["action"] == "switch", dec
    assert dec["desired_policy"] == "sieve", dec
    assert dec["reason"].startswith("hybrid[deferred]"), dec["reason"]
    assert dt < 0.1, "blocked on consult: %.3fs" % dt
    assert poll(log, 1), "llm entry never landed"
    llm_entry = [e for e in entries(log) if e["kind"] == "llm"][0]
    assert llm_entry["llm_approve"] is True and llm_entry["async"] is True
    print("1. hybrid async consult + deferred reason: OK")

    log = "/tmp/ff_hybrid2.jsonl"
    a = make_agent("hybrid", StubLLM(latency=0.2), log)
    a._access_history.append("k1")
    a._llm_cache[("bursty", "lru", "sieve")] = {
        "approve": True, "confidence": 0.8, "reason": "cached"}
    dec = a._decide(base_state())
    assert dec["reason"].startswith("hybrid[rule]"), dec["reason"]
    assert dec["hybrid_detail"]["llm_approval"] is True
    print("2. hybrid cache-hit synchronous annotation: OK")

    log = "/tmp/ff_conflict.jsonl"
    a = make_agent("hybrid_conflict", StubLLM(latency=0.2, pick="lfu"), log)
    a._access_history.append("k1")
    a.decide_every = 5000
    a.cooldown_requests = 0
    a.cooldown_seconds = 0.0

    def phys(state):
        return "lfu", 100, 0.5
    a._physics_signal = phys  # force rule(sieve) vs physics(lfu) conflict
    t0 = time.monotonic()
    dec = a._decide(base_state())
    dt = time.monotonic() - t0
    assert dec["action"] == "switch" and dec["desired_policy"] == "sieve", dec
    assert dt < 0.1, "blocked on arbitration: %.3fs" % dt
    assert poll(log, 1), "arbiter entry never landed"
    a._progress = 5000
    dec2 = a._decide(base_state("sieve"))
    assert dec2["action"] == "switch", dec2
    assert dec2["desired_policy"] == "lfu", dec2
    assert dec2["reason"].startswith("llm override (async)"), dec2["reason"]
    print("3. conflict: rule at grid time + async override next cycle: OK")

    log = "/tmp/ff_fail.jsonl"
    a = make_agent("hybrid_conflict", StubLLM(latency=0.05, fail=True), log)
    a._access_history.append("k1")
    a._physics_signal = phys
    dec = a._decide(base_state())
    assert dec["action"] == "switch" and dec["desired_policy"] == "sieve", dec
    time.sleep(0.2)
    llm_entries = [e for e in entries(log) if e["kind"] == "llm"]
    assert llm_entries and llm_entries[0]["fallback"] is True
    assert a._pending_override is None
    print("4. failed consult -> rule stands, no override: OK")

    log = "/tmp/ff_llm.jsonl"
    a = make_agent("llm", StubLLM(latency=0.05, pick="sieve"), log)
    a._access_history.append("k1")
    a.cooldown_requests = 0
    a.cooldown_seconds = 0.0
    dec = a._decide(base_state())  # rule for bursty = sieve = pick: agree
    assert dec["action"] == "switch" and dec["desired_policy"] == "sieve", dec
    time.sleep(0.2)
    assert a._pending_override is None, "pick == executed, no override"
    a2 = make_agent("llm", StubLLM(latency=0.05, pick="sieve"),
                    "/tmp/ff_llm2.jsonl")
    a2._access_history.append("k1")
    a2.cooldown_requests = 0
    a2.cooldown_seconds = 0.0
    dec = a2._decide({"workload": "stable", "current_policy": "lru",
                      "metrics": {"hit_rate": 0.7, "policy": "lru"}})
    assert dec["desired_policy"] == "lfu", dec  # rule executes at grid time
    time.sleep(0.2)
    assert a2._pending_override is not None, "override missing"
    assert a2._pending_override[0] == "sieve"
    a2._progress = 5000
    dec2 = a2._decide({"workload": "stable", "current_policy": "lfu",
                       "metrics": {"hit_rate": 0.7, "policy": "lfu"}})
    assert dec2["desired_policy"] == "sieve", dec2
    assert dec2["reason"].startswith("llm override (async)"), dec2["reason"]
    print("5. llm mode: grid-time rule + next-cycle override: OK")

    log = "/tmp/ff_nollm.jsonl"
    a = TuningAgent(decision_mode="hybrid_conflict", log_path=log)
    a._access_history.append("k1")
    a._physics_signal = phys
    dec = a._decide(base_state())
    assert dec["action"] == "switch" and dec["desired_policy"] == "sieve", dec
    print("6. no-LLM fallback: OK")

    print("\nagent async-consult regression test: all 6 checks passed")


if __name__ == "__main__":
    main()
