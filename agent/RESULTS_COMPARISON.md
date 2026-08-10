# AdaptiCache: Rule vs LLM vs Hybrid Decision Modes

A/B comparison of the agent's decision modes, each run on a fresh server replaying the identical generated workload (90000 requests, 1MB cache, 100000-key space, seeds 1, 7, 42, 123, 999 (5 seeds), 6 modes: rule / llm / hybrid / hybrid_conflict / hybrid_echo / hybrid_conflict_echo).  Hit rates are per-mode means +/- one standard deviation across seeds.  Two regimes: **adversarial** churn (0.50/0.50 cold+scan, the verified-divergence config) and **moderate** churn (0.30/0.30).

**2026-08-10 update (the preload-gate fix):** every number in this file was regenerated after the discovery that the previously recorded results (rule 0.6506 adversarial / llm +1.3 pt moderate) were inflated by a second harness artifact: the agent's first decision fired at progress 0, racing the benchmark's preload.  When the switch landed mid-preload, `SWITCH_POLICY` rebuilt the eviction policy on a half-loaded map, and the rebuild's `unordered_map`-hash-order re-insertion scrambled the eviction frontier — hash-order luck, not policy choice, inflated every mode by ~0.3-0.4 overall.  Fixes: the benchmark sends `MARK_PRELOADED` after preload and the agent gates decisions on `preload_complete` (`--no-preload-gate` to skip); the cadence is quantized to the decide grid so the first switch fires at an exact multiple of `decide_every`.  With the gate, decision modes are indistinguishable from rule (echo-control gaps 0.09-0.14 pt) and **all of them lose to static lfu/sieve by ~5.4 pt** — see the "Rule agent vs static policies" section.  The fire-and-forget fix (below) remains in place and unchanged.

## Executive Summary (adversarial churn, 2026-08-10 regen)

- Highest mean hit rate: **rule** (rule=0.2503 +/- 0.0493, llm=0.2496 +/- 0.0491, hybrid=0.2495 +/- 0.0501, hybrid_conflict=0.2498 +/- 0.0497, hybrid_echo=0.2489 +/- 0.0500, hybrid_conflict_echo=0.2499 +/- 0.0489).  ALL modes within 0.15 pt of rule — pure per-seed noise.
- LLM mode vs rule: -0.07 pt.  Hybrid vs rule: -0.08 pt.  hybrid_conflict vs rule: -0.05 pt.
- **Timing control** (rule vs hybrid_echo, ~0ms LLM that echoes the rule): **0.14 pt gap** -- harness is clean (gap < 0.5 pt), so mode differences are attributable to the decision logic.
- Verdict: **INCONCLUSIVE.** No mode beats rule by >= 1 pt on a majority of seeds (gaps: llm -0.1, hybrid -0.1, hybrid_conflict -0.1, hybrid_echo -0.1, hybrid_conflict_echo -0.0). **Use rule.**
- The headline number is now the *static* comparison: rule loses to static lfu/sieve by **5.4 pt** (0.2503 vs 0.3041); rule wins P1 (+3.7 pt) and P2 (+1.6 pt) via its early lfu switch, then collapses in P3 (-20.2 pt) where the thrash/rollback cycle and rebuild scrambles destroy the burst pool.

## Executive Summary (moderate churn, 2026-08-10 regen)

- Highest mean hit rate: **llm** (+0.15 pt vs rule, wins on 3/5 seeds) — same coin-flip band as the adversarial null.  The previously reported +1.3 pt llm edge does NOT reproduce; it was part of the preload-gate artifact.
- Timing control: **0.09 pt gap** -- harness is clean.
- LLM fallback rate (LLMError -> rule decision): llm 0.0%, hybrid 0.0% in both regimes.

## Performance Table (adversarial churn)

