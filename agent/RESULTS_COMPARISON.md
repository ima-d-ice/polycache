# AdaptiCache: Rule vs LLM vs Hybrid Decision Modes

A/B comparison of the agent's decision modes, each run on a fresh server replaying the identical generated workload (90000 requests, 1MB cache, 100000-key space, seeds 1, 7, 42, 123, 999 (5 seeds), 6 modes: rule / llm / hybrid / hybrid_conflict / hybrid_echo / hybrid_conflict_echo).  Hit rates are per-mode means +/- one standard deviation across seeds.  Two regimes: **adversarial** churn (0.50/0.50 cold+scan, the verified-divergence config) and **moderate** churn (0.30/0.30, where the rule's signals stay near their decision boundaries).

**2026-08-09 update (experiment 4, clean):** the previous compare (4a) was contaminated by a harness artifact: synchronous LLM consults (2.1-3.6s) shifted the effective switch positions ~6.5K requests off the decision grid, swinging single-seed results +13..20pt for EVERY consult mode.  All consults are now **fire-and-forget** (async worker threads; the rule executes at grid time; llm/arbiter verdicts apply at the next decide point).  Results below are the clean re-run; the contamination analysis is documented at the end.

## Executive Summary (adversarial churn, clean run)

- Highest mean hit rate: **hybrid_conflict_echo** (rule=0.6506 +/- 0.0065, llm=0.6489 +/- 0.0114, hybrid=0.6522 +/- 0.0042, hybrid_conflict=0.6502 +/- 0.0062, hybrid_echo=0.6517 +/- 0.0097, hybrid_conflict_echo=0.6534 +/- 0.0060).  ALL modes within 0.3 pt of rule.
- LLM mode vs rule: -0.2 pt.  Hybrid vs rule: +0.2 pt.  hybrid_conflict vs rule: -0.0 pt.
- **Timing control** (rule vs hybrid_echo, ~0ms LLM that echoes the rule): **0.11 pt gap** -- harness is clean (gap < 0.5 pt), so mode differences are attributable to the decision logic.
- Verdict: **INCONCLUSIVE.** No mode beats rule by >= 1 pt on a majority of seeds (gaps: llm -0.2, hybrid +0.2, hybrid_conflict -0.0, hybrid_echo +0.1, hybrid_conflict_echo +0.3). **Use rule.**

## Executive Summary (moderate churn, clean run)

- Highest mean hit rate: **llm** (+1.3 pt vs rule; wins on 3/5 seeds) -- the first regime where an LLM mode clears the adopt bar.  Read the caveat in the Recommendation: the edge is entirely a P2 gain (+6.5 pt) bought with a P3 loss (-2.3 pt) at every seed, and it does not reproduce across regimes (adversarial: llm -0.2 pt).
- Timing control: **0.01 pt gap** -- harness is clean.
- LLM fallback rate (LLMError -> rule decision): llm 0.0%, hybrid 0.0% in both regimes.

## Performance Table (adversarial churn)

| Mode | Overall HR | P1 HR | P2 HR | P3 HR | Switches | LLM Calls | Fallback % | Avg LLM Latency |
|---|---|---|---|---|---|---|---|---|
| rule | 0.6506 +/- 0.0065 | 0.8067 | 0.5863 | 0.5666 | 3.0 | 0 | 0.0% | - |
| llm | 0.6489 +/- 0.0114 | 0.8108 | 0.5925 | 0.5521 | 5.0 | 60 | 0.0% | 636.04 ms |
| hybrid | 0.6522 +/- 0.0042 | 0.8064 | 0.5884 | 0.5693 | 3.0 | 5 | 0.0% | 2140.63 ms |
| hybrid_conflict | 0.6502 +/- 0.0062 | 0.8133 | 0.5777 | 0.5671 | 3.0 | 0 | 0.0% | - |
| hybrid_echo | 0.6517 +/- 0.0097 | 0.8101 | 0.5813 | 0.5711 | 3.0 | 5 | 0.0% | 0.01 ms |
| hybrid_conflict_echo | 0.6534 +/- 0.0060 | 0.8124 | 0.5877 | 0.5679 | 3.0 | 0 | 0.0% | - |

## Performance Table (moderate churn)

