# AdaptiCache

A self-tuning in-memory cache server written in C++17, paired with an autonomous LangGraph agent that swaps eviction policies at runtime to match the workload — no restart, no downtime.

- **Cache core:** single-threaded epoll event loop, SIEVE / LRU / LFU eviction, TTL, AOF persistence
- **Tuning agent:** rule-based + optional LLM decision modes with cooldown and rollback guardrails
- **Benchmark harness:** fresh-server, equal-pressure, multi-seed A/B comparisons with honest reporting

## What this project does

The cache itself is a small, fast Redis-style server. The interesting part is what happens above it: a LangGraph agent watches live telemetry (hit rate, policy, eviction rate), classifies the workload, and issues `SWITCH_POLICY` commands over TCP — every switch is logged, and a rollback guardrail reverts any change that makes the hit rate drop more than 10%.

Three decision modes are available and A/B comparable on identical workloads (plus `hybrid_conflict`, the current experiment under test):

| Mode | Behavior |
|---|---|
| `rule` | Heuristic classifier: zipf skew → SIEVE, scanning → LRU, stable → LFU, bursty → SIEVE |
| `hybrid` | Rule decides; the LLM is consulted on switch proposals as a **diagnostic annotator** (approve/confidence/reason logged, cannot veto) |
| `hybrid_conflict` | Rule + deterministic eviction-physics signal (burst-pool survival ETA); the LLM arbitrates **only** when they genuinely disagree (scoped veto) |
| `llm` | LLM picks the policy each cycle — **deprecated**: lost to rule in controlled experiments (see below) |

The LLM layer is strictly opt-in (Groq, round-robin across three models with failover), costs on the order of $0.001 per benchmark run, and falls back to rules on any failure.  All LLM consults are **fire-and-forget** (async worker threads): the rule decision always executes at grid time, so consult latency can never shift the switch positions (this fixed a harness artifact that swung the previous compare +13..20 pt).

### Results at a glance (90K requests, 1 MB cache, 5 seeds: 1/7/42/123/999)

Adversarial churn (0.50/0.50 — the verified-divergence config):

| Mode | Overall hit rate (mean ± std) | vs Rule |
|---|---|---|
| **rule** | **65.1% ± 0.7** | — |
| llm (deprecated) | 64.9% ± 1.1 | −0.2 pt |
| hybrid (diagnostic) | 65.2% ± 0.4 | +0.2 pt |
| hybrid_conflict | 65.0% ± 0.6 | −0.0 pt |
| hybrid_echo (control) | 65.2% ± 1.0 | +0.1 pt |

Moderate churn (0.30/0.30): rule 73.5% ± 0.8, llm 74.8% ± 1.5 (+1.3 pt — the first regime where an LLM mode clears the adopt bar, a P2/P3 trade at every seed; see below), hybrid 74.3% ± 1.2, hybrid_conflict 73.4% ± 0.9.