| Mode | Overall HR | P1 HR | P2 HR | P3 HR | Switches | LLM Calls | Fallback % | Avg LLM Latency |
|---|---|---|---|---|---|---|---|---|
| rule | 0.2503 +/- 0.0493 | 0.3264 | 0.2450 | 0.1852 | 5.6 | 0 | 0.0% | - |
| llm | 0.2496 +/- 0.0491 | 0.3263 | 0.2442 | 0.1841 | 6.0 | 63 | 0.0% | 605.63 ms |
| hybrid | 0.2495 +/- 0.0501 | 0.3257 | 0.2441 | 0.1846 | 5.6 | 10 | 0.0% | 1478.52 ms |
| hybrid_conflict | 0.2498 +/- 0.0497 | 0.3263 | 0.2440 | 0.1849 | 5.6 | 0 | 0.0% | - |
| hybrid_echo | 0.2489 +/- 0.0500 | 0.3265 | 0.2425 | 0.1836 | 5.6 | 10 | 0.0% | 0.01 ms |
| hybrid_conflict_echo | 0.2499 +/- 0.0489 | 0.3263 | 0.2438 | 0.1853 | 5.8 | 0 | 0.0% | - |

## Performance Table (moderate churn)

| Mode | Overall HR | P1 HR | P2 HR | P3 HR | Switches | LLM Calls | Fallback % | Avg LLM Latency |
|---|---|---|---|---|---|---|---|---|
| rule | 0.3662 +/- 0.0554 | 0.4150 | 0.3777 | 0.3082 | 4.0 | 0 | 0.0% | - |
| llm | 0.3677 +/- 0.0560 | 0.4159 | 0.3782 | 0.3113 | 6.0 | 62 | 0.0% | 591.60 ms |
| hybrid | 0.3667 +/- 0.0551 | 0.4153 | 0.3789 | 0.3082 | 4.0 | 9 | 0.0% | 1251.23 ms |
| hybrid_conflict | 0.3671 +/- 0.0544 | 0.4162 | 0.3786 | 0.3087 | 4.0 | 0 | 0.0% | - |
| hybrid_echo | 0.3671 +/- 0.0550 | 0.4159 | 0.3786 | 0.3091 | 4.0 | 9 | 0.0% | 0.01 ms |
| hybrid_conflict_echo | 0.3661 +/- 0.0543 | 0.4145 | 0.3778 | 0.3084 | 4.0 | 0 | 0.0% | - |

## Per-Seed Results (adversarial churn)

| Seed | rule | llm | hybrid | hybrid_conflict | hybrid_echo | hybrid_conflict_echo | winner |
|---|---|---|---|---|---|---|---|
| 1 | 0.3364 | 0.3351 | 0.3367 | 0.3364 | 0.3359 | 0.3350 | hybrid |
| 7 | 0.2459 | 0.2462 | 0.2458 | 0.2459 | 0.2457 | 0.2463 | hybrid_conflict_echo |
| 42 | 0.2249 | 0.2246 | 0.2243 | 0.2249 | 0.2238 | 0.2254 | hybrid_conflict_echo |
| 123 | 0.2166 | 0.2146 | 0.2139 | 0.2155 | 0.2127 | 0.2154 | rule |
| 999 | 0.2275 | 0.2275 | 0.2270 | 0.2273 | 0.2265 | 0.2275 | rule |

- Per-seed wins: hybrid 1, hybrid_conflict_echo 2, rule 2.  Winner spread is sub-0.2 pt noise.

## Per-Seed Results (moderate churn)

| Seed | rule | llm | hybrid | hybrid_conflict | hybrid_echo | hybrid_conflict_echo | winner |
|---|---|---|---|---|---|---|---|
| 1 | 0.4067 | 0.4040 | 0.4068 | 0.4067 | 0.4072 | 0.4062 | hybrid_echo |
| 7 | 0.3932 | 0.3943 | 0.3939 | 0.3936 | 0.3937 | 0.3914 | llm |
| 42 | 0.3214 | 0.3227 | 0.3208 | 0.3220 | 0.3211 | 0.3213 | llm |
| 123 | 0.4166 | 0.4233 | 0.4169 | 0.4170 | 0.4178 | 0.4163 | llm |
| 999 | 0.2928 | 0.2943 | 0.2949 | 0.2960 | 0.2956 | 0.2954 | hybrid_conflict |

