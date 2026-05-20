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
        }
    except Exception:  # noqa: BLE001 — defensive; partial OTel installs
        _state = None
    return _state


def record_request(route_path: str, method: str, status: int,
                   duration_s: float) -> None:
    inst = _maybe_bind()
    if inst is None:
        return
    attrs = {
        "http.route": route_path,
        "http.method": method,
        "http.status_class": f"{status // 100}xx",
    }
    try:
        inst["req_counter"].add(1, attributes=attrs)
        inst["duration_hist"].record(duration_s, attributes=attrs)
    except Exception:  # noqa: BLE001 — never let telemetry kill a request
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
