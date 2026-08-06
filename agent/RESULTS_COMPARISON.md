# CachePilot: Rule vs LLM vs Hybrid Decision Modes

A/B comparison of the agent's three decision modes, each run on a fresh server replaying the identical generated workload (90000 requests, 1MB cache, 100000-key space, seed 7).

## Executive Summary

- Best overall hit rate: **hybrid** (overall HR: rule=0.6348, llm=0.6323, hybrid=0.7151).
- LLM mode: worse vs rule (-0.25 pt).
- Hybrid mode: better vs rule (8.03 pt).
- LLM fallback rate (rule-mode safety net): llm 0.0%, hybrid 0.0%.

## Performance Table

| Mode | Overall HR | P1 HR | P2 HR | P3 HR | Switches | LLM Calls | Fallback % | Avg LLM Latency |
|---|---|---|---|---|---|---|---|---|
| rule | 0.6348 | 0.8448 | 0.5240 | 0.5442 | 5 | 0 | 0.0% | - |
| llm | 0.6323 | 0.9676 | 0.4398 | 0.5019 | 6 | 11 | 0.0% | 793.84 ms |
| hybrid | 0.7151 | 0.9079 | 0.6370 | 0.6102 | 5 | 12 | 0.0% | 541.95 ms |

## Cost Analysis

Groq output pricing used (per 1M tokens): gpt-oss-120b $0.50, llama-3.3-70b-versatile $0.30, qwen-3.6-27b $0.20.

| Mode | Total tokens | Est. cost (USD) |
|---|---|---|
| rule | 0 | $0.00 |
| llm | 2749 | $0.0009 |
| hybrid | 2969 | $0.0010 |

Per-model share (llm run): gpt-oss-120b 4 calls (~$0.0005), llama-3.3-70b-versatile 4 calls (~$0.0003), qwen-3.6-27b 3 calls (~$0.0001)

## Latency Analysis

| Mode | Avg LLM latency | Max LLM latency | Decisions |
|---|---|---|---|
| llm | 793.84 ms | 2858.89 ms | 11 |
| hybrid | 541.95 ms | 1346.68 ms | 12 |

Each decision cycle pays one synchronous LLM call (~794 ms average). At a 1 s agent interval this is a small addition to the cache latency budget, but it makes every agent cycle dependent on Groq availability.

## Model Reliability

- Fallback rate (LLMError -> rule decision): llm 0.0%, hybrid 0.0% -- every consult succeeded in this run.  When a consult does fail, the agent logs it and falls back to the rule decision for that cycle instead of crashing.
- Successful-call model distribution (llm run): gpt-oss-120b 4, llama-3.3-70b-versatile 4, qwen-3.6-27b 3

## Agreement Analysis (hybrid)

Hybrid mode executes the rule decision while logging the LLM's pick as a second opinion.

- Rule/LLM agreement: 41.7% of hybrid decisions.

## Recommendation

Adopt **hybrid** decision mode for workloads like this one: it beats the rule baseline by 8.0 pt (hybrid).