- Per-seed wins: llm 3, hybrid_echo 1, hybrid_conflict 1.  llm gap per seed: -0.27, +0.11, +0.13, +0.67, +0.15 (mean +0.15 pt) -- same noise band as adversarial.

## Cost Analysis (both regimes, 5 seeds)

Groq output pricing used (per 1M tokens): gpt-oss-120b $0.50, llama-3.3-70b-versatile $0.30, qwen-3.6-27b $0.20.

| Mode | Adversarial tokens | Est. cost | Moderate tokens | Est. cost |
|---|---|---|---|---|
| rule | 0 | $0.00 | 0 | $0.00 |
| llm | 27067 | $0.0092 | 26621 | $0.0091 |
| hybrid | 4824 | $0.0019 | 4347 | $0.0015 |

Per-model share (adversarial llm run): gpt-oss-120b 23 calls, llama-3.3-70b-versatile 20 calls, qwen-3.6-27b 20 calls.

## Latency Analysis (adversarial churn)

| Mode | Avg LLM latency | Max LLM latency | Decisions |
|---|---|---|---|
| llm | 605.63 ms | 3240.47 ms | 63 |
| hybrid | 1478.52 ms | 4161.88 ms | 10 |

Hybrid consults the LLM ONLY when the rule proposes a switch (and the cooldown would allow it) -- steady-state cycles pay no LLM latency.  Consults are **fire-and-forget** (async worker threads): the rule switch executes at grid time and the consult annotates after the fact, so consult latency (~0.6-1.5s average) can never shift the switch grid.

## Model Reliability

- Fallback rate (LLMError -> rule decision): llm 0.0%, hybrid 0.0% in both regimes -- when a consult does fail, the agent logs it and falls back to the rule decision for that cycle instead of crashing.
- Successful-call model distribution (adversarial llm run): gpt-oss-120b 23, llama-3.3-70b-versatile 20, qwen-3.6-27b 20.

## Agreement & Arbitration Analysis (hybrid modes)

Hybrid is **diagnostic-only**: the rule proposes a switch and the LLM assesses it (approve/confidence/reason) on proposals; the rule always decides and the LLM cannot veto.  Consults are logged as `kind:llm` entries for post-hoc analysis.

- Hybrid consults this run: 10 (adversarial) / 9 (moderate) -- 2 per seed / ~2 per seed.
- Echo control agreement: 100.0% (by construction).

`hybrid_conflict` adds a deterministic eviction-physics signal (burst-pool survival eta).  The LLM is consulted ONLY when the physics proposal and the rule proposal disagree, and its pick executes there (scoped veto, applied at the next decide point since consults are fire-and-forget).

- Rule/physics conflicts arbitrated: **0 in all five clean multi-seed runs** (adversarial x3, moderate x2, including this regen).  The physics signal fired exactly once per seed at the pool-breach moment and **agreed with the rule every time** (both propose sieve).  The engineered rule-vs-physics ambiguity never occurs on this workload -- the scoped-veto experiment is a systematic null: the deterministic signal is redundant with the rule classifier at the breach moment.

## The fire-and-forget fix (why the numbers changed from 4a)

Experiment 4a's results (hybrid +12.9..+20.3 pt, llm +7..+15 pt at 4/5 seeds) were 100% harness artifact:

- The diagnostic hybrid made the **same decisions as rule** (verified in the decision logs), yet scored +17 pt -- impossible for decision logic; the only difference was the 2.1-3.6s synchronous consult delaying each switch's *execution* ~6.5K requests off-grid.
- The delay moved the rollback-guardrail trigger past the burst phase (rule rolled back to lfu mid-burst at prog ~36K; the delayed hybrid rolled back at ~63K and stayed on sieve through P2).  Same decisions, different effective switch positions, +-13..20 pt outcome swings.
- The echo controls proved it: with ~0ms consults, hybrid_echo equaled rule within 0.5 pt in the same runs.
- Fix (agent.py): rule decisions execute at grid time; consults run on daemon threads; llm/arbiter verdicts that differ from what executed queue a pending override applied at the next decide point (cooldown-aware, never lost).  Regression-guarded by `agent/test_agent_async_consults.py` (6 offline checks).

