# CachePilot

High-performance in-memory cache server written in C++17.

- SIEVE eviction policy (USENIX NSDI 2024), plus LRU and LFU alternatives
- Socket server, RESP-like protocol, TTL support, AOF persistence

## Build

    make          # build ./cachepilot
    make clean    # remove build artifacts
