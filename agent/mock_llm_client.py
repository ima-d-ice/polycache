"""Deterministic offline stand-in for the Groq LLM client.

Used only by verification: exercises model rotation, key rotation, failover
and stats without a network or the openai SDK.  ``self_check()`` runs the
assertions that the benchmark's --compare post-checks rely on.
"""

import json
from typing import Dict, List, Optional

from llm_client import API_MODEL_IDS, LLMError, MODELS, RoundRobinLLMClient

_API_TO_SHORT = {v: k for k, v in API_MODEL_IDS.items()}


class _FakeRateLimit(Exception):
    status_code = 429


class _FakeServerError(Exception):
    status_code = 503


class MockLLMClient(RoundRobinLLMClient):
    """Subclass with scripted per-model behavior."""

    def __init__(
        self,
        policy: str = "sieve",
        fail_models: Optional[List[str]] = None,
        retry_models: Optional[List[str]] = None,
        bad_models: Optional[List[str]] = None,
        keys: Optional[List[str]] = None,
        **kwargs
    ) -> None:
        # backoff_base=0 -> retries are instant in offline verification.
        super().__init__(
            keys=keys or ["mock-key-1", "mock-key-2", "mock-key-3"],
            backoff_base=0.0,
            **kwargs
        )
        self.policy = policy
        self.fail_models = set(fail_models or [])
        self.retry_models = set(retry_models or [])
        self.bad_models = set(bad_models or [])
        self.calls: List[tuple] = []  # (model, key)

    def _call_model(self, model: str, api_key: str,
                    messages: List[Dict],
                    extra_body: Optional[Dict] = None) -> tuple:
        short = _API_TO_SHORT.get(model, model)
        self.calls.append((short, api_key))
        if short in self.fail_models:
            raise _FakeServerError("mock %s: server down" % short)
        if short in self.retry_models:
            raise _FakeRateLimit("mock %s: rate limited" % short)
        if short in self.bad_models:
            return "not-json-at-all", 240, 18
        content = json.dumps(
            {"policy": self.policy, "reason": "mock decision"}
        )
        return content, 240, 18


def _expect_error(client: MockLLMClient, label: str) -> None:
    try:
        client.decide_policy({"hit_rate": 0.3}, "skewed", "lru")
    except LLMError:
        return
    raise AssertionError("%s: expected LLMError" % label)


def self_check() -> None:
    """Offline assertions; raises AssertionError on any failure."""
    # 1. Rotation: 6 calls use all three models, evenly.
    c = MockLLMClient(policy="lfu")
    seen = []
    for _ in range(6):
        seen.append(c.decide_policy({"hit_rate": 0.4}, "skewed", "lru")["model_used"])
    assert set(seen) == {"gpt-oss-120b", "llama-3.3-70b-versatile", "qwen-3.6-27b"}, seen
    assert seen == ["gpt-oss-120b", "llama-3.3-70b-versatile", "qwen-3.6-27b"] * 2, seen

    # 2. Key rotation: 6 calls use all 3 keys, evenly.
    keys = [k for _, k in c.calls]
    assert keys == ["mock-key-1", "mock-key-2", "mock-key-3"] * 2, keys

    # 3. Failover: primary down -> secondary answers, retries reset.
    f = MockLLMClient(fail_models=["gpt-oss-120b"], policy="sieve")
    d1 = f.decide_policy({"hit_rate": 0.3}, "scanning", "lru")
    assert d1["model_used"] == "llama-3.3-70b-versatile", d1
    # Next call rotates to llama as primary -> ok directly.
    d2 = f.decide_policy({"hit_rate": 0.3}, "scanning", "lru")
    assert d2["model_used"] == "llama-3.3-70b-versatile", d2

    # 4. Retryable error: rate-limited model retries then fails over.
    r = MockLLMClient(retry_models=["gpt-oss-120b"], policy="sieve")
    d3 = r.decide_policy({"hit_rate": 0.3}, "stable", "lru")
    assert d3["model_used"] == "llama-3.3-70b-versatile", d3

    # 5. All models down -> LLMError.
    _expect_error(MockLLMClient(fail_models=list(MODELS)), "all-down")

    # 6. Garbage response -> LLMError.
    _expect_error(MockLLMClient(bad_models=["gpt-oss-120b"]), "garbage")

    # 7. Invalid policy value -> LLMError.
    bad = MockLLMClient()
    bad._call_model = lambda m, k, msgs: (json.dumps({"policy": "fifo"}), 1, 1)  # type: ignore[assignment]
    _expect_error(bad, "invalid-policy")

    # 8. Stats reflect calls and errors.
    stats = f.get_stats()
    assert stats["llama-3.3-70b-versatile"]["calls"] == 2, stats
    assert stats["gpt-oss-120b"]["errors"] == 1, stats

    # 9. No keys -> inits cleanly, decide_policy raises.
    import os
    saved = {name: os.environ.pop(name) for name in list(os.environ)
             if name.startswith("GROQ_API_KEY")}
    try:
        nk = RoundRobinLLMClient(keys=[])
        assert nk.ready is False
        try:
            nk.decide_policy({}, "", "")
        except LLMError:
            pass
        else:
            raise AssertionError("no-keys: expected LLMError")
    finally:
        for name, value in saved.items():
            if value is not None:
                os.environ[name] = value

    print("mock_llm_client.self_check: all 9 assertions passed")


if __name__ == "__main__":
    self_check()
