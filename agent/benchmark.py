#!/usr/bin/env python3
"""AdaptiCache workload benchmark with real eviction pressure.

Starts a FRESH AdaptiCache server for every mode (kills any running
adapti_cache, restarts with --memory-limit <cache-size-mb> and a clean
temporary AOF), preloads a working set much larger than the cache, then
runs three workload phases that force eviction DURING measurement:

  phase 1 (0-30%):   zipf skew - a burst read of the burst pool (the
                     preload tail), then cold-insert churn mixed with
                     zipfian GETs over the resident pool.
  phase 2 (30-60%):  hot+scan - same burst + churn, reads split between
                     hot zipfian GETs and a round-robin scan of the
                     resident middle band.
  phase 3 (60-100%): mixed - first half repeats phase 1, second half
                     repeats phase 2.  This is the phase the tuning agent
                     is expected to react in.

The burst pool is the last keys the cache was filled with.  Each phase
reads them once at the start; the keys then idle for the rest of the
phase while cold-insert churn (every SET evicts one key) keeps blowing
the eviction frontier past them.  By the next phase's burst read, LRU
has dropped the idle burst keys (they decayed out of the cache) while
LFU holds them by frequency and SIEVE by the visited flag -- so the
burst read exposes the difference.  Writes are cold inserts of keys
(ck%06d) that are never read; GETs never insert in this server, so a
phase without writes would be frozen at preload and measure identically
for every policy -- every phase keeps a write stream alive on purpose.

Modes: static_lru / static_lfu / static_sieve pin the policy; pilot runs
the tuning agent (agent.py, optionally spawned with --spawn-agent) which
reads the access telemetry this tool writes and switches policies.

All numbers come from live TCP/HTTP exchanges with the server.  Nothing is
fabricated: if the server binary is missing or does not come up, the tool
fails loudly.
"""

import argparse
import collections
import json
import os
import random
import socket
import subprocess
import sys
import time
from pathlib import Path

from metrics import fetch_metrics

SCRIPT_DIR = Path(__file__).resolve().parent

STATIC_POLICY = {"static_lru": "lru", "static_lfu": "lfu", "static_sieve": "sieve"}
# Rebuild-control modes (2026-08-10): pin a policy and re-issue
# SWITCH_POLICY at fixed request positions to replicate the agent's rebuild
# scrambles WITHOUT changing policy.  Every switch rebuilds the policy by
# re-adding all keys in unordered_map hash order, which randomizes the
# eviction frontier -- the artifact that inflated the rule-vs-static gap.
# These modes attribute the agent's edge between policy choice and
# frontier scramble.  AGENT_SWITCH_POINTS = pilot rule-mode switch
# positions on seed 1 (probe 2026-08-10: 6744/21744/37248/52496/78496,
# incl. rollback-triggered switches); per-seed positions vary.
AGENT_SWITCH_POINTS = (7000, 22000, 37000, 52000, 78500)
REBUILD_CONTROL = {
    "static_sieve_rebuild": ("sieve", (7000,)),           # single early rebuild
    "sieve_at_schedule":    ("sieve", AGENT_SWITCH_POINTS),
    "lfu_at_schedule":      ("lfu", AGENT_SWITCH_POINTS),
    "static_lfu_derange":   ("lfu", tuple(range(15000, 90000, 15000))),
}
MODES = ("static_lru", "static_lfu", "static_sieve", "pilot",
         "static_sieve_rebuild", "sieve_at_schedule", "lfu_at_schedule",
         "static_lfu_derange")
PHASE_NAMES = {1: "zipf skew", 2: "hot+scan", 3: "mixed"}
EXPECTED_POLICY = {1: "lfu", 2: "lfu", 3: "lfu"}
POLICY_Y = {"lru": 0.0, "lfu": 1.0, "sieve": 2.0}
DEFAULT_ALPHA = 1.2


# --------------------------------------------------------------------------
# Workload generators
# --------------------------------------------------------------------------

def _key_names(keys: int) -> list:
    return ["key%05d" % i for i in range(keys)]


def _zipf_weights(keys: list, alpha: float) -> list:
    weights = [1.0 / (rank ** alpha) for rank in range(1, len(keys) + 1)]
    total = sum(weights)
    return [w / total for w in weights]


def zipfian(keys: list, n: int, alpha: float = DEFAULT_ALPHA, rng=random) -> list:
    """n draws from keys with power-law probability p(k) ~ rank(k)^-alpha."""
    weights = _zipf_weights(keys, alpha)
    return rng.choices(keys, weights=weights, k=n)


def sequential(keys: list, n: int) -> list:
    """Round-robin scan: key0, key1, ..., keyN-1, key0, ..."""
    return [keys[i % len(keys)] for i in range(n)]


def _cold_key(counter: list) -> str:
    """Next never-read key name; the write stream that fuels evictions."""
    name = "ck%06d" % counter[0]
    counter[0] += 1
    return name


def cache_capacity(mb: int, value_size: int) -> int:
    """Max entries the cache holds: memory-limit / (key + value bytes)."""
    key_len = len("key%05d" % 0)  # 8 bytes: "key" + 5 digits
    return (mb * 1024 * 1024) // (key_len + value_size)


def _gen_phase1(keys, burst_pool, n, rng, cold_ratio, alpha, counter) -> list:
    """Zipf-skew: a burst read of the burst pool, then the rest is
    cold-insert churn mixed with zipfian GETs over the resident pool.

    The burst pool is the TAIL of the preload (the last keys the cache
    was filled with, read once here and never again this phase).  The
    cold SETs are never read either, and every one triggers an eviction:
    the churn rate must be high enough that over a phase the eviction
    frontier passes the burst keys, so by the next phase's burst read LRU
    has dropped them while LFU (frequency) and SIEVE (visited) keep them.
    """
    reads = zipfian(keys, n, alpha=alpha, rng=rng)
    out = []
    for i in range(n):
        if i < len(burst_pool):
            out.append(("get", burst_pool[i]))
        elif rng.random() < cold_ratio:
            out.append(("set", _cold_key(counter)))
        else:
            out.append(("get", reads[i]))
    return out


def _gen_phase2(hot_pool, burst_pool, scan_pool, n, rng, scan_write_ratio,
                alpha, counter) -> list:
    """Hot + scan: a burst read of the burst pool, then cold-insert churn
    mixed with hot zipfian GETs and a round-robin scan over the resident
    gap between the hot pool and the burst pool.

    Same eviction-pressure contract as phase 1: the churn (scan_write_ratio)
    must be high enough that one phase turns the cache past the burst keys,
    so LRU forgets them while LFU/SIEVE keep them.
    """
    hot_reads = zipfian(hot_pool, n, alpha=alpha, rng=rng)
    hot_share = (1.0 - scan_write_ratio) / 2.0
    out = []
    for i in range(n):
        if i < len(burst_pool):
            out.append(("get", burst_pool[i]))
            continue
        r = rng.random()
        if r < scan_write_ratio:
            out.append(("set", _cold_key(counter)))
        elif r < scan_write_ratio + hot_share:
            out.append(("get", hot_reads[i]))
        else:
            out.append(("get", scan_pool[i % len(scan_pool)]))
    return out


