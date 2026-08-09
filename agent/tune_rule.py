"""Offline rule-threshold optimizer for the AdaptiCache tuning agent.

Consumes a multi-seed compare_results.json (and optionally per-mode
decision logs) and asks the LLM to propose numeric deltas for the rule
classifier's thresholds -- for HUMAN REVIEW only, never auto-applied.

The point: the rule's decision boundaries (SKEW_ZIPF 1.2, SCAN_RATIO 0.4,
HIGH_CHURN_PER_WINDOW 5.0) were picked once and never tuned.  This script
gives the LLM the physics + the observed switch/failure history and asks
for concrete threshold changes, validated out-of-sample by the operator.

Usage:
    export GROQ_API_KEY_1=...   # or rely on .env
    python3 agent/tune_rule.py --results ./compare_results.json \
        --decisions /tmp/run.s42.compare.rule.decisions.jsonl [--out /tmp/tune_proposal.json]
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional

from llm_client import LLMError, RoundRobinLLMClient

TUNE_PROMPT = (
    "You are tuning the decision thresholds of a rule-based cache policy "
    "classifier.  The classifier maps workload signals to an eviction "
    "policy:\n"
    "  skewed (zipf > SKEW_ZIPF and hit_rate > 0.5) -> sieve\n"
    "  scanning (scan_ratio > SCAN_RATIO) -> lru\n"
    "  bursty (churn per window >= HIGH_CHURN_PER_WINDOW and volatile) "
    "-> sieve\n"
    "  stable -> lfu\n"
    "Current thresholds: SKEW_ZIPF = 1.2, SCAN_RATIO = 0.4, "
    "HIGH_CHURN_PER_WINDOW = 5.0, SKEW_MIN_HIT_RATE = 0.5, "
    "VOLATILITY_THRESHOLD = 0.2.\n"
    "Eviction physics: the server never inserts on GET miss and every SET "
    "evicts one key, so under cold churn all policies' eviction frontiers "
    "walk the preload order; the burst pool (preload tail) dies once the "
    "frontier reaches it.  The classifier's churn signal uses a window "
    "scale that saturates at 1.0 confidence in request-quantized mode.\n"
    "You receive aggregated multi-seed results and per-run decision logs "
    "(switches, reasons, hit rates).  Propose SPECIFIC numeric threshold "
    "deltas, each justified by evidence in the logs, and flag each as "
    "testable on unseen seeds.  Reply with EXACTLY one JSON object:\n"
    '{"proposals": [{"parameter": "SKEW_ZIPF|SCAN_RATIO|'
    'HIGH_CHURN_PER_WINDOW|SKEW_MIN_HIT_RATE|VOLATILITY_THRESHOLD", '
    '"current": 1.2, "proposed": 1.1, "rationale": "<25 words>", '
    '"expected_effect": "<15 words>", "validate_on": "unseen seeds"}], '
    '"summary": "<30 words>"}'
)


def _load_results(path: Path) -> Dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _decision_highlights(paths: List[Path]) -> List[Dict]:
    """Condense each decision log to switch events + arbitration consults."""
    out: List[Dict] = []
    for p in paths:
        try:
            lines = [json.loads(ln) for ln in p.read_text(encoding="utf-8")
                     .splitlines() if ln.strip()]
        except OSError:
            continue
        switches = [d for d in lines if d.get("kind") == "switch"]
        arb = [d for d in lines
               if d.get("kind") == "llm" and d.get("role") == "arbiter"]
        out.append({
            "log": str(p),
            "switches": [{
                "old_policy": d.get("old_policy"),
                "new_policy": d.get("new_policy"),
                "reason": d.get("reason", ""),
                "hit_rate_snapshot": (d.get("metrics_snapshot") or {})
                .get("hit_rate"),
            } for d in switches],
            "arbitrations": [{
                "rule_policy": d.get("rule_policy"),
                "physics_policy": d.get("physics_policy"),
                "llm_policy": d.get("llm_policy"),
                "llm_trust": d.get("llm_trust"),
                "llm_reason": d.get("llm_reason", ""),
            } for d in arb],
        })
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="propose rule-threshold deltas from multi-seed evidence "
                    "(human review only)")
    parser.add_argument("--results", required=True,
                        help="compare_results.json from a --compare run")
    parser.add_argument("--decisions", nargs="+", default=[],
                        help="agent decision JSONL files (switch/arbitration "
                             "history for the LLM)")
    parser.add_argument("--out", default=None,
                        help="write the LLM's proposal JSON here")
    args = parser.parse_args(argv)

    results = _load_results(Path(args.results))
    highlights = _decision_highlights([Path(p) for p in args.decisions])
    payload = {
        "aggregated": results.get("aggregated", {}),
        "per_seed": results.get("per_seed", {}),
        "verdict": results.get("verdict", {}),
        "decision_highlights": highlights,
        "thresholds_current": {
            "SKEW_ZIPF": 1.2,
            "SCAN_RATIO": 0.4,
            "HIGH_CHURN_PER_WINDOW": 5.0,
            "SKEW_MIN_HIT_RATE": 0.5,
            "VOLATILITY_THRESHOLD": 0.2,
        },
    }
    client = RoundRobinLLMClient()
    if not client.ready:
        print("no GROQ_API_KEY_* in environment -- cannot consult the LLM. "
              "Export keys first (set -a; source .env; set +a).",
              file=sys.stderr)
        return 1
    try:
        resp = client._chat([  # noqa: SLF001 - raw consult, no wrapper needed
            {"role": "system", "content": TUNE_PROMPT},
            {"role": "user",
             "content": json.dumps(payload, indent=1)[:24000]},
        ])
        proposal = json.loads(_extract_json(resp["content"]))
    except (LLMError, ValueError) as exc:
        print("consult failed: %s" % exc, file=sys.stderr)
        return 1

    print(json.dumps(proposal, indent=2))
    print("\n== REVIEW ==")
    print("Threshold proposals are SUGGESTIONS. Validate on seeds the "
          "optimizer never saw (e.g. tune on 1/7/42, validate on 123/999) "
          "before touching analyzer.py constants.")
    if args.out:
        Path(args.out).write_text(json.dumps(proposal, indent=2) + "\n",
                                  encoding="utf-8")
        print("wrote %s" % args.out)
    return 0


def _extract_json(content: str) -> str:
    import re
    text = content.strip()
    if text.startswith("```"):
        lines = [ln for ln in text.splitlines() if not ln.startswith("```")]
        text = "\n".join(lines).strip()
    for start, ch in enumerate(text):
        if ch in "{[":  # noqa: SIM300
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


if __name__ == "__main__":
    sys.exit(main())
