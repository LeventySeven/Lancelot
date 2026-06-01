"""T2 #6a (2026-06-02 byte-path build order) — `add_middleware(BaseHTTPMiddleware)`
is supported (regression lock-in).

The #1 cited migration blocker across the founder panel was BaseHTTPMiddleware
for per-request scoping (Bitcart dishka HTTPContainerMiddleware, R2R tenant
ProjectSchemaMiddleware, Langflow middlewares, Prefect CSRF). It turned out the
ASGI-wrapping path (`app = mw_cls(app, **opts)`) ALREADY hosts a Starlette
BaseHTTPMiddleware subclass correctly — it was only mislabeled "NEVER" and
untested. These tests lock in the three properties the founders actually need:
response mutation, streaming pass-through, and per-request scope-state reaching
the handler.
"""
import pytest

import lancelot
from lancelot import TestClient
from lancelot.responses import StreamingResponse

base = pytest.importorskip("starlette.middleware.base")
BaseHTTPMiddleware = base.BaseHTTPMiddleware


def test_basehttp_middleware_mutates_response_headers():
    class HdrMW(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            resp = await call_next(request)
            resp.headers["X-MW"] = "seen"
            return resp

    app = lancelot.App()
    app.add_middleware(HdrMW)

    @app.get("/j")
    def j(request: lancelot.Request):
        return {"ok": True}

    with TestClient(app) as c:
        r = c.get("/j")
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert r.headers.get("x-mw") == "seen"


def test_basehttp_middleware_streams_through():
    """A streaming endpoint behind add_middleware(BaseHTTPMiddleware) delivers
    all chunks intact (Starlette's BaseHTTPMiddleware streams, doesn't buffer)."""
    class PassMW(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            return await call_next(request)

    app = lancelot.App()
    app.add_middleware(PassMW)

    @app.get("/sse")
    def sse(request: lancelot.Request):
        async def gen():
            for i in range(3):
                yield f"data: {i}\n\n".encode()
        return StreamingResponse(gen(), media_type="text/event-stream")

    with TestClient(app) as c:
        r = c.get("/sse")
    assert r.status_code == 200
    assert r.content == b"data: 0\n\ndata: 1\n\ndata: 2\n\n"


def test_basehttp_middleware_scope_state_reaches_handler():
    """The founders' per-request-scoping pattern: a BaseHTTPMiddleware writes
    request.state (→ ASGI scope['state']); the Lancelot handler reads it via
    scope_state. This is the dishka/tenant/CSRF shape that gates migration."""
    class TenantMW(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.tenant = "acme"
            return await call_next(request)

    app = lancelot.App()
    app.add_middleware(TenantMW)

    @app.get("/who")
    def who(request: lancelot.Request):
        st = getattr(request, "scope_state", None) or {}
        return {"tenant": st.get("tenant")}

    with TestClient(app) as c:
        r = c.get("/who")
    assert r.status_code == 200 and r.json() == {"tenant": "acme"}
