"""Tier-C.C5 (marquee) regression guards — `@app.batch` prefix-aware
request grouping for AI workloads.

Design note (worth remembering): each Starlette TestClient owns ONE
asyncio portal-loop. Coalescing only works among requests that land on
the SAME loop, so every test in this file uses ONE shared TestClient
and dispatches concurrent requests via threads against it (the portal
serializes them onto its single loop, where they share the App's batch
registry, lock, and futures).

What we pin:
  - Multiple concurrent requests with the same key are coalesced into ONE
    handler call carrying a `list[Payload]`.
  - `max_batch` triggers an immediate flush before the window expires.
  - Two keys → two independent groups (no cross-contamination).
  - Handler exceptions fan out to every waiter in that batch (no half-success).
  - Wrong-length return list → RuntimeError on every waiter.
  - Signature gate at registration time."""
import asyncio
import threading
import time

import pytest
from pydantic import BaseModel

import lancelot
from lancelot.testclient import TestClient


class _Q(BaseModel):
    text: str
    group: str = "default"


def _fire_concurrent(client, body_for_idx, n: int):
    """Fire `n` POSTs concurrently against ONE TestClient. Returns the
    list of responses in the dispatch order."""
    results: list = [None] * n
    barrier = threading.Barrier(n)

    def hit(i):
        barrier.wait()  # release all threads at the same moment
        results[i] = client.post("/b", json=body_for_idx(i))

    threads = [threading.Thread(target=hit, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


# ---------- happy path: window-based coalescing ------------------------

def test_c5_window_coalesces_concurrent_requests_into_one_call():
    """Three concurrent POSTs that land in the same batch_window MUST be
    coalesced into ONE handler call carrying a 3-item list."""
    calls: list[list] = []
    app = lancelot.App()

    @app.batch("/b", batch_window_ms=120, max_batch=100)
    async def b(items: list[_Q]) -> list[dict]:
        calls.append(items)
        return [{"echo": it.text} for it in items]

    with TestClient(app) as c:
        results = _fire_concurrent(c, lambda i: {"text": f"x{i}"}, 3)

    assert all(r.status_code == 200 for r in results)
    bodies = [r.json() for r in results]
    # Every request got its OWN echo back, indexed correctly.
    assert sorted(b["echo"] for b in bodies) == ["x0", "x1", "x2"]
    # Coalescing happened: fewer handler calls than requests.
    total = sum(len(c) for c in calls)
    assert total == 3
    assert len(calls) < 3, f"no coalescing: {len(calls)} calls for 3 requests"


def test_c5_awaiting_handler_on_timer_flush_succeeds():
    """REGRESSION (effectiveness bench finding): a batch handler that AWAITS
    (the REAL use case — one upstream LLM call) and is flushed by the TIMER
    WINDOW (not max_batch) must succeed. The timer-triggered `_flush` ran inside
    the timer task and cancelled `self.timer_task` — i.e. itself — so the
    cancellation propagated into the awaiting handler and fanned out to every
    waiter as CancelledError (500). Handlers with no `await` (like the other
    tests here) never hit it, which is why it shipped. Pin the awaiting path."""
    app = lancelot.App()

    @app.batch("/b", batch_window_ms=40, max_batch=100)
    async def b(items: list[_Q]) -> list[dict]:
        await asyncio.sleep(0.01)  # a real upstream await — the whole point
        return [{"echo": it.text} for it in items]

    with TestClient(app) as c:
        r = c.post("/b", json={"text": "solo"})

    assert r.status_code == 200, r.text
    assert r.json() == {"echo": "solo"}


# ---------- max_batch immediate flush ----------------------------------

def test_c5_max_batch_flushes_immediately_without_waiting_window():
    """With `max_batch=2` and a long (10s) window, two concurrent
    requests MUST flush immediately when the second arrives — total
    wall time well under the window."""
    app = lancelot.App()

    @app.batch("/b", batch_window_ms=10_000, max_batch=2)
    async def b(items: list[_Q]) -> list[dict]:
        return [{"text": it.text} for it in items]

    with TestClient(app) as c:
        t0 = time.perf_counter()
        results = _fire_concurrent(c, lambda i: {"text": f"x{i}"}, 2)
        dt = time.perf_counter() - t0
    assert all(r.status_code == 200 for r in results)
    assert dt < 2.0, f"max_batch did not short-circuit window: {dt:.2f}s"


# ---------- per-key isolation ------------------------------------------

def test_c5_key_partitions_into_independent_groups():
    """Different `key(item)` values → DIFFERENT batches (no cross-key
    pollution). Pin by asserting every emitted batch is single-group."""
    calls: list[list] = []
    app = lancelot.App()

    @app.batch("/b", batch_window_ms=120, max_batch=100,
               key=lambda it: it.group)
    async def b(items: list[_Q]) -> list[dict]:
        calls.append(items)
        return [{"g": it.group, "text": it.text} for it in items]

    def body(i):
        return {"text": f"x{i}", "group": "alpha" if i < 2 else "beta"}

    with TestClient(app) as c:
        results = _fire_concurrent(c, body, 4)

    assert all(r.status_code == 200 for r in results)
    for cl in calls:
        groups = {it.group for it in cl}
        assert len(groups) == 1, f"mixed groups in one batch: {groups}"


# ---------- exception fanout -------------------------------------------

def test_c5_handler_exception_fans_out_to_every_waiter():
    """Handler raise → every waiter in that batch sees 500."""
    app = lancelot.App()

    @app.batch("/b", batch_window_ms=80, max_batch=100)
    async def b(items: list[_Q]) -> list[dict]:
        raise RuntimeError("upstream blew up")

    with TestClient(app) as c:
        results = _fire_concurrent(c, lambda i: {"text": f"x{i}"}, 2)

    assert [r.status_code for r in results] == [500, 500]


# ---------- wrong-length return ----------------------------------------

def test_c5_wrong_length_return_raises_for_every_waiter():
    """Handler returns the wrong-length list → every waiter sees 500
    (RuntimeError surfaced through the normal exception path)."""
    app = lancelot.App()

    @app.batch("/b", batch_window_ms=80, max_batch=100)
    async def b(items: list[_Q]) -> list[dict]:
        return [{"only": "one"}]

    with TestClient(app) as c:
        results = _fire_concurrent(c, lambda i: {"text": f"x{i}"}, 2)

    assert [r.status_code for r in results] == [500, 500]


# ---------- registration-time signature gate --------------------------

def test_c5_rejects_wrong_signature_at_registration_time():
    """A non-list[T] signature is a registration-time TypeError so the
    bug surfaces immediately, not at first request."""
    app = lancelot.App()

    with pytest.raises(TypeError):
        @app.batch("/b1")
        async def b1(item: _Q) -> dict:
            return {}

    with pytest.raises(TypeError):
        @app.batch("/b2")
        async def b2():
            return []


# ---------- security: bounded key cardinality -------------------------

def test_c5_max_keys_caps_registry_growth():
    """SECURITY: a client sending many unique keys MUST NOT be able to
    inflate the registry indefinitely. After `max_keys` distinct keys,
    new keys are rejected with 500 (clean failure, not slow OOM)."""
    app = lancelot.App()

    @app.batch("/b", batch_window_ms=10, max_batch=1, max_keys=3,
               key=lambda it: it.group)
    async def b(items: list[_Q]) -> list[dict]:
        return [{"g": it.group} for it in items]

    with TestClient(app) as c:
        # First 3 distinct keys: fine.
        assert c.post("/b", json={"text": "x", "group": "g1"}).status_code == 200
        assert c.post("/b", json={"text": "x", "group": "g2"}).status_code == 200
        assert c.post("/b", json={"text": "x", "group": "g3"}).status_code == 200
        # 4th distinct key: cap-blown, 500.
        assert c.post("/b", json={"text": "x", "group": "g4"}).status_code == 500
        # But a key we've already seen still works (uses existing group).
        assert c.post("/b", json={"text": "x", "group": "g1"}).status_code == 200


# ---------- original fn stays directly callable -----------------------

def test_c5_returns_the_original_function():
    """`@app.batch` returns `fn` unchanged so users can also invoke it
    directly (testing, programmatic use)."""
    app = lancelot.App()

    async def go(items: list[_Q]) -> list[dict]:
        return [{"echoed": it.text} for it in items]

    decorated = app.batch("/b", batch_window_ms=10)(go)
    assert decorated is go
    out = asyncio.run(go([_Q(text="a"), _Q(text="b")]))
    assert out == [{"echoed": "a"}, {"echoed": "b"}]
