"""M7 T2.1 + T2.4 + T2.5 + T3.3 — cost ContextVar / tenant / cardinality / agent_id.

T2.1: lancelot.context.cost_usd accumulates per-request cost; emitted as
      `X-Lancelot-Cost-USD` response header + `lancelot.http.cost.usd` metric.
T2.4: tenant_id is bounded by LANCELOT_TENANT_LABEL_MAX; over-cap → _overflow_.
T2.5: RED metrics emit on adverse paths (4xx / 5xx / cancel) — http.status_code
      attribute added so dashboards can slice.
T3.3: lancelot.context.agent_id ContextVar exists (parser wiring is per-app
      middleware, framework just ships the ContextVar).
"""
import os

import pytest

import lancelot
from lancelot.context import cost_usd, tenant_id, agent_id
from lancelot.testclient import TestClient

pytest.importorskip("opentelemetry.sdk.metrics")


_READER = None


def _install_reader():
    """Same OTel install pattern as test_m4_observability."""
    global _READER
    if _READER is None:
        from opentelemetry import metrics as om
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import InMemoryMetricReader

        _READER = InMemoryMetricReader()
        try:
            om.set_meter_provider(MeterProvider(metric_readers=[_READER]))
        except Exception:  # noqa: BLE001
            pass
    from lancelot.metrics import reset_for_tests
    reset_for_tests()
    return _READER


def _all_histogram_attrs(reader, name):
    """Return list of attribute-dicts for every histogram data point named `name`."""
    md = reader.get_metrics_data()
    out = []
    if md is None:
        return out
    for rm in md.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if metric.name == name:
                    for dp in metric.data.data_points:
                        out.append(dict(dp.attributes))
    return out


# ----- T2.1 cost ContextVar -----------------------------------------------

def test_t2_1_cost_emits_response_header():
    reader = _install_reader()
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
    reader = _install_reader()
    app = lancelot.App()

    @app.get("/free")
    async def free() -> dict:
        return {"ok": True}

    with TestClient(app) as client:
        r = client.get("/free")
        # No header when no cost accumulated.
        assert "x-lancelot-cost-usd" not in r.headers


def test_t2_1_cost_recorded_in_otel_histogram():
    reader = _install_reader()
    app = lancelot.App()

    @app.get("/charge")
    async def charge() -> dict:
        cost_usd.set(0.05)
        return {"ok": True}

    with TestClient(app) as client:
        client.get("/charge")
        # Force the reader to pick up metrics.
        reader.collect()
        attrs_list = _all_histogram_attrs(reader, "lancelot.http.cost.usd")
        # At least one data point recorded for this route.
        routes = {a.get("http.route") for a in attrs_list}
        assert "/charge" in routes


# ----- T2.4 cardinality cap -----------------------------------------------

def test_t2_4_cardinality_overflow_bucket(monkeypatch):
    # Force a low cap so we can prove the overflow bucket fires.
    monkeypatch.setenv("LANCELOT_TENANT_LABEL_MAX", "3")
    reader = _install_reader()
    app = lancelot.App()

    @app.get("/multi")
    async def multi() -> dict:
        return {"ok": True}

    # Set 5 distinct tenants; only 3 should make it as distinct labels;
    # remaining → _overflow_.
    with TestClient(app) as client:
        for i in range(5):
            from lancelot.context import tenant_id as tvar

            def hit(tname):
                token = tvar.set(tname)
                try:
                    client.get("/multi")
                finally:
                    tvar.reset(token)

            hit(f"tenant-{i}")

    reader.collect()
    attrs_list = _all_histogram_attrs(reader, "lancelot.http.request.duration")
    tenants = {a.get("lancelot.tenant") for a in attrs_list}
    # We should see at most 4 distinct tenant labels (3 real + _overflow_).
    assert "_overflow_" in tenants, f"tenants: {tenants}"


# ----- T2.5 adverse-path emit ---------------------------------------------

def test_t2_5_adverse_path_includes_status_code():
    reader = _install_reader()
    app = lancelot.App()

    @app.get("/err500")
    async def err500() -> dict:
        raise RuntimeError("synthetic")

    with TestClient(app) as client:
        r = client.get("/err500")
        assert r.status_code == 500

    reader.collect()
    attrs_list = _all_histogram_attrs(reader, "lancelot.http.request.duration")
    found_500 = any(a.get("http.status_code") == "500" for a in attrs_list)
    assert found_500, f"no 500 metric attr; got: {[a.get('http.status_code') for a in attrs_list]}"


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
