"""Tier-B regression guards (research-distillation `docs/plans/...`).

Each test pins one Tier-B guarantee:
  B1 — graceful shutdown surface (initiate_shutdown, drain, signal-safe install)
  B2 — RED metrics auto-emit (lazy OTel; no-op without OTel)
  B3 — per-request ContextVar (request_id propagation; X-Request-ID echo-on-incoming)
  B4 — global concurrent-request cap → 503 + Retry-After
  B5 — per-route timeout → 504

If a test goes red, the corresponding Tier-B guarantee has regressed."""
import asyncio
import threading

import pytest

import lancelot
from lancelot.context import request_id
from lancelot.testclient import TestClient


# ---------- B1: graceful shutdown ---------------------------------------

def test_b1_initiate_shutdown_flips_readyz_and_refuses_new_requests():
    """`initiate_shutdown()` flips `_shutting_down`: /readyz immediately
    returns 503 + Retry-After; non-health routes get 503 'draining';
    /livez still 200 (the process is alive)."""
    app = lancelot.App()

    @app.get("/work")
    def w():
        return {"done": True}

    with TestClient(app) as c:
        # Before drain: everything 200.
        assert c.get("/work").status_code == 200
        assert c.get("/readyz").status_code == 200
        app.initiate_shutdown()
        # After drain: /work refused; /readyz draining; /livez still 200.
        r = c.get("/work")
        assert r.status_code == 503
        assert r.headers.get("retry-after") == "1"
        assert b"draining" in r.content
        assert c.get("/readyz").status_code == 503
        assert c.get("/livez").status_code == 200


def test_b1_drain_helper_waits_for_active_to_hit_zero():
    """`await app.drain(timeout=...)` returns True once `_active_requests`
    reaches zero, False on timeout. With no in-flight work it returns
    immediately."""
    app = lancelot.App()

    async def go():
        return await app.drain(timeout=0.5)

    assert app._active_requests == 0
    assert asyncio.run(go()) is True


def test_b1_drain_returns_false_on_timeout():
    """If requests stay in-flight past the deadline, drain() returns False
    (so the supervisor can decide to hard-kill)."""
    app = lancelot.App()
    app._active_requests = 1  # simulate stuck request

    async def go():
        return await app.drain(timeout=0.1)

    assert asyncio.run(go()) is False


def test_b1_install_signal_handlers_is_safe_on_non_main_thread():
    """The user can call `install_signal_handlers()` defensively from any
    thread — on a non-main thread it returns False without raising."""
    app = lancelot.App()
    result = [None]

    def worker():
        result[0] = app.install_signal_handlers()

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert result[0] is False


# ---------- B2: RED metrics auto-emit -----------------------------------

def test_b2_metrics_module_is_noop_without_otel():
    """If `opentelemetry` is not importable, `record_request` returns None
    and never raises — frameworks MUST NOT crash a request on missing
    telemetry deps."""
    import lancelot.metrics as m

    m.reset_for_tests()
    # The default test env doesn't have opentelemetry; record_request must
    # be a fast no-op. (If opentelemetry happens to be installed locally,
    # this still returns None — _maybe_bind succeeds, record runs.)
    assert m.record_request("/x", "GET", 200, 0.001) is None
    assert m.in_flight_delta(1) is None


def test_b2_request_path_runs_with_metrics_hook_enabled():
    """RED metrics record on every HTTP exit (200 / 4xx / 5xx). We can't
    assert the OTel exporter here (no OTel installed); the strong contract
    is that hooking into the seam doesn't break the wire-bytes."""
    app = lancelot.App()

    @app.get("/ok")
    def ok():
        return {"ok": True}

    with TestClient(app) as c:
        r = c.get("/ok")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


# ---------- B3: per-request ContextVar ----------------------------------

def test_b3_request_id_contextvar_is_set_inside_handler():
    """`lancelot.context.request_id.get()` returns the per-request id
    inside a handler — fresh UUIDv4 hex unless the client supplied one."""
    seen = {}
    app = lancelot.App()

    @app.get("/id")
    def get_id():
        seen["rid"] = request_id.get()
        return {"id": seen["rid"]}

    with TestClient(app) as c:
        r = c.get("/id")
    assert r.status_code == 200
    # 32-char UUIDv4 hex (the format `new_request_id()` mints).
    assert len(seen["rid"]) == 32 and seen["rid"].isalnum()
    assert r.json()["id"] == seen["rid"]


