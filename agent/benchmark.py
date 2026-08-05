#!/usr/bin/env python3
"""CachePilot workload benchmark.

Measures real behavior of a running CachePilot server (./cachepilot) under
four scenarios: policy pinned to lru / lfu / sieve, and 'pilot' where
the tuning agent (agent.py) runs alongside and switches policies.

Every number in the summary comes from live TCP/HTTP exchanges with the
server.  If the server is unreachable the tool fails loudly -- it never
falls back to synthetic numbers.
"""

import argparse
import json
import random
import socket
import sys
import time
from pathlib import Path

from metrics import fetch_metrics

STATIC_POLICY = {"static_lru": "lru", "static_lfu": "lfu", "static_sieve": "sieve"}
MODES = ("static_lru", "static_lfu", "static_sieve", "pilot")
WORKLOADS = ("zipfian", "uniform", "sequential", "mixed")
POLICY_Y = {"lru": 0.0, "lfu": 1.0, "sieve": 2.0}


# --------------------------------------------------------------------------
# Workload generators
# --------------------------------------------------------------------------

def _key_names(keys: int) -> list:
    return ["key%05d" % i for i in range(keys)]


def _zipf_weights(keys: list, alpha: float) -> list:
    weights = [1.0 / (rank ** alpha) for rank in range(1, len(keys) + 1)]
    total = sum(weights)
    return [w / total for w in weights]


def zipfian(keys: list, n: int, alpha: float = 1.0, rng=random) -> list:
    """n draws from keys with power-law probability p(k) ~ rank(k)^-alpha."""
    weights = _zipf_weights(keys, alpha)
    return rng.choices(keys, weights=weights, k=n)


def uniform(keys: list, n: int, rng=random) -> list:
    return rng.choices(keys, k=n)


def sequential(keys: list, n: int) -> list:
    """Round-robin scan: key0, key1, ..., keyN-1, key0, ..."""
    return [keys[i % len(keys)] for i in range(n)]


def mixed(keys: list, n: int, rng=random) -> list:
    """70% zipfian draws interleaved with 30% sequential scan."""
    zipf = zipfian(keys, n, rng=rng)
    scan = sequential(keys, n)
    return [zipf[i] if i % 10 < 7 else scan[i] for i in range(n)]


WORKLOAD_FN = {
    "zipfian": zipfian,
    "uniform": uniform,
    "sequential": sequential,
    "mixed": mixed,
}


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
# Measurement
# --------------------------------------------------------------------------

def run_mode(name, workload, cfg, rng):
    """Run one benchmark mode against the live server; return sample list."""
    samples = []
    hits = total = 0
    switches = 0
    prev_policy = None
    peak_memory = 0
    evictions_start = None
    start_wall = time.time()

    with socket.create_connection((cfg.cache_host, cfg.cache_port), timeout=5) as sock:
        sock.settimeout(10)
        ls = LineSocket(sock)

        if name in STATIC_POLICY:
            if not switch_policy(ls, STATIC_POLICY[name]):
                raise RuntimeError("%s: SWITCH_POLICY %s rejected" %
                                   (name, STATIC_POLICY[name]))

        for i in range(cfg.keys):
            set_value(ls, "key%05d" % i, "v%d" % i)
        for i in range(cfg.pressure):
            set_value(ls, "filler%06d" % i, "x")

        metrics = fetch_metrics(cfg.admin_host, cfg.admin_port)
        evictions_start = int(metrics.get("evictions", 0))

        for start in range(0, len(workload), cfg.block_size):
            end = min(start + cfg.block_size, len(workload))
            block_times = []
            for i in range(start, end):
                key = workload[i]
                t0 = time.perf_counter()
                ok = get_value(ls, key)
                block_times.append((time.perf_counter() - t0) * 1000.0)
                hits += 1 if ok else 0
                total += 1

            metrics = fetch_metrics(cfg.admin_host, cfg.admin_port)
            policy = str(metrics.get("policy", ""))
            if prev_policy is not None and policy != prev_policy:
                switches += 1
            prev_policy = policy
            peak_memory = max(peak_memory, int(metrics.get("memory_bytes", 0)))

            sample = {
                "requests": total,
                "hit_rate": hits / total,
                "latency_ms": sum(block_times) / len(block_times),
                "current_policy": policy,
                "policy_switches": switches,
                "wall_elapsed": time.time() - start_wall,
            }
            samples.append(sample)
            print("%s: %d req  hit=%.4f  lat=%.3fms  policy=%s" %
                  (name, total, sample["hit_rate"], sample["latency_ms"], policy),
                  flush=True)

        metrics = fetch_metrics(cfg.admin_host, cfg.admin_port)
        evictions = int(metrics.get("evictions", 0)) - evictions_start

    return samples, evictions, peak_memory


def preflight(cfg) -> None:
    """Fail loudly (exit 1) if the server is not reachable."""
    try:
        with socket.create_connection((cfg.cache_host, cfg.cache_port), timeout=3) as sock:
            sock.settimeout(3)
            ls = LineSocket(sock)
            if ls.command(b"GET __bench_probe__") != b"$-1":
                raise RuntimeError("unexpected probe reply")
        fetch_metrics(cfg.admin_host, cfg.admin_port)
    except Exception as exc:
        print("benchmark aborted: cannot reach CachePilot server (%s)" % exc,
              file=sys.stderr)
        print("start it first, e.g.:  ./cachepilot --port %d --admin-port %d" %
              (cfg.cache_port, cfg.admin_port), file=sys.stderr)
        sys.exit(1)


