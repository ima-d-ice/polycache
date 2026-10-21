#!/usr/bin/env python3
"""Blob workload benchmark for PolyCache.

Measures what the server actually does with the payloads it now claims to
support: 1KB database-row-sized values and 1MB image-sized values, over a real
TCP connection using RESP2.

For each payload size the tool starts a fresh server, fills it through
pipelined SETs, reads every key back with pipelined GETs, and also issues
sequential request/response round trips for p50/p99 latency. It reports:

  * SET / GET operations per second and MB/s (as observed by the client)
  * p50 / p99 sequential latency
  * used_memory from /metrics and server RSS from ps
  * hit rate and eviction count

The default run uses --no-aof: this measures the server, not the disk. Pass
--aof to include the current JSON-line AOF (it is not fsynced, and large
values are base64-encoded, so expect much lower write throughput).

Nothing is fabricated: if the binary cannot start, the tool exits loudly.
"""

import argparse
import json
import os
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request

HOST = "127.0.0.1"


def parse_size(text: str) -> int:
    text = text.strip().lower()
    units = {"b": 1, "kb": 1024, "mb": 1024 ** 2, "gb": 1024 ** 3}
    for suffix in ("gb", "mb", "kb", "b"):
        if text.endswith(suffix):
            return int(float(text[:-len(suffix)]) * units[suffix])
    return int(text)


def encode(*args) -> bytes:
    out = bytearray(b"*%d\r\n" % len(args))
    for arg in args:
        if isinstance(arg, str):
            arg = arg.encode()
        out += b"$%d\r\n" % len(arg)
        out += arg
        out += b"\r\n"
    return bytes(out)


class Conn:
    def __init__(self, port: int):
        self.sock = socket.create_connection((HOST, port), timeout=30)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.buf = bytearray()
        self.pos = 0

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def _fill(self) -> None:
        # Compact once per arrival instead of erase-from-front per reply:
        # otherwise pipelined 1MB GETs turn into O(batch^2) memmoves.
        if self.pos:
            del self.buf[:self.pos]
            self.pos = 0
        chunk = self.sock.recv(1 << 20)
        if not chunk:
            raise EOFError("server closed the connection")
        self.buf += chunk

    def _line(self) -> bytes:
        while True:
            i = self.buf.find(b"\r\n", self.pos)
            if i >= 0:
                line = bytes(self.buf[self.pos:i])
                self.pos = i + 2
                return line
            self._fill()

    def _exact(self, n: int) -> bytes:
        while len(self.buf) - self.pos < n:
            self._fill()
        out = bytes(self.buf[self.pos:self.pos + n])
        self.pos += n
        return out

    def read_reply(self):
        line = self._line()
        kind = line[:1]
        if kind == b"+":
            return line[1:]
        if kind == b"-":
            raise RuntimeError(line[1:].decode("utf-8", "replace"))
        if kind == b":":
            return int(line[1:])
        if kind == b"$":
            n = int(line[1:])
            if n < 0:
                return None
            data = self._exact(n + 2)
            if data[-2:] != b"\r\n":
                raise RuntimeError("malformed bulk reply")
            return data[:-2]
        raise RuntimeError("unexpected reply: %r" % line[:40])

    def command(self, *args):
        self.sock.sendall(encode(*args))
        return self.read_reply()


def fetch_metrics(port: int) -> dict:
    with urllib.request.urlopen("http://%s:%d/metrics" % (HOST, port),
                                timeout=2) as response:
        return json.loads(response.read().decode())


def rss_mb(pid: int) -> float:
    try:
        out = subprocess.check_output(
            ["ps", "-o", "rss=", "-p", str(pid)], text=True).strip()
        return int(out) / 1024.0 if out else 0.0
    except (subprocess.CalledProcessError, ValueError):
        return 0.0


