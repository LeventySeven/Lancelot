"""Streaming TTFT (time-to-first-token) collector for T1.4.

Opens N concurrent HTTP/1.1 connections to a /stream endpoint and records,
for each connection, the wall-clock time from "request fully sent" to "first
body byte received". Outputs a distribution (p50/p95/p99) — the AI-streaming
UX metric per researchfms/core/.

Why raw sockets and not httpx/aiohttp: we want TTFB measured at the precise
moment the first body byte is read, with no library-side header-buffering
heuristic in the way. Raw asyncio.open_connection + a tiny http/1.1 head
parser gets that.

Usage:
    python ttft_collector.py <host> <port> --path /stream --connections 64 \\
        --requests-per-conn 1 --json-out /tmp/ttft.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Sample:
    ttft_ms: float
    bytes_read: int
    error: Optional[str] = None


@dataclass
class Result:
    samples: list[Sample] = field(default_factory=list)
    n_failed: int = 0


async def read_until_double_crlf(reader: asyncio.StreamReader,
                                 max_header_bytes: int = 16 * 1024) -> tuple[bytes, bytes]:
    """Read the response head up to and including the \\r\\n\\r\\n separator.
    Returns (head_bytes, leftover_after_separator)."""
    buf = bytearray()
    while True:
        if len(buf) > max_header_bytes:
            raise RuntimeError("header too large")
        try:
            chunk = await reader.read(4096)
        except Exception as e:
            raise RuntimeError(f"read header: {e!r}")
        if not chunk:
            raise RuntimeError("connection closed before headers complete")
        buf.extend(chunk)
        idx = buf.find(b"\r\n\r\n")
        if idx >= 0:
            return bytes(buf[: idx + 4]), bytes(buf[idx + 4:])


async def one_stream(host: str, port: int, path: str, deadline_s: float) -> Sample:
    """Open one connection, send a GET to path, return TTFT_ms = time from
    request-fully-sent to first BODY byte received."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=deadline_s
        )
    except Exception as e:
        return Sample(ttft_ms=float("nan"), bytes_read=0, error=f"connect: {e!r}")

    req = (
        b"GET %s HTTP/1.1\r\n"
        b"Host: %s:%d\r\n"
        b"Accept: text/event-stream\r\n"
        b"Connection: close\r\n\r\n"
    ) % (path.encode(), host.encode(), port)

    try:
        writer.write(req)
        await writer.drain()
        # The clock for TTFT starts the instant the request bytes are flushed.
        t_sent = time.perf_counter()
        head, leftover = await asyncio.wait_for(
            read_until_double_crlf(reader), timeout=deadline_s
        )
        # If the head read returned with leftover, the first body byte is
        # already in `leftover` — TTFT is the head-read time itself.
        if leftover:
            ttft = time.perf_counter() - t_sent
            return Sample(ttft_ms=ttft * 1000.0, bytes_read=len(leftover))
        # Otherwise read once more to get the first body byte.
        first = await asyncio.wait_for(reader.read(1), timeout=deadline_s)
        ttft = time.perf_counter() - t_sent
        return Sample(ttft_ms=ttft * 1000.0, bytes_read=1 if first else 0)
    except Exception as e:
        return Sample(ttft_ms=float("nan"), bytes_read=0, error=f"stream: {e!r}")
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


def percentile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return float("nan")
    idx = min(int(q * len(sorted_values)), len(sorted_values) - 1)
    return sorted_values[idx]


async def main_async(args: argparse.Namespace) -> int:
    deadline_s = max(args.deadline_s, 1.0)

    sem = asyncio.Semaphore(args.connections)
    result = Result()

    async def worker() -> None:
        async with sem:
            s = await one_stream(args.host, args.port, args.path, deadline_s)
            if s.error:
                result.n_failed += 1
            else:
                result.samples.append(s)

    tasks = [asyncio.create_task(worker()) for _ in range(args.total_requests)]
    await asyncio.gather(*tasks, return_exceptions=True)

    n_ok = len(result.samples)
    ttfts = sorted(s.ttft_ms for s in result.samples)
    p50 = percentile(ttfts, 0.50)
    p90 = percentile(ttfts, 0.90)
    p95 = percentile(ttfts, 0.95)
    p99 = percentile(ttfts, 0.99)
    avg = statistics.fmean(ttfts) if ttfts else float("nan")

    print(
        f"TTFT collector done: total={args.total_requests} ok={n_ok} "
        f"failed={result.n_failed} | "
        f"avg={avg:.2f}ms p50={p50:.2f} p90={p90:.2f} p95={p95:.2f} p99={p99:.2f} (ms)"
    )

    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(
                {
                    "target": f"{args.host}:{args.port}{args.path}",
                    "connections": args.connections,
                    "total_requests": args.total_requests,
                    "n_ok": n_ok,
                    "n_failed": result.n_failed,
                    "ttft_ms_avg": avg,
                    "ttft_ms_p50": p50,
                    "ttft_ms_p90": p90,
                    "ttft_ms_p95": p95,
                    "ttft_ms_p99": p99,
                    "all_ttft_ms": ttfts,
                },
                f,
                indent=2,
            )
        print(f"json written to {args.json_out}")
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("host")
    p.add_argument("port", type=int)
    p.add_argument("--path", default="/stream")
    p.add_argument("--connections", type=int, default=64,
                   help="max concurrent connections")
    p.add_argument("--total-requests", type=int, default=200,
                   help="total streams to open (sequential within concurrency cap)")
    p.add_argument("--deadline-s", type=float, default=10.0,
                   help="per-request deadline in seconds")
    p.add_argument("--json-out", default=None)
    return p.parse_args()


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async(parse_args())))