## The preload-gate fix (why the numbers changed from 2026-08-09)

The 2026-08-09 clean run (rule 0.6506 adversarial, llm +1.3 pt moderate) contained a second artifact, discovered 2026-08-10:

- The request-quantized cadence started at `_last_decide_progress = -decide_every`, so the first decision fired at **progress 0** -- a wall-clock race against the benchmark's preload loop (preload SETs never advance the agent's progress counter).  The switch therefore landed mid-preload (decision log: `total_keys 861` of 14563 on the seed-7 run) and rebuilt the eviction policy on a half-loaded map.
- `SWITCH_POLICY` rebuilds by re-adding all keys in `unordered_map` hash order -- a **scramble of the eviction frontier**.  On this workload the zipf-hot ranks are the earliest-preloaded keys and the static frontier kills them first by design, so a mid-preload rebuild randomly scatters the hot ranks and they survive the churn.  Every mode inherited the same luck, inflating all of them by ~0.3-0.4 overall.
- Fixes: `MARK_PRELOADED` after preload (server flag `preload_complete` in `/metrics`); the agent gates decisions on it and quantizes the cadence to the decide grid (first switch at exactly `decide_every`, verified in the 2026-08-10 control probe); `--no-preload-gate` for standalone use.  Echo-control gaps dropped from 0.52-1.2 pt (un-quantized gate) to 0.09-0.14 pt.
- With the artifact gone, the honest verdict flipped: the agent's decision modes are within 0.15 pt of rule, and **all of them lose to static lfu/sieve by ~5.4 pt**.

## What the LLM actually did (multi-seed experiments)

- **Design (final)**: `hybrid` is diagnostic-only -- the rule proposes, the LLM assesses, the rule decides; `llm` mode is deprecated.  `hybrid_conflict` (the follow-up experiment) gives the LLM a scoped veto ONLY when a deterministic eviction-physics signal and the rule disagree.  Four controlled experiments led here:
- **(1) Un-gated consults**: the LLM vetoed the pre-workload lru->lfu switch with "no activity, keep current stable policy" and P1 collapsed (overall -30 to -41 pt at every seed).  Fixed by gating consults on observed traffic.
- **(2) Evidence-gated veto**: hybrid consulted on proposals and the LLM vetoed 66.7% of them -- hybrid became static LFU (agreement 33.3%, -10 pt P2 / +12 pt P3 trade).  The tight spread was the veto's default action, not LLM insight.
- **(3) Confidence gate + enriched signals**: a `rule_confidence < 0.8` gate plus raw zipf/scan-ratio/churn/trend/switch-history signals in the prompt.  The gate never opened (churn saturates rule_confidence at 1.0 in request-quantized mode), so hybrid silently equaled rule, and the enriched `llm` mode still flip-flopped (per-seed -3.6 to +7.4 pt -- a roll, not a strategy).
- **(4) Fire-and-forget clean compare (2026-08-09)**: contaminated by the preload-gate artifact (above); all conclusions were re-verified in (5).
- **(5) Preload-gated clean compare (2026-08-10, this file)**: adversarial -- all modes within 0.15 pt of rule (echo gap 0.14 pt).  Moderate -- llm +0.15 pt (3/5 seeds, per-seed -0.27..+0.67; echo gap 0.09 pt).  `hybrid_conflict` arbitrated 0 conflicts across all five clean runs.  **Every LLM experiment on this workload has now failed to beat rule; the LLM layer adds cost and latency for noise.**

### Regression evidence (before the fixes)

