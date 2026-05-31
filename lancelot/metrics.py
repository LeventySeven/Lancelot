"""RED metrics auto-emit (Tier-B.B2 / research-distillation).

R-E-D = Request rate, Error rate, Duration. Per HTTP request, the ASGI
seam calls `record_request(route_path, method, status, duration_s)`.
If `opentelemetry-api` is installed in the user's env we lazy-bind the
meter on the first call and emit a counter + histogram from then on; if
it's NOT installed (typical for the small-startup default), every call is
a single cheap None-check and returns.

`route_path` (the `http.route` attribute, here and on `record_stream_ttft`)
is the route TEMPLATE — e.g. `/items/{id}`, NOT the concrete URL
`/items/42` (M15 T1 #2: recording the concrete URL would emit one metric
time-series per distinct id → unbounded cardinality). For requests that
match no route the ASGI seam passes a BOUNDED sentinel instead of the
sprayed URL: `"__not_found__"` (404), `"__method_not_allowed__"` (405), or
`"__unmatched__"` (any other no-match exit, e.g. the draining 503).

SCOPE (M15 T1 investigation): these metrics fire ONLY on the Python ASGI
seam (lancelot/asgi.py). The Rust `_native.run` server (src/service.rs,
src/dispatch.rs) emits NO OTel metrics at all — `grep -rI 'opentelemetry\\|
record_request\\|histogram' src/` is empty. So RED/TTFT observability is a
property of running via the ASGI app (uvicorn/hypercorn/`App.__call__`),
NOT the prebaked-bytes Rust server tier. Operators who deploy on the native
const tier for raw throughput get no RED metrics from it by design.

Cardinal rule: opentelemetry is NEVER a runtime dep of Lancelot. Pydantic
only. Users opt-in by installing `opentelemetry-api` themselves.
"""
_NOT_INITIALIZED = object()
_state = _NOT_INITIALIZED


def _maybe_bind():
    """Lazy OTel meter binding. Returns a small dict of instruments on
    success, or None on every subsequent call after the first failure
    (the user hasn't installed OTel — don't keep retrying)."""
    global _state
    if _state is not _NOT_INITIALIZED:
        return _state
    try:
        from opentelemetry import metrics as _otel_metrics
    except Exception:  # noqa: BLE001
        _state = None
        return None
    try:
        meter = _otel_metrics.get_meter("lancelot")
        _state = {
            "req_counter": meter.create_counter(
                "lancelot.http.requests",
                description="Total HTTP requests handled by Lancelot.",
            ),
            "duration_hist": meter.create_histogram(
                "lancelot.http.request.duration",
                unit="s",
                description="HTTP request duration in seconds.",
            ),
            "in_flight": meter.create_up_down_counter(
                "lancelot.http.requests.in_flight",
                description="In-flight HTTP requests right now.",
            ),
            # TTFT — time-to-first-token: request start → first STREAMED chunk
            # on the wire. The research transcripts (core/) are unanimous that
            # TTFT (not total duration) is THE user-facing-AI streaming metric
            # ("above 200-300ms users perceive lag"). The total-duration
            # histogram above is the wrong number for a token stream; this is
            # the right one. Recorded once per streaming response.
            "stream_ttft": meter.create_histogram(
                "lancelot.http.stream.ttft",
                unit="s",
                description="Streaming time-to-first-chunk (TTFT) in seconds.",
            ),
            # M7 T2.1 (Sim.ai usage-based billing pattern): per-request $.
            # Lancelot doesn't ship cost RATES — those are handler-side.
            # The metric just emits whatever the handler accumulated in
            # `lancelot.context.cost_usd` before the response went out.
            "cost_hist": meter.create_histogram(
                "lancelot.http.cost.usd",
                unit="USD",
                description="Per-request accumulated cost (USD) — handler-set.",
            ),
            # M7 T4.8 (Turbopuffer pattern): time the request spent WAITING
            # before the handler started (queue_wait = total_in_handler −
            # pure_handler_compute). The missing third number after RED +
            # TTFT for multi-tenant AI backends — surfaces head-of-line
            # blocking + tenant-concurrency-cap saturation. Recorded once
            # per request from the asgi seam.
            "queue_wait_hist": meter.create_histogram(
                "lancelot.http.queue_wait_ms",
                unit="ms",
                description="Time request waited before handler ran (ms).",
            ),
        }
    except Exception:  # noqa: BLE001 — defensive; partial OTel installs
        _state = None
    return _state


_tenant_label_max_cached: int | None = None