def build_workload(keys, n, rng, cfg) -> list:
    """Three-phase workload as (op, key) pairs.

    ``keys`` must arrive shuffled: the cache is preloaded with the first
    ``cache_capacity`` keys, and that resident set is the pool every phase
    reads from.  A key can only be a hit if it is resident, so all pools
    are carved out of it.

    Pools (all within the resident set, in preload/rank order):
      hot   = resident[:hot_size]    -- phase-2 zipf GETs
      scan  = resident[hot_size:cap-burst_size]  (round-robin GETs)
      burst = resident[cap-burst_size:]  (the preload tail)

    The key mechanic is the burst pool: every phase (and each half of
    phase 3) opens by reading it once.  The keys then idle for the rest
    of the phase, which lasts long enough -- with cold-insert churn
    sized to push the eviction frontier past them -- that LRU evicts the
    idle burst keys while LFU (frequency) and SIEVE (visited) keep them.
    The next phase's burst read exposes the difference: it misses under
    LRU and hits under LFU/SIEVE.  Every mode replays the exact same
    request sequence against a fresh server.
    """
    cap = cache_capacity(cfg.cache_size_mb, cfg.value_size)
    resident = keys[:cap]
    hot = resident[:cfg.hot_size]
    burst = resident[cap - cfg.burst_size:]
    scan_pool = resident[cfg.hot_size:cap - cfg.burst_size]
    scan_pool = scan_pool[:cfg.scan_size]
    if not scan_pool:
        scan_pool = resident[cfg.hot_size:cap]
    per_phase = n // 3
    p3 = n - 2 * per_phase
    counter = [0]
    wl = []
    wl.extend(_gen_phase1(resident, burst, per_phase, rng, cfg.cold_ratio,
                          cfg.alpha, counter))
    wl.extend(_gen_phase2(hot, burst, scan_pool, per_phase, rng,
                          cfg.scan_write_ratio, cfg.alpha, counter))
    p3_a = p3 // 2
    wl.extend(_gen_phase1(resident, burst, p3_a, rng, cfg.cold_ratio,
                          cfg.alpha, counter))
    wl.extend(_gen_phase2(hot, burst, scan_pool, p3 - p3_a, rng,
                          cfg.scan_write_ratio, cfg.alpha, counter))
    return wl


