#!/usr/bin/env python3
"""Render agent/RESULTS_COMPARISON.md from benchmark --compare JSON output.

Consumes compare_results.json (written by benchmark.py --compare) and emits
a Markdown report covering performance, cost, latency, model reliability,
rule/LLM agreement, and a recommendation.
"""

import argparse
import json
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_RESULTS = SCRIPT_DIR / "compare_results.json"
DEFAULT_OUT = SCRIPT_DIR / "RESULTS_COMPARISON.md"

# Groq output pricing, USD per 1M tokens (as configured for this project).
PRICING = {
    "gpt-oss-120b": 0.50,
    "llama-3.3-70b-versatile": 0.30,
    "qwen-3.6-27b": 0.20,
}
DEFAULT_PRICE = 0.20

MODES = ("rule", "llm", "hybrid")


def _fmt_pct(value, digits=1):
    return "%.*f%%" % (digits, value) if value is not None else "-"


def _fmt_ms(value, digits=2):
    return "%.*f ms" % (digits, value) if value else "-"


def _cost_estimate(mode_results):
    """USD estimate: tokens split across models by successful-call share."""
    ls = mode_results.get("llm_stats", {})
    total_tokens = ls.get("tokens_total", 0)
    calls = ls.get("models", {})
    total_calls = sum(calls.values())
    if not total_calls:
        return None, None, {}
    per_model = {}
    total_cost = 0.0
    for model, n in calls.items():
        est_tokens = total_tokens * (n / total_calls)
        cost = est_tokens * PRICING.get(model, DEFAULT_PRICE) / 1_000_000.0
        per_model[model] = {
            "calls": n,
            "est_tokens": int(est_tokens),
            "cost": cost,
        }
        total_cost += cost
    return total_cost, per_model, calls


