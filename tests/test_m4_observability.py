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


_READER = None


def _install_reader():
    """One process-wide InMemoryMetricReader. `set_meter_provider` is
    once-per-process: a second provider is silently rejected, leaving its
    reader orphaned (get_metrics_data() → None). Because other test files
    (e.g. test_m15_metric_cardinality) also install a reader and are loaded as
    separate top-level modules under pytest's default import mode, we recover
    the reader from whatever MeterProvider is ACTUALLY active — creating one
    only if no SDK provider is set yet — so collection order can't orphan us."""
    global _READER
    if _READER is None:
        from opentelemetry import metrics as om
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import InMemoryMetricReader

        provider = om.get_meter_provider()
        readers = getattr(getattr(provider, "_sdk_config", None),
                          "metric_readers", None)
        if readers:
            for r in readers:
                if isinstance(r, InMemoryMetricReader):
                    _READER = r
                    break
        if _READER is None:
            _READER = InMemoryMetricReader()
            try:
                om.set_meter_provider(MeterProvider(metric_readers=[_READER]))
            except Exception:  # noqa: BLE001 — provider already set this process
                pass
    from lancelot import metrics as lmetrics
    lmetrics.reset_for_tests()  # rebind lancelot's cached meter to the provider
    return _READER


def _scoped(reader, name, routes):
    return [p for p in _counter_points(reader, name)
            if p.attributes.get("http.route") in routes]


def test_v3_1_red_metrics_exact_under_concurrent_load():
    reader = _install_reader()

    # Unique routes so collisions with other files' `/ok`/`/boom` can't leak
    # in; we still snapshot-then-delta because the InMemoryMetricReader is
    # process-wide + cumulative (other test files emit into the same reader),
    # so an absolute total would be order-dependent. The delta keeps the
    # exactly-once-per-request property assertable regardless of collection
    # order (pytest runs this test atomically — prior emits are in the
    # baseline, later emits happen after the asserts).
    app = lancelot.App()

    @app.get("/v31_ok")
    async def ok():
        return {"ok": True}

    @app.get("/v31_boom")
    async def boom():
        raise RuntimeError("boom")  # → 500

    _mine = {"/v31_ok", "/v31_boom"}

    def _counts():
        pts = _scoped(reader, "lancelot.http.requests", _mine)
        total = sum(p.value for p in pts)
        err = sum(p.value for p in pts
                  if p.attributes.get("http.status_class") == "5xx")
        ok_ct = sum(p.value for p in pts
                    if p.attributes.get("http.status_class") == "2xx")
        dur_count = sum(
            p.count for p in
            _scoped(reader, "lancelot.http.request.duration", _mine))
        return total, err, ok_ct, dur_count

    base = _counts()

    N = 200
    # Deterministic mix: 1 in 4 errors.
    paths = ["/v31_boom" if i % 4 == 0 else "/v31_ok" for i in range(N)]
    n_err = sum(p == "/v31_boom" for p in paths)

    with TestClient(app, raise_server_exceptions=False) as c:
        def hit(p):
            return c.get(p).status_code

        with cf.ThreadPoolExecutor(max_workers=32) as ex:
            codes = list(ex.map(hit, paths))

    assert sum(c == 200 for c in codes) == N - n_err
    assert sum(c == 500 for c in codes) == n_err

    total, err, ok_ct, dur_count = (a - b for a, b in zip(_counts(), base))

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


def test_v3_4_stream_ttft_metric_recorded():
    """TTFT (time-to-first-chunk) is recorded for streaming responses — the
    AI-streaming UX metric the research transcripts (core/) say matters most.
    Distinct from the total-duration histogram (which is the wrong number for a
    token stream)."""
    from lancelot.stream import Event
    from lancelot.testclient import TestClient

    reader = _install_reader()
    app = lancelot.App()

    @app.stream("/s")
    async def s():
        yield Event("delta", {"i": 0})
        yield Event("delta", {"i": 1})

    with TestClient(app) as c:
        with c.stream("GET", "/s") as r:
            assert r.status_code == 200
            b"".join(r.iter_bytes())

    pts = _counter_points(reader, "lancelot.http.stream.ttft")
    assert pts, "no stream TTFT data point recorded"
    assert sum(p.count for p in pts) >= 1
    assert any(p.attributes.get("http.route") == "/s" for p in pts)
    assert all(p.sum >= 0 for p in pts)  # TTFT is a non-negative duration
