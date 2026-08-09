"""Instant mock LLM client that echoes the rule classifier's proposal.

Used as the timing control in the benchmark's compare: hybrid_echo makes
exactly the decisions rule mode makes, at ~0 ms consult latency, so any
rule-vs-hybrid_echo difference in hit rate is a harness artifact, not an
information effect.  Runs fully offline (no keys, no network, no openai
SDK).
"""

from typing import Dict, Optional

from llm_client import RoundRobinLLMClient


class EchoLLMClient(RoundRobinLLMClient):
    """RoundRobinLLMClient whose every consult agrees with the rule."""

    def __init__(self, policy_for_workload: Optional[Dict[str, str]] = None,
                 **kwargs) -> None:
        super().__init__(
            keys=["mock-echo-key"],
            model_pool=["echo-model"],
            backoff_base=0.0,
            **kwargs
        )
        self._policy_for_workload = dict(policy_for_workload or {})

    def decide_policy(self, metrics: Dict, workload_class: str = "",
                      current_policy: str = "") -> Dict:
        rule_policy = self._policy_for_workload.get(workload_class, "")
        return {
            "policy": rule_policy,
            "reason": "echo: agrees with rule classifier",
            "model_used": "echo-model",
            "latency_ms": 0.01,
            "tokens_prompt": 0,
            "tokens_completion": 0,
        }

    def evaluate_switch(self, metrics: Dict, workload_class: str = "",
                        current_policy: str = "", proposed_policy: str = "",
                        signals: Dict = None,
                        switch_history: list = None) -> Dict:
        # Echo always approves the rule's proposal: hybrid_echo then makes
        # exactly the decisions rule makes (the timing control property).
        return {
            "approve": True,
            "confidence": 1.0,
            "reason": "echo: approves rule proposal",
            "model_used": "echo-model",
            "latency_ms": 0.01,
            "tokens_prompt": 0,
            "tokens_completion": 0,
        }

    def arbitrate_conflict(self, metrics: Dict, workload_class: str = "",
                           current_policy: str = "",
                           rule_proposed_policy: str = "",
                           physics_proposed_policy: str = "",
                           signals: Dict = None,
                           switch_history: list = None) -> Dict:
        # Echo sides with the rule: hybrid_conflict_echo makes exactly the
        # decisions rule makes (the timing control property), even when the
        # physics signal proposes something else.
        return {
            "policy": rule_proposed_policy,
            "trust": "rule",
            "confidence": 1.0,
            "reason": "echo: sides with rule proposal",
            "model_used": "echo-model",
            "latency_ms": 0.01,
            "tokens_prompt": 0,
            "tokens_completion": 0,
        }