| Mode | Overall HR | P1 HR | P2 HR | P3 HR | Switches | LLM Calls | Fallback % | Avg LLM Latency |
|---|---|---|---|---|---|---|---|---|
| rule | 0.7353 +/- 0.0076 | 0.8736 | 0.6708 | 0.6645 | 3.0 | 0 | 0.0% | - |
| llm | 0.7482 +/- 0.0147 | 0.8717 | 0.7356 | 0.6418 | 5.0 | 68 | 0.0% | 730.62 ms |
| hybrid | 0.7425 +/- 0.0120 | 0.8741 | 0.6856 | 0.6707 | 3.0 | 5 | 0.0% | 3575.07 ms |
| hybrid_conflict | 0.7340 +/- 0.0087 | 0.8727 | 0.6686 | 0.6637 | 3.0 | 0 | 0.0% | - |
| hybrid_echo | 0.7352 +/- 0.0050 | 0.8736 | 0.6705 | 0.6643 | 3.0 | 5 | 0.0% | 0.01 ms |
| hybrid_conflict_echo | 0.7397 +/- 0.0064 | 0.8766 | 0.6773 | 0.6683 | 3.0 | 0 | 0.0% | - |

## Per-Seed Results (adversarial churn)

| Seed | rule | llm | hybrid | hybrid_conflict | winner |
|---|---|---|---|---|---|
| 1 | 0.6470 | 0.6395 | 0.6528 | 0.6492 | hybrid |
| 7 | 0.6527 | 0.6401 | 0.6528 | 0.6523 | hybrid_echo |
| 42 | 0.6416 | 0.6602 | 0.6476 | 0.6555 | llm |
| 123 | 0.6529 | 0.6422 | 0.6492 | 0.6413 | hybrid_conflict_echo |
| 999 | 0.6588 | 0.6624 | 0.6586 | 0.6523 | llm |

- Per-seed wins: hybrid 1, hybrid_conflict_echo 1, hybrid_echo 1, llm 2.  Winner spread is sub-0.5 pt noise.

## Per-Seed Results (moderate churn)

| Seed | rule | llm | hybrid | hybrid_conflict | winner |
|---|---|---|---|---|---|
| 1 | 0.7234 | 0.7269 | 0.7240 | 0.7294 | hybrid_conflict_echo |
| 7 | 0.7377 | 0.7591 | 0.7422 | 0.7436 | llm |
| 42 | 0.7325 | 0.7425 | 0.7422 | 0.7380 | llm |
| 123 | 0.7408 | 0.7482 | 0.7473 | 0.7334 | llm |
| 999 | 0.7422 | 0.7644 | 0.7567 | 0.7256 | llm |

- Per-seed wins: hybrid 1, hybrid_conflict_echo 1, llm 3.  llm gap per seed: +0.4, +2.1, +1.0, +0.7, +2.2 (mean +1.3 pt).

## Cost Analysis (both regimes, 5 seeds)

Groq output pricing used (per 1M tokens): gpt-oss-120b $0.50, llama-3.3-70b-versatile $0.30, qwen-3.6-27b $0.20.

| Mode | Adversarial tokens | Est. cost | Moderate tokens | Est. cost |
|---|---|---|---|---|
| rule | 0 | $0.00 | 0 | $0.00 |
| llm | 26427 | $0.0088 | 29669 | $0.0101 |
| hybrid | 2788 | $0.0014 | 2808 | $0.0014 |

Per-model share (adversarial llm run): gpt-oss-120b 20 calls, llama-3.3-70b-versatile 20 calls, qwen-3.6-27b 20 calls.

## Latency Analysis

| Mode | Avg LLM latency (adv) | Max LLM latency (adv) | Decisions (adv) |
|---|---|---|---|
| llm | 636.04 ms | 3311.45 ms | 60 |
| hybrid | 2140.63 ms | 3507.14 ms | 5 |

Hybrid consults the LLM ONLY when the rule proposes a switch (and the cooldown would allow it) -- steady-state cycles pay no LLM latency.  Consults are **fire-and-forget** (async worker threads): the rule switch executes at grid time and the consult annotates after the fact, so consult latency (~2.1-3.6s average) can never shift the switch grid.

## Model Reliability

- Fallback rate (LLMError -> rule decision): llm 0.0%, hybrid 0.0% in both regimes -- when a consult does fail, the agent logs it and falls back to the rule decision for that cycle instead of crashing.
- Successful-call model distribution (adversarial llm run): gpt-oss-120b 20, llama-3.3-70b-versatile 20, qwen-3.6-27b 20.

## Agreement & Arbitration Analysis (hybrid modes)

Hybrid is **diagnostic-only**: the rule proposes a switch and the LLM assesses it (approve/confidence/reason) on proposals; the rule always decides and the LLM cannot veto.  Consults are logged as `kind:llm` entries for post-hoc analysis.

- Hybrid consults this run: 5 per regime (1 per seed, agreement 60% / 100%).
- Echo control agreement: 100.0% (by construction).

