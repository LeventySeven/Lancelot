"""M6 T2.5 — periodic-burst client for the chaos cohort.

Generates traffic bursts on top of the existing benign+slow-loris cohort:
every BURST_PERIOD_S seconds, spike concurrency to BURST_C connections for
BURST_DURATION_S seconds, then idle. Repeats until total --duration-s
elapses.

The point: real adversarial load isn't steady. A constant slow-loris
attack is easy; bursts + jitter expose latency hotspots a flat attack
doesn't. Lancelot's response p99 should stay bounded across the burst
window — not just on average.

Usage:
    python burst_client.py <host> <port> --path /echo \\
        --burst-c 500 --burst-period-s 60 --burst-duration-s 5 \\
        --duration-s 120 --json-out /tmp/burst.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass, field


@dataclass
class BurstSummary:
    burst_idx: int
    n_total: int = 0
    n_ok: int = 0
    n_err: int = 0
    latencies_ms: list[float] = field(default_factory=list)

    def p(self, q: float) -> float:
        if not self.latencies_ms:
            return float("nan")
        s = sorted(self.latencies_ms)
        return s[min(int(q * len(s)), len(s) - 1)]


async def burst_worker(host: str, port: int, path: str, deadline: float,
                       stats: BurstSummary) -> None:
    try:
        import httpx
    except ImportError:
        sys.stderr.write("httpx required: pip install httpx\n")
        return

    async with httpx.AsyncClient(timeout=10.0) as client:
        url = f"http://{host}:{port}{path}"
        while time.monotonic() < deadline:
            t0 = time.perf_counter()
            try:
                resp = await client.get(url)
                dt_ms = (time.perf_counter() - t0) * 1000.0
                if 200 <= resp.status_code < 400:
                    stats.n_ok += 1
                    stats.latencies_ms.append(dt_ms)
                else:
                    stats.n_err += 1
            except Exception:
                stats.n_err += 1
            stats.n_total += 1


async def run_burst(args: argparse.Namespace, burst_idx: int) -> BurstSummary:
    """One burst: BURST_C workers for BURST_DURATION_S seconds."""
    stats = BurstSummary(burst_idx=burst_idx)
    deadline = time.monotonic() + args.burst_duration_s

    tasks = [asyncio.create_task(
        burst_worker(args.host, args.port, args.path, deadline, stats))
        for _ in range(args.burst_c)]
    await asyncio.gather(*tasks, return_exceptions=True)
    return stats


async def main_async(args: argparse.Namespace) -> int:
    print(f"burst client start: host={args.host}:{args.port} path={args.path} "
          f"burst_c={args.burst_c} burst_period_s={args.burst_period_s} "
          f"burst_duration_s={args.burst_duration_s} "
          f"total_duration_s={args.duration_s}", flush=True)

    bursts: list[BurstSummary] = []
    t_start = time.monotonic()
    burst_idx = 0
    next_burst_at = t_start  # first burst immediate

    while time.monotonic() - t_start < args.duration_s:
        sleep_for = next_burst_at - time.monotonic()
        if sleep_for > 0:
            await asyncio.sleep(min(sleep_for, 1.0))
            continue
        burst_idx += 1
        print(f"burst #{burst_idx} starting @ t={time.monotonic() - t_start:.1f}s "
              f"(c={args.burst_c}, dur={args.burst_duration_s}s)", flush=True)
        b = await run_burst(args, burst_idx)
        bursts.append(b)
        print(f"burst #{burst_idx} done: n_ok={b.n_ok} n_err={b.n_err} "
              f"p50={b.p(0.50):.2f}ms p99={b.p(0.99):.2f}ms", flush=True)
        next_burst_at += args.burst_period_s

    # Aggregate.
    all_lats = [x for b in bursts for x in b.latencies_ms]
    if all_lats:
        all_lats.sort()
        agg_p50 = all_lats[len(all_lats) // 2]
        agg_p99 = all_lats[min(int(len(all_lats) * 0.99), len(all_lats) - 1)]
        agg_mean = statistics.fmean(all_lats)
    else:
        agg_p50 = agg_p99 = agg_mean = float("nan")

    n_ok = sum(b.n_ok for b in bursts)
    n_err = sum(b.n_err for b in bursts)
    print(f"\nburst total: bursts={len(bursts)} n_ok={n_ok} n_err={n_err} "
          f"avg={agg_mean:.2f}ms p50={agg_p50:.2f} p99={agg_p99:.2f}",
          flush=True)

    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump({
                "host": args.host, "port": args.port, "path": args.path,
                "burst_c": args.burst_c,
                "burst_period_s": args.burst_period_s,
                "burst_duration_s": args.burst_duration_s,
                "duration_s": args.duration_s,
                "n_bursts": len(bursts),
                "n_ok": n_ok, "n_err": n_err,
                "agg_p50_ms": agg_p50, "agg_p99_ms": agg_p99,
                "agg_mean_ms": agg_mean,
                "per_burst": [
                    {
                        "idx": b.burst_idx, "n_total": b.n_total,
                        "n_ok": b.n_ok, "n_err": b.n_err,
                        "p50_ms": b.p(0.50), "p99_ms": b.p(0.99),
                    } for b in bursts
                ],
            }, f, indent=2)
        print(f"json written: {args.json_out}", flush=True)
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("host")
    p.add_argument("port", type=int)
    p.add_argument("--path", default="/echo")
    p.add_argument("--burst-c", type=int, default=500,
                   help="concurrent connections during each burst")
    p.add_argument("--burst-period-s", type=float, default=60.0,
                   help="seconds between burst starts")
    p.add_argument("--burst-duration-s", type=float, default=5.0,
                   help="length of each burst")
    p.add_argument("--duration-s", type=float, default=120.0,
                   help="total run duration")
    p.add_argument("--json-out", default=None)
    return p.parse_args()


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async(parse_args())))
