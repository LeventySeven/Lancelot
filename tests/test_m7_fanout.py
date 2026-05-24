"""M7 T1.2 — TransportPool.fanout() parallel-probe with first-N-of-M.

Chroma/Huber pattern (TRANSCRIPTS_YC_ROOT_ACCESS.md:14052–14207):
"an LLM generates a 'query plan' that creates 10–30 distinct search
probes simultaneously across structured and unstructured data sources".

Tests:
  - fires N parallel calls
  - resolves after first_n successes
  - cancels laggards (no orphan tasks)
  - deadline cap honored
  - bounded concurrency works
  - partial-failure shape (exceptions returned, not raised)
"""
import asyncio
import time

import httpx
import pytest

pytest.importorskip("httpx")

from lancelot.llm import TransportPool


def _make_pool(handler):
    return TransportPool(transport=httpx.MockTransport(handler))


# ----- T1.2.a fires all calls in parallel ---------------------------------

def test_t1_2_fanout_fires_in_parallel():
    """10 mock calls, each sleeping ~20ms. With concurrency=None (default
    unbounded), wall-clock should be ~20ms not ~200ms."""
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(time.perf_counter())
        await asyncio.sleep(0.02)
        return httpx.Response(200, json={"ok": True})

    pool = _make_pool(handler)

    async def run():
        coros = [pool.request("GET", f"https://up/{i}") for i in range(10)]
        t0 = time.perf_counter()
        results = await pool.fanout(coros)
        elapsed = time.perf_counter() - t0
        await pool.aclose()
        return elapsed, results

    elapsed, results = asyncio.run(run())
    assert len(results) == 10
    # Parallel: should be much faster than 10 × 20ms = 200ms.
    assert elapsed < 0.15, f"expected ~20-50ms, got {elapsed*1000:.0f}ms"


# ----- T1.2.b first_n cancels laggards ------------------------------------

def test_t1_2_fanout_first_n_cancels_laggards():
    """10 mock calls; 5 fast (10ms) + 5 slow (500ms). first_n=5 should
    resolve in ~10ms and CANCEL the 5 slow ones (they never log a
    complete time)."""
    completed = []
    started = []

    async def handler(request: httpx.Request) -> httpx.Response:
        started.append(request.url.path)
        # Fast for first 5 URLs, slow for last 5.
        i = int(request.url.path.lstrip("/"))
        delay = 0.005 if i < 5 else 0.5
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        completed.append(request.url.path)
        return httpx.Response(200, json={"i": i})

    pool = _make_pool(handler)

    async def run():
        coros = [pool.request("GET", f"https://up/{i}") for i in range(10)]
        t0 = time.perf_counter()
        results = await pool.fanout(coros, first_n=5, cancel_laggards=True)
        elapsed = time.perf_counter() - t0
        await pool.aclose()
        return elapsed, results

    elapsed, results = asyncio.run(run())
    assert len(results) >= 5  # at least 5 successes
    # Should be FAST (the 5 fast ones finished); not the 500ms slow path.
    assert elapsed < 0.2, f"expected <200ms, got {elapsed*1000:.0f}ms"
    # Most of the slow ones should NOT have completed (cancelled).
    assert len(completed) < 10, (
        f"expected some cancellation, all completed: {completed}")


# ----- T1.2.c deadline caps wall-clock ------------------------------------

def test_t1_2_fanout_deadline_cap():
    """deadline=0.05 with all slow calls — should bail at the deadline."""

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1.0)  # would block 1s
        return httpx.Response(200)

    pool = _make_pool(handler)

    async def run():
        coros = [pool.request("GET", f"https://up/{i}") for i in range(5)]
        t0 = time.perf_counter()
        results = await pool.fanout(coros, deadline=0.05)
        elapsed = time.perf_counter() - t0
        await pool.aclose()
        return elapsed, results

    elapsed, results = asyncio.run(run())
    assert elapsed < 0.2, f"deadline not honored; got {elapsed*1000:.0f}ms"
    # Most results should be either incomplete or cancelled exceptions.


# ----- T1.2.d concurrency cap throttles in-flight -------------------------

def test_t1_2_fanout_concurrency_throttles():
    """concurrency=2 with 6 calls of 20ms each — wall-clock ≈ 6/2 × 20ms = 60ms."""
    in_flight = [0]
    max_in_flight = [0]

    async def handler(request: httpx.Request) -> httpx.Response:
        in_flight[0] += 1
        if in_flight[0] > max_in_flight[0]:
            max_in_flight[0] = in_flight[0]
        try:
            await asyncio.sleep(0.02)
        finally:
            in_flight[0] -= 1
        return httpx.Response(200)

    pool = _make_pool(handler)

    async def run():
        coros = [pool.request("GET", f"https://up/{i}") for i in range(6)]
        await pool.fanout(coros, concurrency=2)
        await pool.aclose()

    asyncio.run(run())
    assert max_in_flight[0] <= 2, f"expected ≤2 concurrent, saw {max_in_flight[0]}"


# ----- T1.2.e partial-failure: exceptions are returned, not raised --------

def test_t1_2_fanout_returns_exceptions_partial_failure():
    """Some calls 500, some 200. Caller gets a mixed result list; no
    raise — like asyncio.gather(return_exceptions=True)."""

    async def handler(request: httpx.Request) -> httpx.Response:
        i = int(request.url.path.lstrip("/"))
        if i % 2 == 0:
            return httpx.Response(200, json={"i": i})
        else:
            raise RuntimeError(f"upstream-{i}")

    pool = _make_pool(handler)

    async def run():
        coros = [pool.request("GET", f"https://up/{i}") for i in range(6)]
        return await pool.fanout(coros)

    results = asyncio.run(run())
    successes = [r for r in results if not isinstance(r, Exception)]
    exceptions = [r for r in results if isinstance(r, Exception)]
    assert len(successes) == 3
    assert len(exceptions) == 3


# ----- T1.2.f empty input → empty output, no error ------------------------

def test_t1_2_fanout_empty_returns_empty():
    pool = _make_pool(lambda r: httpx.Response(200))

    async def run():
        out = await pool.fanout([])
        await pool.aclose()
        return out

    assert asyncio.run(run()) == []