`hybrid_conflict` adds a deterministic eviction-physics signal (burst-pool survival eta).  The LLM is consulted ONLY when the physics proposal and the rule proposal disagree, and its pick executes there (scoped veto, applied at the next decide point since consults are fire-and-forget).

- Rule/physics conflicts arbitrated: **0 in all three clean multi-seed runs** (adversarial x2, moderate x1).  The physics signal fired exactly once per seed at the pool-breach moment (prog ~25K adversarial, ~40-42K moderate, silent at 2/5 moderate seeds) and **agreed with the rule every time** (both propose sieve).  The engineered rule-vs-physics ambiguity never occurs on this workload -- the scoped-veto experiment is a systematic null: the deterministic signal is redundant with the rule classifier at the breach moment.

## The fire-and-forget fix (why the numbers changed from 4a)

Experiment 4a's results (hybrid +12.9..+20.3 pt, llm +7..+15 pt at 4/5 seeds) were 100% harness artifact:

- The diagnostic hybrid made the **same decisions as rule** (verified in the decision logs), yet scored +17 pt -- impossible for decision logic; the only difference was the 2.1-3.6s synchronous consult delaying each switch's *execution* ~6.5K requests off-grid.
- The delay moved the rollback-guardrail trigger past the burst phase (rule rolled back to lfu mid-burst at prog ~36K; the delayed hybrid rolled back at ~63K and stayed on sieve through P2).  Same decisions, different effective switch positions, +-13..20 pt outcome swings.
- The echo controls proved it: with ~0ms consults, hybrid_echo equaled rule within 0.5 pt in the same runs.
- Fix (agent.py): rule decisions execute at grid time; consults run on daemon threads; llm/arbiter verdicts that differ from what executed queue a pending override applied at the next decide point (cooldown-aware, never lost).  Regression-guarded by `agent/test_agent_async_consults.py` (6 offline checks).

## What the LLM actually did (multi-seed experiments)

- **Design (final)**: `hybrid` is diagnostic-only -- the rule proposes, the LLM assesses, the rule decides; `llm` mode is deprecated.  `hybrid_conflict` (the follow-up experiment) gives the LLM a scoped veto ONLY when a deterministic eviction-physics signal and the rule disagree.  Three controlled experiments led here:
- **(1) Un-gated consults**: the LLM vetoed the pre-workload lru->lfu switch with "no activity, keep current stable policy" and P1 collapsed (overall -30 to -41 pt at every seed).  Fixed by gating consults on observed traffic.
- **(2) Evidence-gated veto**: hybrid consulted on proposals and the LLM vetoed 66.7% of them -- hybrid became static LFU (agreement 33.3%, -10 pt P2 / +12 pt P3 trade).  The tight spread was the veto's default action, not LLM insight.
- **(3) Confidence gate + enriched signals**: a `rule_confidence < 0.8` gate plus raw zipf/scan-ratio/churn/trend/switch-history signals in the prompt.  The gate never opened (churn saturates rule_confidence at 1.0 in request-quantized mode), so hybrid silently equaled rule, and the enriched `llm` mode still flip-flopped (per-seed -3.6 to +7.4 pt -- a roll, not a strategy).
- **(4) Clean compare (this run)**: adversarial churn -- all modes within 0.3 pt of rule (llm -0.2).  Moderate churn -- llm +1.3 pt (3/5 seeds) via P2 +6.5 / P3 -2.3 at every seed; hybrid +0.7; hybrid_conflict -0.1.  `hybrid_conflict` arbitrated 0 conflicts across all three clean runs.

### Regression evidence (before the fixes)

With decisions starting mid-phase-1 and the LLM consulted pre-evidence, the real Groq LLM cost 26-41 pt at every seed (rule 0.6623 +/- 0.0067, llm 0.2528 +/- 0.0494, hybrid 0.3626 +/- 0.1084).  Single-seed "LLM wins" claims are unreliable on this workload: both the decision timing and the LLM roll change the outcome by +-40 pt.

## Recommendation

**INCONCLUSIVE -- use rule.**  Adversarial churn (the primary, verified-divergence config): every mode within 0.3 pt of rule (gaps: llm -0.2, hybrid +0.2, hybrid_conflict -0.0, hybrid_echo +0.1, hybrid_conflict_echo +0.3).  Moderate churn: llm is the first mode to clear the adopt bar (+1.3 pt, 3/5 seeds), but the edge is a P2/P3 trade at every seed, llm's per-seed spread is 2x the rule's, and it does not reproduce across regimes -- the same flip-flop roll documented in experiment (3).  Treat the moderate llm result as a candidate signal requiring reproduction on unseen seeds before any adoption; the LLM adds latency and cost for a per-seed coin-flip.