def free_port() -> int:
    sock = socket.socket()
    try:
        sock.bind((HOST, 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def start_server(args, port: int, admin_port: int, log_path: str, aof_path: str):
    cmd = [args.server_path,
           "--port", str(port),
           "--admin-port", str(admin_port),
           "--memory-limit", str(args.memory_limit),
           "--max-value-size", str(args.max_value_size)]
    if args.aof:
        cmd += ["--aof-file", aof_path]
    else:
        cmd.append("--no-aof")

    log = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=log)
    deadline = time.time() + 10
    while time.time() < deadline:
        if proc.poll() is not None:
            log.close()
            with open(log_path, encoding="utf-8") as handle:
                print(handle.read(), file=sys.stderr)
            raise SystemExit("server exited during startup")
        try:
            fetch_metrics(admin_port)
            return proc, log
        except Exception:
            time.sleep(0.05)
    proc.kill()
    log.close()
    raise SystemExit("server did not become ready")


def stop_server(proc, log) -> None:
    if proc is None:
        return
    proc.terminate()
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(2)
    log.close()


def percentile(values, q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = int(round(q * (len(ordered) - 1)))
    return ordered[max(0, min(index, len(ordered) - 1))]


def make_value(payload: int, index: int) -> bytes:
    if payload <= 4096:
        row = json.dumps({
            "id": index,
            "name": "user-%d" % index,
            "active": True,
            "note": "polycache row payload",
        }, separators=(",", ":")).encode()
        if len(row) >= payload:
            return row[:payload]
        return row + b"." * (payload - len(row))
    # Large payloads model an image/PDF: incompressible binary bytes.
    return os.urandom(payload)


def default_keys(payload: int, memory_mb: int) -> int:
    budget = int(memory_mb * 1024 * 1024 * 0.50)
    return max(16, min(20000, budget // payload))


def run_once(args, payload: int, keys: int, run_index: int) -> dict:
    port = free_port()
    admin_port = free_port()
    tmpdir = tempfile.mkdtemp(prefix="polycache_bench_")
    log_path = os.path.join(tmpdir, "server.log")
    aof_path = os.path.join(tmpdir, "bench.aof")

    proc = None
    log = None
    try:
        proc, log = start_server(args, port, admin_port, log_path, aof_path)
        baseline_rss = rss_mb(proc.pid)
        conn = Conn(port)

        value = make_value(payload, 0)
        key_names = [b"row:%06d" % i for i in range(keys)]

        seq_n = min(keys, 1000 if payload < 65536 else 100)
        set_latencies = []
        for i in range(seq_n):
            started = time.perf_counter()
            reply = conn.command(b"SET", key_names[i], value)
            set_latencies.append((time.perf_counter() - started) * 1000.0)
            if reply != b"OK":
                raise RuntimeError("SET failed: %r" % reply)

        batch = max(1, min(64, (8 * 1024 * 1024) // payload))
        started = time.perf_counter()
        for start in range(0, keys, batch):
            chunk_keys = key_names[start:start + batch]
            conn.sock.sendall(b"".join(encode(b"SET", k, value) for k in chunk_keys))
            for _ in chunk_keys:
                if conn.read_reply() != b"OK":
                    raise RuntimeError("pipelined SET failed")
        elapsed = time.perf_counter() - started
        set_ops = keys / elapsed
        set_mbs = keys * payload / elapsed / 1e6

        after_set_metrics = fetch_metrics(admin_port)
        after_set_rss = rss_mb(proc.pid)

        get_latencies = []
        for i in range(seq_n):
            started = time.perf_counter()
            reply = conn.command(b"GET", key_names[i])
            get_latencies.append((time.perf_counter() - started) * 1000.0)
            if reply is None or len(reply) != payload:
                raise RuntimeError("GET returned the wrong payload")

        started = time.perf_counter()
        received = 0
        for start in range(0, keys, batch):
            chunk_keys = key_names[start:start + batch]
            conn.sock.sendall(b"".join(encode(b"GET", k) for k in chunk_keys))
            for _ in chunk_keys:
                reply = conn.read_reply()
                if reply is None or len(reply) != payload:
                    raise RuntimeError("pipelined GET returned the wrong payload")
                received += len(reply)
        elapsed = time.perf_counter() - started
        get_ops = keys / elapsed
        get_mbs = received / elapsed / 1e6

        after_get_metrics = fetch_metrics(admin_port)
        conn.close()

        return {
            "payload": payload,
            "keys": keys,
            "run": run_index,
            "set_ops": set_ops,
            "set_mbs": set_mbs,
            "get_ops": get_ops,
            "get_mbs": get_mbs,
            "set_p50_ms": percentile(set_latencies, 0.50),
            "set_p99_ms": percentile(set_latencies, 0.99),
            "get_p50_ms": percentile(get_latencies, 0.50),
            "get_p99_ms": percentile(get_latencies, 0.99),
            "used_memory_mb": after_set_metrics.get("used_memory", 0) / (1024 ** 2),
            "used_memory_peak_mb":
                after_set_metrics.get("used_memory_peak", 0) / (1024 ** 2),
            "rss_mb": after_set_rss,
            "baseline_rss_mb": baseline_rss,
            "hit_rate": after_get_metrics.get("hit_rate", 0.0),
            "evictions": after_get_metrics.get("evictions", 0),
            "total_keys": after_set_metrics.get("total_keys", 0),
        }
    finally:
        if proc is not None:
            stop_server(proc, log)
        try:
            os.remove(aof_path)
        except OSError:
            pass
        try:
            os.remove(log_path)
        except OSError:
            pass
        try:
            os.rmdir(tmpdir)
        except OSError:
            pass


def median(rows, field: str) -> float:
    return statistics.median(row[field] for row in rows)


def human_size(payload: int) -> str:
    if payload >= 1024 ** 2:
        return "%d MB" % (payload // 1024 ** 2)
    if payload >= 1024:
        return "%d KB" % (payload // 1024)
    return "%d B" % payload


def print_table(results, memory_mb: int, aof: bool) -> None:
    print()
    print("PolyCache blob benchmark")
    print("  memory limit: %d MB   AOF: %s" %
          (memory_mb, "on (JSON lines, not fsynced)" if aof else "off"))
    print()
    header = ("payload", "keys", "SET ops/s", "SET MB/s", "GET ops/s", "GET MB/s",
              "SET p50/p99 ms", "GET p50/p99 ms", "used MB", "RSS MB", "hit rate")
    print("  ".join("%-15s" % h if h == "payload" else "%12s" % h for h in header))
    print("-" * 150)
    for payload, rows in results:
        print("%-15s" % human_size(payload) +
              "  ".join("%12s" % part for part in (
                  "%d" % rows[0]["keys"],
                  "%.0f" % median(rows, "set_ops"),
                  "%.1f" % median(rows, "set_mbs"),
                  "%.0f" % median(rows, "get_ops"),
                  "%.1f" % median(rows, "get_mbs"),
                  "%.2f / %.2f" % (median(rows, "set_p50_ms"),
                                   median(rows, "set_p99_ms")),
                  "%.2f / %.2f" % (median(rows, "get_p50_ms"),
                                   median(rows, "get_p99_ms")),
                  "%.1f" % median(rows, "used_memory_mb"),
                  "%.1f" % median(rows, "rss_mb"),
                  "%.3f" % median(rows, "hit_rate"),
              )))
    print()
    for payload, rows in results:
        evictions = int(median(rows, "evictions"))
        peak = median(rows, "used_memory_peak_mb")
        print("  %s: evictions=%d, peak used_memory=%.1f MB, baseline RSS=%.1f MB" %
              (human_size(payload), evictions, peak,
               median(rows, "baseline_rss_mb")))
    print()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure PolyCache with 1KB row and 1MB image payloads.")
    parser.add_argument("--server-path", default="./polycache",
                        help="path to the polycache binary")
    parser.add_argument("--memory-limit", type=int, default=1024,
                        help="server --memory-limit in MB (default 1024)")
    parser.add_argument("--max-value-size", type=int, default=512,
                        help="server --max-value-size in MB (default 512)")
    parser.add_argument("--payloads", default="1KB,1MB",
                        help="comma-separated payload sizes (default 1KB,1MB)")
    parser.add_argument("--keys", type=int, default=0,
                        help="keys per payload; 0 chooses 50%% of the cache budget")
    parser.add_argument("--runs", type=int, default=3,
                        help="fresh-server repetitions per payload (default 3)")
    parser.add_argument("--aof", action="store_true",
                        help="enable the AOF instead of --no-aof")
    parser.add_argument("--json", default="",
                        help="optional path for the raw JSON results")
    args = parser.parse_args(argv)

    if not os.path.exists(args.server_path):
        print("server binary not found: %s (run make first)" % args.server_path,
              file=sys.stderr)
        return 1
    if args.runs < 1:
        print("--runs must be at least 1", file=sys.stderr)
        return 1

    payloads = [parse_size(part) for part in args.payloads.split(",") if part.strip()]
    if not payloads or any(p <= 0 for p in payloads):
        print("invalid --payloads", file=sys.stderr)
        return 1

    all_results = []
    for payload in payloads:
        keys = args.keys or default_keys(payload, args.memory_limit)
        rows = []
        for run in range(args.runs):
            print("running %s payload, %d keys, run %d/%d" %
                  (human_size(payload), keys, run + 1, args.runs), flush=True)
            rows.append(run_once(args, payload, keys, run))
        all_results.append((payload, rows))

    print_table(all_results, args.memory_limit, args.aof)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump([{"payload": p, "runs": r} for p, r in all_results],
                      handle, indent=2)
        print("wrote %s" % args.json)

    return 0


if __name__ == "__main__":
    sys.exit(main())
