# AdaptiCache

A self-tuning in-memory cache server written in C++17, paired with an autonomous LangGraph agent that swaps eviction policies at runtime to match the workload — no restart, no downtime.

- **Cache core:** single-threaded epoll event loop, SIEVE / LRU / LFU eviction, TTL, AOF persistence
- **Tuning agent:** rule-based classifier with cooldown and rollback guardrails
- **Benchmark harness:** fresh-server, equal-pressure, multi-seed A/B comparisons with honest reporting

## What this project does

The cache itself is a small, fast Redis-style server. The interesting part is what happens above it: a LangGraph agent watches live telemetry (hit rate, policy, eviction rate), classifies the workload, and issues `SWITCH_POLICY` commands over TCP — every switch is logged, and a rollback guardrail reverts any change that makes the hit rate drop more than 10%.

The agent is rule-based: a heuristic classifier maps the workload to a policy (zipf skew → SIEVE, scanning → LRU, stable → LFU, bursty → SIEVE). The LLM decision layer was evaluated across five controlled experiments and demoted: no mode beat the rule, and the consults added cost and latency for noise. The repo ships the rule-only agent.

### Results at a glance (90K requests, 1 MB cache, 5 seeds: 1/7/42/123/999, preload-gated)

Adversarial churn (0.50/0.50 — the verified-divergence config):

