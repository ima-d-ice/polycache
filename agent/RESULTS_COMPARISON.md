# AdaptiCache: Rule vs LLM vs Hybrid Decision Modes

A/B comparison of the agent's decision modes, each run on a fresh server replaying the identical generated workload (90000 requests, 1MB cache, 100000-key space, seeds 1, 7, 42, 123, 999 (5 seeds)).  Hit rates are per-mode means +/- one standard deviation across seeds; the per-seed table is below.

## Executive Summary

- Highest mean hit rate: **llm** (overall HR: rule=0.6542 +/- 0.0095, llm=0.6647 +/- 0.0408, hybrid=0.6512 +/- 0.0073, hybrid_echo=0.6494 +/- 0.0065).  Mean-based ranking; the Recommendation below applies the multi-seed majority-wins verdict.
- LLM mode vs rule: better (1.05 pt).
- Hybrid mode vs rule: worse (-0.30 pt).
- LLM fallback rate (LLMError -> rule decision): llm 0.0%, hybrid 0.0%.

- **Timing control** (rule vs hybrid_echo, ~0ms LLM that echoes the rule): 0.48 pt gap -- harness is clean (gap < 0.5 pt), so mode differences are attributable to the decision logic.

## Performance Table

| Mode | Overall HR | P1 HR | P2 HR | P3 HR | Switches | LLM Calls | Fallback % | Avg LLM Latency |
|---|---|---|---|---|---|---|---|---|
| rule | 0.6542 +/- 0.0095 | 0.8096 | 0.5915 | 0.5693 | 3.0 | 0 | 0.0% | - |
| llm | 0.6647 +/- 0.0408 | 0.7993 | 0.6219 | 0.5804 | 5.0 | 75 | 0.0% | 534.43 ms |
| hybrid | 0.6512 +/- 0.0073 | 0.8069 | 0.5871 | 0.5674 | 3.0 | 0 | 0.0% | - |
| hybrid_echo | 0.6494 +/- 0.0065 | 0.8025 | 0.5887 | 0.5648 | 3.0 | 0 | 0.0% | - |

## Per-Seed Results

| Seed | rule | llm | hybrid | hybrid_echo | winner |
|---|---|---|---|---|---|
| 1 | 0.6483 | 0.6338 | 0.6430 | 0.6412 | rule |
| 7 | 0.6502 | 0.7242 | 0.6568 | 0.6470 | llm |
| 42 | 0.6626 | 0.6894 | 0.6437 | 0.6469 | llm |
| 123 | 0.6660 | 0.6296 | 0.6552 | 0.6576 | rule |
| 999 | 0.6440 | 0.6466 | 0.6575 | 0.6544 | hybrid |

- Per-seed wins: hybrid 1, llm 2, rule 2.

## Cost Analysis

Groq output pricing used (per 1M tokens): gpt-oss-120b $0.50, llama-3.3-70b-versatile $0.30, qwen-3.6-27b $0.20.

| Mode | Total tokens | Est. cost (USD) |
|---|---|---|
| rule | 0 | $0.00 |
| llm | 32742 | $0.0110 |
| hybrid | 0 | $0.0000 |

Per-model share (llm run): gpt-oss-120b 26 calls (~$0.0057), llama-3.3-70b-versatile 25 calls (~$0.0033), qwen-3.6-27b 24 calls (~$0.0021)

## Latency Analysis

| Mode | Avg LLM latency | Max LLM latency | Decisions |
|---|---|---|---|
| llm | 534.43 ms | 1877.80 ms | 75 |
| hybrid | - | - | 0 |

Hybrid consults the LLM ONLY when the rule proposes a switch (and the cooldown would allow it) -- steady-state cycles pay no LLM latency.  Each such consult is synchronous (~- average).

## Model Reliability

- Fallback rate (LLMError -> rule decision): llm 0.0%, hybrid 0.0% -- when a consult does fail, the agent logs it and falls back to the rule decision for that cycle instead of crashing.
- Successful-call model distribution (llm run): gpt-oss-120b 26, llama-3.3-70b-versatile 25, qwen-3.6-27b 24

## Agreement Analysis (hybrid)

Hybrid is **diagnostic-only**: the rule proposes a switch and the LLM assesses it (approve/confidence/reason) on proposals; the rule always decides and the LLM cannot veto.  Consults are logged as `kind:llm` entries for post-hoc analysis.

- Hybrid consults this run: 0 (agreement n/a (no consults)).
- Echo control agreement: - (by construction).

## What the LLM actually did (multi-seed experiments)

- **Design (final)**: `hybrid` is diagnostic-only -- the rule proposes, the LLM assesses, the rule decides; `llm` mode is deprecated.  Three controlled experiments led here:
- **(1) Un-gated consults**: the LLM vetoed the pre-workload lru->lfu switch with "no activity, keep current stable policy" and P1 collapsed (overall -30 to -41 pt at every seed).  Fixed by gating consults on observed traffic.
- **(2) Evidence-gated veto**: hybrid consulted on proposals and the LLM vetoed 66.7% of them -- hybrid became static LFU (agreement 33.3%, -10 pt P2 / +12 pt P3 trade).  The tight spread was the veto's default action, not LLM insight.
- **(3) Confidence gate + enriched signals**: a `rule_confidence < 0.8` gate plus raw zipf/scan-ratio/churn/trend/switch-history signals in the prompt.  The gate never opened (churn saturates rule_confidence at 1.0 in request-quantized mode), so hybrid silently equaled rule, and the enriched `llm` mode still flip-flopped (per-seed -3.6 to +7.4 pt -- a roll, not a strategy).
- **This run**: hybrid consulted 0 time(s); `llm` mode made 75 calls and beat rule on 2/5 seeds (per-seed wins above).

### Regression evidence (before the fixes)

With decisions starting mid-phase-1 and the LLM consulted pre-evidence, the real Groq LLM cost 26-41 pt at every seed (rule 0.6623 +/- 0.0067, llm 0.2528 +/- 0.0494, hybrid 0.3626 +/- 0.1084).  Single-seed "LLM wins" claims are unreliable on this workload: both the decision timing and the LLM roll change the outcome by +-40 pt.

## Recommendation

**INCONCLUSIVE.** No mode beats rule by >= 1 pt on a majority of seeds (gaps: llm +1.1, hybrid -0.3, hybrid_echo -0.5). Use rule.

