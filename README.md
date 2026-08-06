# CachePilot

High-performance in-memory cache server written in C++17.

- SIEVE eviction policy (USENIX NSDI 2024), plus LRU and LFU alternatives
- Text, newline-delimited protocol (RESP-like responses)
- TTL support with a background expiry sweeper
- AOF persistence (one JSON line per write, flushed synchronously, replayed on boot)
- Admin HTTP server: `GET /metrics` (JSON), `GET /health`

## Build

    make          # build ./cachepilot
    make clean    # remove build artifacts

Builds on Linux (real `epoll`) and macOS (poll-based compatibility shim in
`src/epoll_compat.h`). The only vendored dependency is nlohmann/json.

## Run

    ./cachepilot [--port PORT] [--admin-port PORT] [--memory-limit MB] [--aof-file FILE]

Defaults: cache port `6379`, admin port `8080`, memory limit `64` MB, AOF
file `cachepilot.aof`. `SIGINT`/`SIGTERM` trigger a graceful shutdown.

## Protocol

Requests are a single line terminated by `\n` (optionally `\r\n`), with
whitespace-separated tokens. Verbs are case-insensitive.

    SET key value [ttl_seconds]
    GET key
    DEL key
    METRICS
    SWITCH_POLICY lru|lfu|sieve

Responses:

    +OK                                  SET / SWITCH_POLICY success
    $<len>\r\n<value>                    GET hit
    $-1                                  GET miss
    :1  |  :0                            DEL removed | not present
    {json}                               METRICS
    -ERR <reason>                        errors

## Admin HTTP API

    GET /metrics   →  {"evictions":0,"hit_rate":...,"hits":0,"memory_bytes":0,
                       "miss_rate":...,"misses":0,"policy":"lru","total_keys":0}
    GET /health    →  {"status":"ok"}

One request per connection; `Connection: close`.

## Layout

    src/             C++ server, storage, eviction, AOF, admin, main
      eviction/      SIEVE / LRU / LFU policies
      third_party/   nlohmann/json (vendored single header)
    agent/           Python tuning agent + phased benchmark tool
      benchmark.py   eviction-policy benchmark (static + pilot + --compare)
      agent.py       LangGraph tuning agent (fetch -> analyze -> decide -> act)
      llm_client.py  opt-in multi-model Groq client (round-robin + failover)
      analyzer.py    workload classifier (zipf / scan / churn signals)
      metrics.py     admin-endpoint client (fetch_metrics + signal helpers)
      compare_report.py  renders RESULTS_COMPARISON.md from --compare JSON

## Benchmark & tuning

`agent/benchmark.py` starts a FRESH `cachepilot` for every mode, preloads
exactly the cache's key capacity, and replays one generated workload so
all policies measure the same request sequence.

    python3 agent/benchmark.py --mode all --cache-size-mb 1 --hot-size 11000 \
        --requests 90000 --seed 7

Workload design: each phase opens with a burst read of the preload-tail
keys, which then idle for the rest of the phase.  Cold-insert churn
(default 50% of requests) makes every SET evict a key and blows the
eviction frontier past the burst keys, so LRU drops the idle burst while
LFU (frequency) and SIEVE (visited) keep them -- the next phase's burst
read exposes the difference.  GETs never insert in this server, so a
phase without writes would be frozen at preload and measure identically
for every policy.

Verified result (`--requests 90000 --cache-size-mb 1 --hot-size 11000 --seed 7`):

    Mode         | P1 zipf skew | P2 hot+scan | P3 mixed  | Overall
    static_lru   |      0.2826  |     0.0811  |   0.1153  |  0.1583
    static_lfu   |      0.2832  |     0.2276  |   0.3828  |  0.3003
    static_sieve |      0.2832  |     0.2276  |   0.3828  |  0.3003

P1 is a warmup phase (identical across policies); P2/P3 show LRU 14-27
points behind LFU/SIEVE.  Key flags: `--mode` (all|static_lru|static_lfu|
static_sieve|pilot), `--cache-size-mb`, `--value-size`, `--cold-ratio`,
`--scan-write-ratio`, `--burst-size`, `--hot-size`, `--requests`, `--seed`.

Pilot mode runs the tuning agent (`agent.py`, optionally spawned with
`--spawn-agent`): it polls the admin metrics endpoint and the benchmark's
access telemetry, classifies the workload, and issues `SWITCH_POLICY`
commands subject to a cooldown and a rollback guardrail (reverts if the
hit rate drops >10% after a switch).  Every decision is logged to
`/tmp/cachepilot_bench.decisions.jsonl`.  The benchmark writes
`hit_rate_vs_requests.png` and `pilot_decisions.png`.

    python3 agent/benchmark.py --mode pilot --requests 90000 \
        --cache-size-mb 1 --hot-size 11000 --seed 7 --spawn-agent

## Optional LLM decision layer (opt-in)

`agent.py` has three decision modes (default `rule`):

    python3 agent/agent.py --decision-mode rule    # heuristics only (default)
    python3 agent/agent.py --decision-mode llm     # LLM picks the policy
    python3 agent/agent.py --decision-mode hybrid  # rule decides, LLM logged as 2nd opinion

The LLM layer is strictly opt-in: with no keys configured the agent warns
once and falls back to `rule` mode, and any failed LLM consult falls back
to the rule-based decision for that cycle.  Keys are read from the
environment as `GROQ_API_KEY_1` .. `GROQ_API_KEY_N` (contiguous, stopping
at the first gap), rotated one key per request.

Models rotate per call: gpt-oss-120b -> llama-3.3-70b-versatile ->
qwen-3.6-27b.  On 429/503 the client backs off (1s/2s/4s) and after
exhausted retries fails over to the next model; all three failing (or an
unparseable / invalid response) raises `LLMError` and the agent falls back
to rules.  Per-model call counters are available via
`RoundRobinLLMClient.get_stats()`.

A/B comparison across the three modes (each sub-run on a fresh server
replaying the same workload).  Fair by default: every mode runs the full
`--requests` length.  An explicit `--llm-requests` cap (cheaper API spend)
produces unequal-length sub-runs and triggers a loud NOT-COMPARABLE
warning — it exists only for cheap smoke tests:

    python3 agent/benchmark.py --compare --requests 50000 --cache-size-mb 1 \
        --hot-size 11000 --seed 7 --spawn-agent

Writes `hit_rate_comparison.png`, `llm_latency_vs_requests.png`,
`model_distribution.png`, and `compare_results.json`; renders the report
with:

    python3 agent/compare_report.py          # -> agent/RESULTS_COMPARISON.md