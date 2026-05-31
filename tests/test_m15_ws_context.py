"""M15 T10 — WebSocket ContextVar / traceparent observability seam.

Before T10 the WS handshake (`_ws`) never set ANY of the per-request
ContextVars (`request_id` / `trace_id` / `trace_parent`), so a WebSocket
connection got no request-id echo and — critically — `TransportPool`
calls made from inside a WS handler did NOT forward the socket's
`traceparent` to the upstream (the HTTP path does). T10 factors the
`_http` header-scan + ContextVar-set into a shared helper and calls it
from `_ws` inside a try/finally (set on handshake, RESET on disconnect so
nothing leaks across connections).

These are REAL-SERVER tests: a subprocess runs a uvicorn-hosted Lancelot
app with `ws="websockets"`, and a real `websockets` client drives it. The
handler reports what it observed (request_id, forwarded traceparent) back
over the socket so the test can assert on the wire.
"""
import asyncio
import signal
import socket
import subprocess
import sys
import time

import pytest

websockets = pytest.importorskip("websockets")


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _spawn(server_src: str):
    port = _free_port()
    proc = subprocess.Popen([sys.executable, "-c", server_src, str(port)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.time() + 15
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(proc.stderr.read().decode()[:800])
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    return proc, port


def _stop(proc):
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


# A WS server whose handler reports the request_id it observes via the
# `lancelot.context.request_id` ContextVar (set at handshake by T10).
_RID_SERVER = r"""
import sys
import uvicorn
import lancelot
from lancelot import context
from lancelot.websockets import WebSocket, WebSocketDisconnect

app = lancelot.App()

@app.websocket("/ctx")
async def ctx(ws: WebSocket):
    await ws.accept()
    # Report the request_id the seam set from the handshake headers.
    await ws.send_text("rid=" + context.request_id.get())
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass

uvicorn.run(app.asgi(), host="127.0.0.1", port=int(sys.argv[1]),
            log_level="warning", ws="websockets")
"""


def test_ws_request_id_echoed():
    """A WS client connecting with `x-request-id: abc123` → a handler
    reading `context.request_id` sees `abc123`. A connection WITHOUT the
    header gets a freshly-minted (non-"-") id."""
    proc, port = _spawn(_RID_SERVER)
    try:
        async def run():
            uri = f"ws://127.0.0.1:{port}/ctx"
            # (1) client supplies the request-id → handler honors it.
            async with websockets.connect(
                    uri, additional_headers={"x-request-id": "abc123"}) as ws:
                got = await ws.recv()
            # (2) no header → handler sees a minted id (not "-").
            async with websockets.connect(uri) as ws2:
                got_minted = await ws2.recv()
            return got, got_minted

        got, got_minted = asyncio.run(run())
        assert got == "rid=abc123", got
        assert got_minted.startswith("rid="), got_minted
        minted = got_minted[len("rid="):]
        assert minted not in ("", "-"), f"request_id not minted: {minted!r}"
        assert minted != "abc123", "leaked the previous connection's id!"
    finally:
        _stop(proc)


def test_ws_contextvars_reset_across_connections():
    """Two sequential WS connections must NOT leak request_id — each sees
    its own honored id or a fresh mint, never the prior connection's."""
    proc, port = _spawn(_RID_SERVER)
    try:
        async def run():
            uri = f"ws://127.0.0.1:{port}/ctx"
            async with websockets.connect(
                    uri, additional_headers={"x-request-id": "first"}) as ws:
                a = await ws.recv()
            # No header this time — must NOT inherit "first".
            async with websockets.connect(uri) as ws2:
                b = await ws2.recv()
            async with websockets.connect(
                    uri, additional_headers={"x-request-id": "third"}) as ws3:
                c = await ws3.recv()
            return a, b, c

        a, b, c = asyncio.run(run())
        assert a == "rid=first", a
        assert b != "rid=first", f"leaked across reset: {b}"
        assert b.startswith("rid=") and b[len("rid="):] not in ("", "-")
        assert c == "rid=third", c
    finally:
        _stop(proc)


# A WS server whose handler makes a real TransportPool GET to a capture
# HTTP endpoint (served by the SAME app) and reports the traceparent the
# upstream received — proving the socket's trace context is forwarded.
_TP_SERVER = r"""
import sys
import uvicorn
import lancelot
from lancelot.llm import TransportPool
from lancelot.websockets import WebSocket, WebSocketDisconnect

app = lancelot.App()
pool = TransportPool()

@app.get("/capture")
async def capture(request: lancelot.Request):
    # Echo back whatever traceparent the inbound call carried.
    return {"traceparent": request.headers.get("traceparent", "")}

@app.websocket("/up")
async def up(ws: WebSocket):
    await ws.accept()
    port = int(sys.argv[1])
    # A handler-side outbound call: T10 sets trace_parent from the WS
    # handshake, so the pool must forward it onto this GET.
    r = await pool.request("GET", f"http://127.0.0.1:{port}/capture")
    body = r.json()
    await ws.send_text("up_tp=" + (body.get("traceparent") or ""))
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass

uvicorn.run(app.asgi(), host="127.0.0.1", port=int(sys.argv[1]),
            log_level="warning", ws="websockets")
"""


def test_ws_traceparent_forwarded():
    """A WS handler making a TransportPool call forwards the socket's
    `traceparent` to the upstream (joins the distributed trace) — the same
    propagation the HTTP path already does."""
    httpx = pytest.importorskip("httpx")
    proc, port = _spawn(_TP_SERVER)
    try:
        tp = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"

        async def run():
            uri = f"ws://127.0.0.1:{port}/up"
            async with websockets.connect(
                    uri, additional_headers={"traceparent": tp}) as ws:
                return await ws.recv()

        got = asyncio.run(run())
        assert got == "up_tp=" + tp, got
    finally:
        _stop(proc)
