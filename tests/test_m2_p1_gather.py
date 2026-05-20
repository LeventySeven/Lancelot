"""M2-P1 — bounded fan-out helper. Replaces 8-15 LOC of `Semaphore +
asyncio.gather(*, return_exceptions=True)` boilerplate that recurs ≥5x
across real products (AEMON, Tavily, Artificial Societies, MiroFish,
Perplexity Replica)."""
import asyncio
import time

import pytest


def test_gather_bounded_concurrency():
    import lancelot

    inflight = 0
    peak = 0

    async def worker(i):
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        await asyncio.sleep(0.02)
        inflight -= 1
        return i

    async def main():
        coros = [worker(i) for i in range(20)]
        results = await lancelot.gather(*coros, concurrency=4)
        return results

    results = asyncio.run(main())
    assert sorted(results) == list(range(20))
    assert peak <= 4, f"unbounded inflight: peak={peak}"


def test_gather_return_exceptions_partial_failure():
    """A single failure does NOT cancel the rest (return_exceptions=True
    semantics by default, like Tavily + AEMON's hand-roll)."""
    import lancelot

    async def ok(i):
        await asyncio.sleep(0.01)
        return i

    async def boom():
        raise RuntimeError("nope")

    async def main():
        return await lancelot.gather(ok(1), boom(), ok(2), concurrency=2)

    results = asyncio.run(main())
    assert results[0] == 1 and results[2] == 2
    assert isinstance(results[1], RuntimeError)


def test_gather_concurrency_default_unbounded():
    """concurrency=None (default) => no bounded gate, behaves like
    asyncio.gather (still return_exceptions=True)."""
    import lancelot

    async def main():
        return await lancelot.gather(*(asyncio.sleep(0, i)
                                       for i in range(5)))

    results = asyncio.run(main())
    assert results == list(range(5))
