# PolyCache
**C++17** • **Epoll** • **SIEVE / LRU / LFU** • **AOF Persistence**

A high-performance in-memory cache server written in C++17 with hot-swappable eviction policies and a rigorous statistical benchmarking harness.

## Architecture & Tech Stack

| Component | Technology |
| :--- | :--- |
| **Network I/O** | Single-threaded `epoll` event loop (macOS `poll` compat), non-blocking I/O |
| **Eviction Policies** | Hand-rolled SIEVE (NSDI 2024), LRU, LFU (behind a unified `EvictionPolicy` interface) |
| **Persistence** | Synchronous AOF (Append-Only File) with JSON-line formatting and crash replay |
| **Dependencies** | `nlohmann/json` (vendored single header) |
| **Testing** | Plain C++17 assertions (`CHECK` macros), zero external test frameworks |

## Core Features

* **Runtime Policy Switching:** A single `SWITCH_POLICY` TCP command swaps between SIEVE, LRU, and LFU live. The rebuild preserves the eviction frontier (metadata sort, not a flush), ensuring zero data loss and no downtime.
* **Modern Eviction (SIEVE):** Implements the 2024 SIEVE algorithm, which uses a single hand pointer to clear visited bits, offering $O(1)$ amortized operations and superior scan-resistance compared to LRU.
* **Durability by Design:** Prioritizes data safety over write throughput via synchronous `fsync`-per-write AOF logging.
* **Admin & Metrics:** A background worker thread runs an HTTP server exposing live cache metrics (`/metrics`) and health checks (`/health`).

## Quick Start

```bash
# Build the server and run unit tests
make
make test

# Start the server (Defaults: port 6379, admin 8080, 64MB limit)
./polycache --memory-limit 64 --aof-file polycache.aof
```

*Note: Graceful shutdown is triggered by `SIGINT` or `SIGTERM`.*

## Protocol & API Reference

The server uses a simple, case-insensitive text protocol over TCP (terminated by `\r\n`).

### TCP Commands

| Command | Response |
| :--- | :--- |
| `SET key value [ttl]` | `+OK` |
| `GET key` | `$<len>\r\n<value>` (hit) or `$-1` (miss) |
| `DEL key` | `:1` (removed) or `:0` (absent) |
| `SWITCH_POLICY lru\|lfu\|sieve` | `+OK` (Atomic swap under storage lock) |
| `METRICS` | JSON object of cache stats |

```bash
# Example usage via netcat
printf 'SET user:42 alice 60\r\nGET user:42\r\n' | nc 127.0.0.1 6379
```

### Admin HTTP API

```bash
curl -s http://127.0.0.1:8080/metrics
# {"evictions":12,"hit_rate":0.85,"hits":850,"memory_bytes":1048576,"policy":"sieve"}

curl -s http://127.0.0.1:8080/health
# {"status":"ok"}
```

## Performance & Benchmarking

Included is a Python benchmarking harness (`benchmark.py`) that starts a fresh server per mode, preloads the cache to capacity, and replays identical phased workloads to measure policy divergence under adversarial conditions.

### Workload Design
The workload forces the eviction frontier to walk the preload order via 50% cold-write churn. A "burst pool" at the tail of the preload is read once and left to idle; LRU drops these keys under churn, while LFU and SIEVE retain them due to frequency and visited-bit protections.

### Benchmark Results (90K requests, 1MB cache, 5 seeds)
SIEVE and LFU tie and consistently outperform LRU in scan-heavy phases.

| Mode | Overall Hit Rate (mean ± std) | P1 (Zipf) | P2 (Scan) | P3 (Mixed) |
| :--- | :--- | :--- | :--- | :--- |
| **static_sieve** | **0.3041 ± 0.0502** | 0.2890 | 0.2293 | **0.3868** |
| **static_lfu** | **0.3041 ± 0.0502** | 0.2890 | 0.2293 | **0.3868** |
| static_lru | 0.1630 ± 0.0508 | 0.2885 | 0.0846 | 0.1200 |

## Project Structure

* `/src`: C++17 server core (network I/O, storage engine, eviction policies, AOF persistence).
* `/tests`: Plain C++17 unit tests for protocol parsing, storage mechanics, and SIEVE semantics.
* `benchmark.py`: Python 3 statistical benchmarking harness with multi-seed aggregation.
* `Makefile`: Build system for the server and test binaries.