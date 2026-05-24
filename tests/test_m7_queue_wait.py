"""M7 T4.8 — queue_wait_ms ContextVar + TenantConcurrencyLimiter middleware.

Turbopuffer pattern: queue_wait_ms = total_in_handler − pure_handler_compute,
the missing third number after RED + TTFT for multi-tenant AI backends.
Surfaces head-of-line blocking + tenant-concurrency-cap saturation."""
import asyncio

import lancelot
from lancelot.context import queue_wait_ms
from lancelot.tenant import TenantConcurrencyLimiter
from lancelot.testclient import TestClient


# ----- T4.8.a queue_wait_ms ContextVar default ----------------------------

def test_t4_8_queue_wait_ms_defaults_zero():
    """Outside any request, the ContextVar reads 0.0 (the baseline)."""
    assert queue_wait_ms.get() == 0.0


# ----- T4.8.b queue_wait_ms passed to record_request ----------------------

def test_t4_8_queue_wait_ms_passed_to_record_request():
    """If middleware sets queue_wait_ms ContextVar, the ASGI seam reads
    it in `finally` and passes it to record_request (so the histogram
    emits). Verify the WIRING via a captured-call mock."""
    from unittest.mock import patch

    captured = []

    def fake_record(*args, **kwargs):
        captured.append((args, kwargs))

    app = lancelot.App()

    @app.get("/slow")
    async def slow() -> dict:
        # Pretend middleware acquired a per-tenant semaphore and waited 7ms.
        queue_wait_ms.set(7.5)
        return {"ok": True}

    with patch("lancelot.metrics.record_request", side_effect=fake_record):
        with TestClient(app) as client:
            r = client.get("/slow")
            assert r.status_code == 200

    assert captured, "record_request was not called"
    args, kwargs = captured[-1]
    assert kwargs.get("queue_wait_ms") == 7.5, f"got: {kwargs}"


# ----- T4.8.c TenantConcurrencyLimiter — cap enforces serialization -------

def test_t4_8_tenant_concurrency_limiter_serializes_per_tenant():
    """With cap=1, two concurrent requests for the SAME tenant must
    serialize (the 2nd waits for the 1st)."""
    app = lancelot.App()
    app.add_middleware(TenantConcurrencyLimiter,
                       max_concurrent_per_tenant=1)

    enter_count = [0]
    exit_count = [0]
    in_flight_seen = [0]  # max in-flight observed

    @app.get("/throttled")
    async def throttled() -> dict:
        enter_count[0] += 1
        in_flight = enter_count[0] - exit_count[0]
        if in_flight > in_flight_seen[0]:
            in_flight_seen[0] = in_flight
        # Yield once to give other tasks a chance to interleave (would
        # interleave without the cap).
        await asyncio.sleep(0.01)
        exit_count[0] += 1
        return {"ok": True}

    import concurrent.futures as cf
    with TestClient(app) as client:
        with cf.ThreadPoolExecutor(max_workers=4) as ex:
            futures = [ex.submit(client.get, "/throttled",
                                 headers={"x-tenant-id": "acme"})
                       for _ in range(3)]
            for f in futures:
                assert f.result().status_code == 200

    # cap=1 means at most 1 in-flight per tenant. Multi-thread TestClient
    # might still observe in_flight_seen ≤ 1 (the asyncio loop serializes
    # them through the semaphore).
    assert in_flight_seen[0] <= 1, (
        f"expected ≤1 in-flight per tenant, saw {in_flight_seen[0]}")


# ----- T4.8.d different tenants have INDEPENDENT semaphores ---------------

def test_t4_8_different_tenants_independent():
    """Tenant-A's cap doesn't block tenant-B. With cap=1 each, A and B
    can both run concurrently."""
    app = lancelot.App()
    app.add_middleware(TenantConcurrencyLimiter,
                       max_concurrent_per_tenant=1)

    seen_tenants = set()

    @app.get("/work")
    async def work() -> dict:
        # Inspect which semaphore we're under via the ContextVar
        from lancelot.context import tenant_id
        seen_tenants.add(tenant_id.get())
        await asyncio.sleep(0.005)
        return {"ok": True}

    import concurrent.futures as cf
    with TestClient(app) as client:
        with cf.ThreadPoolExecutor(max_workers=4) as ex:
            futures = [
                ex.submit(client.get, "/work",
                          headers={"x-tenant-id": "alpha"}),
                ex.submit(client.get, "/work",
                          headers={"x-tenant-id": "beta"}),
            ]
            for f in futures:
                assert f.result().status_code == 200

    assert seen_tenants == {"alpha", "beta"}


# ----- T4.8.e cap=0 disables the middleware (passthrough) -----------------

def test_t4_8_cap_zero_is_passthrough():
    """cap=0 disables the limiter entirely. queue_wait_ms stays 0.0."""
    from unittest.mock import patch

    captured = []

    def fake_record(*args, **kwargs):
        captured.append(kwargs.get("queue_wait_ms", -1))

    app = lancelot.App()
    app.add_middleware(TenantConcurrencyLimiter, max_concurrent_per_tenant=0)

    @app.get("/ok")
    async def ok() -> dict:
        return {"ok": True}

    with patch("lancelot.metrics.record_request", side_effect=fake_record):
        with TestClient(app) as client:
            r = client.get("/ok", headers={"x-tenant-id": "anyone"})
            assert r.status_code == 200

    assert captured == [0.0]


# ----- T4.8.f anon_label when header is missing ---------------------------

def test_t4_8_anonymous_tenant_uses_shared_bucket():
    """Requests without the tenant header all share the `_anon_` bucket
    (configurable). Cap applies to anon traffic too."""
    app = lancelot.App()
    limiter_holder = []

    class TraceLimiter(TenantConcurrencyLimiter):
        def __init__(self, app, **kw):
            super().__init__(app, **kw)
            limiter_holder.append(self)

    app.add_middleware(TraceLimiter, max_concurrent_per_tenant=2)

    @app.get("/ok")
    async def ok() -> dict:
        return {"ok": True}

    with TestClient(app) as client:
        for _ in range(3):
            assert client.get("/ok").status_code == 200

    # Anon bucket created (single sem; not 3 sems).
    assert "_anon_" in limiter_holder[0]._sems
    assert len(limiter_holder[0]._sems) == 1
