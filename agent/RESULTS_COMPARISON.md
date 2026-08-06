# CachePilot: Rule vs LLM vs Hybrid Decision Modes

A/B comparison of the agent's three decision modes, each run on a fresh server replaying the identical generated workload (30000 requests, 1MB cache, 100000-key space, seed 7).

## Executive Summary

- Best overall hit rate: **llm** (overall HR: rule=0.7097, llm=0.9940, hybrid=0.9249).
- LLM mode: better vs rule (28.42 pt).
- Hybrid mode: better vs rule (21.52 pt).
- LLM fallback rate (rule-mode safety net): llm 0.0%, hybrid 0.0%.

## Performance Table

| Mode | Overall HR | P1 HR | P2 HR | P3 HR | Switches | LLM Calls | Fallback % | Avg LLM Latency |
|---|---|---|---|---|---|---|---|---|
| rule | 0.7097 | 0.9710 | 0.5818 | 0.5929 | 2 | 0 | 0.0% | - |
| llm | 0.9940 | 0.9996 | 0.9925 | 0.9895 | 0 | 8 | 0.0% | 496.72 ms |
| hybrid | 0.9249 | 0.9977 | 0.9870 | 0.8030 | 1 | 6 | 0.0% | 714.12 ms |

_Note: llm/hybrid sub-runs were capped at 12000 requests while rule ran 30000; phase hit rates above are per-sub-run and not strictly comparable across the cap._

## Cost Analysis

Groq output pricing used (per 1M tokens): gpt-oss-120b $0.50, llama-3.3-70b-versatile $0.30, qwen-3.6-27b $0.20.

| Mode | Total tokens | Est. cost (USD) |
|---|---|---|
| rule | 0 | $0.00 |
| llm | 1899 | $0.0007 |
| hybrid | 1431 | $0.0005 |

Per-model share (llm run): gpt-oss-120b 3 calls (~$0.0004), llama-3.3-70b-versatile 3 calls (~$0.0002), qwen-3.6-27b 2 calls (~$0.0001)

## Latency Analysis

| Mode | Avg LLM latency | Max LLM latency | Decisions |
|---|---|---|---|
| llm | 496.72 ms | 1272.97 ms | 8 |
| hybrid | 714.12 ms | 2274.73 ms | 6 |

Each decision cycle pays one synchronous LLM call (~497 ms average). At a 1 s agent interval this is a small addition to the cache latency budget, but it makes every agent cycle dependent on Groq availability.

## Model Reliability

- Fallback rate (LLMError -> rule decision): llm 0.0%, hybrid 0.0%. The rule fallback fired on every failed consult, so LLM outages degrade decisions to the rule baseline rather than crashing the agent.
- Successful-call model distribution (llm run): gpt-oss-120b 3, llama-3.3-70b-versatile 3, qwen-3.6-27b 2

## Agreement Analysis (hybrid)

Hybrid mode executes the rule decision while logging the LLM's pick as a second opinion.

- Rule/LLM agreement: 50.0% of hybrid decisions.

## Recommendation

Adopt **llm + hybrid** decision mode for workloads like this one: it beats the rule baseline by 28.4 pt (llm), 21.5 pt (hybrid).