# --------------------------------------------------------------------------
# Output: summary table + plots
# --------------------------------------------------------------------------

def print_summary(results, cfg) -> None:
    rows = []
    for name, samples, evictions, peak_memory in results:
        final_hit = samples[-1]["hit_rate"] if samples else 0.0
        avg_lat = sum(s["latency_ms"] for s in samples) / len(samples)
        switches = samples[-1]["policy_switches"] if samples else 0
        rows.append((name, final_hit, avg_lat, peak_memory, switches, evictions))

    print("\n%12s | %14s | %12s | %12s | %15s" %
          ("Mode", "Final Hit Rate", "Avg Latency", "Peak Memory", "Policy Switches"))
    print("-" * 72)
    for name, hit, lat, mem, switches, ev in rows:
        print("%12s | %13.4f | %11.3fms | %11.0f B | %15d" %
              (name, hit, lat, mem, switches))
    print("-" * 72)
    for name, hit, lat, mem, switches, ev in rows:
        print("note %s: evictions during run = %d" % (name, ev))
    if cfg.pressure == 0:
        print("note: no eviction pressure was added (--pressure 0); with the "
              "default 64MB limit and %d small keys the cache never evicts, so "
              "hit rates converge near 1.0. Restart the server with a small "
              "--memory-limit or use --pressure to make policies differ." % cfg.keys)
    print("note: modes share one live server, so earlier modes leave state "
          "(hot keys, leftovers) that later modes inherit -- pilot is most "
          "affected. For isolated measurements use --mode <single> against a "
          "freshly started server.")


def plot_hit_rates(results, cfg) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(9, 6))
    for name, samples, _ev, _mem in results:
        xs = [s["requests"] for s in samples]
        ys = [s["hit_rate"] for s in samples]
        ax.plot(xs, ys, marker="o", markersize=3, label=name)
    ax.set_xlabel("requests issued")
    ax.set_ylabel("hit rate")
    ax.set_ylim(0.0, 1.05)
    ax.set_title("CachePilot hit rate vs requests (%s workload)" % cfg.workload)
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
    ys = [POLICY_Y.get(s["current_policy"], -1) for s in samples]
    ax.step(xs, ys, where="post", label="policy")
    ax.set_yticks(list(POLICY_Y.values()))
    ax.set_yticklabels(list(POLICY_Y.keys()))
    ax.set_xlabel("requests issued")
    ax.set_ylabel("eviction policy")
    ax.set_title("Pilot mode: agent policy switches")

    switch_points = []
    for i in range(1, len(samples)):
        if samples[i]["current_policy"] != samples[i - 1]["current_policy"]:
            switch_points.append(i)

    decisions = _agent_decisions(cfg.agent_log)
    used = 0
    for idx in switch_points:
        x = samples[idx]["requests"]
        new_policy = samples[idx]["current_policy"]
        label = "switch -> %s" % new_policy
        for d in decisions:
            if d.get("new_policy") == new_policy:
                reason = str(d.get("reason", ""))[:60]
                label = "%s -> %s\n%s" % (d.get("old_policy", "?"),
                                          d.get("new_policy", "?"), reason)
                used += 1
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
# CLI
# --------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="CachePilot benchmark: measure a running server under "
                    "fixed policies and under agent (pilot) control.")
    parser.add_argument("--mode", default="all", choices=MODES + ("all",))
    parser.add_argument("--workload", default="mixed", choices=WORKLOADS)
    parser.add_argument("--requests", type=int, default=50000)
    parser.add_argument("--keys", type=int, default=10000,
                        help="keys preloaded before the GET workload")
    parser.add_argument("--block-size", type=int, default=1000,
                        help="requests per measurement sample")
    parser.add_argument("--cache-host", default="localhost")
    parser.add_argument("--cache-port", type=int, default=6379)
    parser.add_argument("--admin-host", default="localhost")
    parser.add_argument("--admin-port", type=int, default=8080)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--pressure", type=int, default=0,
                        help="extra filler keys preloaded to force evictions "
                             "(pair with a small server --memory-limit)")
    parser.add_argument("--agent-log", default=None,
                        help="agent decision JSONL (--log) for switch reasons")
    parser.add_argument("--plot-prefix", default="")
    args = parser.parse_args(argv)

    modes = MODES if args.mode == "all" else (args.mode,)
    preflight(args)

    rng = random.Random(args.seed)
    keys = _key_names(args.keys)
    workload = WORKLOAD_FN[args.workload](keys, args.requests, rng=rng)

    results = []
    for mode in modes:
        print("== mode %s (workload=%s, %d requests) ==" %
              (mode, args.workload, args.requests))
        try:
            samples, evictions, peak = run_mode(mode, workload, args, rng)
        except Exception as exc:
            print("mode %s FAILED: %s" % (mode, exc), file=sys.stderr)
            print("results are NOT comparable; refusing to continue.",
                  file=sys.stderr)
            return 1
        results.append((mode, samples, evictions, peak))

    print_summary(results, args)
    plot_hit_rates(results, args)
    plot_pilot(results, args)
    return 0


def run() -> None:
    sys.exit(main())


if __name__ == "__main__":
    run()