With decisions starting mid-phase-1 and the LLM consulted pre-evidence, the real Groq LLM cost 26-41 pt at every seed (rule 0.6623 +/- 0.0067, llm 0.2528 +/- 0.0494, hybrid 0.3626 +/- 0.1084).  Single-seed "LLM wins" claims are unreliable on this workload: decision timing, rebuild scrambles, and the LLM roll each change the outcome by +-40 pt.

## Recommendation

**INCONCLUSIVE -- use rule.**  Both regimes: every mode within 0.15 pt of rule, echo controls clean (0.09-0.14 pt).  The moderate llm +0.15 pt (3/5 seeds, spread -0.27..+0.67) is the same coin-flip documented in experiment (3).  The LLM adds latency and cost for noise.  **The agent itself does not justify its switching:** with the preload gate, rule (0.2503) loses to static lfu/sieve (0.3041) by 5.4 pt adversarial; only the P1/P2 gains are adaptive, and they are paid for with a P3 collapse.

## Rule agent vs static policies (same-run 5-seed table, 2026-08-10, preload-gated)

One `--mode all` invocation per seed (90K / 1MB / hot-11000, seeds 1/7/42/123/999, `--spawn-agent`, preload gate active) measures static_lru / static_lfu / static_sieve / pilot (rule agent) on byte-identical workloads, plus four **rebuild-control** modes that re-issue `SWITCH_POLICY` at fixed request positions WITHOUT changing policy (isolating the rebuild scramble from policy choice).

| Mode | Overall (mean +/- std) | P1 | P2 | P3 |
|---|---|---|---|---|
| static_lru | 0.1630 +/- 0.0508 | 0.2885 | 0.0846 | 0.1200 |
| static_sieve | 0.3041 +/- 0.0502 | 0.2890 | 0.2293 | 0.3867 |
| static_lfu | 0.3041 +/- 0.0502 | 0.2890 | 0.2293 | 0.3867 |
| **pilot (rule agent)** | **0.2482 +/- 0.0490** | 0.3207 | 0.2443 | 0.1854 |
| static_sieve_rebuild (1 rebuild @ 7K) | 0.1767 +/- 0.0499 | 0.3074 | 0.1136 | 0.1147 |
| sieve_at_schedule (5 rebuilds) | 0.2490 +/- 0.0493 | 0.3106 | 0.2517 | 0.1900 |
| lfu_at_schedule (5 rebuilds) | 0.2490 +/- 0.0493 | 0.3106 | 0.2517 | 0.1900 |
| static_lfu_derange (rebuild every 15K) | 0.2864 +/- 0.0510 | 0.2943 | 0.3254 | 0.2434 |

### What this table says

- **Rebuilds dominate; policy choice is ~irrelevant.**  `sieve_at_schedule` and `lfu_at_schedule` (same 5 rebuilds at the pilot's switch positions, pinned to DIFFERENT policies) measure *identically* (0.2490) and equal pilot (0.2482) to ~0.1 pt.  The agent's decisions contribute ~nothing; the rebuild scramble at its switch positions determines the outcome.
- **Rebuilds are net harmful.**  Static lfu/sieve (0 rebuilds) 0.3041 > one rebuild (0.1767, -12.7 pt) > 5 rebuilds at the agent's positions (0.2490) — with the derange control (6 rebuilds) at 0.2864 the monotone relationship is scrambled by *when* the rebuilds land (the 5-schedule positions land badly in P3; every-15K keeps one rebuild inside P3's burst window, which is the one place a scramble helps lfu).
- **The rule agent's remaining edge over static lru (+8.5 pt) is real but smaller than its loss to lfu/sieve (-5.6 pt).**  Pilot wins P1 (+3.2 pt over static lfu) and P2 (+1.5 pt) through its early lfu switch, then loses P3 by -20.1 pt -- the rollback/rebuild thrash destroys the burst pool (lfu_at_schedule's P3 is 0.1900, static lfu's is 0.3867).
- The earlier claim (rule 0.6493, +35 pt over statics) was the mid-preload rebuild artifact documented above; the same-run numbers here are preload-gated and reproduce the README eviction sweep to 4 decimals.
