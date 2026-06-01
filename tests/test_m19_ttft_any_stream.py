"""T1 #3 (2026-06-02 byte-path build order) — TTFT attaches to ANY streamed body.

Verified gap (founder workflow: LangServe/Langflow/R2R/LitServe): `record_stream_ttft`
only fired inside Lancelot's own `@app.stream` seam (asgi.py `_flush_serialized`), so
a handler returning a user-instantiated `StreamingResponse` / `sse_starlette`
`EventSourceResponse` — the shape these products actually ship — emitted NO TTFT.

Fix: a transparent `send` wrapper records TTFT on the first streamed body chunk for
any streaming response, defined as content-type `text/event-stream` OR a first
`http.response.body` with `more_body=True`. Honest semantics: this is wall-clock to
first-framework-byte (incl. queue wait), NOT true upstream LLM time-to-first-token.

Unit-level (in-process ASGI): we monkeypatch `lancelot.metrics.record_stream_ttft`
to capture the call — this directly asserts the hook fires on the bypass paths
without needing an OTel pipeline, and the wrapper adds no transport behavior
(byte-passthrough), so the existing real-server streaming suites still guard load.
"""
import lancelot
from lancelot import TestClient
from lancelot.responses import StreamingResponse


def _patch_ttft(monkeypatch):
    calls = []

    def _rec(route_path, method, ttft_s):
        calls.append((route_path, method, ttft_s))

    monkeypatch.setattr("lancelot.metrics.record_stream_ttft", _rec)
    return calls


def test_ttft_recorded_for_user_streamingresponse(monkeypatch):
    """A handler returning a StreamingResponse (text/event-stream) — which
    bypasses the @app.stream seam — records TTFT once with the route template,
    and the streamed bytes are delivered intact."""
    calls = _patch_ttft(monkeypatch)
    app = lancelot.App()

    @app.get("/sse")
    def sse(request: lancelot.Request):
        async def gen():
            yield b"data: one\n\n"
            yield b"data: two\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    with TestClient(app) as client:
        r = client.get("/sse")
    assert r.status_code == 200
    assert b"data: one\n\n" in r.content and b"data: two\n\n" in r.content
    assert len(calls) == 1, f"expected exactly one TTFT record, got {calls}"
    route, method, ttft = calls[0]
    assert route == "/sse" and method == "GET"
    assert ttft >= 0.0


def test_ttft_recorded_for_delegated_asgi_streaming_response(monkeypatch):
    """A returned ASGI-callable response with NO body_iterator (the
    sse_starlette EventSourceResponse shape, delegated via `await result(...)`)
    also gets TTFT via the send wrapper."""
    calls = _patch_ttft(monkeypatch)
    app = lancelot.App()

    class _FakeEventSource:
        # Looks like a Starlette ASGI-callable response: body is None, callable,
        # has media_type, exposes NO body_iterator attribute at all.
        media_type = "text/event-stream"
        body = None
        status_code = 200
        headers = {}

        async def __call__(self, scope, receive, send):
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"text/event-stream")]})
            await send({"type": "http.response.body",
                        "body": b"data: hi\n\n", "more_body": True})
            await send({"type": "http.response.body",
                        "body": b"", "more_body": False})

    @app.get("/es")
    def es(request: lancelot.Request):
        return _FakeEventSource()

    with TestClient(app) as client:
        r = client.get("/es")
    assert r.status_code == 200
    assert b"data: hi\n\n" in r.content
    assert len(calls) == 1, f"expected one TTFT record on delegation, got {calls}"
    assert calls[0][0] == "/es" and calls[0][1] == "GET"


def test_no_ttft_for_unary_response(monkeypatch):
    """A normal unary handler (single non-streaming body) records NO TTFT —
    the hook must not fire on every response or pollute the metric."""
    calls = _patch_ttft(monkeypatch)
    app = lancelot.App()

    @app.get("/unary")
    def unary(request: lancelot.Request):
        return {"ok": True}

    with TestClient(app) as client:
        r = client.get("/unary")
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert calls == [], f"TTFT must not fire for a unary response; got {calls}"