def phase_of(i: int, n: int) -> int:
    p1 = n // 3
    p2 = 2 * (n // 3)
    return 1 if i < p1 else (2 if i < p2 else 3)


# --------------------------------------------------------------------------
# Minimal line-based TCP client (server responses are \r\n-terminated;
# a GET hit is  $<len>\r\n<value>\r\n , a miss is  $-1)
# --------------------------------------------------------------------------

class LineSocket:
    def __init__(self, sock):
        self.sock = sock
        self.buf = b""

    def read_until(self, marker=b"\r\n"):
        while True:
            idx = self.buf.find(marker)
            if idx != -1:
                line = self.buf[:idx]
                self.buf = self.buf[idx + len(marker):]
                return line
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("connection closed by server")
            self.buf += chunk

    def read_exact(self, n: int):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("connection closed by server")
            self.buf += chunk
        data, self.buf = self.buf[:n], self.buf[n:]
        return data

    def command(self, raw: bytes) -> bytes:
        self.sock.sendall(raw + b"\r\n")
        return self.read_until()


def set_value(ls: LineSocket, key: str, value: str) -> None:
    reply = ls.command(b"SET " + key.encode() + b" " + value.encode())
    if reply != b"+OK":
        raise RuntimeError("SET %s -> %r" % (key, reply))


def get_value(ls: LineSocket, key: str) -> bool:
    """One GET round trip; returns True on hit, False on miss."""
    reply = ls.command(b"GET " + key.encode())
    if reply == b"$-1":
        return False
    if not reply.startswith(b"$"):
        raise RuntimeError("unexpected GET reply: %r" % reply)
    length = int(reply[1:])
    ls.read_exact(length + 2)  # value plus the trailing \r\n
    return True


def switch_policy(ls: LineSocket, policy: str) -> bool:
    return ls.command(b"SWITCH_POLICY " + policy.encode()) == b"+OK"


# --------------------------------------------------------------------------
# Server lifecycle: fresh restart before every mode
# --------------------------------------------------------------------------

def kill_server() -> None:
    proc = subprocess.run(["pkill", "-x", "adapti_cache"],
                          capture_output=True, text=True)
    if proc.returncode == 0:
        print("killed existing adapti_cache process")
    time.sleep(0.25)


def start_server(cfg):
    """Kill any adapti_cache, start a fresh one with a small memory limit."""
    kill_server()
    if not cfg.server_path.exists():
        print("server binary not found: %s (run 'make' first)" % cfg.server_path,
              file=sys.stderr)
        sys.exit(1)
    for path in (cfg.aof_path, cfg.server_log):
        try:
            os.remove(path)
        except OSError:
            pass
    log_fh = open(cfg.server_log, "a", encoding="utf-8")
    proc = subprocess.Popen(
        [str(cfg.server_path), "--port", str(cfg.cache_port),
         "--admin-port", str(cfg.admin_port),
         "--memory-limit", str(cfg.cache_size_mb),
         "--aof-file", str(cfg.aof_path)],
        stdout=subprocess.DEVNULL, stderr=log_fh)
    deadline = time.time() + cfg.wait_timeout
    while time.time() < deadline:
        try:
            fetch_metrics(cfg.admin_host, cfg.admin_port, timeout=0.5)
            print("server up: %s --memory-limit %dMB (fresh state)" %
                  (cfg.server_path, cfg.cache_size_mb), flush=True)
            return proc
        except Exception:
            time.sleep(0.1)
    log_fh.close()
    proc.terminate()
    try:
        proc.wait(2)
    except subprocess.TimeoutExpired:
        proc.kill()
    print("server failed to start; last stderr lines:", file=sys.stderr)
    try:
        lines = open(cfg.server_log, encoding="utf-8").read().splitlines()
        print("\n".join(lines[-8:]), file=sys.stderr)
    except OSError:
        pass
    sys.exit(1)


def stop_server(proc) -> None:
    if proc is None:
        return
    proc.terminate()
    try:
        proc.wait(2)
    except subprocess.TimeoutExpired:
        proc.kill()
    print("server stopped")


def spawn_agent(cfg):
    """Start agent.py attached to the fresh server + access telemetry."""
    cmd = [sys.executable, str(SCRIPT_DIR / "agent.py"),
           "--cache-host", cfg.cache_host, "--cache-port", str(cfg.cache_port),
           "--admin-host", cfg.admin_host, "--admin-port", str(cfg.admin_port),
           "--cooldown", str(cfg.agent_cooldown),
           "--interval", str(cfg.agent_interval),
           "--decide-every", str(cfg.decide_every),
           "--cooldown-req", str(cfg.cooldown_req),
           "--access-log", str(cfg.access_log),
           "--log", str(cfg.agent_log)]
    if cfg.decision_mode == "hybrid_echo":
        # Timing control: hybrid logic with a ~0ms mock LLM that always
        # echoes the rule proposal (identical decisions to rule, identical
        # consult cadence to hybrid, no Groq keys needed).
        cmd += ["--decision-mode", "hybrid", "--mock-llm", "echo"]
    elif cfg.decision_mode == "hybrid_conflict_echo":
        # Timing control for hybrid_conflict: echo sides with the rule in
        # every arbitration, so decisions equal rule's exactly.
        cmd += ["--decision-mode", "hybrid_conflict", "--mock-llm", "echo"]
    else:
        cmd += ["--decision-mode", str(cfg.decision_mode)]
    if cfg.decision_mode in ("hybrid_conflict", "hybrid_conflict_echo"):
        # Eviction-physics context: exact preload numbers so the agent's
        # deterministic burst-pool signal can compute survival ETA.
        cap = cache_capacity(cfg.cache_size_mb, cfg.value_size)
        cmd += ["--capacity-keys", str(cap),
                "--burst-keys", str(cfg.burst_size)]
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


# --------------------------------------------------------------------------
# Measurement
# --------------------------------------------------------------------------

def run_mode(name, workload, preload_keys, cfg, telemetry_fh):
    """Run one benchmark mode against the (already started) server.

    preload_keys arrive shuffled: the survivors of the oversize preload are
    then a uniform sample of the working set (with an ascending preload the
    FIFO eviction of the full cache would evict the hot low-rank keys and
    every policy would miss the same way).  The shuffle is identical for
    every mode, so the preload survivor set is identical too.
    """
    n = len(workload)
    phase_start_req = {1: 0, 2: n // 3, 3: 2 * (n // 3)}
    phase_hits = {1: 0, 2: 0, 3: 0}
    phase_gets = {1: 0, 2: 0, 3: 0}
    phase_policy_start = {}
    phase_policy_end = {}
    phase_switch_to_expected = {1: None, 2: None, 3: None}
    total_hits = 0
    total_gets = 0
    switches = 0
    prev_policy = None
    prev_sample_policy = None
    peak_memory = 0
    samples = []

    with socket.create_connection((cfg.cache_host, cfg.cache_port),
                                  timeout=5) as sock:
        sock.settimeout(10)
        ls = LineSocket(sock)

        pinned = STATIC_POLICY.get(name)
        rebuild_at = frozenset()
        if pinned is None and name in REBUILD_CONTROL:
            pinned, rebuild_at = REBUILD_CONTROL[name]
        if pinned is not None:
            if not switch_policy(ls, pinned):
                raise RuntimeError("%s: SWITCH_POLICY %s rejected" %
                                   (name, pinned))
        rebuild_count = 0

        # Preload exactly the cache's capacity in keys: hot pool + burst
        # pool + a slice of the scan pool, shuffled.  Nothing is evicted
        # during preload (it fits), so the policy state is built only by
        # the phase traffic -- and there is no tail of never-read keys for
        # evictions to hide in.  The same order is used for every mode.
        cap = cache_capacity(cfg.cache_size_mb, cfg.value_size)
        preload_order = preload_keys[:cap]
        for key in preload_order:
            set_value(ls, key, "x" * cfg.value_size)
        # Preload-done gate: the agent must not decide while the cache is
        # still filling (progress only counts GETs, so an early poll would
        # otherwise fire a switch mid-preload, scrambling the eviction
        # frontier before any workload traffic -- the 2026-08-10 artifact).
        reply = ls.command(b"MARK_PRELOADED")
        if reply != b"+OK":
            raise RuntimeError("MARK_PRELOADED -> %r" % reply)

        ev_before = int(fetch_metrics(cfg.admin_host, cfg.admin_port)
                        .get("evictions", 0))

        for start in range(0, n, cfg.block_size):
            end = min(start + cfg.block_size, n)
            times = []
            for i in range(start, end):
                if i in rebuild_at:
                    reply = ls.command(b"SWITCH_POLICY " + pinned.encode())
                    if reply != b"+OK":
                        raise RuntimeError(
                            "rebuild %s: SWITCH_POLICY %s -> %r" %
                            (name, pinned, reply))
                    rebuild_count += 1
                op, key = workload[i]
                ph = phase_of(i, n)
                t0 = time.perf_counter()
                if op == "set":
                    set_value(ls, key, "w" * cfg.value_size)
                    times.append((time.perf_counter() - t0) * 1000.0)
                else:
                    ok = get_value(ls, key)
                    times.append((time.perf_counter() - t0) * 1000.0)
                    phase_gets[ph] += 1
                    total_gets += 1
                    if ok:
                        total_hits += 1
                        phase_hits[ph] += 1
                if telemetry_fh is not None:
                    telemetry_fh.write(json.dumps({"op": op, "key": key}) + "\n")
            if telemetry_fh is not None:
                telemetry_fh.flush()

            metrics = fetch_metrics(cfg.admin_host, cfg.admin_port)
            policy = str(metrics.get("policy", ""))
            if prev_policy is not None and policy != prev_policy:
                switches += 1
            prev_policy = policy
            peak_memory = max(peak_memory, int(metrics.get("memory_bytes", 0)))

            ph = phase_of(end - 1, n)
            phase_policy_start.setdefault(ph, policy)
            phase_policy_end[ph] = policy
            expected = EXPECTED_POLICY.get(ph)
            # Detection = the agent SWITCHED INTO the expected policy during
            # this phase (switches are only visible at block granularity).
            if (expected and policy == expected and
                    phase_switch_to_expected[ph] is None and
                    prev_sample_policy is not None and
                    prev_sample_policy != policy):
                phase_switch_to_expected[ph] = end
            prev_sample_policy = policy

            phase_hit_rate = (phase_hits[ph] / phase_gets[ph]
                              if phase_gets[ph] else 0.0)
            samples.append({
                "requests": end,
                "phase": ph,
                "phase_hit_rate": phase_hit_rate,
                "overall_hit_rate": (total_hits / total_gets
                                     if total_gets else 0.0),
                "latency_ms": sum(times) / len(times),
                "policy": policy,
                "switches": switches,
            })
            print("%s phase%d %6d req  phase_hit=%.4f  lat=%.3fms  policy=%s" %
                  (name, ph, end, phase_hit_rate, samples[-1]["latency_ms"],
                   policy), flush=True)

        evictions = (int(fetch_metrics(cfg.admin_host, cfg.admin_port)
                         .get("evictions", 0)) - ev_before)

    stats = {
        "phase_rates": {ph: (phase_hits[ph] / phase_gets[ph]
                             if phase_gets[ph] else 0.0) for ph in (1, 2, 3)},
        "overall": (total_hits / total_gets if total_gets else 0.0),
        "evictions": evictions,
        "peak_memory": peak_memory,
        "switches": switches,
        "phase_policy_start": phase_policy_start,
        "phase_policy_end": phase_policy_end,
        "phase_switch_to_expected": phase_switch_to_expected,
        "phase_start_req": phase_start_req,
    }
    return samples, stats


# --------------------------------------------------------------------------
# Output: summary table + pilot report + plots
# --------------------------------------------------------------------------

def print_summary(results, cfg) -> None:
    print("\nexpected best policy per phase:   P1 LFU~SIEVE | P2 LFU | "
          "P3 LFU (post-switch)\n")
    header = ("%-12s | %17s | %17s | %17s | %8s" %
              ("Mode", "P1 zipf skew", "P2 hot+scan", "P3 mixed", "Overall"))
    print(header)
    print("-" * len(header))
    for name, samples, stats in results:
        avg_lat = (sum(s["latency_ms"] for s in samples) / len(samples)
                   if samples else 0.0)
        print("%-12s | %16.4f | %16.4f | %16.4f | %7.4f" %
              (name, stats["phase_rates"][1], stats["phase_rates"][2],
               stats["phase_rates"][3], stats["overall"]))
        print("%-12s   (avg lat %.3fms | peak mem %d B | evictions %d | "
              "switches %d)" % (name, avg_lat, stats["peak_memory"],
                                stats["evictions"], stats["switches"]))
    print("-" * len(header))
    print("note: every mode ran against a freshly restarted server "
          "(--memory-limit %dMB, shuffled %d-key space, preload exactly "
          "fills the cache with %d keys, values %dB; burst pool %d keys "
          "read at each phase start and allowed to idle past the LRU "
          "horizon; cold-insert churn %.0f%% / %.0f%% of phases 1/2)" %
          (cfg.cache_size_mb, cfg.working_set,
           cache_capacity(cfg.cache_size_mb, cfg.value_size),
           cfg.value_size, cfg.burst_size,
           cfg.cold_ratio * 100, cfg.scan_write_ratio * 100))

    by_mode = {name: stats for name, _s, stats in results}
    if all(("static_" + pol) in by_mode for pol in ("lru", "lfu", "sieve")):
        print("\nper-phase policy comparison (higher is better):")
        for ph, label in ((1, "zipf skew"), (2, "hot+scan"), (3, "mixed")):
            rates = [by_mode["static_" + pol]["phase_rates"][ph]
                     for pol in ("lru", "lfu", "sieve")]
            print("Phase %d (%s) complete. LRU: %.4f, LFU: %.4f, "
                  "SIEVE: %.4f" % (ph, label, rates[0], rates[1], rates[2]))


def print_pilot_report(results, cfg) -> None:
    pilot = [r for r in results if r[0] == "pilot"]
    if not pilot:
        return
    stats = pilot[0][2]
    print("\npilot (agent) report:")
    start_policy = stats["phase_policy_start"].get(1, "?")
    print("  agent started on %s" % start_policy)
    for ph in (1, 2, 3):
        expected = EXPECTED_POLICY[ph]
        start_policy = stats["phase_policy_start"].get(ph, "?")
        end_policy = stats["phase_policy_end"].get(ph, "?")
        mark = "OK" if end_policy == expected else "X"
        detail = ""
        switch_at = stats["phase_switch_to_expected"][ph]
        if switch_at is not None:
            detail = " switched to %s at req %d (delay ~%d req from phase start)" % (
                expected, switch_at, switch_at - stats["phase_start_req"][ph])
        elif end_policy == expected:
            detail = " (policy %s from phase start; no switch needed)" % expected
        else:
            detail = " (policy %s throughout; no switch to %s observed)" % (
                start_policy, expected)
        print("  agent was on %-5s during phase %d, ended on %-5s [%s]%s" %
              (start_policy, ph, end_policy, mark, detail))
    if not cfg.spawn_agent:
        print("  (agent was not spawned by the benchmark; run agent.py "
              "manually with --access-log %s --cooldown <small>" % cfg.access_log)


def plot_hit_rates(results, cfg) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(9, 6))
    for name, samples, _stats in results:
        xs = [s["requests"] for s in samples]
        ys = [s["overall_hit_rate"] for s in samples]
        ax.plot(xs, ys, marker="o", markersize=3, label=name)
    p1 = cfg.requests // 3
    p2 = 2 * (cfg.requests // 3)
    for x, label in ((p1, "zipf skew"), (p2, "hot+scan"), (cfg.requests, "mixed")):
        ax.axvline(x, color="gray", linestyle="--", lw=0.8)
        ax.text(x, 0.02, label, rotation=90, fontsize=8, color="gray")
    ax.set_xlabel("requests issued")
    ax.set_ylabel("hit rate (overall)")
    ax.set_ylim(0.0, 1.05)
    ax.set_title("AdaptiCache hit rate vs requests "
                 "(fresh %dMB server per mode)" % cfg.cache_size_mb)
    ax.legend()
    fig.tight_layout()
    out = "%shit_rate_vs_requests.png" % cfg.plot_prefix
    fig.savefig(out)
    plt.close(fig)
    print("saved %s" % out)


def _agent_decisions(path):
    decisions = []
    if not path:
        return decisions
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                decisions.append(json.loads(line))
    except OSError as exc:
        print("warning: cannot read agent log %s (%s)" % (path, exc),
              file=sys.stderr)
    return decisions


def plot_pilot(results, cfg) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    pilot = [r for r in results if r[0] == "pilot"]
    if not pilot:
        return
    samples = pilot[0][1]
    if not samples:
        return

    fig, ax = plt.subplots(figsize=(9, 6))
    xs = [s["requests"] for s in samples]
    ys = [POLICY_Y.get(s["policy"], -1) for s in samples]
    ax.step(xs, ys, where="post", label="policy")
    ax.set_yticks(list(POLICY_Y.values()))
    ax.set_yticklabels(list(POLICY_Y.keys()))
    ax.set_xlabel("requests issued")
    ax.set_ylabel("eviction policy")
    ax.set_title("Pilot mode: agent policy switches")

    switch_points = []
    for i in range(1, len(samples)):
        if samples[i]["policy"] != samples[i - 1]["policy"]:
            switch_points.append(i)

    decisions = _agent_decisions(cfg.agent_log)
    for idx in switch_points:
        x = samples[idx]["requests"]
        new_policy = samples[idx]["policy"]
        label = "switch -> %s" % new_policy
        for d in decisions:
            if d.get("new_policy") == new_policy:
                reason = str(d.get("reason", ""))[:60]
                label = "%s -> %s\n%s" % (d.get("old_policy", "?"),
                                          d.get("new_policy", "?"), reason)
                break
        ax.axvline(x, color="tab:red", alpha=0.5, linestyle="--")
        ax.annotate(label, xy=(x, POLICY_Y.get(new_policy, 0.0)),
                    xytext=(x, 2.35), rotation=45, fontsize=7,
                    ha="right", va="top")
    if not switch_points:
        ax.text(0.5, 0.5,
                "no policy switches observed -- is agent.py running?",
                transform=ax.transAxes, ha="center", fontsize=10,
                color="tab:red")

    fig.tight_layout()
    out = "%spilot_decisions.png" % cfg.plot_prefix
    fig.savefig(out)
    plt.close(fig)
    print("saved %s" % out)


# --------------------------------------------------------------------------
# A/B comparison: rule vs llm vs hybrid decision modes
# --------------------------------------------------------------------------

COMPARE_MODES = ("rule", "llm", "hybrid", "hybrid_conflict",
                 "hybrid_echo", "hybrid_conflict_echo")


def _llm_stats(decisions):
    """Aggregate LLM telemetry out of the agent's kind:"llm" log lines."""
    llm_lines = [d for d in decisions if d.get("kind") == "llm"]
    ok = [d for d in llm_lines
          if d.get("fallback") is not True and d.get("llm_model")]
    fall = [d for d in llm_lines if d.get("fallback") is True]
    lat = [float(d.get("llm_latency_ms", 0.0)) for d in ok]
    tokens_p = sum(int(d.get("llm_tokens_prompt", 0) or 0) for d in ok)
    tokens_c = sum(int(d.get("llm_tokens_completion", 0) or 0) for d in ok)
    agreements = [int(d.get("agreement", -1)) for d in ok if "agreement" in d]
    arbiter_lines = [d for d in ok if d.get("role") == "arbiter"]
    return {
        "api_calls": len(llm_lines),
        "ok_calls": len(ok),
        "fallback": len(fall),
        "fallback_pct": round(100.0 * len(fall) / len(llm_lines), 1)
                        if llm_lines else 0.0,
        "avg_latency_ms": round(sum(lat) / len(lat), 2) if lat else 0.0,
        "max_latency_ms": round(max(lat), 2) if lat else 0.0,
        "tokens_prompt": tokens_p,
        "tokens_completion": tokens_c,
        "tokens_total": tokens_p + tokens_c,
        "agreement_pct": (round(100.0 * sum(agreements) / len(agreements), 1)
                          if agreements else None),
        "agreement_list": agreements,
        "arbiter_calls": len(arbiter_lines),
        "arbiter_picks": [d.get("llm_policy") for d in arbiter_lines],
        "models": dict(collections.Counter(d.get("llm_model") for d in ok)),
    }


def _compare_paths(cfg, mode, seed, ext):
    """Per-mode artifact path for a compare sub-run.

    Multi-seed compares must not reuse AOF/log files across seeds: a
    replayed AOF silently re-introduces the previous seed's keys.
    """
    prefix = ("%s.s%d" % (cfg.aof_prefix, seed)) if seed is not None \
        else cfg.aof_prefix
    return Path("%s.compare.%s%s" % (prefix, mode, ext))


def compare_all(cfg, workload, preload_keys, seed=None, results_out=None):
    """Run rule / llm / hybrid / hybrid_echo back to back, fresh server each.

    Every sub-run replays the SAME generated workload: by default all modes
    run the full --requests length so the hit rates are comparable.  An
    explicit --llm-requests cap shrinks the llm/hybrid sub-runs (cheaper
    API spend) but makes the run NOT comparable -- a loud warning is
    printed and flagged in the JSON/report.

    ``seed`` namespaces the per-mode AOF/log artifacts (multi-seed runs).
    ``results_out`` collects {seed: results} for the aggregation step.
    """
    capped = (cfg.llm_requests > 0 and cfg.llm_requests < cfg.requests)
    if capped:
        print("\nWARNING: --llm-requests %d < --requests %d.  The llm/hybrid "
              "sub-runs will be SHORTER than rule, so hit rates are NOT "
              "comparable across modes.  Drop --llm-requests (default) to "
              "hide\n" % (cfg.llm_requests, cfg.requests), file=sys.stderr)
    results = {}
    for mode in COMPARE_MODES:
        print("\n== compare sub-run: %s ==" % mode)
        cfg.aof_path = _compare_paths(cfg, mode, seed, ".aof")
        cfg.server_log = _compare_paths(cfg, mode, seed, ".log")
        cfg.access_log = _compare_paths(cfg, mode, seed, ".access.jsonl")
        cfg.agent_log = str(_compare_paths(cfg, mode, seed, ".decisions.jsonl"))
        # Truncate the per-mode append-only files: a leftover from a previous
        # run/sub-run would pollute the LLM stats and switch log below.
        for path in (cfg.aof_path, cfg.server_log, cfg.access_log,
                     Path(cfg.agent_log)):
            try:
                os.remove(path)
            except OSError:
                pass
        n_req = (cfg.requests if mode == "rule"
                 else (min(cfg.requests, cfg.llm_requests)
                       if cfg.llm_requests > 0 else cfg.requests))
        cfg.decision_mode = mode

        server = start_server(cfg)
        telemetry_fh = open(cfg.access_log, "a", encoding="utf-8")
        agent = spawn_agent(cfg)
        time.sleep(0.3)
        samples, stats = run_mode("compare_%s" % mode, workload[:n_req],
                                  preload_keys, cfg, telemetry_fh)
        if agent is not None:
            agent.terminate()
            try:
                agent.wait(2)
            except subprocess.TimeoutExpired:
                agent.kill()
        telemetry_fh.close()
        stop_server(server)

        decisions = _agent_decisions(cfg.agent_log)
        results[mode] = {
            "samples": samples,
            "stats": stats,
            "requests": n_req,
            "_log_path": cfg.agent_log,
            "llm_stats": _llm_stats(decisions),
            "switches": [
                {"timestamp": d.get("timestamp", ""),
                 "old_policy": d.get("old_policy", ""),
                 "new_policy": d.get("new_policy", "")}
                for d in decisions if d.get("kind") == "switch"
            ],
        }

    # Multi-model assertion: with 10+ successful real LLM decisions at least
    # two different models must have been consulted (round-robin works).
    # The echo client (hybrid_echo timing control) is excluded: it always
    # answers from "echo-model" and would otherwise mask or fake this check.
    real_modes = [m for m in ("llm", "hybrid") if m in results]
    ok_calls = sum(results[m]["llm_stats"]["ok_calls"] for m in real_modes)
    distinct = set()
    for m in real_modes:
        distinct.update(results[m]["llm_stats"]["models"].keys())
    if ok_calls >= 10 and len(distinct) < 2:
        raise AssertionError(
            "expected >=2 distinct LLM models over %d calls, saw %r"
            % (ok_calls, sorted(distinct)))

    if results_out is not None:
        results_out[seed] = results
    _plot_compare(results, cfg)
    _dump_compare_json(results, cfg)
    _print_compare_table(results, cfg)
    return results


def _print_compare_table(results, cfg) -> None:
    header = ("%-13s | %10s | %8s | %8s | %8s | %9s | %10s | %11s | %16s" %
              ("Mode", "Overall HR", "P1 HR", "P2 HR", "P3 HR", "Switches",
               "LLM Calls", "Fallback %", "Avg LLM Latency"))
    print("\n" + header)
    print("-" * len(header))
    for mode in COMPARE_MODES:
        v = results[mode]
        ls = v["llm_stats"]
        print("%-13s | %9.4f | %7.4f | %7.4f | %7.4f | %8d | %9d | %10.1f | %15.2fms"
              % (mode, v["stats"]["overall"], v["stats"]["phase_rates"][1],
                 v["stats"]["phase_rates"][2], v["stats"]["phase_rates"][3],
                 v["stats"]["switches"], ls["api_calls"], ls["fallback_pct"],
                 ls["avg_latency_ms"]))
    print("-" * len(header))
    if cfg.llm_requests > 0 and cfg.llm_requests < cfg.requests:
        print("\nWARNING: llm/hybrid sub-runs were capped at %d requests "
              "while rule ran the full %d -- these hit rates are NOT "
              "comparable across modes.  Re-run with --llm-requests 0 "
              "(default) for a fair comparison."
              % (cfg.llm_requests, cfg.requests))


def _plot_compare(results, cfg) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    # 1) hit rate curves for all three decision modes.
    fig, ax = plt.subplots(figsize=(9, 6))
    for mode in COMPARE_MODES:
        samples = results[mode]["samples"]
        ax.plot([s["requests"] for s in samples],
                [s["overall_hit_rate"] for s in samples],
                marker="o", markersize=3, label=mode)
    ax.set_xlabel("requests issued")
    ax.set_ylabel("hit rate (overall)")
    ax.set_ylim(0.0, 1.05)
    ax.set_title("Decision-mode comparison (rule / llm / hybrid, "
                 "fresh server each)")
    ax.legend()
    fig.tight_layout()
    out = "%shit_rate_comparison.png" % cfg.plot_prefix
    fig.savefig(out)
    plt.close(fig)
    print("saved %s" % out)

    # 2) LLM latency per decision, colored by the model that answered.
    model_colors = {"gpt-oss-120b": "tab:blue",
                    "llama-3.3-70b-versatile": "tab:orange",
                    "qwen-3.6-27b": "tab:green"}
    fig, ax = plt.subplots(figsize=(9, 6))
    seen = set()
    for mode in ("llm", "hybrid"):
        decisions = _agent_decisions(results[mode].get("_log_path"))
        pts = [(i, float(d.get("llm_latency_ms", 0.0)), d.get("llm_model"))
               for i, d in enumerate(decisions)
               if d.get("kind") == "llm" and d.get("llm_model")]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        if not ys:
            continue
        for (i, y, model) in pts:
            ax.scatter(i, y, s=12, alpha=0.8,
                       color=model_colors.get(model, "tab:gray"))
            seen.add(model)
    handles = [plt.Line2D([], [], marker="o", linestyle="None", markersize=6,
                          color=model_colors.get(m, "tab:gray"), label=m)
               for m in sorted(seen)]
    ax.legend(handles=handles)
    ax.set_xlabel("decision cycle (index within run)")
    ax.set_ylabel("LLM latency (ms)")
    ax.set_title("LLM decision latency (fallback cycles excluded)")
    fig.tight_layout()
    out = "%sllm_latency_vs_requests.png" % cfg.plot_prefix
    fig.savefig(out)
    plt.close(fig)
    print("saved %s" % out)

    # 3) model distribution pie across llm + hybrid.
    counts = collections.Counter()
    for mode in ("llm", "hybrid"):
        counts.update(results[mode]["llm_stats"]["models"])
    if counts:
        fig, ax = plt.subplots(figsize=(6, 6))
        ax.pie(list(counts.values()),
               labels=list(counts.keys()),
               autopct="%1.0f%%",
               colors=[model_colors.get(m, "tab:gray") for m in counts])
        ax.set_title("LLM model usage (llm + hybrid sub-runs)")
        fig.tight_layout()
        out = "%smodel_distribution.png" % cfg.plot_prefix
        fig.savefig(out)
        plt.close(fig)
        print("saved %s" % out)


def _dump_compare_json(results, cfg) -> None:
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "config": {
            "requests": cfg.requests,
            "llm_requests": cfg.llm_requests,
            "capped": cfg.llm_requests > 0 and cfg.llm_requests < cfg.requests,
            "working_set": cfg.working_set,
            "cache_size_mb": cfg.cache_size_mb,
            "value_size": cfg.value_size,
            "seed": getattr(cfg, "seed", None),
            "hot_size": cfg.hot_size,
            "burst_size": cfg.burst_size,
            "scan_size": cfg.scan_size,
        },
        "modes": {},
    }
    for mode in COMPARE_MODES:
        v = results[mode]
        payload["modes"][mode] = {
            "requests": v["requests"],
            "overall": v["stats"]["overall"],
            "phase_rates": v["stats"]["phase_rates"],
            "switches": v["stats"]["switches"],
            "switch_log": v["switches"],
            "llm_stats": v["llm_stats"],
        }
    out = "%scompare_results.json" % cfg.plot_prefix
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    print("saved %s" % out)


# --------------------------------------------------------------------------
# Multi-seed aggregation
# --------------------------------------------------------------------------

def _mean_std(values):
    """(mean, sample-std, min, max); std 0.0 when n < 2."""
    values = [float(v) for v in values]
    n = len(values)
    mean = sum(values) / n
    if n < 2:
        std = 0.0
    else:
        var = sum((v - mean) ** 2 for v in values) / (n - 1)
        std = var ** 0.5
    return {
        "mean": round(mean, 4),
        "std": round(std, 4),
        "min": round(min(values), 4),
        "max": round(max(values), 4),
    }


def _aggregate_llm_stats(per_seed_modes):
    """Merge llm_stats across seeds (calls/tokens sum, latency weighted)."""
    stats = [m["llm_stats"] for m in per_seed_modes]
    api = sum(s["api_calls"] for s in stats)
    ok = sum(s["ok_calls"] for s in stats)
    fall = sum(s["fallback"] for s in stats)
    lat = sum(s["avg_latency_ms"] * s["ok_calls"] for s in stats)
    agreements = [a for s in stats for a in (s.get("agreement_list") or [])]
    models = collections.Counter()
    for s in stats:
        models.update(s["models"])
    return {
        "api_calls": api,
        "ok_calls": ok,
        "fallback": fall,
        "fallback_pct": round(100.0 * fall / api, 1) if api else 0.0,
        "avg_latency_ms": round(lat / ok, 2) if ok else 0.0,
        "max_latency_ms": round(max((s["max_latency_ms"] for s in stats),
                                    default=0.0), 2),
        "tokens_prompt": sum(s["tokens_prompt"] for s in stats),
        "tokens_completion": sum(s["tokens_completion"] for s in stats),
        "tokens_total": sum(s["tokens_total"] for s in stats),
        "agreement_pct": (round(100.0 * sum(agreements) / len(agreements), 1)
                          if agreements else None),
        "arbiter_calls": sum(s.get("arbiter_calls", 0) for s in stats),
        "arbiter_picks": [p for s in stats
                          for p in (s.get("arbiter_picks") or [])],
        "models": dict(models),
    }


def _aggregate_seeds(per_seed, seeds, cfg):
    """Mean +/- std across seeds, per-seed winners, honest verdict.

    The verdict demands three things before recommending a mode:
      1. mean hit-rate gap vs rule >= 1 pt,
      2. the mode wins on a majority of seeds,
      3. the timing control (rule vs hybrid_echo) shows gap < 0.5 pt,
         i.e. identical decisions at identical timing really do measure
         identically.
    Otherwise the evidence is inconclusive.
    """
    modes = COMPARE_MODES
    print("\n===== MULTI-SEED SUMMARY (%d seeds: %s) ====="
          % (len(seeds), ", ".join(str(s) for s in seeds)))

    aggregated = {}
    for mode in modes:
        ms = [per_seed[s][mode] for s in seeds]
        overall = _mean_std([m["stats"]["overall"] for m in ms])
        phase_rates = {
            str(ph): _mean_std([m["stats"]["phase_rates"][ph] for m in ms])
            for ph in (1, 2, 3)
        }
        switches = sum(m["stats"]["switches"] for m in ms) / len(ms)
        llm_stats = _aggregate_llm_stats(ms)
        aggregated[mode] = {
            "overall": overall,
            "phase_rates": phase_rates,
            "switches": round(switches, 1),
            "llm_stats": llm_stats,
        }
        print("  %-12s overall %.4f +- %.4f | P1 %.4f +- %.4f | "
              "P2 %.4f +- %.4f | P3 %.4f +- %.4f | switches %.1f"
              % (mode, overall["mean"], overall["std"],
                 phase_rates["1"]["mean"], phase_rates["1"]["std"],
                 phase_rates["2"]["mean"], phase_rates["2"]["std"],
                 phase_rates["3"]["mean"], phase_rates["3"]["std"],
                 switches))

    wins = {}
    for seed in seeds:
        winner = max(modes, key=lambda m: per_seed[seed][m]["stats"]["overall"])
        wins[winner] = wins.get(winner, 0) + 1
    print("\n  per-seed wins:", ", ".join("%s %d" % (m, n)
                                          for m, n in sorted(wins.items()))
          if wins else "none")

    rule = aggregated["rule"]
    echo = aggregated.get("hybrid_echo")
    echo_gap = abs(echo["overall"]["mean"] - rule["overall"]["mean"]) * 100.0 \
        if echo else None
    if echo_gap is not None:
        print("  timing control: |rule - hybrid_echo| = %.2f pt (must be "
              "< 0.5 pt)" % echo_gap)

    verdict = {
        "adopt": None,
        "gaps_pt": {m: round((aggregated[m]["overall"]["mean"]
                              - rule["overall"]["mean"]) * 100.0, 2)
                    for m in modes},
        "per_seed_wins": wins,
        "echo_control_gap_pt": round(echo_gap, 2) if echo_gap is not None
        else None,
        "text": "",
    }
    # Only 'llm' can be adopted: hybrid is diagnostic-only and equals the
    # rule by construction, so a hybrid "win" is harness noise.
    candidates = [m for m in ("llm",)
                  if verdict["gaps_pt"][m] >= 1.0
                  and wins.get(m, 0) > len(seeds) / 2]
    control_ok = echo_gap is None or echo_gap < 0.5
    if candidates and control_ok:
        verdict["adopt"] = max(candidates,
                               key=lambda m: verdict["gaps_pt"][m])
        verdict["text"] = ("Adopt **%s**: +%.1f pt vs rule mean, wins on "
                           "%d/%d seeds, timing control clean (%.2f pt)."
                           % (verdict["adopt"],
                              verdict["gaps_pt"][verdict["adopt"]],
                              wins.get(verdict["adopt"], 0), len(seeds),
                              verdict["echo_control_gap_pt"] or 0.0))
    elif not control_ok and echo_gap is not None:
        verdict["text"] = ("**INCONCLUSIVE -- HARNESS FAILED CONTROL.** "
                           "rule vs hybrid_echo differ by %.2f pt (>= 0.5), "
                           "so hit-rate gaps between modes cannot be "
                           "attributed to the decision logic." % echo_gap)
    else:
        verdict["text"] = ("**INCONCLUSIVE.** No mode beats rule by >= 1 pt "
                           "on a majority of seeds (gaps: %s). Use rule."
                           % ", ".join("%s %+.1f" % (m, g)
                                       for m, g in verdict["gaps_pt"].items()
                                       if m != "rule"))
    print("\n  VERDICT: %s\n" % verdict["text"])

    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "config": {
            "requests": cfg.requests,
            "llm_requests": cfg.llm_requests,
            "capped": cfg.llm_requests > 0
            and cfg.llm_requests < cfg.requests,
            "working_set": cfg.working_set,
            "cache_size_mb": cfg.cache_size_mb,
            "value_size": cfg.value_size,
            "seed": getattr(cfg, "seed", None),
            "hot_size": cfg.hot_size,
            "burst_size": cfg.burst_size,
            "scan_size": cfg.scan_size,
        },
        "seeds": list(seeds),
        "seeds_n": len(seeds),
        "per_seed": {
            str(seed): {
                mode: {
                    "requests": per_seed[seed][mode]["requests"],
                    "overall": per_seed[seed][mode]["stats"]["overall"],
                    "phase_rates": per_seed[seed][mode]["stats"]["phase_rates"],
                    "switches": per_seed[seed][mode]["stats"]["switches"],
                    "switch_log": per_seed[seed][mode]["switches"],
                    "llm_stats": per_seed[seed][mode]["llm_stats"],
                }
                for mode in modes
            }
            for seed in seeds
        },
        "aggregated": aggregated,
        "verdict": verdict,
    }
    out = "%scompare_results.json" % cfg.plot_prefix
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    print("saved %s (multi-seed aggregated)" % out)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="AdaptiCache benchmark: fresh per-mode server, phased "
                    "workload with real eviction pressure, policy comparison.")
    parser.add_argument("--mode", default="all", choices=MODES + ("all",))
    parser.add_argument("--requests", type=int, default=50000)
    parser.add_argument("--working-set", type=int, default=100000,
                        help="unique keys preloaded (and used by the "
                             "phase-1 zipf pool) before the phases")
    parser.add_argument("--cache-size-mb", type=int, default=2,
                        help="server --memory-limit for every mode")
    parser.add_argument("--value-size", type=int, default=64,
                        help="exact byte size of every stored value")
    parser.add_argument("--alpha", type=float, default=DEFAULT_ALPHA,
                        help="zipf skew exponent (phase 1 and hot reads)")
    parser.add_argument("--hot-size", type=int, default=17000,
                        help="phase 2 hot pool: zipf GETs on this many keys")
    parser.add_argument("--scan-size", type=int, default=40000,
                        help="phase 2 scan pool: round-robin GETs, must "
                             "exceed the cache's key capacity to collapse LRU")
    parser.add_argument("--burst-size", type=int, default=3000,
                        help="burst pool read at each phase start, then "
                             "idle; past the LRU horizon LRU drops these "
                             "keys, LFU/SIEVE keep them")
    parser.add_argument("--cold-ratio", type=float, default=None,
                        help="share of phase-1 (and the phase-3 first-half) "
                             "requests that SET a brand-new key never read "
                             "again.  Each SET evicts one key, so this is the "
                             "churn that must blow the eviction frontier past "
                             "the burst pool within a phase (>= capacity / "
                             "phase length keeps it honest).  Default 0.50 "
                             "(0.30 with --churn-regime moderate)")
    parser.add_argument("--scan-write-ratio", type=float, default=None,
                        help="analogous churn for phase 2 / phase-3 second "
                             "half; the remaining requests split 50/50 between "
                             "hot zipfian GETs and the resident scan.  "
                             "Default 0.50 (0.30 with --churn-regime moderate)")
    parser.add_argument("--churn-regime", default="adversarial",
                        choices=("adversarial", "moderate"),
                        help="adversarial = 0.50/0.50 cold+scan churn (the "
                             "verified-divergence config that saturates the "
                             "rule classifier's confidence); moderate = "
                             "0.30/0.30 churn just above the divergence "
                             "floor, where the rule's signals stay near "
                             "their decision boundaries and genuine "
                             "rule-vs-physics ambiguity can occur.  Explicit "
                             "--cold-ratio/--scan-write-ratio override it.")
    parser.add_argument("--block-size", type=int, default=1000)
    parser.add_argument("--cache-host", default="localhost")
    parser.add_argument("--cache-port", type=int, default=6379)
    parser.add_argument("--admin-host", default="localhost")
    parser.add_argument("--admin-port", type=int, default=8080)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--seeds", type=int, nargs="+", default=None,
                        help="compare across multiple seeds (e.g. --seeds 1 7 "
                             "42 123 999): per-seed compare runs, then an "
                             "aggregated mean +- std verdict.  Defaults to a "
                             "single seed (--seed or 7).")
    parser.add_argument("--server-path", default=str(SCRIPT_DIR.parent / "adapticache"))
    parser.add_argument("--aof-prefix", default="/tmp/adaptivecache_bench")
    parser.add_argument("--wait-timeout", type=float, default=10.0)
    parser.add_argument("--agent-log", default=None,
                        help="agent decision JSONL for plot annotations")
    parser.add_argument("--spawn-agent", action="store_true",
                        help="start agent.py for the pilot mode")
    parser.add_argument("--agent-cooldown", type=float, default=1.0)
    parser.add_argument("--agent-interval", type=float, default=1.0)
    parser.add_argument("--decide-every", type=int, default=5000,
                        help="agent decides at fixed workload positions "
                             "(every N requests, counted from the access "
                             "telemetry) instead of wall-clock intervals -- "
                             "removes run-to-run timing jitter so compare "
                             "sub-runs measure identically (0 = wall-clock)")
    parser.add_argument("--cooldown-req", type=int, default=12000,
                        help="agent post-switch cooldown in requests (0 = "
                             "use --agent-cooldown seconds).  Default > "
                             "--decide-every so the cooldown actually "
                             "throttles: a switch can be undone at most "
                             "every ~2-3 decisions.")
    parser.add_argument("--decision-mode", default="rule",
                        choices=("rule", "llm", "hybrid", "hybrid_conflict"),
                        help="agent decision mode for the pilot / compare "
                             "sub-runs (default rule)")
    parser.add_argument("--compare", action="store_true",
                        help="A/B run: rule -> llm -> hybrid -> hybrid_echo "
                             "on fresh servers, then comparison plots + JSON. "
                             "With --seeds the comparison repeats per seed "
                             "and aggregates mean +/- std.")
    parser.add_argument("--llm-requests", type=int, default=0,
                        help="cap the llm/hybrid compare sub-runs to this "
                             "many requests (0 = same as --requests, the "
                             "fair default; a smaller cap makes the run "
                             "cheaper but NOT comparable across modes)")
    parser.add_argument("--plot-prefix", default="")
    args = parser.parse_args(argv)

    if args.cache_size_mb < 1:
        print("--cache-size-mb must be >= 1", file=sys.stderr)
        return 1
    if args.value_size < 1 or args.working_set < 1 or args.requests < 3:
        print("--value-size/--working-set/--requests must be positive",
              file=sys.stderr)
        return 1
    if args.alpha <= 0:
        print("--alpha must be > 0", file=sys.stderr)
        return 1
    # Churn regime defaults: adversarial = verified-divergence 0.50/0.50;
    # moderate = gentler churn where the rule's signals stay near their
    # thresholds (the regime where rule-vs-physics conflict can occur).
    if args.cold_ratio is None:
        args.cold_ratio = 0.50 if args.churn_regime == "adversarial" else 0.30
    if args.scan_write_ratio is None:
        args.scan_write_ratio = (0.50 if args.churn_regime == "adversarial"
                                 else 0.30)
    if not (0.0 <= args.cold_ratio < 1.0 and
            0.0 <= args.scan_write_ratio < 1.0):
        print("--cold-ratio/--scan-write-ratio must be in [0, 1)",
              file=sys.stderr)
        return 1
    if args.hot_size < 1 or args.scan_size < 1 or args.burst_size < 1:
        print("--hot-size/--scan-size/--burst-size must be positive",
              file=sys.stderr)
        return 1
    if args.llm_requests < 0:
        print("--llm-requests must be >= 0 (0 = run all modes at --requests)",
              file=sys.stderr)
        return 1
    cap = cache_capacity(args.cache_size_mb, args.value_size)
    if args.hot_size + args.burst_size > cap:
        print("--hot-size + --burst-size must be <= the cache's key "
              "capacity (%d at %dMB/%dB values)" %
              (cap, args.cache_size_mb, args.value_size), file=sys.stderr)
        return 1
    if args.hot_size + args.burst_size + args.scan_size > args.working_set:
        print("--hot-size + --burst-size + --scan-size must be <= "
              "--working-set", file=sys.stderr)
        return 1
    if args.requests < args.burst_size * 6:
        print("--requests must be at least 6x --burst-size (each phase "
              "must have room for the burst read plus real traffic)",
              file=sys.stderr)
        return 1
    per_phase = args.requests // 3
    idle_churn = (args.cold_ratio + args.scan_write_ratio) / 2.0
    per_phase_churn = idle_churn * (per_phase - args.burst_size)
    if per_phase_churn < cap // 2:
        print("warning: per-phase churn (~%.0f SETs) may not blow the "
              "eviction frontier past the burst pool (cache holds %d "
              "keys); raise --cold-ratio/--scan-write-ratio for sharper "
              "LRU-vs-LFU/SIEVE separation" % (per_phase_churn, cap),
              file=sys.stderr)

    args.server_path = Path(args.server_path)
    args.aof_path = Path(args.aof_prefix + ".aof")
    args.server_log = Path(args.aof_prefix + ".log")
    args.access_log = Path(args.aof_prefix + ".access.jsonl")
    if args.agent_log is None:
        args.agent_log = args.aof_prefix + ".decisions.jsonl"

    rng = random.Random(args.seed)
    keys = _key_names(args.working_set)
    rng.shuffle(keys)
    workload = build_workload(keys, args.requests, rng, args)
    preload_keys = keys

    modes = MODES if args.mode == "all" else (args.mode,)
    results = []
    server = None
    agent = None
    telemetry_fh = None
    try:
        if args.compare:
            seeds = list(args.seeds) if args.seeds \
                else [args.seed if args.seed is not None else 7]
            per_seed = {}
            for seed in seeds:
                seed_rng = random.Random(seed)
                seed_keys = _key_names(args.working_set)
                seed_rng.shuffle(seed_keys)
                seed_workload = build_workload(seed_keys, args.requests,
                                               seed_rng, args)
                print("\n===== compare seed %d (%d seeds planned) ====="
                      % (seed, len(seeds)))
                compare_all(args, seed_workload, seed_keys, seed=seed,
                            results_out=per_seed)
            if len(seeds) > 1:
                _aggregate_seeds(per_seed, seeds, args)
            return 0
        for mode in modes:
            print("\n== mode %s ==" % mode)
            server = start_server(args)
            if mode == "pilot" and args.spawn_agent:
                telemetry_fh = open(args.access_log, "a", encoding="utf-8")
                agent = spawn_agent(args)
                time.sleep(0.3)
            samples, stats = run_mode(mode, workload, preload_keys, args,
                                      telemetry_fh if mode == "pilot" else None)
            results.append((mode, samples, stats))
            if agent is not None:
                agent.terminate()
                try:
                    agent.wait(2)
                except subprocess.TimeoutExpired:
                    agent.kill()
                agent = None
            if telemetry_fh is not None:
                telemetry_fh.close()
                telemetry_fh = None
            stop_server(server)
            server = None
    finally:
        stop_server(server)
        if agent is not None:
            agent.kill()
        if telemetry_fh is not None:
            telemetry_fh.close()

    print_summary(results, args)
    print_pilot_report(results, args)
    plot_hit_rates(results, args)
    plot_pilot(results, args)
    return 0


def run() -> None:
    sys.exit(main())


if __name__ == "__main__":
    run()