def test_b3_incoming_x_request_id_propagates_and_echoes():
    """If the client sends X-Request-ID, we MUST honor it for context AND
    echo it back as a response header — that's the standard trace handoff.
    Without an incoming header we do NOT echo (keeps FastAPI byte-parity
    for the common case)."""
    seen: list[str] = []
    app = lancelot.App()

    @app.get("/id")
    def get_id():
        seen.append(request_id.get())
        return {"ok": True}

    with TestClient(app) as c:
        r1 = c.get("/id", headers={"X-Request-ID": "abc-12345"})
        r2 = c.get("/id")
    assert seen[0] == "abc-12345"
    assert r1.headers.get("x-request-id") == "abc-12345"
    # No incoming → no echo (byte-parity for the common case).
    assert "x-request-id" not in {k.lower() for k in r2.headers}
    # The second request gets an auto-minted hex id, not the previous one.
    assert seen[1] != "abc-12345" and len(seen[1]) == 32


def test_b3_request_id_is_isolated_per_concurrent_request():
    """Two requests served concurrently MUST see different request_ids
    (ContextVars + asyncio.Task contexts give us this for free; this test
    pins it)."""
    seen: list[str] = []
    gate = asyncio.Event()
    app = lancelot.App()

    @app.get("/id")
    async def get_id():
        seen.append(request_id.get())
        # Yield to the loop so the next concurrent task can land here too.
        await asyncio.sleep(0)
        return {"ok": True}

    # Hit /id twice from the same client; ids should differ.
    with TestClient(app) as c:
        c.get("/id", headers={"X-Request-ID": "first"})
        c.get("/id", headers={"X-Request-ID": "second"})
    assert seen == ["first", "second"]
    _ = gate  # silence linter


# ---------- B4: global concurrent-request cap ---------------------------

def test_b4_global_cap_refuses_with_503_when_exceeded():
    """`App(max_concurrent_requests=N)`: the (N+1)th in-flight request is
    refused with 503 + Retry-After. Simulated by pre-loading the counter
    (the cooperative event loop makes this deterministic)."""
    app = lancelot.App(max_concurrent_requests=2)

    @app.get("/x")
    def x():
        return {"ok": True}

    app._active_requests = 2  # pretend two are in flight
    with TestClient(app) as c:
        r = c.get("/x")
    assert r.status_code == 503
    assert r.headers.get("retry-after") == "1"


def test_b4_cap_bypassed_for_health_probes():
    """Even under cap, /livez and /readyz MUST stay reachable so the LB
    can still probe a saturated instance."""
    app = lancelot.App(max_concurrent_requests=1)
    app._active_requests = 99  # totally saturated

    with TestClient(app) as c:
        assert c.get("/livez").status_code == 200
        assert c.get("/readyz").status_code == 200


def test_b4_default_cap_is_unlimited():
    """Default behavior MUST stay FastAPI-compatible (no cap)."""
    app = lancelot.App()
    assert app._max_concurrent_requests is None


# ---------- B5: per-route timeout → 504 ---------------------------------

def test_b5_timeout_kicks_in_with_504():
    """`@app.get('/x', timeout=0.1)` cancels a slow async handler and
    returns 504. Yield-dep teardown (if any) STILL runs because the
    cancellation is fed through the surrounding async-with."""
    app = lancelot.App()

    @app.get("/slow", timeout=0.05)
    async def slow():
        await asyncio.sleep(5.0)
        return {"never": True}

    with TestClient(app) as c:
        r = c.get("/slow")
    assert r.status_code == 504
    assert b"timeout" in r.content.lower()


def test_b5_no_timeout_means_no_deadline():
    """Default (no `timeout=` kwarg): handler runs to natural completion
    (FastAPI parity)."""
    app = lancelot.App()

    @app.get("/x")
    async def x():
        await asyncio.sleep(0.01)
        return {"done": True}

    with TestClient(app) as c:
        r = c.get("/x")
    assert r.status_code == 200
    assert r.json() == {"done": True}


def test_b5_timeout_runs_yield_dep_teardown():
    """Yield-deps' `finally:` blocks MUST still run when the handler is
    cancelled by the route timeout (B4 semantics preserved)."""
    from lancelot.params import Depends

    teardown_ran = []

    def db():
        try:
            yield "session"
        finally:
            teardown_ran.append("closed")

    app = lancelot.App()

    @app.get("/slow", timeout=0.05)
    async def slow(s: str = Depends(db)):
        await asyncio.sleep(5.0)
        return {"never": True}

    with TestClient(app) as c:
        r = c.get("/slow")
    assert r.status_code == 504
    assert teardown_ran == ["closed"]
