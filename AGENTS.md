# CachePilot Project Rules

## Current State (read first)

- **C++ core is implemented and committed** (commit `step 2`). Any further `src/*` edits happen on clean committed code — modify carefully, verify with `make`, keep the change focused.
- Implemented + committed: `Storage` (mutex + eviction + memory limit), `TTLManager`, eviction policies (`SIEVE`/`LRU`/`LFU`), `protocol.cpp` (`parse_command`), `AOFLogger` (append + flush per write + `replay`), `Server` (single-thread epoll event loop, per-client partial-read buffers, EPOLLOUT-based pending send), `AdminServer` (separate epoll loop on its own port, GET `/metrics` + `/health`, 404/405, `Connection: close`, one request per connection), `main.cpp` (CLI parsing `--port/--admin-port/--memory-limit/--aof-file`, SIGINT/SIGTERM handler that stops both loops, AOF replay on boot, admin on a worker thread + server on main). Verified: two integration passes × 20 process restarts, and full binary end-to-end (SET/GET/metrics/health/404, AOF replay across restart, SIGINT + SIGTERM graceful shutdown).
- Still stubs: `agent/` only — a separate Python component, all stubs (`run()` raises `NotImplementedError`). All C++ is implemented.
- `src/third_party/nlohmann/json.hpp` is the only vendored dependency; `src/epoll_compat.h` translates epoll→poll so the repo also builds/runs on macOS (production path is real epoll on Linux). The shim supports multiple concurrent epoll instances in one process (unique atomic epfd counter + global mutex around the instance map — a raced counter used to hand both loops the same fake epfd, which merged the two watch sets and made each loop close the other's listen socket).

## Build & Verify

```sh
make          # builds ./cachepilot (g++, -std=c++17 -O2 -Wall -Wextra -pthread -MMD -MP)
make clean    # removes *.o, *.d, cachepilot
```

- Makefile globs `find src -name '*.cpp'`: new `.cpp` files anywhere under `src/` compile automatically, no Makefile edits.
- No tests, no linter, no CI. `make` + run `./cachepilot` is the only verification.
- Build artifacts (`*.o`/`*.d`) sit next to sources in `src/`; gitignored, never commit.

## Implemented Core (verify against these, not the README)

- Protocol: text, newline-delimited. `parse_command` trims, splits on any whitespace, uppercases the verb. Verbs `SET`/`GET`/`DEL`/`METRICS`/`SWITCH_POLICY`; the rest of the tokens go to `cmd.args`; unknown verbs → `UNKNOWN`.
- `Storage::set(key, value, ttl_sec)` — TTL param already exists; default memory limit 64MB, default policy `lru`.
- Eviction abstract interface is `EvictionPolicy` with `touch/add/remove/evict/memory_used`; policies `LRU`, `LFU` (freq buckets), `SIEVE` (hand-rolled Node* linked list). All called only under the storage lock — NOT thread-safe.
- `switch_policy(name)` is case-insensitive; unknown name → false.
- `TTLManager` runs its own background thread (`sweep_loop`, ~100 ms) that calls back into `Storage::expire_key`, which takes the storage mutex. This is the exception to the single-thread design; keep it that way.
- `Server` event loop: one thread, non-blocking fds, per-client incoming/outgoing buffers. On `EPOLLHUP`/`EPOLLERR` with `EPOLLIN`, read buffered data first (drain) before closing; the wake pipe (for `stop()`) must be non-blocking or the drain-loop `recv` blocks forever. Read-write dispatch order matters: EOF is detected by `recv() == 0`, not by the HUP flag alone.
- `AdminServer` mirrors `Server`'s loop (`AdminServer::start()` blocks; call it from a `std::thread`). Same rules apply: wake pipe non-blocking, `epoll_ctl(EPOLL_CTL_DEL)` before `close()` on client fds (a stale watch on a reused fd number makes the other loop wake on a wrong fd — and `close_conn` on a listener looks like a client and kills the listener).

## Architecture Rules

- Storage is mutex-protected. All storage ops happen under `std::lock_guard`.
- Server is a single epoll thread (`Server::start()`); `Server::stop()` wakes it via a socketpair.
- AOF logger flushes to disk synchronously after every SET/DEL (durability over speed), storing one JSON line per write; `replay` re-reads them on boot.
- Protocol is text-based, newline-delimited (`\r\n`).

## Tech Stack & Code Style

- C++17 only (no `std::format`/C++20); Linux/POSIX, target epoll not select/poll.
- Single-header JSON: nlohmann/json. No Boost, no other external deps.
- `#pragma once` in headers; `using namespace std;` allowed in `.cpp`, keep `std::` prefixes in `.h`.
- Prefer `std::optional`, `std::unique_ptr`, `std::mutex`. Raw pointers only for epoll event data and SIEVE linked-list nodes.
- Do NOT add threading inside eviction policies.

## Forbidden

- No C++20 features (`std::format`).
- No threading inside eviction policies.