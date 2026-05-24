"""M7 T2.3 — fleet rollup + region tagging (Conductor pattern).

Conductor's "head of engineering layer aggregates status across all
running agents, surfaces exceptions and decisions that need human
input" — for the multi-process / multi-region Lancelot deploy, this is
the equivalent: each worker exposes `/fleet/status` with its region +
worker_id + in-flight count + drain state, so a thin aggregator can
roll up the fleet without speaking per-worker OTel.

What ships:
  - `LANCELOT_REGION` env (default `default`) — set per deploy.
  - `LANCELOT_WORKER_ID` env (default = process PID) — set per worker
    when using a process manager.
  - `lancelot.fleet.fleet_info(app)` — returns the JSON the
    `/fleet/status` endpoint serves.
  - `lancelot.fleet.region()` / `worker_id()` — read once, cheap.
  - OTel resource attributes: when the user has opentelemetry-sdk
    installed, the Lancelot meter provider gets `lancelot.region` +
    `lancelot.worker_id` as resource attributes (so EVERY metric is
    tagged with the region without per-emit overhead).
"""
import os
import time

# Once-per-process snapshot; reading env is cheap but constant.
_REGION: str | None = None
_WORKER_ID: str | None = None
_STARTED_AT: float = time.time()


def region() -> str:
    """Return this worker's region (env `LANCELOT_REGION`, default
    'default'). Cached process-wide; first call wins."""
    global _REGION
    if _REGION is None:
        _REGION = os.environ.get("LANCELOT_REGION", "default") or "default"
    return _REGION


def worker_id() -> str:
    """Return this worker's id. Default is the process PID as a string.
    Operators set `LANCELOT_WORKER_ID` when running multiple workers
    behind a load balancer."""
    global _WORKER_ID
    if _WORKER_ID is None:
        _WORKER_ID = os.environ.get("LANCELOT_WORKER_ID") or str(os.getpid())
    return _WORKER_ID


def started_at() -> float:
    """Unix epoch seconds when this worker started. Used by fleet rollup
    to surface "this worker has been up for X seconds" — useful for
    rolling-restart progress monitoring."""
    return _STARTED_AT


def fleet_info(app) -> dict:
    """One process's snapshot for the `/fleet/status` endpoint.

    Shape:
        {
          "region": "us-east-1",
          "worker_id": "12345",
          "status": "ok" | "draining",
          "in_flight": 42,
          "uptime_s": 12345.0,
          "lancelot_version": "0.0.0",
        }

    The aggregator (the operator's choice — HAProxy/Lua, nginx, or a
    purpose-built dashboard) GETs /fleet/status on each worker and
    rolls up the fleet view.
    """
    try:
        in_flight = int(getattr(app, "_active_requests", 0))
    except Exception:  # noqa: BLE001
        in_flight = 0
    try:
        draining = bool(getattr(app, "_shutting_down", False))
    except Exception:  # noqa: BLE001
        draining = False
    return {
        "region": region(),
        "worker_id": worker_id(),
        "status": "draining" if draining else "ok",
        "in_flight": in_flight,
        "uptime_s": round(time.time() - _STARTED_AT, 3),
        "lancelot_version": _lancelot_version(),
    }


def _lancelot_version() -> str:
    try:
        import lancelot
        return getattr(lancelot, "__version__", "0.0.0")
    except Exception:  # noqa: BLE001
        return "0.0.0"


def install_otel_resource_attrs():
    """Best-effort: when the user has opentelemetry-sdk installed AND
    the Lancelot meter provider was created via `set_meter_provider`,
    monkey-patch the resource attributes to include region + worker_id.

    This is intentionally NOT auto-called — it requires the user to
    have already installed their OTel pipeline. Documented in
    docs/operator/runbook.md as the wiring step."""
    try:
        from opentelemetry import metrics as om
        from opentelemetry.sdk.resources import Resource
        provider = om.get_meter_provider()
        # The SDK's MeterProvider has a `_resource` attribute. If we're
        # talking to the API stub (no SDK), this attribute doesn't exist
        # — silently skip.
        existing = getattr(provider, "_resource", None)
        if existing is None:
            return False
        merged = existing.merge(Resource.create({
            "lancelot.region": region(),
            "lancelot.worker_id": worker_id(),
        }))
        provider._resource = merged
        return True
    except Exception:  # noqa: BLE001
        return False


def reset_for_tests() -> None:
    """Test-only: reset cached region + worker_id so per-test env
    overrides take effect."""
    global _REGION, _WORKER_ID
    _REGION = None
    _WORKER_ID = None


__all__ = [
    "region", "worker_id", "started_at", "fleet_info",
    "install_otel_resource_attrs", "reset_for_tests",
]