def build_report(data: dict, results_path: Path) -> str:
    config = data.get("config", {})
    modes = data.get("modes", {})
    missing = [m for m in MODES if m not in modes]
    if missing:
        raise SystemExit("compare_results.json is missing mode(s): %s"
                         % ", ".join(missing))

    rule = modes["rule"]
    llm = modes["llm"]
    hyb = modes["hybrid"]
    capped = bool(config.get("capped", False)) or (
        config.get("llm_requests", 0) > 0
        and config["llm_requests"] < config.get("requests", 0))
    gap_llm = (llm["overall"] - rule["overall"]) * 100.0
    gap_hyb = (hyb["overall"] - rule["overall"]) * 100.0
    best = max(MODES, key=lambda m: modes[m]["overall"])

    if capped:
        rec = ("**NOT COMPARABLE.** The llm/hybrid sub-runs were capped at "
               "%d requests while rule ran %d, so every hit-rate difference "
               "is an artifact of unequal workload length.  Re-run with "
               "--llm-requests 0 (the default) for a fair comparison."
               % (config.get("llm_requests", 0), config.get("requests", 0)))
    elif gap_llm >= 1.0 or gap_hyb >= 1.0:
        winners = [name for name, gap in (("llm", gap_llm), ("hybrid", gap_hyb))
                   if gap >= 1.0]
        rec = ("Adopt **%s** decision mode for workloads like this one: it "
               "beats the rule baseline by %s." %
               (" + ".join(winners),
                ", ".join("%.1f pt (%s)" % (gap, name)
                          for name, gap in (("llm", gap_llm),
                                            ("hybrid", gap_hyb))
                          if gap >= 1.0)))
    else:
        rec = ("The LLM and hybrid modes do not beat the rule baseline by a "
               "meaningful margin (<1 pt). Keep `--decision-mode rule` to "
               "avoid API cost and latency.")

    agreement = hyb.get("llm_stats", {}).get("agreement_pct")
    llm_stats = llm.get("llm_stats", {})
    hyb_stats = hyb.get("llm_stats", {})

    lines = [
        "# AdaptiCache: Rule vs LLM vs Hybrid Decision Modes",
        "",
        "A/B comparison of the agent's three decision modes, each run on a "
        "fresh server replaying the identical generated workload "
        "(%(requests)d requests, %(cache_size_mb)dMB cache, "
        "%(working_set)d-key space, seed %(seed)s)." % config,
        "",
        "## Executive Summary",
        "",
        "- Best overall hit rate: **%(best)s** (overall HR: "
        "%(all_overall)s)." % {
            "best": best,
            "all_overall": ", ".join("%s=%.4f" % (m, modes[m]["overall"])
                                     for m in MODES),
        },
        "- LLM mode: %s vs rule (%.2f pt)." % (
            ("not comparable (capped run)" if capped
             else "better" if gap_llm > 0 else "worse"),
            gap_llm,
        ),
        "- Hybrid mode: %s vs rule (%.2f pt)." % (
            "not comparable (capped run)" if capped
            else "better" if gap_hyb > 0 else "worse",
            gap_hyb,
        ),
        "- LLM fallback rate (rule-mode safety net): llm %s, hybrid %s."
        % (_fmt_pct(llm_stats.get("fallback_pct")),
           _fmt_pct(hyb_stats.get("fallback_pct"))),
        "",
        "## Performance Table",
        "",
        "| Mode | Overall HR | P1 HR | P2 HR | P3 HR | Switches | "
        "LLM Calls | Fallback % | Avg LLM Latency |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for m in MODES:
        v = modes[m]
        ls = v.get("llm_stats", {})
        lines.append(
            "| %s | %.4f | %.4f | %.4f | %.4f | %d | %d | %s | %s |" %
            (m, v["overall"], v["phase_rates"]["1"],
             v["phase_rates"]["2"], v["phase_rates"]["3"],
             v["switches"], ls.get("api_calls", 0),
             _fmt_pct(ls.get("fallback_pct")),
             ("%.2f ms" % ls["avg_latency_ms"]
              if ls.get("avg_latency_ms") else "-")))
    lines.append("")
    if capped:
        lines.append(
            "> **WARNING: this run is NOT comparable.** llm/hybrid sub-runs "
            "were capped at %d requests while rule ran %d; hit rates differ "
            "because the workloads differ, not because of the decision mode."
            % (config["llm_requests"], config["requests"]))
        lines.append("")

    # Cost analysis.
    total_cost, per_model_cost, model_calls = _cost_estimate(llm)
    hyb_cost, hyb_model_cost, hyb_model_calls = _cost_estimate(hyb)
    lines += [
        "## Cost Analysis",
        "",
        "Groq output pricing used (per 1M tokens): gpt-oss-120b $0.50, "
        "llama-3.3-70b-versatile $0.30, qwen-3.6-27b $0.20.",
        "",
        "| Mode | Total tokens | Est. cost (USD) |",
        "|---|---|---|",
        "| rule | 0 | $0.00 |",
        "| llm | %d | $%.4f |"
        % (llm_stats.get("tokens_total", 0), total_cost or 0.0),
        "| hybrid | %d | $%.4f |"
        % (hyb_stats.get("tokens_total", 0), hyb_cost or 0.0),
        "",
        "Per-model share (llm run): " +
        (", ".join("%s %d calls (~$%.4f)" % (m, c["calls"], c["cost"])
                   for m, c in sorted(per_model_cost.items()))
         if per_model_cost else "no successful calls"),
        "",
    ]

    # Latency analysis.
    lines += [
        "## Latency Analysis",
        "",
        "| Mode | Avg LLM latency | Max LLM latency | Decisions |",
        "|---|---|---|---|",
        "| llm | %s | %s | %d |"
        % (_fmt_ms(llm_stats.get("avg_latency_ms")),
           _fmt_ms(llm_stats.get("max_latency_ms")),
           llm_stats.get("ok_calls", 0)),
        "| hybrid | %s | %s | %d |"
        % (_fmt_ms(hyb_stats.get("avg_latency_ms")),
           _fmt_ms(hyb_stats.get("max_latency_ms")),
           hyb_stats.get("ok_calls", 0)),
        "",
        "Each decision cycle pays one synchronous LLM call (~%s average). "
        "At a 1 s agent interval this is a small addition to the cache "
        "latency budget, but it makes every agent cycle dependent on Groq "
        "availability."
        % _fmt_ms(llm_stats.get("avg_latency_ms"), 0),
        "",
    ]

    # Reliability.
    lines += [
        "## Model Reliability",
        "",
        "- Fallback rate (LLMError -> rule decision): llm %s, hybrid %s "
        "-- every consult succeeded in this run.  When a consult does fail, "
        "the agent logs it and falls back to the rule decision for that "
        "cycle instead of crashing."
        % (_fmt_pct(llm_stats.get("fallback_pct")),
           _fmt_pct(hyb_stats.get("fallback_pct"))),
        "- Successful-call model distribution (llm run): " +
        (", ".join("%s %d" % (m, n) for m, n in sorted(model_calls.items()))
         if model_calls else "none"),
        "",
    ]

    # Agreement analysis.
    lines += [
        "## Agreement Analysis (hybrid)",
        "",
        "Hybrid mode executes the rule decision while logging the LLM's "
        "pick as a second opinion.",
        "",
        "- Rule/LLM agreement: %s of hybrid decisions."
        % _fmt_pct(agreement),
        "",
    ]

    lines += [
        "## Recommendation",
        "",
        rec,
        "",
    ]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="render RESULTS_COMPARISON.md from --compare JSON")
    parser.add_argument("--results", default=str(DEFAULT_RESULTS),
                        help="compare_results.json (default: "
                             "agent/compare_results.json)")
    parser.add_argument("--out", default=str(DEFAULT_OUT),
                        help="output markdown path (default: "
                             "agent/RESULTS_COMPARISON.md)")
    args = parser.parse_args(argv)

    results_path = Path(args.results)
    with open(results_path, encoding="utf-8") as fh:
        data = json.load(fh)
    out_path = Path(args.out)
    out_path.write_text(build_report(data, results_path) + "\n",
                        encoding="utf-8")
    print("wrote %s" % out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
