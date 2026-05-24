"""RED metrics auto-emit (Tier-B.B2 / research-distillation).

R-E-D = Request rate, Error rate, Duration. Per HTTP request, the ASGI
seam calls `record_request(route_path, method, status, duration_s)`.
If `opentelemetry-api` is installed in the user's env we lazy-bind the
meter on the first call and emit a counter + histogram from then on; if
it's NOT installed (typical for the small-startup default), every call is
a single cheap None-check and returns.

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
        }
    except Exception:  # noqa: BLE001 — defensive; partial OTel installs
        _state = None
    return _state


def _tenant_label_max() -> int:
    """M7 T2.4 (Hightouch pattern): cap distinct tenant labels we emit
    on metrics so a runaway-tenant-id key doesn't blow up cardinality.
    Over-cap → bucket to "_overflow_". Per-process state, set once."""
    import os
    try:
        return max(1, int(os.environ.get("LANCELOT_TENANT_LABEL_MAX", "1000")))
    except (ValueError, TypeError):
        return 1000


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
                   cost_usd: float = 0.0) -> None:
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
    except Exception:  # noqa: BLE001 — never let telemetry kill a request
        pass


def record_stream_ttft(route_path: str, method: str, ttft_s: float) -> None:
    """Record time-to-first-chunk for a streaming response (the AI-streaming
    UX metric). Cheap None-check when OTel isn't installed."""
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
    global _state
    _state = _NOT_INITIALIZED
    # M7 T2.4: also clear the tenant-label set so each test starts fresh.
    _tenant_label_set.clear()
