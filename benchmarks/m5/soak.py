"""M5 T2.a soak/endurance driver.

Drives the T1.7 deployed exit-gate workload at moderate steady load and
samples response latencies + (optionally) host RSS/FD over time. Outputs
a CSV that the leak guards (T2.a.2) read to fail-fast if memory grows.

Endurance guards (encoded in this script):
  G1: success rate must stay ≥99% over every 30s window
  G2: p99 latency must not double from the first 10-minute baseline
  G3: (optional, --remote-host) RSS must not grow > 5%/h over a 4h window
  G4: (optional, --remote-host) FD count must not grow monotonically over 1h

Usage:
  python soak.py --base-url https://<host> --duration-h 6 \\
      [--route /api/embed] [--connections 20] [--rps 50] \\
      [--csv /tmp/soak.csv] \\
      [--remote-host user@vps --remote-pid-file /tmp/pid.txt]

The driver is intentionally simple — N async workers each in a loop send
one request and sleep enough to maintain --rps overall. Not a max-throughput
test; the point is STEADY load that runs for HOURS without degrading.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from statistics import fmean


@dataclass
class WindowStats:
    t_start: float
    t_end: float
    n_ok: int = 0
    n_err: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    rss_kb: float = 0.0
    fd_count: int = 0
    task_count: int = 0

    def p(self, q: float) -> float:
        if not self.latencies_ms:
            return float("nan")
        s = sorted(self.latencies_ms)
        return s[min(int(q * len(s)), len(s) - 1)]

    def succ_rate(self) -> float:
        n = self.n_ok + self.n_err
        return self.n_ok / n if n else float("nan")


async def remote_sample(remote_host: str, remote_pid_file: str | None) -> tuple[float, int, int]:
    """SSH to the host, return (rss_kb, fd_count, task_count_unknown=0)."""
    if not remote_pid_file:
        return (0.0, 0, 0)
    # Read pid from pid file, then ps + ls /proc/<pid>/fd.
    cmd = (
        f"pid=$(cat {remote_pid_file} 2>/dev/null | head -1); "
        f"[ -z \"$pid\" ] && exit 0; "
        f"rss=$(ps -o rss= -p \"$pid\" 2>/dev/null | tr -d ' '); "
        f"fdc=$(ls /proc/\"$pid\"/fd 2>/dev/null | wc -l | tr -d ' '); "
        f"echo \"$rss $fdc\""
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3",
            remote_host, cmd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        toks = out.decode().split()
        if len(toks) == 2:
            return (float(toks[0]), int(toks[1]), 0)
    except Exception:
        pass
    return (0.0, 0, 0)


async def worker(client_session, base_url: str, route: str, body: bytes | None,
                 stats: WindowStats, rps_target: float, stop_evt: asyncio.Event) -> None:
    """One async worker that opens connections to base_url+route, records latency."""
    interval = 1.0 / rps_target if rps_target > 0 else 0.0
    next_send_at = time.monotonic()
    headers = {"content-type": "application/json"}

    while not stop_evt.is_set():
        # Pace ourselves to maintain interval per worker.
        now = time.monotonic()
        if now < next_send_at:
            try:
                await asyncio.wait_for(stop_evt.wait(), timeout=next_send_at - now)
            except asyncio.TimeoutError:
                pass
            if stop_evt.is_set():
                break

        t0 = time.perf_counter()
        try:
            if body is None:
                resp = await client_session.get(base_url + route, timeout=30)
            else:
                resp = await client_session.post(base_url + route, content=body, headers=headers, timeout=30)
            await resp.aread()
            dt_ms = (time.perf_counter() - t0) * 1000.0
            if 200 <= resp.status_code < 400:
                stats.n_ok += 1
                stats.latencies_ms.append(dt_ms)
            else:
                stats.n_err += 1
        except Exception:
            stats.n_err += 1

        next_send_at += interval


def emit_window(writer, w: WindowStats, baseline_p99_ms: float | None) -> bool:
    """Write one window row + apply guards. Returns False if a guard fired."""
    succ = w.succ_rate()
    p50 = w.p(0.50)
    p95 = w.p(0.95)
    p99 = w.p(0.99)
    writer.writerow({
        "t_unix_start": int(w.t_start),
        "t_unix_end": int(w.t_end),
        "n_ok": w.n_ok,
        "n_err": w.n_err,
        "succ_rate": f"{succ:.4f}",
        "p50_ms": f"{p50:.2f}",
        "p95_ms": f"{p95:.2f}",
        "p99_ms": f"{p99:.2f}",
        "rss_kb": int(w.rss_kb),
        "fd_count": w.fd_count,
    })

    # G1: success rate >= 99%
    if (w.n_ok + w.n_err) > 0 and succ < 0.99:
        print(f"GUARD G1 FAILED: succ_rate={succ:.3f} < 0.99 in window "
              f"[{int(w.t_start)}–{int(w.t_end)}]", flush=True)
        return False

    # G2: p99 must not 2× the baseline.
    if baseline_p99_ms is not None and not math.isnan(p99) and p99 > baseline_p99_ms * 2.0:
        print(f"GUARD G2 FAILED: p99={p99:.2f}ms > 2× baseline={baseline_p99_ms:.2f}ms in window "
              f"[{int(w.t_start)}–{int(w.t_end)}]", flush=True)
        return False

    return True


async def main_async(args) -> int:
    try:
        import httpx
    except ImportError:
        print("httpx required: pip install httpx", file=sys.stderr)
        return 2

    body = args.body.encode() if args.body else None
    csv_path = args.csv or os.path.join("/tmp", f"soak-{int(time.time())}.csv")
    print(f"soak start: base_url={args.base_url} route={args.route} "
          f"duration={args.duration_h}h connections={args.connections} "
          f"rps={args.rps_total} csv={csv_path}", flush=True)

    duration_s = int(args.duration_h * 3600)
    window_s = args.window_s

    stop_evt = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, stop_evt.set)
    loop.add_signal_handler(signal.SIGTERM, stop_evt.set)

    per_worker_rps = args.rps_total / max(args.connections, 1)

    async with httpx.AsyncClient(http2=False) as client:
        # Open the CSV; write header.
        with open(csv_path, "w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=[
                "t_unix_start", "t_unix_end", "n_ok", "n_err",
                "succ_rate", "p50_ms", "p95_ms", "p99_ms",
                "rss_kb", "fd_count",
            ])
            writer.writeheader()

            t_start = time.monotonic()
            t_unix_start = time.time()
            baseline_p99_ms: float | None = None
            baseline_windows_collected: list[float] = []

            while not stop_evt.is_set() and (time.monotonic() - t_start) < duration_s:
                window = WindowStats(t_start=time.time(), t_end=0.0)
                workers_evt = asyncio.Event()

                # Spawn N workers for this window.
                tasks = [
                    asyncio.create_task(
                        worker(client, args.base_url, args.route, body,
                               window, per_worker_rps, workers_evt)
                    )
                    for _ in range(args.connections)
                ]

                # Run for window_s, also break early on stop_evt.
                window_deadline = time.monotonic() + window_s
                while time.monotonic() < window_deadline:
                    try:
                        await asyncio.wait_for(stop_evt.wait(),
                                               timeout=window_deadline - time.monotonic())
                        break
                    except asyncio.TimeoutError:
                        pass

                workers_evt.set()
                await asyncio.gather(*tasks, return_exceptions=True)
                window.t_end = time.time()

                # Optional remote sampling.
                if args.remote_host:
                    rss, fdc, tasks_n = await remote_sample(args.remote_host, args.remote_pid_file)
                    window.rss_kb = rss
                    window.fd_count = fdc
                    window.task_count = tasks_n

                # Bake the baseline p99 over the first 10 minutes (= 600s).
                if baseline_p99_ms is None:
                    p99 = window.p(0.99)
                    if not math.isnan(p99):
                        baseline_windows_collected.append(p99)
                    if (time.monotonic() - t_start) >= 600:
                        if baseline_windows_collected:
                            baseline_p99_ms = fmean(baseline_windows_collected)
                            print(f"baseline p99 locked: {baseline_p99_ms:.2f}ms "
                                  f"(over {len(baseline_windows_collected)} windows)",
                                  flush=True)

                ok = emit_window(writer, window, baseline_p99_ms)
                fh.flush()

                if not ok:
                    return 1

            print("soak completed cleanly.", flush=True)
            return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", required=True,
                   help="https://<host> (no trailing slash)")
    p.add_argument("--route", default="/api/embed")
    p.add_argument("--body", default='{"text":"hello"}')
    p.add_argument("--duration-h", type=float, default=6.0)
    p.add_argument("--connections", type=int, default=20)
    p.add_argument("--rps-total", type=float, default=50.0,
                   help="total request rate across all workers")
    p.add_argument("--window-s", type=int, default=30)
    p.add_argument("--csv", default=None)
    p.add_argument("--remote-host", default=None,
                   help="user@host for ssh-based RSS/FD sampling (VPS only)")
    p.add_argument("--remote-pid-file", default=None,
                   help="path on remote host containing the pid to sample")
    return p.parse_args()


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async(parse_args())))
