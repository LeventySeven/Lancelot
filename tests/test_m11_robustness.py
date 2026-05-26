"""M11 — robustness "does-not-break-like-FastAPI" fixes (2026-05-26 sweep).

  R1 — StreamingResponse(..., background=fn): the response-level background
       MUST run after the body is sent (Starlette parity). Lancelot's seam
       only ran the DI-injected BackgroundTasks, silently dropping cleanup
       (temp files, DB sessions, audit logs).
  R2 — @app.stream finally must not let a send() on a broken socket mask the
       CancelledError that signals client disconnect (corrupts yield-dep
       teardown exc_info + the cancellation signal).
"""
import asyncio

import pytest

import lancelot
from lancelot.responses import StreamingResponse
from lancelot.testclient import TestClient


# ---------- R1: StreamingResponse.background runs -----------------------

def test_r1_streaming_response_background_runs():
    app = lancelot.App()
    log = []

    async def gen():
        yield b"data: a\n\n"
        yield b"data: b\n\n"

    def cleanup():
        log.append("cleaned")

    @app.get("/s")
    def s():
        return StreamingResponse(gen(), media_type="text/event-stream",
                                 background=cleanup)

    with TestClient(app) as client:
        r = client.get("/s")
        assert r.status_code == 200
        assert b"data: a\n\n" in r.content
    assert log == ["cleaned"], (
        "StreamingResponse.background must run after the body is sent")


def test_r1_streaming_response_async_background_runs():
    app = lancelot.App()
    log = []

    async def gen():
        yield b"x"

    async def acleanup():
        log.append("async-cleaned")

    @app.get("/s")
    def s():
        return StreamingResponse(gen(), background=acleanup)

    with TestClient(app) as client:
        assert client.get("/s").status_code == 200
    assert log == ["async-cleaned"]


# ---------- R2: stream finally must not mask CancelledError -------------

def _drive_stream_with_failing_eof(app, path):
    """Drive a stream route where the client connection breaks: send() raises
    ConnectionResetError on the terminating (more_body=False) frame. Returns
    the exception type that propagated out of the app."""
    asgi = app.asgi()
    scope = {"type": "http", "method": "GET", "path": path,
             "raw_path": path.encode(), "query_string": b"", "headers": [],
             "client": ("127.0.0.1", 5), "server": ("127.0.0.1", 80),
             "scheme": "http"}
    state = {"body_sent": False}

    async def receive():
        if not state["body_sent"]:
            state["body_sent"] = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.sleep(3600)

    async def send(m):
        if m.get("type") == "http.response.body" and m.get("more_body") is False:
            raise ConnectionResetError("client gone")

    captured = {"exc": None}

    async def go():
        try:
            await asgi(scope, receive, send)
        except BaseException as e:  # noqa: BLE001
            captured["exc"] = e

    asyncio.run(go())
    return captured["exc"]


def test_r2_stream_finally_does_not_mask_cancellederror():
    app = lancelot.App()

    @app.stream("/s")
    async def s():
        yield b"data: chunk\n\n"
        raise asyncio.CancelledError()  # simulate client-disconnect cancel

    exc = _drive_stream_with_failing_eof(app, "/s")
    # The CancelledError must survive — NOT be replaced by the
    # ConnectionResetError raised by send() in the finally.
    assert isinstance(exc, asyncio.CancelledError), (
        f"finally's send() masked the cancellation: got {type(exc).__name__}")
