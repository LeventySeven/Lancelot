"""T2 #6b (2026-06-02 byte-path build order) — `@app.middleware("http")` streams.

The old `HTTPMiddlewareAdapter` did SYNCHRONOUS full-buffer capture: it buffered
the entire inner response, then re-emitted it as a `Response` with the DEFAULT
media type — so a streaming endpoint behind `@app.middleware("http")` came out
with a corrupted, DUPLICATED content-type (`text/event-stream, text/plain`) and
the whole body buffered (no streaming). The rewrite uses a pure-asyncio streaming
task-group model: the inner app runs as a task, `call_next` returns once the
response START arrives, and the body streams through chunk-by-chunk.

These tests pin: correct single content-type + streamed body, status/header
preservation, response mutation, and that a short-circuiting middleware (one that
never calls call_next) does NOT execute the inner handler.
"""
import lancelot
from lancelot import TestClient
from lancelot.responses import StreamingResponse


def test_http_middleware_over_stream_has_single_content_type():
    """A StreamingResponse behind @app.middleware('http') keeps exactly one,
    correct content-type (regression: the sync-capture adapter emitted
    'text/event-stream, text/plain') and delivers all chunks."""
    app = lancelot.App()

    @app.middleware("http")
    async def passthrough(request, call_next):
        return await call_next(request)

    @app.get("/sse")
    def sse(request: lancelot.Request):
        async def gen():
            for i in range(3):
                yield f"data: {i}\n\n".encode()
        return StreamingResponse(gen(), media_type="text/event-stream")

    with TestClient(app) as c:
        r = c.get("/sse")
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/event-stream", r.headers["content-type"]
    assert r.content == b"data: 0\n\ndata: 1\n\ndata: 2\n\n"


def test_http_middleware_preserves_inner_status_and_header():
    """call_next surfaces the inner response's status code and headers to the
    middleware fn (so it can read/mutate them)."""
    app = lancelot.App()

    @app.middleware("http")
    async def stamp(request, call_next):
        resp = await call_next(request)
        resp.headers["x-mw"] = "1"
        return resp

    @app.get("/made")
    def made(request: lancelot.Request):
        from lancelot.responses import JSONResponse
        return JSONResponse({"ok": True}, status_code=201)

    with TestClient(app) as c:
        r = c.get("/made")
    assert r.status_code == 201
    assert r.json() == {"ok": True}
    assert r.headers.get("x-mw") == "1"


def test_http_middleware_short_circuit_does_not_run_inner():
    """A middleware that returns its own Response WITHOUT calling call_next must
    NOT execute the inner handler (auth-reject pattern)."""
    ran = {"inner": False}
    app = lancelot.App()

    @app.middleware("http")
    async def gate(request, call_next):
        from lancelot.responses import JSONResponse
        return JSONResponse({"denied": True}, status_code=403)

    @app.get("/secret")
    def secret(request: lancelot.Request):
        ran["inner"] = True
        return {"ok": True}

    with TestClient(app) as c:
        r = c.get("/secret")
    assert r.status_code == 403 and r.json() == {"denied": True}
    assert ran["inner"] is False, "inner handler ran despite short-circuit"


def test_http_middleware_unary_response_single_content_type():
    """A normal JSON route behind @app.middleware('http') still has exactly one
    content-type and the right body (no regression for the common case)."""
    app = lancelot.App()

    @app.middleware("http")
    async def passthrough(request, call_next):
        return await call_next(request)

    @app.get("/j")
    def j(request: lancelot.Request):
        return {"ok": True}

    with TestClient(app) as c:
        r = c.get("/j")
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert r.headers["content-type"] == "application/json"
