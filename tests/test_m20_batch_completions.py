"""M20 Batch 3 — @app.batch completion bundle (byte-path mine FINDINGS T1.x).

T1.4 `request_timeout_ms`: an item that ages past the deadline waiting for a
batch slot is dropped BEFORE the handler runs and its waiter gets a 504
(BatchItemTimeout), never a silent skip (a skipped-but-unresolved future hangs
the client forever — the M4 failure class). Disabled by default (0).

Tests cover both the deterministic unit path (direct `_run_batch` with pre-aged
timestamps) and the full HTTP path incl. the awaiting handler + 504 mapping.
"""
import asyncio
import time

import pytest
from pydantic import BaseModel

import lancelot
from lancelot.batch import BatchItemTimeout, _BatchGroup
from lancelot.testclient import TestClient


class _Q(BaseModel):
    x: int


# ---------- T1.4 unit: deterministic drop-stale (no real waiting) ----------

def test_t14_pre_aged_item_times_out_and_handler_not_called():
    async def go():
        called: list = []

        async def handler(items):
            called.append(list(items))
            return [f"r:{i}" for i in items]

        grp = _BatchGroup(handler, window_s=0.05, max_batch=10,
                          request_timeout_s=0.02)
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        old = time.monotonic() - 1.0  # 1s old, far past the 20ms deadline
        await grp._run_batch([("x", [fut], old)])  # entry = (payload, [waiters], enq)

        assert fut.done()
        with pytest.raises(BatchItemTimeout):
            fut.result()
        assert called == []  # dead item dropped pre-call — no wasted upstream call

    asyncio.run(go())


def test_t14_mixed_stale_and_fresh_only_fresh_reaches_handler():
    async def go():
        called: list = []

        async def handler(items):
            called.append(list(items))
            return [f"r:{i}" for i in items]

        grp = _BatchGroup(handler, window_s=0.05, max_batch=10,
                          request_timeout_s=0.02)
        loop = asyncio.get_running_loop()
        now = time.monotonic()
        stale = loop.create_future()
        fresh = loop.create_future()
        await grp._run_batch([("old", [stale], now - 1.0), ("new", [fresh], now)])

        with pytest.raises(BatchItemTimeout):
            stale.result()
        assert fresh.result() == "r:new"        # fresh waiter still served
        assert called == [["new"]]              # handler saw ONLY the fresh payload

    asyncio.run(go())


def test_t14_fresh_item_with_awaiting_handler_succeeds():
    async def go():
        async def handler(items):
            await asyncio.sleep(0.01)           # a real upstream await
            return [s.upper() for s in items]

        grp = _BatchGroup(handler, window_s=0.05, max_batch=10,
                          request_timeout_s=10.0)
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        await grp._run_batch([("hi", [fut], time.monotonic())])
        assert fut.result() == "HI"

    asyncio.run(go())


def test_t14_disabled_by_default_never_drops():
    async def go():
        async def handler(items):
            return [i for i in items]

        grp = _BatchGroup(handler, window_s=0.05, max_batch=10)  # no timeout
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        await grp._run_batch([("x", [fut], time.monotonic() - 1000.0)])  # ancient
        assert fut.result() == "x"  # served anyway — deadline disabled

    asyncio.run(go())


# ---------- T1.4 integration: full HTTP path + 504 mapping ----------

def test_t14_route_returns_504_when_item_ages_out():
    app = lancelot.App()

    # window (120ms) >> deadline (5ms): a lone item always ages out in the
    # window before the timer flush -> handler never runs -> 504.
    @app.batch("/bt", batch_window_ms=120, request_timeout_ms=5)
    async def b(items: list[_Q]) -> list[int]:
        return [it.x for it in items]

    with TestClient(app) as c:
        r = c.post("/bt", json={"x": 1})
    assert r.status_code == 504, r.text
    assert r.json()["detail"] == "Gateway Timeout"


def test_t14_route_default_serves_normally():
    app = lancelot.App()

    @app.batch("/bn", batch_window_ms=20)  # no request_timeout_ms
    async def b(items: list[_Q]) -> list[int]:
        await asyncio.sleep(0.005)          # awaiting handler, still fine
        return [it.x for it in items]

    with TestClient(app) as c:
        r = c.post("/bn", json={"x": 7})
    assert r.status_code == 200, r.text
    assert r.json() == 7


def test_t14_route_generous_deadline_serves_normally():
    app = lancelot.App()

    @app.batch("/bg", batch_window_ms=20, request_timeout_ms=10_000)
    async def b(items: list[_Q]) -> list[int]:
        return [it.x for it in items]

    with TestClient(app) as c:
        r = c.post("/bg", json={"x": 3})
    assert r.status_code == 200, r.text
    assert r.json() == 3


# ---------- T1.1 dedup: identical-key collapse -> one upstream call ----------

def test_t11_dedup_collapses_identical_keys_into_one_call():
    async def go():
        calls: list = []

        async def handler(items):
            calls.append(list(items))
            await asyncio.sleep(0)          # awaiting handler (the real shape)
            return [f"r:{p}" for p in items]

        grp = _BatchGroup(handler, window_s=0.05, max_batch=100,
                          dedup_key=lambda p: p)
        # 3 identical + 1 distinct, all in the same window.
        results = await asyncio.gather(
            grp.submit("a"), grp.submit("a"), grp.submit("a"), grp.submit("b"))

        assert results == ["r:a", "r:a", "r:a", "r:b"]   # every waiter served
        assert len(calls) == 1                           # ONE upstream call...
        assert sorted(calls[0]) == ["a", "b"]            # ...with only unique payloads

    asyncio.run(go())


def test_t11_dedup_off_by_default_keeps_duplicates():
    async def go():
        calls: list = []

        async def handler(items):
            calls.append(list(items))
            return list(items)

        grp = _BatchGroup(handler, window_s=0.05, max_batch=100)  # no dedup_key
        results = await asyncio.gather(
            grp.submit("a"), grp.submit("a"), grp.submit("a"))

        assert results == ["a", "a", "a"]
        assert calls == [["a", "a", "a"]]   # opt-in OFF: duplicates preserved

    asyncio.run(go())


def test_t11_dedup_exception_fans_to_every_waiter():
    async def go():
        async def handler(items):
            raise ValueError("boom")

        grp = _BatchGroup(handler, window_s=0.05, max_batch=100,
                          dedup_key=lambda p: p)
        rs = await asyncio.gather(
            grp.submit("a"), grp.submit("a"), grp.submit("b"),
            return_exceptions=True)
        assert all(isinstance(r, ValueError) for r in rs)  # no half-success, no hang

    asyncio.run(go())


def test_t11_dedup_over_http_serves_all_identical_waiters():
    import threading

    app = lancelot.App()
    seen: list = []

    @app.batch("/dd", batch_window_ms=120, max_batch=100,
               dedup_key=lambda it: it.x)
    async def b(items: list[_Q]) -> list[int]:
        seen.append(len(items))
        return [it.x for it in items]

    def fire(client, bodies):
        out = [None] * len(bodies)
        barrier = threading.Barrier(len(bodies))

        def hit(i):
            barrier.wait()
            out[i] = client.post("/dd", json=bodies[i])

        ts = [threading.Thread(target=hit, args=(i,)) for i in range(len(bodies))]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        return out

    with TestClient(app) as c:
        results = fire(c, [{"x": 5}, {"x": 5}, {"x": 5}])

    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    assert [r.json() for r in results] == [5, 5, 5]     # all 3 waiters served
    assert seen and max(seen) == 1                       # handler saw 1 unique payload
