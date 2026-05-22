"""M4 Tier-V3 — observability under load.

V3.1 RED metrics (B2): the per-request counter + duration histogram must be
recorded EXACTLY once per request across all exit paths (success AND error),
with no double-count and no lost errors, under concurrency. Captured via the
real OTel SDK's InMemoryMetricReader (metrics live in-process; TestClient drives
the same ASGI `finally` that records them).

V3.2 request-id ContextVar isolation (B3): under concurrency each request must
see ITS OWN request-id — no cross-request bleed — on BOTH the async path (asyncio
Task context) and the sync path (thread pool, where the copy_context fix lives).
"""
import concurrent.futures as cf

import pytest

import lancelot
from lancelot.context import request_id
from lancelot.testclient import TestClient

pytest.importorskip("opentelemetry.sdk.metrics")


def _counter_points(reader, name):
    pts = []
    for rm in (reader.get_metrics_data().resource_metrics or []):
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == name:
                    pts.extend(m.data.data_points)
    return pts


def test_v3_1_red_metrics_exact_under_concurrent_load():
    from opentelemetry import metrics as om
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    reader = InMemoryMetricReader()
    try:
        om.set_meter_provider(MeterProvider(metric_readers=[reader]))
    except Exception:  # noqa: BLE001 — provider already set this process
        pass
    from lancelot import metrics as lmetrics
    lmetrics.reset_for_tests()  # rebind lancelot's cached meter to our provider

    app = lancelot.App()

    @app.get("/ok")
    async def ok():
        return {"ok": True}

    @app.get("/boom")
    async def boom():
        raise RuntimeError("boom")  # → 500

    N = 200
    # Deterministic mix: 1 in 4 errors.
    paths = ["/boom" if i % 4 == 0 else "/ok" for i in range(N)]
    n_err = sum(p == "/boom" for p in paths)

    with TestClient(app, raise_server_exceptions=False) as c:
        def hit(p):
            return c.get(p).status_code

        with cf.ThreadPoolExecutor(max_workers=32) as ex:
            codes = list(ex.map(hit, paths))

    assert sum(c == 200 for c in codes) == N - n_err
    assert sum(c == 500 for c in codes) == n_err

    pts = _counter_points(reader, "lancelot.http.requests")
    total = sum(p.value for p in pts)
    err = sum(p.value for p in pts
              if p.attributes.get("http.status_class") == "5xx")
    ok_ct = sum(p.value for p in pts
                if p.attributes.get("http.status_class") == "2xx")
    dur = _counter_points(reader, "lancelot.http.request.duration")
    dur_count = sum(p.count for p in dur)

    # Exactly once per request — no double-count, no lost errors.
    assert total == N, f"counter {total} != {N} requests"
    assert err == n_err, f"5xx count {err} != {n_err} errors (lost/miscounted)"
    assert ok_ct == N - n_err
    assert dur_count == N, f"duration histogram count {dur_count} != {N}"


@pytest.mark.parametrize("path,handler_kind", [("/rid_async", "async"),
                                               ("/rid_sync", "sync")])
def test_v3_2_request_id_isolation_under_concurrency(path, handler_kind):
    app = lancelot.App()

    if handler_kind == "async":
        @app.get("/rid_async")
        async def rid_async():
            return {"rid": request_id.get()}
    else:
        @app.get("/rid_sync")
        def rid_sync():  # SYNC → thread pool (the copy_context path)
            return {"rid": request_id.get()}

    N = 200
    with TestClient(app) as c:
        def hit(i):
            r = c.get(path, headers={"X-Request-ID": f"req-{i}"})
            return r.json()["rid"]

        with cf.ThreadPoolExecutor(max_workers=32) as ex:
            got = list(ex.map(hit, range(N)))

    # Every request observed ITS OWN id — zero cross-request bleed.
    assert got == [f"req-{i}" for i in range(N)], \
        f"request-id bleed: {[(i, g) for i, g in enumerate(got) if g != f'req-{i}'][:5]}"
