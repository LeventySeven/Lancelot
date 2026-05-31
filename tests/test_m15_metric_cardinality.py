"""M15 T1 (#2) — RED/TTFT metric label is the route TEMPLATE, not the
concrete URL, and unmatched paths use a BOUNDED sentinel.

The bug: the ASGI seam recorded `scope["path"]` (the concrete URL, e.g.
`/items/42`) as the `http.route` attribute on every RED metric AND on the
streaming-TTFT histogram. A path-parameterized route therefore explodes the
metric cardinality (one time-series per distinct id), and a URL-spraying 404
scanner does the same on the not-found path. The fix records the matched
`Route.path` template (`/items/{id}`) and bounded sentinels for 404/405.

Captured via the real OTel SDK's InMemoryMetricReader (the same seam the
existing M4 observability test uses) — metrics live in-process; TestClient
drives the same ASGI `finally` that records them.
"""
import pytest

import lancelot
from lancelot.stream import Event
from lancelot.testclient import TestClient

pytest.importorskip("opentelemetry.sdk.metrics")


def _install_reader():
    """Return the SINGLE process-wide InMemoryMetricReader.

    `set_meter_provider` is once-per-process: a second provider is silently
    rejected by the OTel SDK, leaving its reader orphaned (get_metrics_data()
    → None). Several test files (this one + test_m4_observability) install a
    reader, and under pytest's default import mode they're top-level modules
    so a module-level `_READER` can't be shared across files. To be robust to
    collection order we recover the reader from whatever MeterProvider is
    ACTUALLY active — only creating one if no SDK provider is set yet. That
    way, whichever metric test ran first wins the provider and we all read the
    same in-memory reader."""
    from opentelemetry import metrics as om
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    provider = om.get_meter_provider()
    readers = getattr(getattr(provider, "_sdk_config", None),
                      "metric_readers", None)
    reader = None
    if readers:
        for r in readers:
            if isinstance(r, InMemoryMetricReader):
                reader = r
                break
    if reader is None:
        reader = InMemoryMetricReader()
        try:
            om.set_meter_provider(MeterProvider(metric_readers=[reader]))
        except Exception:  # noqa: BLE001 — provider already set this process
            pass
    from lancelot import metrics as lmetrics
    lmetrics.reset_for_tests()  # rebind lancelot's cached meter to the provider
    return reader


def _points(reader, name):
    pts = []
    for rm in (reader.get_metrics_data().resource_metrics or []):
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == name:
                    pts.extend(m.data.data_points)
    return pts


def _routes_for(reader, name):
    return {p.attributes.get("http.route") for p in _points(reader, name)}


def test_metric_label_is_route_template():
    reader = _install_reader()
    app = lancelot.App()

    @app.get("/items/{id}")
    async def get_item(id: int):
        return {"id": id}

    with TestClient(app) as c:
        assert c.get("/items/42").status_code == 200
        assert c.get("/items/43").status_code == 200

    routes = _routes_for(reader, "lancelot.http.requests")
    # The TEMPLATE is recorded; the concrete URLs are NOT (no cardinality blowup).
    assert "/items/{id}" in routes, routes
    assert "/items/42" not in routes, routes
    assert "/items/43" not in routes, routes


def test_metric_404_uses_sentinel():
    reader = _install_reader()
    app = lancelot.App()

    @app.get("/known")
    async def known():
        return {"ok": True}

    with TestClient(app, raise_server_exceptions=False) as c:
        assert c.get("/no-such-path-xyzzy").status_code == 404

    routes = _routes_for(reader, "lancelot.http.requests")
    assert "__not_found__" in routes, routes
    # The concrete sprayed URL must NOT become a label.
    assert "/no-such-path-xyzzy" not in routes, routes


def test_metric_405_uses_sentinel():
    reader = _install_reader()
    app = lancelot.App()

    @app.get("/only-get")
    async def only_get():
        return {"ok": True}

    with TestClient(app, raise_server_exceptions=False) as c:
        # Wrong method on an existing path → 405.
        assert c.post("/only-get").status_code == 405

    routes = _routes_for(reader, "lancelot.http.requests")
    assert "__method_not_allowed__" in routes, routes
    assert "/only-get" not in routes, routes


def test_stream_ttft_label_is_template():
    reader = _install_reader()
    app = lancelot.App()

    @app.stream("/s/{id}")
    async def s(id: int):
        yield Event("delta", {"i": 0})
        yield Event("delta", {"i": 1})

    with TestClient(app) as c:
        with c.stream("GET", "/s/7") as r:
            assert r.status_code == 200
            b"".join(r.iter_bytes())

    routes = _routes_for(reader, "lancelot.http.stream.ttft")
    assert "/s/{id}" in routes, routes
    assert "/s/7" not in routes, routes
