"""Render RESULTS_COMPARISON.md from the compare run's JSON.

Supports both schemas:
  - single-seed:  {config, modes: {mode: {overall, phase_rates, ...}}}
  - multi-seed:   {config, seeds, per_seed, aggregated, verdict}

The multi-seed verdict is produced by the benchmark's aggregation and is
rendered verbatim; the recommendation therefore can never outrun the
evidence.
"""

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_RESULTS = SCRIPT_DIR.parent / "compare_results.json"
DEFAULT_OUT = SCRIPT_DIR / "RESULTS_COMPARISON.md"

PRICING = {
    "gpt-oss-120b": 0.50,
    "llama-3.3-70b-versatile": 0.30,
    "qwen-3.6-27b": 0.20,
}
DEFAULT_PRICE = 0.20

MODES = ("rule", "llm", "hybrid", "hybrid_conflict",
         "hybrid_echo", "hybrid_conflict_echo")


def _fmt_pct(value, digits=1):
    return "%.*f%%" % (digits, value) if value is not None else "-"


def _fmt_ms(value, digits=2):
    return "%.*f ms" % (digits, value) if value else "-"


def _fmt_stat(stat, digits=4):
    """Format a mean +/- std stat (std shown only for multi-seed runs)."""
    if stat is None:
        return "-"
    base = "%.*f" % (digits, stat["mean"])
    if stat.get("std", 0.0) > 0.0:
        base += " +/- %.*f" % (digits, stat["std"])
    return base


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
    aggregated = data.get("aggregated", {})
    per_seed = data.get("per_seed", {})
    seeds = data.get("seeds", [])
    verdict = data.get("verdict") or {}
    multi = bool(aggregated)

    missing = [m for m in MODES
               if m not in (aggregated if multi else modes)]
    if missing:
        raise SystemExit("compare_results.json is missing mode(s): %s"
                         % ", ".join(missing))

    capped = bool(config.get("capped", False)) or (
        config.get("llm_requests", 0) > 0
        and config["llm_requests"] < config.get("requests", 0))

    def overall(mode):
        return (aggregated[mode]["overall"]["mean"] if multi
                else modes[mode]["overall"])

    def phase(mode, ph):
        if multi:
            return aggregated[mode]["phase_rates"][str(ph)]["mean"]
        return modes[mode]["phase_rates"][str(ph)]

    def switches(mode):
        return aggregated[mode]["switches"] if multi else modes[mode]["switches"]

    def llm_stats(mode):
        return (aggregated[mode]["llm_stats"] if multi
                else modes[mode].get("llm_stats", {}))

    rule_overall = overall("rule")
    best = max(MODES, key=overall)
    gaps_pt = {m: (overall(m) - rule_overall) * 100.0 for m in MODES}

    rec = str(verdict.get("text") or "")
    if not rec:
        if capped:
            rec = ("**NOT COMPARABLE.** The llm/hybrid sub-runs were capped "
                   "at %d requests while rule ran %d, so every hit-rate "
                   "difference is an artifact of unequal workload length.  "
                   "Re-run with --llm-requests 0 (the default) for a fair "
                   "comparison." % (config.get("llm_requests", 0),
                                    config.get("requests", 0)))
        elif gaps_pt["llm"] >= 1.0:
            rec = ("Adopt **llm** decision mode for workloads like this "
                   "one: it beats the rule baseline by %.1f pt.  (hybrid "
                   "is diagnostic-only and equals rule by construction.)"
                   % gaps_pt["llm"])
        else:
            rec = ("Neither LLM mode beats the rule baseline by a "
                   "meaningful margin (<1 pt). Keep "
                   "`--decision-mode rule` to avoid API cost and latency.")

    llm_stats_m = llm_stats("llm")
    hyb_stats = llm_stats("hybrid")
    echo_stats = llm_stats("hybrid_echo")

    intro = ("A/B comparison of the agent's decision modes, each run on a "
             "fresh server replaying the identical generated workload "
             "(%(requests)d requests, %(cache_size_mb)dMB cache, "
             "%(working_set)d-key space" % config)
    if multi:
        intro += ", seeds %s (%d seeds)" % (", ".join(map(str, seeds)),
                                            len(seeds))
    intro += ")."
    if multi:
        intro += ("  Hit rates are per-mode means +/- one standard "
                  "deviation across seeds; the per-seed table is below.")

    lines = [
        "# AdaptiCache: Rule vs LLM vs Hybrid Decision Modes",
        "",
        intro,
        "",
        "## Executive Summary",
        "",
        "- Highest mean hit rate: **%(best)s** (overall HR: "
        "%(all_overall)s).  Mean-based ranking; the Recommendation below "
        "applies the multi-seed majority-wins verdict." % {
            "best": best,
            "all_overall": ", ".join("%s=%s" % (m, _fmt_stat(
                aggregated[m]["overall"] if multi
                else {"mean": modes[m]["overall"], "std": 0.0}))
                for m in MODES),
        },
        "- LLM mode vs rule: %s." % (
            "not comparable (capped run)" if capped
            else "%s (%.2f pt)" % ("better" if gaps_pt["llm"] > 0 else "worse",
                                   gaps_pt["llm"])),
        "- Hybrid mode vs rule: %s." % (
            "not comparable (capped run)" if capped
            else "%s (%.2f pt)" % ("better" if gaps_pt["hybrid"] > 0
                                   else "worse", gaps_pt["hybrid"])),
        "- LLM fallback rate (LLMError -> rule decision): llm %s, hybrid %s."
        % (_fmt_pct(llm_stats_m.get("fallback_pct")),
           _fmt_pct(hyb_stats.get("fallback_pct"))),
        "",
    ]
    if multi and verdict.get("echo_control_gap_pt") is not None:
        echo_gap = verdict["echo_control_gap_pt"]
        echo_label = (
            "harness is clean (gap < 0.5 pt), so mode differences are "
            "attributable to the decision logic"
            if echo_gap < 0.5
            else "**HARNESS FAILED: differences are NOT attributable "
                 "to decision logic**"
        )
        lines += [
            "- **Timing control** (rule vs hybrid_echo, ~0ms LLM that echoes "
            "the rule): %.2f pt gap -- %s." % (echo_gap, echo_label),
            "",
        ]

    lines += [
        "## Performance Table",
        "",
        "| Mode | Overall HR | P1 HR | P2 HR | P3 HR | Switches | "
        "LLM Calls | Fallback % | Avg LLM Latency |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for m in MODES:
        ls = llm_stats(m)
        overall_fmt = (_fmt_stat(aggregated[m]["overall"]) if multi
                       else "%.4f" % modes[m]["overall"])
        lines.append(
            "| %s | %s | %.4f | %.4f | %.4f | %s | %d | %s | %s |" %
            (m, overall_fmt, phase(m, 1), phase(m, 2), phase(m, 3),
             switches(m), ls.get("api_calls", 0),
             _fmt_pct(ls.get("fallback_pct")),
             ("%.2f ms" % ls["avg_latency_ms"]
              if ls.get("avg_latency_ms") else "-")))
    lines.append("")
    if capped:
        lines += [
            "> **WARNING: this run is NOT comparable.** llm/hybrid sub-runs "
            "were capped at %d requests while rule ran %d; hit rates differ "
            "because the workloads differ, not because of the decision mode."
            % (config["llm_requests"], config["requests"]),
            "",
        ]

    if multi:
        lines += [
            "## Per-Seed Results",
            "",
            "| Seed | rule | llm | hybrid | hybrid_echo | winner |",
            "|---|---|---|---|---|---|",
        ]
        for seed in seeds:
            sm = per_seed[str(seed)]
            winner = max(MODES, key=lambda m: sm[m]["overall"])
            lines.append(
                "| %s | %.4f | %.4f | %.4f | %.4f | %s |"
                % (seed, sm["rule"]["overall"], sm["llm"]["overall"],
                   sm["hybrid"]["overall"], sm["hybrid_echo"]["overall"],
                   winner))
        lines.append("")
        wins = verdict.get("per_seed_wins", {})
        if wins:
            lines += [
                "- Per-seed wins: %s." % ", ".join(
                    "%s %d" % (m, n) for m, n in sorted(wins.items())),
                "",
            ]

    # Cost analysis.
    total_cost, per_model_cost, model_calls = _cost_estimate(
        aggregated["llm"] if multi else modes["llm"])
    hyb_cost, hyb_model_cost, hyb_model_calls = _cost_estimate(
        aggregated["hybrid"] if multi else modes["hybrid"])
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
        % (llm_stats_m.get("tokens_total", 0), total_cost or 0.0),
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
        % (_fmt_ms(llm_stats_m.get("avg_latency_ms")),
           _fmt_ms(llm_stats_m.get("max_latency_ms")),
           llm_stats_m.get("ok_calls", 0)),
        "| hybrid | %s | %s | %d |"
        % (_fmt_ms(hyb_stats.get("avg_latency_ms")),
           _fmt_ms(hyb_stats.get("max_latency_ms")),
           hyb_stats.get("ok_calls", 0)),
        "",
        "Hybrid consults the LLM ONLY when the rule proposes a switch "
        "(and the cooldown would allow it) -- steady-state cycles pay no "
        "LLM latency.  Consults are FIRE-AND-FORGET (async worker threads): "
        "the rule switch executes at grid time and the consult annotates "
        "after the fact, so consult latency (~%s average) can never shift "
        "the switch grid."
        % _fmt_ms(hyb_stats.get("avg_latency_ms"), 0),
        "",
    ]

    # Reliability.
    lines += [
        "## Model Reliability",
        "",
        "- Fallback rate (LLMError -> rule decision): llm %s, hybrid %s "
        "-- when a consult does fail, the agent logs it and falls back to "
        "the rule decision for that cycle instead of crashing."
        % (_fmt_pct(llm_stats_m.get("fallback_pct")),
           _fmt_pct(hyb_stats.get("fallback_pct"))),
        "- Successful-call model distribution (llm run): " +
        (", ".join("%s %d" % (m, n) for m, n in sorted(model_calls.items()))
         if model_calls else "none"),
        "",
    ]

    # Agreement analysis (hybrid) + arbitration analysis (hybrid_conflict).
    hyb_agreement = hyb_stats.get("agreement_pct")
    hyb_calls = hyb_stats.get("api_calls", 0)
    conf_stats = llm_stats("hybrid_conflict")
    conf_calls = conf_stats.get("arbiter_calls", 0)
    conf_picks = conf_stats.get("arbiter_picks") or []
    lines += [
        "## Agreement & Arbitration Analysis (hybrid modes)",
        "",
        "Hybrid is **diagnostic-only**: the rule proposes a switch and the "
        "LLM assesses it (approve/confidence/reason) on proposals; the rule "
        "always decides and the LLM cannot veto.  Consults are logged as "
        "`kind:llm` entries for post-hoc analysis.",
        "",
        "- Hybrid consults this run: %d (agreement %s)."
        % (hyb_calls, _fmt_pct(hyb_agreement) if hyb_agreement is not None
           else "n/a (no consults)"),
        "- Echo control agreement: %s (by construction)."
        % _fmt_pct(echo_stats.get("agreement_pct")),
        "",
        "`hybrid_conflict` adds a deterministic eviction-physics signal "
        "(burst-pool survival eta).  The LLM is consulted ONLY when the "
        "physics proposal and the rule proposal disagree, and its pick "
        "executes there (scoped veto).",
        "",
        "- Rule/physics conflicts arbitrated: %d (picks: %s)."
        % (conf_calls, ", ".join(str(p) for p in conf_picks) or "none"),
        "",
    ]

    # Multi-seed experiment narrative: what the LLM layer actually is and
    # the three controlled experiments that demoted it.  Static design
    # history + this run's observed numbers.  Only emitted for multi-seed
    # compares.
    if multi:
        llm_calls = llm_stats_m.get("api_calls", 0)
        llm_wins = wins.get("llm", 0)
        lines += [
            "## What the LLM actually did (multi-seed experiments)",
            "",
            "- **Design (final)**: `hybrid` is diagnostic-only -- the rule "
            "proposes, the LLM assesses, the rule decides; `llm` mode is "
            "deprecated.  `hybrid_conflict` (the follow-up experiment) gives "
            "the LLM a scoped veto ONLY when a deterministic eviction-physics "
            "signal and the rule disagree.  Three controlled experiments led "
            "here:",
            "- **(1) Un-gated consults**: the LLM vetoed the pre-workload "
            "lru->lfu switch with \"no activity, keep current stable "
            "policy\" and P1 collapsed (overall -30 to -41 pt at every "
            "seed).  Fixed by gating consults on observed traffic.",
            "- **(2) Evidence-gated veto**: hybrid consulted on proposals "
            "and the LLM vetoed 66.7% of them -- hybrid became static LFU "
            "(agreement 33.3%, -10 pt P2 / +12 pt P3 trade).  The tight "
            "spread was the veto's default action, not LLM insight.",
            "- **(3) Confidence gate + enriched signals**: a "
            "`rule_confidence < 0.8` gate plus raw zipf/scan-ratio/churn/"
            "trend/switch-history signals in the prompt.  The gate never "
            "opened (churn saturates rule_confidence at 1.0 in "
            "request-quantized mode), so hybrid silently equaled rule, and "
            "the enriched `llm` mode still flip-flopped (per-seed -3.6 to "
            "+7.4 pt -- a roll, not a strategy).",
            "- **This run**: hybrid consulted %d time(s); `llm` mode made "
            "%d calls and beat rule on %d/%d seeds (per-seed wins above)."
            % (hyb_calls, llm_calls, llm_wins, len(seeds)),
            "",
            "### Regression evidence (before the fixes)",
            "",
            "With decisions starting mid-phase-1 and the LLM consulted "
            "pre-evidence, the real Groq LLM cost 26-41 pt at every seed "
            "(rule 0.6623 +/- 0.0067, llm 0.2528 +/- 0.0494, hybrid "
            "0.3626 +/- 0.1084).  Single-seed \"LLM wins\" claims are "
            "unreliable on this workload: both the decision timing and the "
            "LLM roll change the outcome by +-40 pt.",
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
