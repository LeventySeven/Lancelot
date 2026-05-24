"""M7 T2.1 + T2.4 + T2.5 + T3.3 — cost ContextVar / tenant / cardinality / agent_id.

T2.1: lancelot.context.cost_usd accumulates per-request cost; emitted as
      `X-Lancelot-Cost-USD` response header + (when handler-set) passed to
      `record_request` so the cost histogram fires.
T2.4: tenant_id is bounded by LANCELOT_TENANT_LABEL_MAX; over-cap → _overflow_.
T2.5: RED metrics emit on adverse paths (4xx / 5xx / cancel) — http.status_code
      attribute added so dashboards can slice.
T3.3: lancelot.context.agent_id ContextVar exists (parser wiring is per-app
      middleware, framework just ships the ContextVar).

Test design: the M4 tests own the OTel meter + InMemoryMetricReader (once-
per-process by SDK design). Rather than fight pytest test-ordering for the
shared reader, these tests exercise the primitives directly:
  - T2.1 header path: real ASGI client (no OTel needed)
  - T2.1 metric path: mock record_request, assert the wiring (cost_usd
    ContextVar → record_request(cost_usd=...))
  - T2.4: unit-test `_bounded_tenant` directly (pure function)
  - T2.5: unit-test `record_request` attrs include http.status_code
  - T3.3: pure asyncio ContextVar isolation (no HTTP/OTel)
The end-to-end histogram emit path is exercised by test_m4_observability."""
import os
from unittest.mock import patch

import lancelot
from lancelot.context import cost_usd, tenant_id, agent_id
from lancelot.testclient import TestClient


# ----- T2.1 cost ContextVar -----------------------------------------------

def test_t2_1_cost_emits_response_header():
    """Handler sets cost_usd → ASGI seam emits X-Lancelot-Cost-USD header.
    No OTel reader needed; the header path is independent."""
    app = lancelot.App()

    @app.get("/billable")
    async def billable() -> dict:
        cost_usd.set(0.0125)  # 1.25¢ — simulated upstream LLM cost
        return {"ok": True}

    with TestClient(app) as client:
        r = client.get("/billable")
        assert r.status_code == 200
        # X-Lancelot-Cost-USD set to 6-decimal precision.
        assert r.headers.get("x-lancelot-cost-usd") == "0.012500"


def test_t2_1_cost_absent_when_handler_does_not_set():
    """No cost_usd.set() → no X-Lancelot-Cost-USD header (zero-default
    suppression — don't pollute the cost metric with 0.0 readings)."""
    app = lancelot.App()

    @app.get("/free")
    async def free() -> dict:
        return {"ok": True}

    with TestClient(app) as client:
        r = client.get("/free")
        # No header when no cost accumulated.
        assert "x-lancelot-cost-usd" not in r.headers


def test_t2_1_cost_recorded_via_record_request_wiring():
    """ASGI seam reads cost_usd ContextVar in the request `finally` block
    and passes it to `record_request(cost_usd=...)`. The histogram emit
    itself is a thin wrapper over the OTel SDK and is exercised by
    test_m4_observability; here we just verify the WIRING — that the
    handler's cost_usd.set(...) lands as the cost_usd kwarg on the
    next record_request call. Avoids cross-test OTel state pollution."""
    captured = []

    def fake_record(*args, **kwargs):
        captured.append((args, kwargs))

    app = lancelot.App()

    @app.get("/charge")
    async def charge() -> dict:
        cost_usd.set(0.05)
        return {"ok": True}

    with patch("lancelot.metrics.record_request", side_effect=fake_record):
        with TestClient(app) as client:
            r = client.get("/charge")
            assert r.status_code == 200

    assert captured, "record_request was not called"
    args, kwargs = captured[-1]
    # record_request(path, method, status, duration, tenant=..., cost_usd=...)
    assert args[0] == "/charge"
    assert args[1] == "GET"
    assert args[2] == 200
    # cost_usd is a kwarg per asgi.py call site.
    assert kwargs.get("cost_usd") == 0.05, f"got: {kwargs}"


# ----- T2.4 cardinality cap -----------------------------------------------

def test_t2_4_bounded_tenant_under_cap_returns_value(monkeypatch):
    """Tenants under the LANCELOT_TENANT_LABEL_MAX cap pass through."""
    monkeypatch.setenv("LANCELOT_TENANT_LABEL_MAX", "3")
    from lancelot.metrics import _bounded_tenant, _tenant_label_set
    _tenant_label_set.clear()  # fresh slate

    assert _bounded_tenant("tenant-a") == "tenant-a"
    assert _bounded_tenant("tenant-b") == "tenant-b"
    assert _bounded_tenant("tenant-c") == "tenant-c"
    # Re-emitting an already-seen tenant returns it (under cap, no
    # eviction needed).
    assert _bounded_tenant("tenant-a") == "tenant-a"


