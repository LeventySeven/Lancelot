"""M7 T4.8 — per-tenant concurrency limiter middleware (Turbopuffer pattern).

Limits how many concurrent requests a single tenant can have in-flight.
Surfaces the wait time as `lancelot.http.queue_wait_ms` (via the
`queue_wait_ms` ContextVar), so dashboards see when a tenant is hitting
its cap — the missing third number alongside RED + TTFT for multi-tenant
AI backends.

Cardinal rule: this is OPT-IN. The framework ships:
  - The `queue_wait_ms` ContextVar (`lancelot.context`)
  - The metric emission (`lancelot.metrics.record_request`)
  - This reference middleware that any handler-side limiter pattern can
    follow (your own auth-aware version goes here)

Wiring (typical):
    app = lancelot.App()
    app.add_middleware(TenantConcurrencyLimiter,
                       max_concurrent_per_tenant=10,
                       label_header="x-tenant-id")

Tenant identity comes from the configurable header (default
`X-Tenant-Id`); unset = single shared "_anon_" bucket. Per-tenant
`asyncio.Semaphore` is lazy-created on first sight. Cap is read once
per request (cheap), so env-tuning at startup is fine.
"""
import asyncio
import os
import time
from typing import Dict


def _env_default_cap() -> int:
    """LANCELOT_TENANT_CONCURRENCY override (per-process). 0 disables
    the cap (the middleware acts as a no-op, only recording 0.0
    queue_wait_ms). Default 10 — generous but bounded."""
    try:
        v = int(os.environ.get("LANCELOT_TENANT_CONCURRENCY", "10"))
        return max(0, v)
    except (ValueError, TypeError):
        return 10


class TenantConcurrencyLimiter:
    """Pure-ASGI middleware: limits concurrent requests per tenant.

    Args:
        app: the inner ASGI app (set by add_middleware).
        max_concurrent_per_tenant: max in-flight per tenant.
            0 → disabled (passthrough, only records 0.0 queue_wait_ms).
            If None, reads from `LANCELOT_TENANT_CONCURRENCY` env (default 10).
        label_header: HTTP header to read the tenant id from
            (case-insensitive). Default `x-tenant-id`.
        anon_label: sentinel for missing header. Default `_anon_`.

    Why a per-tenant Semaphore (not a single global with key):
        asyncio.Semaphore is the canonical knee-of-the-curve primitive
        for per-tenant throttling; a dict[tenant → Semaphore] is the
        smallest correct model. The dict grows unbounded BY DESIGN —
        cardinality control is the OPERATOR's job (set the label_header
        to your auth-pinned tenant id, not raw user input). Pair with
        the `_bounded_tenant` cap on the OTel-emit side for metric
        cardinality control (M7 T2.4).
    """

    def __init__(self, app, *,
                 max_concurrent_per_tenant: int | None = None,
                 label_header: str = "x-tenant-id",
                 anon_label: str = "_anon_"):
        self.app = app
        self.cap = (_env_default_cap()
                    if max_concurrent_per_tenant is None
                    else max(0, max_concurrent_per_tenant))
        self.header_b = label_header.lower().encode("ascii")
        self.anon = anon_label
        # Lazy semaphore map. asyncio.Semaphore is not picklable but
        # never crosses a process boundary — fine for our multi-proc
        # SO_REUSEPORT deploy model.
        self._sems: Dict[str, asyncio.Semaphore] = {}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or self.cap == 0:
            # Lifespan/websocket pass through; cap=0 → disabled
            # (still important: keeps queue_wait_ms at 0.0 baseline).
            return await self.app(scope, receive, send)

        # Read tenant from header (single pass, case-insensitive).
        tenant = self.anon
        for k, v in scope.get("headers", ()):
            if k == self.header_b:
                try:
                    tenant = v.decode("latin1").strip() or self.anon
                except Exception:  # noqa: BLE001
                    tenant = self.anon
                break

        sem = self._sems.get(tenant)
        if sem is None:
            sem = asyncio.Semaphore(self.cap)
            self._sems[tenant] = sem

        t_acq_start = time.perf_counter()
        async with sem:
            wait_ms = (time.perf_counter() - t_acq_start) * 1000.0
            # Publish queue_wait + tenant ContextVars so the ASGI seam's
            # record_request emit (in `finally`) picks them up.
            from lancelot.context import (
                queue_wait_ms as _qw_var,
                tenant_id as _tid_var,
            )
            qw_token = _qw_var.set(wait_ms)
            tid_token = _tid_var.set(tenant)
            try:
                await self.app(scope, receive, send)
            finally:
                _qw_var.reset(qw_token)
                _tid_var.reset(tid_token)


__all__ = ["TenantConcurrencyLimiter"]