**The headline result is the static comparison:** the rule agent loses to static lfu/sieve by ~5.4 pt (rule 0.2482 vs 0.3041: the agent's early lfu switch wins P1/P2 by +3.2/+1.6 pt, then the rollback/rebuild thrash loses P3 by −20.1 pt). The earlier recorded numbers (rule 65%) were inflated by a harness artifact — the agent's first decision raced the benchmark's preload and rebuilt the policy on a half-loaded map (a `SWITCH_POLICY` rebuild re-adds keys in `unordered_map` hash order, scrambling the eviction frontier). Fixed with a `MARK_PRELOADED` preload gate + decision-grid quantization; every recorded number in this README was regenerated after the fix.

## Architecture

```mermaid
flowchart LR
    C[TCP clients] -->|text protocol| S[Server<br/>single-thread epoll loop]
    A[Admin HTTP<br/>GET /metrics, /health] -.-> S

    S <--> ST[Storage<br/>mutex-protected]
    ST --> P[(Eviction policies<br/>SIEVE / LRU / LFU)]
    TTL[TTL sweeper<br/>background thread] --> ST
    AOF[(AOF log<br/>JSON lines, fsync per write)] --> ST

    AG[LangGraph agent<br/>fetch → analyze → decide → act] -->|SWITCH_POLICY| S
    AG -->|polls /metrics + access telemetry| A
 ```

The agent decision cycle:

```mermaid
sequenceDiagram
    participant B as Benchmark / workload
    participant AG as LangGraph agent
    participant C as Cache server

    loop every interval (default 1s)
        AG->>C: poll /metrics + access telemetry
        C-->>AG: hit_rate, policy, workload signals
        AG->>AG: classify (zipf / scan / churn)
        opt target != current and cooldown ok
            AG->>C: SWITCH_POLICY <target>
            C-->>AG: +OK (policy swapped atomically)
        end
        AG->>AG: 3-snapshot guardrail: revert if hit rate drops >10%
    end
```

## Tech Stack

| Component | Technology |
|---|---|
| Cache server | C++17, epoll (poll-based shim on macOS), `-O2` |
| Eviction policies | Hand-rolled SIEVE (NSDI 2024) linked list, LRU, LFU frequency buckets |
| Persistence | AOF append-only log, one JSON line per write, synchronous fsync, replay on boot |
| JSON | nlohmann/json (vendored single header, only dependency) |
| Agent framework | Python 3.12, LangGraph (StateGraph) |
| Benchmarking | Custom phased generator: zipf skew, hot+scan, mixed phases with burst + churn |
| Tests | Plain C++17 assertions, zero test framework, `make test` |

## Key Design Decisions

**Why SIEVE?** SIEVE is the 2024 cache-eviction successor to LRU: a single hand pointer walks the list clearing a `visited` bit, making the hot working set cheap to protect and the cold tail trivial to evict. It costs O(1) amortized per operation and beats LRU in the scan-heavy phase of the benchmark. The policy interface (`touch` / `add` / `remove` / `evict`) means all three policies are drop-in switchable at runtime.

**Why a single-threaded epoll loop?** One thread, one `epoll_wait`, per-client buffers, `EPOLLOUT`-based pending sends. No locks in the hot path — the only contention is the TTL sweeper, which takes the storage mutex for expired keys. The admin server runs its own epoll loop on a worker thread. This keeps the concurrency story simple enough to reason about and debug.

**Why synchronous AOF fsync?** Durability over speed: every SET/DEL is appended and flushed before the response is sent, and the log is replayed on boot. Write throughput suffers by design; the project prefers being able to prove nothing is lost on crash.

**Why an agent instead of a static policy?** Real workloads shift, and the agent's early switch does buy real P1/P2 gains (+3.2/+1.6 pt over static lfu) — but the controlled answer is more honest: on this benchmark, the rule agent's switching costs more than it earns (P3 −20.1 pt from rollback/rebuild thrash; overall −5.4 pt vs static lfu/sieve). The project's value is in *proving* that: a benchmark harness with fresh-server equal-pressure A/Bs, multi-seed verdicts, and two caught-and-fixed harness artifacts (a synchronous-consult latency artifact; a mid-preload switch racing the preload loop). The agent infrastructure (rule classifier, cooldown, rollback guardrail) is fully implemented and tested; the measurements just say a static LFU/SIEVE is the better default on this workload.

**Why plain-assert tests?** The repo's only dependency is nlohmann/json. Tests are plain C++17 `CHECK` macros compiled straight to binaries — `make test` builds and runs all three, zero framework, zero install step.

## Quick Start

```sh
make                 # builds ./adaptivecache
make test            # builds + runs the unit tests (all green)

./adaptivecache [--port PORT] [--admin-port PORT] \
             [--memory-limit MB] [--aof-file FILE]
```

Defaults: cache port `6379`, admin port `8080`, memory limit `64` MB, AOF file `adapticache.aof`. `SIGINT`/`SIGTERM` trigger graceful shutdown. Builds on Linux (real epoll) and macOS (compatibility shim in `src/epoll_compat.h`).

## Protocol & API

Requests are a single line terminated by `\n` (optionally `\r\n`), whitespace-separated tokens, case-insensitive verbs.

```sh
# SET with optional TTL, then GET
printf 'SET user:42 alice 60\r\nGET user:42\r\n' | nc 127.0.0.1 6379

# DELETE, policy switch, metrics
printf 'DEL user:42\r\nSWITCH_POLICY sieve\r\nMETRICS\r\n' | nc 127.0.0.1 6379
```

| Command | Response |
|---|---|
| `SET key value [ttl_sec]` | `+OK` |
| `GET key` | `$<len>\r\n<value>` on hit, `$-1` on miss |
| `DEL key` | `:1` removed / `:0` absent |
| `METRICS` | JSON: hits, misses, hit_rate, evictions, memory_bytes, policy, total_keys |
| `SWITCH_POLICY lru\|lfu\|sieve` | `+OK` (case-insensitive; unknown name → `-ERR`) |

Admin HTTP (one request per connection, `Connection: close`):

```sh
curl -s http://127.0.0.1:8080/metrics
# {"evictions":0,"hit_rate":0.5,"hits":1,"memory_bytes":6,...}
curl -s http://127.0.0.1:8080/health
# {"status":"ok"}
```

## Benchmarks

`agent/benchmark.py` starts a fresh `adapticache` for every mode, preloads exactly the cache's key capacity, and replays one generated workload so every policy measures the same request sequence.

```sh
python3 agent/benchmark.py --mode all --requests 90000 \
    --cache-size-mb 1 --hot-size 11000 --seed 7
```

### Eviction policies — 5-seed consistency (90K requests, 1 MB cache)

LFU and SIEVE beat LRU in the scan-heavy phases at **every seed** (P3 gap +0.26-0.28 absolute, 2.3x-3.9x relative). Absolute numbers vary seed to seed; the divergence does not.

| Seed | Overall LRU | Overall LFU/SIEVE | P3 LRU | P3 LFU/SIEVE |
|---|---|---|---|---|
| 1 | 0.2517 | 0.3913 | 0.1993 | 0.4634 |
| 7 | 0.1583 | 0.3003 | 0.1153 | 0.3828 |
| 42 | 0.1365 | 0.2778 | 0.0977 | 0.3660 |
| 123 | 0.1278 | 0.2670 | 0.0909 | 0.3536 |
| 999 | 0.1407 | 0.2839 | 0.0969 | 0.3679 |

Workload design (why this diverges at all): this server never inserts on GET miss and every SET evicts exactly one key, so the eviction frontier walks the preload order under cold churn — even zipf-hot ranks die. The burst pool sits at the preload tail and per-phase churn must exceed the burst-key rank offset, or the frontier never reaches the burst keys and every policy scores the same. At `--requests 30000` the churn is too low and all policies measure identically — 90K is the minimum comparable length.

### Rule agent vs static policies — same-run 5-seed table (2026-08-10, preload-gated)

One `--mode all` invocation per seed (same 90K/1MB/11000 config, same harness, same 5 seeds) puts the rule agent, the three static policies, and four **rebuild-control** modes (same-policy `SWITCH_POLICY` re-issues at fixed request positions) in a single table. The static rows reproduce the eviction sweep above to 4 decimals, confirming the workloads are byte-identical.

| Mode | Overall (mean ± std) | P1 | P2 | P3 |
|---|---|---|---|---|
| static_lru | 0.1630 ± 0.0508 | 0.2885 | 0.0846 | 0.1200 |
| static_sieve | 0.3041 ± 0.0502 | 0.2890 | 0.2293 | 0.3867 |
| static_lfu | 0.3041 ± 0.0502 | 0.2890 | 0.2293 | 0.3867 |
| **pilot (rule agent)** | **0.2482 ± 0.0490** | 0.3207 | 0.2443 | 0.1854 |
| static_sieve_rebuild (1 @ 7K) | 0.1767 ± 0.0499 | 0.3074 | 0.1136 | 0.1147 |
| sieve_at_schedule (5 rebuilds) | 0.2490 ± 0.0493 | 0.3106 | 0.2517 | 0.1900 |
| lfu_at_schedule (5 rebuilds) | 0.2490 ± 0.0493 | 0.3106 | 0.2517 | 0.1900 |
| static_lfu_derange (rebuild every 15K) | 0.2864 ± 0.0510 | 0.2943 | 0.3254 | 0.2434 |

**The rebuild is the agent's only lever — and it's a net tax.** `sieve_at_schedule` and `lfu_at_schedule` pin *different* policies but re-issue `SWITCH_POLICY` at the agent's own switch positions, and they measure identically (0.2490) to the pilot (0.2482) to ~0.1 pt: every `SWITCH_POLICY` rebuilds the policy by re-adding resident keys in `unordered_map` hash order, which scrambles the eviction frontier, and that scramble — not the policy choice — determines the outcome. Rebuilds hurt monotonically at the agent's positions: static lfu/sieve (0 rebuilds) 0.3041 > pilot/at_schedule (5) 0.248-0.249 > single rebuild at 7K 0.1767. The agent's only adaptive gains are real but small: P1 +3.2 pt and P2 +1.5 pt over static lfu from its early lfu switch (a preload-gated, request-quantized decision at exactly request 5000), paid for with a P3 −20.1 pt collapse where the rollback/rebuild thrash destroys the burst pool (pilot P3 0.1854 vs static lfu 0.3867).

Takeaway: the 2026-08-09 "rule beats static by ~35 pt" table was 100% artifact — that run's first switch raced the preload loop and rebuilt the policy mid-fill. With the preload gate, the honest verdict is **static lfu/sieve > rule agent (−5.4 pt) > static lru**, and the rebuild-control modes pin the mechanism. The defensible claims are (a) the agent's early switch buys real P1/P2 gains, (b) the rollback guardrail contains the downside to ~−5.4 pt (not the −40 pt a bad policy choice would cost), and (c) the harness that caught two artifacts of its own making is the project's actual deliverable.

## Tests

```sh
make test
```

| Test binary | Covers |
|---|---|
| `test_sieve` | insertion-order eviction, `touch()`/visited-skip semantics, middle `remove()` relinking, size accounting |
| `test_protocol` | SET / GET / DEL / METRICS / SWITCH_POLICY parsing, case-insensitivity, whitespace/tab splitting, UNKNOWN verbs |
| `test_storage` | set/get/del + counters, deterministic eviction under a memory limit, policy switching (keys preserved), TTL expiry |

Each binary compiles only the translation units it exercises — the server's `main.cpp` never links into the tests.

## Known Limitations & Future Work

- **The agent loses to static lfu/sieve on this benchmark** (rule 0.2482 vs 0.3041 adversarial, preload-gated) — the measured answer, not the hoped-for one. The rule agent's switching pays a rebuild-scramble tax that costs more than its P1/P2 adaptation earns; a static policy is the better default on this workload. (The `experiment/m3-no-sort-switch` branch implements a no-sort switch primitive that eliminates the rebuild tax.)
- **The rule agent can thrash.** The 90K rule trace shows sieve↔lfu flip-flops with rollback churn; cooldown bounds but does not eliminate it.
- **fsync-per-write AOF** prioritizes durability over write throughput by design.
- **No replication or sharding** — the single epoll thread is the throughput ceiling, and the design favors reasoning about concurrency over scaling out.
- **macOS `epoll_compat.h` is a shim**, not production epoll; the shim has one known race class (shared fake epfd across instances) which is mitigated with an atomic counter + global mutex.
- **No authentication** on the TCP or admin ports — this is a benchmark/research server, not a production deployment.

## License

Academic/research use. The workload generator and benchmark methodology are self-contained; no external datasets are required.
