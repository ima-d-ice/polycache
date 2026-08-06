"""Optional LLM decision client for the CachePilot tuning agent.

Talks to Groq's OpenAI-compatible endpoint.  The client is strictly opt-in:
with no keys it initializes cleanly but every ``decide_policy`` call raises
``LLMError``, and the agent falls back to the rule-based decisions.

Keys are read from the environment as ``GROQ_API_KEY_1`` .. ``GROQ_API_KEY_N``
(contiguous, stopping at the first gap) and rotated one key per API request.
With a single key it is reused for every request.

Models rotate per call: gpt-oss-120b -> llama-3.3-70b-versatile ->
qwen-3.6-27b -> gpt-oss-120b ... The primary model for a call advances by one
each time, and if a model keeps failing (429/503 after exponential backoff
1s/2s/4s, or any other error) the client fails over to the next model in
rotation order with the retry count reset.
"""

import json
import os
import time
from typing import Dict, List, Optional

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
MODELS = ["gpt-oss-120b", "llama-3.3-70b-versatile", "qwen-3.6-27b"]
# Short public labels above map to the real Groq API model ids.
API_MODEL_IDS = {
    "gpt-oss-120b": "openai/gpt-oss-120b",
    "llama-3.3-70b-versatile": "llama-3.3-70b-versatile",
    "qwen-3.6-27b": "qwen/qwen3.6-27b",
}
# Optional per-model request overrides (extra_body): reasoning models must
# reduce/disable thinking or they burn the token budget on reasoning text.
MODEL_EXTRA_BODY = {
    "gpt-oss-120b": {"reasoning_effort": "low"},
    "qwen-3.6-27b": {"reasoning_effort": "none"},
}
VALID_POLICIES = ("lru", "lfu", "sieve")

SYSTEM_PROMPT = (
    "You are a cache eviction policy optimizer for a key-value store that "
    "supports LRU, LFU, and SIEVE. Observe the workload metrics and reply "
    "with EXACTLY one JSON object and nothing else, in this shape: "
    '{"policy": "lru", "reason": "<10 words why>"}. '
    'The "policy" field must be exactly one of: "lru", "lfu", "sieve". '
    "Choose the policy that maximizes cache hit rate for the observed "
    "workload."
)


class LLMError(Exception):
    """Raised when an LLM decision cannot be produced."""


def _load_keys_from_env() -> List[str]:
    """GROQ_API_KEY_1 .. GROQ_API_KEY_N, stopping at the first gap."""
    keys: List[str] = []
    idx = 1
    while True:
        value = os.environ.get("GROQ_API_KEY_%d" % idx)
        if not value:
            break
        keys.append(value.strip())
        idx += 1
    return keys


