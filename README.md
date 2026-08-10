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

**The headline result is the adaptive win, measured 2026-08-11 with the fixed harness:** the rule agent beats the best static policy (lfu/sieve tie at 0.3041) by +16.6 pt on adversarial churn (rule 0.4705 ± 0.0520 vs 0.3041 ± 0.0502; P2 +36.9 pt, P3 +13.8 pt, P1 −0.5 pt for its slow lfu switch). The agent switches lru→lfu→sieve (3–4 times per seed, ended on sieve). Two older verdicts are superseded: the 2026-08-09 "rule 65%" table was a preload-race artifact, and the 2026-08-10 "rule loses 5.4 pt" table measured the old destructive hash-order rebuild (`SWITCH_POLICY` re-added keys in `unordered_map` order, scrambling the eviction frontier). The preload gate (`MARK_PRELOADED` + decision-grid quantization) and the ordered-rebuild fix (`ba9554b`) removed both artifacts; every number in this README is from the regenerated 5-seed sweep.

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

**Why an agent instead of a static policy?** Real workloads shift, and the controlled answer (preload-gated, ordered rebuild, 2026-08-11 sweep) is that the rule agent's adaptation earns its keep: +16.6 pt overall over the best static policy (0.4705 vs 0.3041), driven by the early lfu switch that keeps the burst pool alive through P2/P3 (+36.9/+13.8 pt). The project's value is in *proving* that: a benchmark harness with fresh-server equal-pressure A/Bs, multi-seed verdicts, and two caught-and-fixed harness artifacts (a preload-race that inflated the 08-09 table; a destructive hash-order rebuild that sank the pilot in the 08-10 table). The agent infrastructure (rule classifier, cooldown, rollback guardrail) is fully implemented and tested; on this workload the adaptive agent is the better default.

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

### Rule agent vs static policies — same-run 5-seed table

One `--mode all --seeds 1 7 42 123 999` invocation (90K/1MB/11000 config, fresh server per mode, fresh `_seedN` artifacts per seed) puts the rule agent, the three static policies, and four **rebuild-control** modes (same-policy `SWITCH_POLICY` re-issues at fixed request positions) in a single table. Measured 2026-08-11 on `master` (Part 1, metadata-sort rebuild).

| Mode | Overall (mean ± std) | P1 | P2 | P3 |
|---|---|---|---|---|
| static_lru | 0.1630 ± 0.0508 | 0.2885 | 0.0846 | 0.1200 |
| static_sieve | 0.3041 ± 0.0502 | 0.2890 | 0.2293 | 0.3868 |
| static_lfu | 0.3041 ± 0.0502 | 0.2890 | 0.2293 | 0.3868 |
| **pilot (rule agent)** | **0.4705 ± 0.0520** | 0.2838 | 0.5982 | 0.5244 |
| static_sieve_rebuild (1 @ 7K) | 0.3041 ± 0.0502 | 0.2890 | 0.2293 | 0.3868 |
| sieve_at_schedule (5 rebuilds) | 0.3041 ± 0.0502 | 0.2890 | 0.2293 | 0.3868 |
| lfu_at_schedule (5 rebuilds) | 0.3041 ± 0.0502 | 0.2890 | 0.2293 | 0.3868 |
| static_lfu_derange (rebuild every 15K) | 0.3041 ± 0.0502 | 0.2890 | 0.2293 | 0.3868 |

**The rebuild tax is gone.** Every rebuild-control mode measures exactly == its static baseline (0.3041, to 4 decimals on all 5 seeds): `switch_policy` rebuilds the target policy in order (metadata sort on master; a continuously-maintained recency list on the M3 branch), and a same-policy switch is a literal no-op. `sieve_at_schedule` and `lfu_at_schedule` re-issue the pilot's own switch positions without changing policy and still land on 0.3041 — the policy choice, not the switch mechanism, is now the only lever, and that lever is the agent's.

Takeaway: the 2026-08-09 "rule beats static by ~35 pt" table was 100% artifact (first switch raced the preload loop and rebuilt the policy mid-fill), and the 2026-08-10 "rule loses by 5.4 pt" table was the hash-order rebuild artifact. With the preload gate and the ordered rebuild, the verdict is **rule agent (0.4705) > static lfu/sieve (0.3041) > static lru (0.1630)** on adversarial churn. The agent's gains: an early lfu switch that wins P2/P3 (the burst pool survives), plus the rollback guardrail containing any bad decision. The harness that caught two artifacts of its own making remains the project's actual deliverable.

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

- **The adaptive win depends on the ordered rebuild** — with the old `unordered_map`-hash-order rebuild the agent lost to statics by 5.4 pt; with the metadata-sort rebuild (master) and the recency-list walk (M3 branch) it wins by +16.6 pt and the rebuild-control modes measure exactly == statics. The M3 branch eliminates the sorted-rebuild step entirely (walk the maintained recency list; same-policy switch is a literal no-op).
- **The rule agent can thrash.** The 90K rule trace shows lru→lfu→sieve transitions with occasional rollback churn; cooldown bounds but does not eliminate it.
- **fsync-per-write AOF** prioritizes durability over write throughput by design.
- **No replication or sharding** — the single epoll thread is the throughput ceiling, and the design favors reasoning about concurrency over scaling out.
- **macOS `epoll_compat.h` is a shim**, not production epoll; the shim has one known race class (shared fake epfd across instances) which is mitigated with an atomic counter + global mutex.
- **No authentication** on the TCP or admin ports — this is a benchmark/research server, not a production deployment.

## License

Academic/research use. The workload generator and benchmark methodology are self-contained; no external datasets are required.