Eviction-policy divergence (LFU/SIEVE vs LRU) is reproducible across 5 seeds. The earlier single-seed claim that hybrid beats rule by +8.0 pt does **not** reproduce: that run drew a lucky LLM roll at seed 7. With the LLM layer the spread across seeds is ~1-4 pt — it adds variance, not value, on this workload. The LLM layer was demoted to a diagnostic role after three controlled experiments (un-gated consults cost 26–41 pt; evidence-gated vetoes became static-LFU; a confidence gate never opened and enriched signals still flip-flopped), and experiment 4 — a deterministic physics signal with LLM arbitration of genuine rule-vs-physics conflicts — never produced a single conflict across three clean multi-seed runs (the physics signal always agrees with the rule at the burst-pool breach moment). Full numbers: [Benchmarks](#benchmarks) and `agent/RESULTS_COMPARISON.md`.

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
    LLM[Groq LLM layer<br/>gpt-oss-120b / llama-3.3-70b / qwen-3.6-27b] --> AG
```

The agent decision cycle:

```mermaid
sequenceDiagram
    participant B as Benchmark / workload
    participant AG as LangGraph agent
    participant C as Cache server
    participant G as Groq LLM

    loop every interval (default 1s)
        AG->>C: poll /metrics + access telemetry
        C-->>AG: hit_rate, policy, workload signals
        AG->>AG: classify (zipf / scan / churn)
        alt decision_mode = hybrid / hybrid_conflict (diagnostic/arbiter)
            opt rule proposes a switch (or rule-physics conflict)
                AG-->>G: consult (FIRE-AND-FORGET: async worker thread)
                G-->>AG: eval / arbitration (logged; never blocks a switch)
            end
        end
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
| LLM inference | Groq: gpt-oss-120b, llama-3.3-70b-versatile, qwen-3.6-27b (round-robin + failover) |
| Benchmarking | Custom phased generator: zipf skew, hot+scan, mixed phases with burst + churn |
| Tests | Plain C++17 assertions, zero test framework, `make test` |

## Key Design Decisions

**Why SIEVE?** SIEVE is the 2024 cache-eviction successor to LRU: a single hand pointer walks the list clearing a `visited` bit, making the hot working set cheap to protect and the cold tail trivial to evict. It costs O(1) amortized per operation and beats LRU in the scan-heavy phase of the benchmark. The policy interface (`touch` / `add` / `remove` / `evict`) means all three policies are drop-in switchable at runtime.

**Why a single-threaded epoll loop?** One thread, one `epoll_wait`, per-client buffers, `EPOLLOUT`-based pending sends. No locks in the hot path — the only contention is the TTL sweeper, which takes the storage mutex for expired keys. The admin server runs its own epoll loop on a worker thread. This keeps the concurrency story simple enough to reason about and debug.

**Why synchronous AOF fsync?** Durability over speed: every SET/DEL is appended and flushed before the response is sent, and the log is replayed on boot. Write throughput suffers by design; the project prefers being able to prove nothing is lost on crash.

**Why an agent instead of a static policy?** Real workloads shift. The benchmark's own phases (skewed → hot+scan → mixed) show that a single fixed policy gives up 14-28 points in hit rate versus the right policy per phase. The agent pays one cheap metrics poll per second to keep the cache on the right policy, and the rollback guardrail bounds the downside of a bad switch.

**Why hybrid?** Hybrid is now a **diagnostic layer**: the rule decides, and the LLM annotates switch proposals (approve/confidence/reason) in the decision log. Three controlled 5-seed experiments led to this demotion: (1) un-gated consults let the LLM veto the pre-workload lru→lfu switch ("no activity, keep current stable policy") and cost 26–41 pt; (2) evidence-gated vetoes made the LLM veto 66.7% of proposals — hybrid became static LFU with a −10 pt P2 / +12 pt P3 trade, i.e. the veto's default action, not insight; (3) a rule-confidence gate plus raw zipf/scan/churn/trend/switch-history signals produced no improvement — the gate never opened in request-quantized mode and the enriched `llm` mode still flip-flopped (±4.1 pt across seeds; per-seed −3.6 to +7.4 pt is a roll, not a strategy). Experiment 4 (`hybrid_conflict` — a deterministic burst-pool-survival physics signal with LLM arbitration of genuine rule-vs-physics conflicts) is a systematic null: the physics signal fired exactly once per seed at the breach moment and always agreed with the rule, so the arbiter was never exercised in three clean multi-seed runs (adversarial ×2, moderate ×1). `llm` mode remains opt-in but deprecated; `hybrid` costs ~$0.0002 and ~0.6-3.6 s only when a switch is actually proposed — and since consults are fire-and-forget, that latency never shifts the switch grid.

**Why fair A/B?** Every decision-mode sub-run replays the identical workload at identical length on a fresh server. An explicit `--llm-requests` cap (for cheap smoke tests) prints a loud NOT-COMPARABLE warning and is excluded from the report's recommendation. Equal pressure or nothing.

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

### Decision modes — fair 5-seed comparison (90K requests, equal length, adversarial churn)

| Mode | Overall HR (mean ± std) | P1 HR | P2 HR | P3 HR | Switches | LLM calls | Fallback | Est. cost |
|---|---|---|---|---|---|---|---|---|
| **rule** | **0.6506 ± 0.0065** | 0.8067 | 0.5863 | 0.5666 | 3.0 | 0 | 0% | $0.00 |
| llm (deprecated) | 0.6489 ± 0.0114 | 0.8108 | 0.5925 | 0.5521 | 5.0 | 60 | 0% | ~$0.001 |
| hybrid (diagnostic) | 0.6522 ± 0.0042 | 0.8064 | 0.5884 | 0.5693 | 3.0 | 5 | 0% | ~$0.0002 |
| hybrid_conflict | 0.6502 ± 0.0062 | 0.8133 | 0.5777 | 0.5671 | 3.0 | 0 | 0% | $0.00 |
| hybrid_echo (control) | 0.6517 ± 0.0097 | 0.8101 | 0.5813 | 0.5711 | 3.0 | 5 | 0% | $0.00 |
| hybrid_conflict_echo | 0.6534 ± 0.0060 | 0.8124 | 0.5877 | 0.5679 | 3.0 | 0 | 0% | $0.00 |

No mode beats rule by ≥ 1 pt on a majority of seeds — **rule is the recommended default**. `hybrid_echo` (echo LLM) confirms the agent plumbing: |rule − echo| = 0.11 pt < 0.5 pt control bound. The earlier single-seed table (seed 7: rule 0.6348, hybrid 0.7151) is **not** reproducible: the +8.0 pt hybrid was a lucky LLM roll. Under **moderate churn** (0.30/0.30) llm is the first mode to clear the adopt bar (+1.3 pt mean, 3/5 seeds) — but the edge is a P2 gain (+6.5 pt) traded for a P3 loss (−2.3 pt) at every seed, its spread is 2x the rule's, and it contradicts the adversarial null (llm −0.2 pt); treat it as a candidate signal requiring reproduction, not an adoption. `hybrid_conflict` arbitrated **zero** conflicts across all three clean multi-seed runs (the physics signal always agrees with the rule). Full detail: `agent/RESULTS_COMPARISON.md` (regenerate with `python3 agent/compare_report.py --results ./compare_results.json`).

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

- **Decision-mode results are single-run evidence.** The agent's 1s polling cycle aligns to wall clock, not workload position, so switch timing jitter moves the numbers run to run (llm -0.25pt vs rule at 90K is well within that noise). Stronger claims need repeated runs with a mean/std summary.
- **The rule agent can thrash.** The 90K rule trace shows sieve↔lfu flip-flops with rollback churn; cooldown bounds but does not eliminate it.
- **LLM consults are fire-and-forget by design.** Rule decisions execute at grid time; consult latency (~0.5-3.6s Groq call) is absorbed by async worker threads and llm/arbiter verdicts apply at the next decide point, so latency can never shift the switch grid (a synchronous variant of this cost the previous compare run +13..20 pt of artifact).
- **fsync-per-write AOF** prioritizes durability over write throughput by design.
- **No replication or sharding** — the single epoll thread is the throughput ceiling, and the design favors reasoning about concurrency over scaling out.
- **macOS `epoll_compat.h` is a shim**, not production epoll; the shim has one known race class (shared fake epfd across instances) which is mitigated with an atomic counter + global mutex.
- **No authentication** on the TCP or admin ports — this is a benchmark/research server, not a production deployment.

## License

Academic/research use. The workload generator and benchmark methodology are self-contained; no external datasets are required.