def test_t2_4_bounded_tenant_over_cap_buckets_to_overflow(monkeypatch):
    """Over the cap → '_overflow_'; previously-seen tenants still pass."""
    monkeypatch.setenv("LANCELOT_TENANT_LABEL_MAX", "3")
    from lancelot.metrics import _bounded_tenant, _tenant_label_set
    _tenant_label_set.clear()

    for t in ("alpha", "beta", "gamma"):
        assert _bounded_tenant(t) == t  # fills the cap
    # 4th, 5th distinct tenants → overflow bucket
    assert _bounded_tenant("delta") == "_overflow_"
    assert _bounded_tenant("epsilon") == "_overflow_"
    # Already-seen tenants still pass through
    assert _bounded_tenant("alpha") == "alpha"


def test_t2_4_bounded_tenant_sentinel_and_empty():
    """Sentinel '-' and empty string return '-' unchanged (no slot used
    in the cap — sentinel doesn't count)."""
    from lancelot.metrics import _bounded_tenant, _tenant_label_set
    _tenant_label_set.clear()
    assert _bounded_tenant("-") == "-"
    assert _bounded_tenant("") == "-"
    # Sentinel did not consume a slot.
    assert len(_tenant_label_set) == 0


# ----- T2.5 adverse-path emit ---------------------------------------------

def test_t2_5_adverse_path_includes_status_code_via_wiring():
    """ASGI seam passes the final status (500 for handler-raised exception)
    to `record_request`, which adds it as `http.status_code` attr. Verify
    the WIRING via a captured-call mock (the attrs-build is exercised by
    test_m4 + the dedicated unit test below)."""
    captured = []

    def fake_record(*args, **kwargs):
        captured.append((args, kwargs))

    app = lancelot.App()

    @app.get("/err500")
    async def err500() -> dict:
        raise RuntimeError("synthetic")

    with patch("lancelot.metrics.record_request", side_effect=fake_record):
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get("/err500")
            assert r.status_code == 500

    assert captured, "record_request was not called"
    args, kwargs = captured[-1]
    # record_request(path, method, status, duration, ...)
    assert args[2] == 500, f"expected status=500 in record_request, got {args[2]}"


def test_t2_5_record_request_attrs_include_http_status_code(monkeypatch):
    """Unit test: `record_request` builds an attrs dict that includes the
    raw `http.status_code` (not just `http.status_class`) so dashboards
    can slice on the exact adverse code (499, 504, 503, ...). We patch the
    instruments to capture attrs without touching the real meter cache."""
    captured = {}

    class FakeInst:
        def add(self, value, attributes=None):
            captured.setdefault("counter", []).append(attributes)
        def record(self, value, attributes=None):
            captured.setdefault("hist", []).append(attributes)

    fake_state = {
        "req_counter": FakeInst(),
        "duration_hist": FakeInst(),
        "in_flight": FakeInst(),
        "stream_ttft": FakeInst(),
        "cost_hist": FakeInst(),
    }
    # Bypass _maybe_bind by setting _state to our fakes.
    monkeypatch.setattr("lancelot.metrics._state", fake_state)

    from lancelot.metrics import record_request
    record_request("/api/x", "GET", 504, 0.123, tenant="t1", cost_usd=0.0)
    record_request("/api/y", "POST", 200, 0.05, tenant="-", cost_usd=0.01)

    counter_attrs = captured["counter"]
    assert any(a.get("http.status_code") == "504" for a in counter_attrs)
    assert any(a.get("http.status_code") == "200" for a in counter_attrs)
    # status_class should also still be present (existing dashboards rely on it).
    assert any(a.get("http.status_class") == "5xx" for a in counter_attrs)
    assert any(a.get("http.status_class") == "2xx" for a in counter_attrs)


# ----- T3.3 agent_id ContextVar exists -------------------------------------

def test_t3_3_agent_id_context_var_isolation():
    """The agent_id ContextVar is set per-Task — no bleed across tasks.
    (Same shape as the M4 request_id isolation test; framework ships the
    primitive, user middleware populates it from header.)"""
    import asyncio

    async def run_one(value: str) -> str:
        agent_id.set(value)
        await asyncio.sleep(0)  # force a scheduling boundary
        return agent_id.get()

    async def main():
        # Two tasks set distinct agent_ids; results must NOT bleed.
        result_a, result_b = await asyncio.gather(
            run_one("agent-alice"), run_one("agent-bob")
        )
        return result_a, result_b

    a, b = asyncio.run(main())
    assert a == "agent-alice"
    assert b == "agent-bob"
    # Outside any task, default sentinel is "-".
    assert agent_id.get() == "-"
