"""M20 Batch C — @app.batch pipeline concurrency (byte-path mine FINDINGS
T1.3 + T1.5 + T3.4).

T1.3 `max_concurrent_batches=N`: bound how many batches may run their handler
(the upstream LLM call) AT ONCE per registry, with a SHARED asyncio.Semaphore.
Set N>=2 to PIPELINE — while batch k is upstream, the next window's batch k+1
accumulates and, on flush, runs concurrently (up to N). Default 0 = UNBOUNDED
= exactly the pre-T1.3 behavior (no semaphore, no counter, no shed).

T1.5 compute-overload shed: when the N slots are full (pipeline SATURATED) a
NEW arrival sheds with BatchOverloadError -> 503 + Retry-After. Only the new
arrival sheds; items already in a pending/in-flight batch keep their slot and
complete. Default (cap=0) never sheds on compute.

T3.4 name-the-knob: every batch shed carries `X-Lancelot-Shed-Reason` —
`batch-overload` (compute cap, 503), `batch-keys` (max_keys cap, 503),
`batch-timeout` (per-item deadline, 504).

THE M4 LESSON lives here: the semaphore is held ACROSS `await handler(...)`. A
bug there deadlocks or poisons waiters — so the pipelining + shed assertions
exercise REAL awaiting handlers (gated on an asyncio.Event, or asyncio.sleep),
never a synchronous return that would never touch the held-across-await path.
The HTTP tests drive concurrent requests onto the TestClient's single portal
loop (the only place coalescing happens) to verify the 503 + header wiring."""
import asyncio
import threading
import time

import pytest
from pydantic import BaseModel

import lancelot
from lancelot.batch import BatchOverloadError, _BatchRegistry
from lancelot.testclient import TestClient


class _Q(BaseModel):
    x: int


# ====================================================================
# T1.3 — pipelining: batch k+1 runs concurrently with batch k (N>=2)
# ====================================================================

def test_t13_two_batches_run_concurrently_under_cap():
    """With max_concurrent_batches=2 and max_batch=1 (each submit is its own
    batch), two awaiting handlers must be IN-FLIGHT at the same time — proof
    the slot is held across the await and the second batch is not serialized
    behind the first."""
    async def go():
        gate = asyncio.Event()      # released only after both handlers entered
        started = 0
        peak = 0

        async def handler(items):
            nonlocal started, peak
            started += 1
            peak = max(peak, started)
            await gate.wait()       # hold the slot across a real await
            started -= 1
            return list(items)

        reg = _BatchRegistry(handler, window_s=0.01, max_batch=1, key_fn=None,
                             max_concurrent_batches=2)
        t1 = asyncio.create_task(reg.submit("a"))
        t2 = asyncio.create_task(reg.submit("b"))
        # Let both flush + enter the handler (max_batch=1 flushes immediately).
        for _ in range(50):
            await asyncio.sleep(0)
            if peak >= 2:
                break
        assert peak == 2, "both batches must run concurrently under N=2"
        gate.set()
        # submit() returns the per-waiter scalar (results[i]), not the list.
        assert await t1 == "a"
        assert await t2 == "b"

    asyncio.run(go())


def test_t13_cap_bounds_concurrent_runs_overflow_sheds():
    """With max_concurrent_batches=2 (and max_batch=1 so each submit flushes
    its own batch), at most 2 handlers ever run at once — the shared registry
    semaphore + counter cap concurrency to N=2. The 3rd/4th arrivals, finding
    both slots taken, shed (T1.5) rather than pile up; the 2 in-flight batches
    complete cleanly. (With the submit-time shed in place, the cap is enforced
    by shedding new arrivals, not by queueing them.)"""
    async def go():
        release = asyncio.Event()
        running = 0
        peak = 0

        async def handler(items):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await release.wait()
            running -= 1
            return list(items)

        reg = _BatchRegistry(handler, window_s=0.01, max_batch=1, key_fn=None,
                             max_concurrent_batches=2)
        # 4 distinct keys -> 4 independent groups, all sharing ONE semaphore.
        tasks = [asyncio.ensure_future(reg.submit(f"k{i}")) for i in range(4)]
        for _ in range(50):
            await asyncio.sleep(0)
            if peak >= 2:
                break
        await asyncio.sleep(0)
        assert peak == 2, "the shared cap must bound concurrent runs to N=2"
        assert running <= 2
        release.set()
        out = await asyncio.gather(*tasks, return_exceptions=True)
        served = [r for r in out if not isinstance(r, BatchOverloadError)]
        shed = [r for r in out if isinstance(r, BatchOverloadError)]
        assert len(served) == 2, out          # exactly N ran
        assert len(shed) == 2, out            # the overflow pair shed
        assert all(e.shed_reason == "batch-overload" for e in shed)

    asyncio.run(go())


