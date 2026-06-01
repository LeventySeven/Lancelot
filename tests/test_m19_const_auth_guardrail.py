"""T1 #2 (2026-06-02 byte-path build order) — const-tier auth-bypass guardrail.

Verified footgun (founder workflow: Prefect /health behind a Basic-auth allowlist,
Letta /health behind CheckPasswordMiddleware): a `const_route` is served prebaked
from Rust under `App.run()` ZERO-GIL — BEFORE the ASGI middleware stack — so it
bypasses EVERY middleware, including auth/allowlist. A const health/static path
silently strips the auth check → a regression, not a win.

Guardrail: when const routes coexist with middleware, `run()` warns (default) or
raises (strict, `LANCELOT_CONST_STRICT`) naming the shadowed paths + middleware,
so a prebaked path can't silently become unauthenticated.
"""
import warnings

import pytest

import lancelot
from lancelot.app import (
    ConstRouteAuthBypassWarning,
    _audit_const_route_middleware_bypass,
)


class _DummyAuthMiddleware:
    def __init__(self, app, **opts):
        self.app = app


def test_const_route_with_add_middleware_warns():
    """const_route + add_middleware → warns (ConstRouteAuthBypassWarning) and
    reports the shadowed const path."""
    app = lancelot.App()
    app.add_middleware(_DummyAuthMiddleware)
    app.const_route("GET", "/health", b'{"ok":true}', "application/json")

    with pytest.warns(ConstRouteAuthBypassWarning, match="/health"):
        shadowed = _audit_const_route_middleware_bypass(app, strict=False)
    assert ("GET", "/health") in shadowed


def test_const_route_with_http_middleware_warns():
    """const_route + @app.middleware('http') also trips the guardrail."""
    app = lancelot.App()

    @app.middleware("http")
    async def _mw(request, call_next):
        return await call_next(request)

    app.const_route("GET", "/static.js", b"x", "text/javascript")
    with pytest.warns(ConstRouteAuthBypassWarning):
        shadowed = _audit_const_route_middleware_bypass(app, strict=False)
    assert ("GET", "/static.js") in shadowed


def test_const_route_strict_raises():
    """strict=True (LANCELOT_CONST_STRICT) → raise instead of warn, so a
    misconfigured deploy fails closed rather than serving an unauthenticated
    prebaked path."""
    app = lancelot.App()
    app.add_middleware(_DummyAuthMiddleware)
    app.const_route("GET", "/health", b"ok", "text/plain")
    with pytest.raises(RuntimeError, match="auth"):
        _audit_const_route_middleware_bypass(app, strict=True)


def test_const_route_without_middleware_is_silent():
    """const_route but NO middleware → nothing to bypass → no warning, []."""
    app = lancelot.App()
    app.const_route("GET", "/health", b"ok", "text/plain")
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # any warning would fail the test
        shadowed = _audit_const_route_middleware_bypass(app, strict=False)
    assert shadowed == []


def test_middleware_without_const_route_is_silent():
    """Middleware but no const route → nothing prebaked → no warning, []."""
    app = lancelot.App()
    app.add_middleware(_DummyAuthMiddleware)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        shadowed = _audit_const_route_middleware_bypass(app, strict=False)
    assert shadowed == []