def _tenant_label_max() -> int:
    """M7 T2.4 (Hightouch pattern): cap distinct tenant labels we emit
    on metrics so a runaway-tenant-id key doesn't blow up cardinality.
    Over-cap → bucket to "_overflow_". Per-process state, set once — so we
    read+parse the env ONCE and memoize (was re-reading on every new tenant)."""
    global _tenant_label_max_cached
    if _tenant_label_max_cached is None:
        import os
        try:
            _tenant_label_max_cached = max(
                1, int(os.environ.get("LANCELOT_TENANT_LABEL_MAX", "1000")))
        except (ValueError, TypeError):
            _tenant_label_max_cached = 1000
    return _tenant_label_max_cached


# Bounded-set of tenant labels we've seen this process. NOT thread-safe;
# the GIL serializes Python attribute access so over-counting by one is
# acceptable. Cleared by reset_for_tests().
_tenant_label_set: set = set()


def _bounded_tenant(tenant: str) -> str:
    """M7 T2.4: return the tenant label or '_overflow_' if we've already
    seen LANCELOT_TENANT_LABEL_MAX distinct tenants. Pure addition; never
    evicts (tenant set is process-life since the goal is cardinality cap,
    not LRU eviction)."""
    if not tenant or tenant == "-":
        return "-"
    if tenant in _tenant_label_set:
        return tenant
    if len(_tenant_label_set) >= _tenant_label_max():
        return "_overflow_"
    _tenant_label_set.add(tenant)
    return tenant


def record_request(route_path: str, method: str, status: int,
                   duration_s: float, tenant: str = "-",
                   cost_usd: float = 0.0,
                   queue_wait_ms: float = 0.0) -> None:
    # `route_path` is the route TEMPLATE or a bounded 404/405/unmatched
    # sentinel (cardinality bound — see module docstring). Called ONLY from
    # the Python ASGI seam; the Rust `_native` server emits no metrics.
    inst = _maybe_bind()
    if inst is None:
        return
    # M7 T2.5: include status (not just status_class) on metric attrs so
    # adverse-path filtering (e.g. status=499 for client-disconnect; 504
    # for per-route timeout; 503 for upstream) is preserved in dashboards
    # without inflating cardinality past the 5-7 expected adverse codes.
    attrs = {
        "http.route": route_path,
        "http.method": method,
        "http.status_class": f"{status // 100}xx",
        "http.status_code": str(status),
        # M7 T2.4: cardinality-bounded tenant label.
        "lancelot.tenant": _bounded_tenant(tenant),
    }
    try:
        inst["req_counter"].add(1, attributes=attrs)
        inst["duration_hist"].record(duration_s, attributes=attrs)
        # M7 T2.1: cost per request (Sim.ai usage-based-billing pattern).
        # Skip the emit on the zero-default — most handlers don't accumulate
        # cost, no need to flood the cost metric with 0.0 readings.
        if cost_usd > 0:
            ch = inst.get("cost_hist")
            if ch is not None:
                ch.record(cost_usd, attributes=attrs)
        # M7 T4.8 (Turbopuffer pattern): queue-wait emit on every request
        # (including the zero case — saturated semaphores ARE the signal;
        # the histogram's "always-near-zero" baseline is what dashboards
        # alert against). Cheap when OTel isn't installed (still gated by
        # the early _maybe_bind None-return).
        qw = inst.get("queue_wait_hist")
        if qw is not None:
            qw.record(queue_wait_ms, attributes=attrs)
    except Exception:  # noqa: BLE001 — never let telemetry kill a request
        pass


def record_stream_ttft(route_path: str, method: str, ttft_s: float) -> None:
    """Record time-to-first-chunk for a streaming response (the AI-streaming
    UX metric). Cheap None-check when OTel isn't installed. `route_path` is
    the route TEMPLATE (M15 T1 #2 — same cardinality bound as record_request);
    a streaming route is always matched, so there's no sentinel case here."""
    inst = _maybe_bind()
    if inst is None:
        return
    try:
        inst["stream_ttft"].record(
            ttft_s, attributes={"http.route": route_path, "http.method": method})
    except Exception:  # noqa: BLE001
        pass


def in_flight_delta(delta: int) -> None:
    """Increment/decrement the `requests.in_flight` UpDownCounter. Cheap
    on the no-OTel path."""
    inst = _maybe_bind()
    if inst is None:
        return
    try:
        inst["in_flight"].add(delta)
    except Exception:  # noqa: BLE001
        pass


def reset_for_tests() -> None:
    """Force-re-bind on next call. Tests only — exposes nothing to users."""
    global _state, _tenant_label_max_cached
    _state = _NOT_INITIALIZED
    # M7 T2.4: also clear the tenant-label set so each test starts fresh.
    _tenant_label_set.clear()
    _tenant_label_max_cached = None  # re-read the env cap on next call