class RoundRobinLLMClient:
    """Multi-model, multi-key Groq client with round-robin failover."""

    def __init__(
        self,
        keys: Optional[List[str]] = None,
        model_pool: Optional[List[str]] = None,
        base_url: str = GROQ_BASE_URL,
        max_tokens: int = 200,
        temperature: float = 0.1,
        backoff_base: float = 1.0,
    ) -> None:
        self.models = list(model_pool) if model_pool else list(MODELS)
        self.base_url = base_url
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.backoff_base = backoff_base

        self._keys = list(keys) if keys is not None else _load_keys_from_env()
        self.ready = bool(self._keys)

        # Rotation state: primary model index for the next call, and a
        # request counter used to rotate API keys.
        self._model_idx = 0
        self._request_count = 0

        # Per-model counters.
        self._stats: Dict[str, Dict] = {}

    # ------------------------------------------------------------- stats

    def key_count(self) -> int:
        return len(self._keys)

    def get_stats(self) -> Dict[str, Dict]:
        """Per-model counters (calls/errors/avg+max latency)."""
        out: Dict[str, Dict] = {}
        for model, s in sorted(self._stats.items()):
            calls = s["calls"]
            out[model] = {
                "calls": calls,
                "errors": s["errors"],
                "avg_latency_ms": round(s["latency_sum"] / calls, 2) if calls else 0.0,
                "max_latency_ms": round(s["latency_max"], 2),
            }
        return out

    def _record(self, model: str, latency_s: float, error: bool = False) -> None:
        s = self._stats.setdefault(
            model,
            {"calls": 0, "errors": 0, "latency_sum": 0.0, "latency_max": 0.0},
        )
        s["calls"] += 1
        if error:
            s["errors"] += 1
        s["latency_sum"] += latency_s * 1000.0
        s["latency_max"] = max(s["latency_max"], latency_s * 1000.0)

    # --------------------------------------------------- single HTTP call

    def _call_model(self, model: str, api_key: str,
                    messages: List[Dict], extra_body: Optional[Dict] = None) -> tuple:
        """One API request; returns (content, prompt_tokens, completion_tokens).

        RuntimeError-class errors from the SDK/transport propagate up; the
        caller decides whether to retry or fail over.
        """
        import openai  # lazy: only needed when the LLM is actually used

        client = openai.OpenAI(api_key=api_key, base_url=self.base_url)
        kwargs = dict(
            model=model,
            messages=messages,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
        )
        if extra_body:
            kwargs["extra_body"] = extra_body
        resp = client.chat.completions.create(**kwargs)
        content = resp.choices[0].message.content or ""
        usage = resp.usage
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        return content, prompt_tokens, completion_tokens

    def _retryable(self, exc: Exception) -> bool:
        return getattr(exc, "status_code", None) in (429, 500, 502, 503, 504)

    # ----------------------------------------------------------- decide

    def decide_policy(
        self,
        metrics: Dict,
        workload_class: str = "",
        current_policy: str = "",
    ) -> Dict:
        """Ask the LLM which eviction policy to run.

        Returns {policy, reason, model_used, latency_ms, tokens_prompt,
        tokens_completion}.  Raises ``LLMError`` when every model fails or
        the response is unparseable / names an unknown policy.
        """
        if not self.ready:
            raise LLMError(
                "no Groq keys in environment (GROQ_API_KEY_1..N not found)"
            )

        user_content = json.dumps(
            {
                "current_policy": current_policy or "unknown",
                "workload_class": workload_class or "unknown",
                "metrics": {
                    "hit_rate": round(float(metrics.get("hit_rate", 0.0)), 4),
                    "miss_rate": round(float(metrics.get("miss_rate", 0.0)), 4),
                    "hits": int(metrics.get("hits", 0)),
                    "misses": int(metrics.get("misses", 0)),
                    "requests": int(metrics.get("requests", 0)),
                    "total_keys": int(metrics.get("total_keys", 0)),
                },
            }
        )
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

        # Per-call rotation: the primary model for THIS call.
        primary = self._model_idx % len(self.models)
        order = [self.models[(primary + offset) % len(self.models)]
                 for offset in range(len(self.models))]
        self._model_idx = (self._model_idx + 1) % len(self.models)

        last_error: Optional[Exception] = None
        for model in order:
            retries = 0
            while True:
                try:
                    key = self._keys[self._request_count % len(self._keys)]
                    self._request_count += 1
                    start = time.monotonic()
                    content, p_tokens, c_tokens = self._call_model(
                        API_MODEL_IDS.get(model, model), key, messages,
                        extra_body=MODEL_EXTRA_BODY.get(model))
                    elapsed = time.monotonic() - start
                    latency_ms = elapsed * 1000.0
                    self._record(model, elapsed)
                    self._last_model = model
                    return self._parse(content, latency_ms,
                                       p_tokens, c_tokens)
                except LLMError:
                    raise
                except Exception as exc:  # noqa: BLE001 - SDK raises many types
                    if self._retryable(exc) and retries < 3:
                        retries += 1
                        time.sleep(self.backoff_base * (2 ** (retries - 1)))
                        continue
                    self._record(model, 0.0, error=True)
                    last_error = exc
                    break  # fail over to the next model; noqa: BLE001
        raise LLMError("all models failed: %s" % (last_error or "unknown"))

    def _parse(self, content: str, latency_ms: float, prompt_tokens: int,
               completion_tokens: int) -> Dict:
        try:
            payload = json.loads(_extract_json(content))
        except (ValueError, TypeError) as exc:
            raise LLMError("unparseable LLM response: %r (%s)"
                           % (content[:120], exc))
        policy = str(payload.get("policy", "")).lower()
        if policy not in VALID_POLICIES:
            raise LLMError("invalid policy from LLM: %r" % policy)
        return {
            "policy": policy,
            "reason": str(payload.get("reason", ""))[:120],
            "model_used": self._last_model,
            "latency_ms": round(latency_ms, 2),
            "tokens_prompt": prompt_tokens,
            "tokens_completion": completion_tokens,
        }


def _extract_json(content: str) -> str:
    """Dig a JSON object out of a possibly-reasoning/markdown-wrapped reply."""
    import re
    text = content.strip()
    if text.startswith("```"):
        lines = [ln for ln in text.splitlines() if not ln.startswith("```")]
        text = "\n".join(lines).strip()
    # Drop a provider reasoning block: ``` thinking ... ``` response.
    text = re.sub(r"`+\\s*thinking\\s*\\n.*?\\n\\s*response\\s*`+",
                  "", text, flags=re.DOTALL)
    # Otherwise scan for a brace span that actually parses as JSON.
    for start, ch in enumerate(text):
        if ch in "{[":  # noqa: SIM300 - allow looking for either opener
            for end in range(len(text), start, -1):
                if text[end - 1] not in "}]":
                    continue
                candidate = text[start:end]
                try:
                    json.loads(candidate)
                except (ValueError, TypeError):
                    continue
                return candidate
    raise ValueError("no JSON object in LLM response")