# ====================================================================
# T1.5 — overload shed: NEW arrival sheds while in-flight ones succeed
# ====================================================================

def test_t15_shed_new_arrival_when_saturated_inflight_succeed():
    """N=1: while the single slot is held by an awaiting batch, a NEW submit
    sheds with BatchOverloadError (shed_reason batch-overload). The in-flight
    batch is untouched and completes — only the overflow arrival is shed."""
    async def go():
        release = asyncio.Event()
        entered = asyncio.Event()

        async def handler(items):
            entered.set()
            await release.wait()
            return list(items)

        reg = _BatchRegistry(handler, window_s=0.01, max_batch=1, key_fn=None,
                             max_concurrent_batches=1)
        held = asyncio.create_task(reg.submit("inflight"))
        await entered.wait()        # the slot is now genuinely occupied

        with pytest.raises(BatchOverloadError) as ei:
            await reg.submit("overflow")
        assert ei.value.shed_reason == "batch-overload"
        assert ei.value.retry_after >= 1

        release.set()
        assert await held == "inflight"     # the in-flight waiter still served

    asyncio.run(go())


def test_t15_slot_frees_after_inflight_completes_then_serves():
    """After a saturated slot frees (the in-flight batch returns), a fresh
    submit is served normally again — the shed is transient back-pressure,
    not a permanently wedged pipeline (the in-flight counter decremented)."""
    async def go():
        release = asyncio.Event()
        entered = asyncio.Event()

        async def handler(items):
            entered.set()
            await release.wait()
            return [s.upper() for s in items]

        reg = _BatchRegistry(handler, window_s=0.01, max_batch=1, key_fn=None,
                             max_concurrent_batches=1)
        held = asyncio.create_task(reg.submit("a"))
        await entered.wait()
        with pytest.raises(BatchOverloadError):
            await reg.submit("b")           # saturated -> shed

        release.set()                       # release stays set for later batches
        assert await held == "A"            # slot frees here (scalar fanout)

        # Now the pipeline is idle; a fresh submit must be served (no shed) —
        # release is already set so the new handler returns straight away.
        assert await reg.submit("c") == "C"

    asyncio.run(go())


def test_t15_default_unbounded_never_sheds_on_compute():
    """max_concurrent_batches unset (0) => no semaphore, no in-flight counter,
    and NO compute shed even with many concurrent in-flight awaiting batches
    (byte-identical to pre-T1.3). All run at once; none shed."""
    async def go():
        release = asyncio.Event()
        running = 0
        peak = 0

        async def handler(items):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await release.wait()
            running -= 1
            return list(items)

        reg = _BatchRegistry(handler, window_s=0.01, max_batch=1, key_fn=None)
        # No cap: the registry holds no semaphore/counter at all.
        assert reg._sema is None and reg._inflight is None
        tasks = [asyncio.create_task(reg.submit(f"k{i}")) for i in range(6)]
        for _ in range(50):
            await asyncio.sleep(0)
            if peak >= 6:
                break
        assert peak == 6, "unbounded default must let every batch run at once"
        release.set()
        out = await asyncio.gather(*tasks, return_exceptions=True)
        assert all(not isinstance(r, BatchOverloadError) for r in out)

    asyncio.run(go())


# ====================================================================
# HTTP path — awaiting handler, 503 + X-Lancelot-Shed-Reason wiring
# ====================================================================

