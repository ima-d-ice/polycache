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
    agent/           Python tuning agent (work in progress)