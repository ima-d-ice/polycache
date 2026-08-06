# CachePilot: Rule vs LLM vs Hybrid Decision Modes

A/B comparison of the agent's three decision modes, each run on a fresh server replaying the identical generated workload (30000 requests, 1MB cache, 100000-key space, seed 7).

## Executive Summary

- Best overall hit rate: **hybrid** (overall HR: rule=0.7107, llm=0.7369, hybrid=0.7685).
- LLM mode: better vs rule (2.62 pt).
- Hybrid mode: better vs rule (5.79 pt).
- LLM fallback rate (rule-mode safety net): llm 0.0%, hybrid 0.0%.

## Performance Table

| Mode | Overall HR | P1 HR | P2 HR | P3 HR | Switches | LLM Calls | Fallback % | Avg LLM Latency |
|---|---|---|---|---|---|---|---|---|
| rule | 0.7107 | 0.9594 | 0.5843 | 0.6034 | 2 | 0 | 0.0% | - |
| llm | 0.7369 | 0.9917 | 0.6045 | 0.6297 | 4 | 15 | 0.0% | 506.07 ms |
| hybrid | 0.7685 | 0.9785 | 0.6848 | 0.6576 | 3 | 15 | 0.0% | 453.66 ms |

## Cost Analysis

Groq output pricing used (per 1M tokens): gpt-oss-120b $0.50, llama-3.3-70b-versatile $0.30, qwen-3.6-27b $0.20.

| Mode | Total tokens | Est. cost (USD) |
|---|---|---|
| rule | 0 | $0.00 |
| llm | 3610 | $0.0012 |
| hybrid | 3595 | $0.0012 |

Per-model share (llm run): gpt-oss-120b 5 calls (~$0.0006), llama-3.3-70b-versatile 5 calls (~$0.0004), qwen-3.6-27b 5 calls (~$0.0002)

## Latency Analysis

| Mode | Avg LLM latency | Max LLM latency | Decisions |
|---|---|---|---|
| llm | 506.07 ms | 1731.98 ms | 15 |
| hybrid | 453.66 ms | 1501.74 ms | 15 |

Each decision cycle pays one synchronous LLM call (~506 ms average). At a 1 s agent interval this is a small addition to the cache latency budget, but it makes every agent cycle dependent on Groq availability.

## Model Reliability

- Fallback rate (LLMError -> rule decision): llm 0.0%, hybrid 0.0% -- every consult succeeded in this run.  When a consult does fail, the agent logs it and falls back to the rule decision for that cycle instead of crashing.
- Successful-call model distribution (llm run): gpt-oss-120b 5, llama-3.3-70b-versatile 5, qwen-3.6-27b 5

## Agreement Analysis (hybrid)

Hybrid mode executes the rule decision while logging the LLM's pick as a second opinion.

- Rule/LLM agreement: 26.7% of hybrid decisions.

## Recommendation

Adopt **llm + hybrid** decision mode for workloads like this one: it beats the rule baseline by 2.6 pt (llm), 5.8 pt (hybrid).