def _fire(client, path, bodies):
    """Fire len(bodies) POSTs concurrently against ONE TestClient (the portal
    loop serializes them onto its single asyncio loop, where they share the
    App's batch registry). Returns responses in dispatch order."""
    out = [None] * len(bodies)
    barrier = threading.Barrier(len(bodies))

    def hit(i):
        barrier.wait()
        out[i] = client.post(path, json=bodies[i])

    ts = [threading.Thread(target=hit, args=(i,)) for i in range(len(bodies))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return out


def test_t15_http_overflow_sheds_503_while_inflight_succeed():
    """End-to-end: N=1, max_batch=1, a slow (awaiting) handler. Many concurrent
    requests -> at least one is served 200 while the overflow gets a sheddable
    503 carrying X-Lancelot-Shed-Reason: batch-overload. Proves the held-across-
    await slot + the submit-time shed + the ASGI seam all wire together."""
    app = lancelot.App()

    @app.batch("/p", batch_window_ms=5, max_batch=1, max_concurrent_batches=1)
    async def b(items: list[_Q]) -> list[int]:
        await asyncio.sleep(0.10)           # hold the single slot across an await
        return [it.x for it in items]

    with TestClient(app) as c:
        results = _fire(c, "/p", [{"x": i} for i in range(8)])

    codes = [r.status_code for r in results]
    assert 200 in codes, codes                       # at least one in-flight served
    sheds = [r for r in results if r.status_code == 503]
    assert sheds, codes                              # overflow shed under saturation
    for r in sheds:
        assert r.headers.get("x-lancelot-shed-reason") == "batch-overload"
        assert r.headers.get("retry-after") == "1"
        assert r.json()["detail"] == "Service Unavailable"


def test_t15_http_unbounded_default_serves_all_concurrent():
    """Default (no max_concurrent_batches): the same concurrent burst against a
    slow handler is NEVER shed on compute — every request gets 200. Confirms
    the cap is strictly opt-in and the default path is unchanged."""
    app = lancelot.App()

    @app.batch("/u", batch_window_ms=5, max_batch=1)   # no cap
    async def b(items: list[_Q]) -> list[int]:
        await asyncio.sleep(0.05)
        return [it.x for it in items]

    with TestClient(app) as c:
        results = _fire(c, "/u", [{"x": i} for i in range(8)])

    assert all(r.status_code == 200 for r in results), \
        [(r.status_code, r.text) for r in results]


def test_t15_http_pipeline_n2_serves_concurrent_window_fill():
    """N=2 pipelining over HTTP: with max_batch large enough that the burst
    coalesces, a moderate concurrent load is served (200) — the second batch
    fills/runs while the first is upstream. Sanity that N>=2 is not over-shed."""
    app = lancelot.App()
    seen: list = []

    @app.batch("/n2", batch_window_ms=15, max_batch=4, max_concurrent_batches=2)
    async def b(items: list[_Q]) -> list[int]:
        seen.append(len(items))
        await asyncio.sleep(0.03)
        return [it.x for it in items]

    with TestClient(app) as c:
        results = _fire(c, "/n2", [{"x": i} for i in range(6)])

    # Every caller served (some batches coalesce; 2 may run concurrently).
    assert all(r.status_code == 200 for r in results), \
        [(r.status_code, r.text) for r in results]
    assert sorted(r.json() for r in results) == list(range(6))


# ====================================================================
# T3.4 — name-the-knob on the OTHER batch shed seams
# ====================================================================

def test_t34_max_keys_shed_carries_batch_keys_reason():
    """The max_keys cap shed (503) names `batch-keys` in X-Lancelot-Shed-Reason
    (distinct from the compute `batch-overload`)."""
    app = lancelot.App()

    # max_keys=1: the SECOND distinct key overflows the registry -> 503.
    @app.batch("/k", batch_window_ms=80, max_keys=1, key=lambda it: it.x)
    async def b(items: list[_Q]) -> list[int]:
        await asyncio.sleep(0.04)
        return [it.x for it in items]

    with TestClient(app) as c:
        # Two distinct keys concurrently; one occupies the only key slot, the
        # other overflows. Fire enough to guarantee an overflow lands.
        results = _fire(c, "/k", [{"x": 1}, {"x": 2}, {"x": 3}, {"x": 4}])

    sheds = [r for r in results if r.status_code == 503]
    assert sheds, [r.status_code for r in results]
    for r in sheds:
        assert r.headers.get("x-lancelot-shed-reason") == "batch-keys"


def test_t34_item_timeout_shed_carries_batch_timeout_reason():
    """The per-item deadline shed (504) names `batch-timeout`."""
    app = lancelot.App()

    # window (120ms) >> deadline (5ms): a lone item ages out before flush -> 504.
    @app.batch("/to", batch_window_ms=120, request_timeout_ms=5)
    async def b(items: list[_Q]) -> list[int]:
        return [it.x for it in items]

    with TestClient(app) as c:
        r = c.post("/to", json={"x": 1})

    assert r.status_code == 504, r.text
    assert r.headers.get("x-lancelot-shed-reason") == "batch-timeout"
    assert r.json()["detail"] == "Gateway Timeout"


# ====================================================================
# Edge: handler exception under the cap still releases the slot
# ====================================================================

def test_t13_handler_exception_releases_slot_no_wedge():
    """A handler that RAISES under the cap must still release its slot + free
    the in-flight count (the finally in _run_batch) — otherwise the pipeline
    wedges closed and every future arrival sheds forever (the M4 hang class,
    applied to the slot). After the raise, a fresh submit is served."""
    async def go():
        mode = {"boom": True}

        async def handler(items):
            await asyncio.sleep(0)
            if mode["boom"]:
                raise ValueError("upstream blew up")
            return [s.upper() for s in items]

        reg = _BatchRegistry(handler, window_s=0.01, max_batch=1, key_fn=None,
                             max_concurrent_batches=1)
        with pytest.raises(ValueError):
            await reg.submit("a")           # raises -> waiter sees it
        assert reg._inflight[0] == 0, "slot must be released after a raise"

        mode["boom"] = False
        assert await reg.submit("b") == "B"     # pipeline not wedged (scalar)

    asyncio.run(go())
