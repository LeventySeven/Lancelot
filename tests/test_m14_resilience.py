"""M14 — TransportPool resilience: per-stream idle timeout + outbound rate cap.
"""
import asyncio
import time

import pytest

from lancelot.llm import TransportPool, UpstreamIdleTimeout, _TokenBucket


class _FakeResp:
    """Minimal stand-in for an httpx streaming response: items is a list of
    (chunk_bytes_or_exception, delay_secs)."""
    def __init__(self, items):
        self._items = items

    async def aiter_bytes(self):
        for item, delay in self._items:
            if delay:
                await asyncio.sleep(delay)
            if isinstance(item, BaseException):
                raise item
            yield item


# ---------- idle timeout -------------------------------------------------

def _drain(pool, resp, idle):
    async def go():
        out = []
        async for c in pool.idle_guarded_chunks(resp, idle_secs=idle):
            out.append(c)
        return out
    return asyncio.run(go())


def test_idle_passthrough_when_off():
    pool = TransportPool()
    try:
        assert _drain(pool, _FakeResp([(b"a", 0), (b"b", 0)]), 0) == [b"a", b"b"]
    finally:
        asyncio.run(pool.aclose())


def test_idle_normal_stream_all_chunks():
    pool = TransportPool()
    try:
        items = [(b"x", 0.01) for _ in range(5)]
        assert _drain(pool, _FakeResp(items), 0.5) == [b"x"] * 5
    finally:
        asyncio.run(pool.aclose())


def test_idle_fires_on_stall():
    pool = TransportPool()
    try:
        # first chunk immediate, second stalls 0.3s > idle 0.05 → timeout
        with pytest.raises(UpstreamIdleTimeout):
            _drain(pool, _FakeResp([(b"a", 0), (b"b", 0.3)]), 0.05)
    finally:
        asyncio.run(pool.aclose())


def test_idle_timer_resets_between_chunks():
    pool = TransportPool()
    try:
        # each gap (0.04s) < idle (0.1s) → no timeout across many chunks
        items = [(b"c", 0.04) for _ in range(6)]
        assert _drain(pool, _FakeResp(items), 0.1) == [b"c"] * 6
    finally:
        asyncio.run(pool.aclose())


def test_idle_upstream_error_propagates():
    pool = TransportPool()
    try:
        with pytest.raises(RuntimeError, match="boom"):
            _drain(pool, _FakeResp([(b"a", 0), (RuntimeError("boom"), 0)]), 0.5)
    finally:
        asyncio.run(pool.aclose())


def test_idle_env_knob(monkeypatch):
    monkeypatch.setenv("LANCELOT_UPSTREAM_STREAM_IDLE_SECS", "60")
    pool = TransportPool()
    try:
        assert pool._stream_idle_secs == 60.0
    finally:
        asyncio.run(pool.aclose())


# ---------- outbound rate limit (token bucket) ---------------------------

def test_token_bucket_starts_full_then_throttles():
    b = _TokenBucket(10.0)

    async def go():
        t0 = time.monotonic()
        for _ in range(10):
            await b.acquire()        # bucket starts full → immediate
        first = time.monotonic() - t0
        t1 = time.monotonic()
        for _ in range(10):
            await b.acquire()        # must wait ~1s for refill at 10/s
        return first, time.monotonic() - t1

    first, second = asyncio.run(go())
    assert first < 0.2, first
    assert second >= 0.7, second


def test_token_bucket_rejects_nonpositive_rate():
    with pytest.raises(ValueError):
        _TokenBucket(0)


def test_rate_limit_default_off():
    pool = TransportPool()
    assert pool._bucket is None
    asyncio.run(pool.aclose())


def test_rate_limit_kwarg_and_env(monkeypatch):
    pool = TransportPool(rate_limit=5.0)
    assert pool._bucket is not None and pool._bucket._rate == 5.0
    asyncio.run(pool.aclose())
    monkeypatch.setenv("LANCELOT_UPSTREAM_RATE_LIMIT", "3")
    pool2 = TransportPool()
    assert pool2._bucket is not None and pool2._bucket._rate == 3.0
    asyncio.run(pool2.aclose())


def test_rate_limit_throttles_requests():
    import httpx

    def handler(req):
        return httpx.Response(200, json={})

    pool = TransportPool(transport=httpx.MockTransport(handler), rate_limit=5.0)

    async def go():
        t0 = time.monotonic()
        for _ in range(6):           # 6 calls at 5/s: ~1 wait after the burst
            await pool.request("GET", "https://api.x/y")
        return time.monotonic() - t0

    elapsed = asyncio.run(go())
    assert elapsed >= 0.15, elapsed   # at least one refill wait occurred
    asyncio.run(pool.aclose())
