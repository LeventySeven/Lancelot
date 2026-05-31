"""M16 P8 — `pool.stream` disconnect-cancel + `pool.relay` upstream->client relay.

Confirmed gap (CLAUDE.md cardinal rule #2): `TransportPool.stream(method, url,
**kw)` took only `**kw` — NO `request=` param, NO `_await_cancellable` wrap —
unlike `request()` which wraps its call so a client disconnect aborts it. So a
client disconnecting mid-stream did NOT abort the upstream stream: the user kept
paying for tokens nobody was watching (the relay-path analogue of the unary C2
cancel). There was also no open+forward+cleanup helper.

Fix (pure Python — NO Rust pump variant; that per-chunk path is Tier-D, the
stream is upstream-rate-limited):
  * `pool.stream(method, url, *, request=req, **kw)` — when `request=` is bound,
    the stream's chunk iteration races against the request's `_cancel_event`
    (set by the seam on client disconnect), and the upstream is closed.
  * `pool.relay(method, url, *, request=req)` — an async-context-manager that
    opens the upstream stream, forwards chunks via the shipped
    `idle_guarded_chunks`, cancels on client disconnect, and `aclose()`s the
    upstream in `finally`.

The disconnect test is a real subprocess server + a raw socket that HARD-CLOSES
mid-relay; a fake slow "upstream" (an httpx.MockTransport streaming an endless
body) records via a sidecar whether it ran to completion / was cancelled +
whether the relay's cleanup ran. Mirrors tests/test_m15_unary_cancel.py.
"""
import asyncio
import os
import signal
import socket
import subprocess
import sys
import time

import pytest

httpx = pytest.importorskip("httpx")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _spawn(src: str):
    port = _free_port()
    proc = subprocess.Popen([sys.executable, "-c", src, str(port)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.time() + 12
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited rc={proc.returncode}: "
                               f"{proc.stderr.read().decode()[:1000]}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return proc, port
        except OSError:
            time.sleep(0.05)
    proc.kill()
    raise RuntimeError("server did not start")


def _stop(*procs):
    for p in procs:
        p.send_signal(signal.SIGTERM)
    for p in procs:
        try:
            p.communicate(timeout=6)
        except subprocess.TimeoutExpired:
            p.kill()
            p.communicate()


# A relay handler streaming from a fake slow upstream. The "upstream" is an
# httpx async byte stream that emits a chunk every 0.1s for a long time and
# records its furthest-reached index to a sidecar file; its `finally` records
# that it was torn down. The relay handler uses `pool.relay(...)` so a client
# disconnect mid-relay must CANCEL the upstream (index stops advancing) and run
# the upstream's `finally` (aclose -> the generator's finally fires).
_RELAY_CANCEL = r"""
import sys, asyncio, lancelot, httpx

PROGRESS = sys.argv[1] + ".progress"
CLOSED = sys.argv[1] + ".closed"

async def _upstream_body():
    # Endless slow token feed; records progress + a teardown marker.
    try:
        i = 0
        while True:
            yield f"data: tok{i}\n\n".encode()
            with open(PROGRESS, "w") as f:
                f.write(str(i))
            i += 1
            await asyncio.sleep(0.1)
    finally:
        with open(CLOSED, "w") as f:
            f.write("upstream-closed")

def _handler(request):
    # The fake upstream server: any request gets the endless streaming body.
    return httpx.Response(200, content=_upstream_body(),
                          headers={"content-type": "text/event-stream"})

pool = lancelot.llm.TransportPool(transport=httpx.MockTransport(_handler))

app = lancelot.App()

@app.stream("/relay")
async def relay(request: lancelot.Request):
    # The relay primitive: open upstream, forward chunks, cancel-on-disconnect,
    # aclose() upstream in finally.
    async with pool.relay("POST", "https://upstream.invalid/v1/stream",
                          request=request) as upstream:
        async for chunk in upstream:
            yield chunk

@app.get("/ping")
async def ping(request: lancelot.Request):
    return {"ok": True}

import uvicorn
uvicorn.run(app.asgi(), host="127.0.0.1", port=int(sys.argv[1]),
            log_level="warning", access_log=False, loop="uvloop", http="httptools")
"""


def test_pool_stream_cancels_on_client_disconnect():
    """Client disconnect mid-relay -> the upstream stream is CANCELLED (its
    index stops advancing) and the upstream's finally (aclose) RAN. Without the
    fix the relay keeps pulling upstream tokens nobody is watching."""
    proc, port = _spawn(_RELAY_CANCEL)
    progress = f"{port}.progress"
    closed = f"{port}.closed"
    for p in (progress, closed):
        try:
            os.remove(p)
        except OSError:
            pass
    try:
        # Open the relay on a raw socket, let a few chunks flow, then HARD-CLOSE.
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        s.sendall(b"GET /relay HTTP/1.1\r\nHost: x\r\n"
                  b"Accept: text/event-stream\r\n\r\n")
        time.sleep(0.35)               # ~3 upstream chunks flow
        s.setblocking(False)
        try:
            s.recv(65536)
        except BlockingIOError:
            pass
        s.close()                      # client gone mid-relay

        # Capture the upstream index just after the disconnect, then later: it
        # must STOP advancing (the upstream was cancelled), not run forever.
        time.sleep(0.2)
        try:
            with open(progress) as f:
                after = int(f.read())
        except (OSError, ValueError):
            after = -1
        time.sleep(0.8)                # ~8 more upstream chunks if still alive
        try:
            with open(progress) as f:
                later = int(f.read())
        except (OSError, ValueError):
            later = -1
        assert after >= 0, "upstream never started"
        assert later - after <= 1, (
            f"upstream kept producing after client disconnect "
            f"(NOT cancelled): {after} -> {later}")
        # The upstream's finally ran -> aclose propagated through the relay.
        assert os.path.exists(closed), (
            "upstream aclose/finally did NOT run on client disconnect "
            "(the relay leaked the upstream stream)")
        # The server still serves the next request (the seam didn't wedge).
        r = httpx.get(f"http://127.0.0.1:{port}/ping", timeout=5)
        assert r.status_code == 200 and r.json() == {"ok": True}
    finally:
        _stop(proc)
        for p in (progress, closed):
            try:
                os.remove(p)
            except OSError:
                pass


# ---- in-process unit tests for the relay/forward happy path + regression ----

class _FakeStreamCtx:
    """A stand-in async-CM matching httpx's `client.stream(...)` return: it
    enters to a response exposing `aiter_bytes()`. Records aclose via the
    response items' generator finally."""
    def __init__(self, items, closed_flag):
        self._items = items
        self._closed = closed_flag

    async def __aenter__(self):
        outer = self

        class _Resp:
            headers = {"content-type": "text/event-stream"}

            async def aiter_bytes(self):
                try:
                    for it, delay in outer._items:
                        if delay:
                            await asyncio.sleep(delay)
                        yield it
                finally:
                    outer._closed["closed"] = True
        return _Resp()

    async def __aexit__(self, *exc):
        return False


def test_relay_forwards_and_closes():
    """pool.relay forwards ALL chunks to the client on the happy path and
    aclose()s the upstream at the end (the upstream generator's finally runs)."""
    from lancelot.llm import TransportPool

    closed = {"closed": False}

    async def go():
        pool = TransportPool(transport=httpx.MockTransport(
            lambda req: httpx.Response(200)))
        # Monkeypatch pool.stream to hand back our fake stream CM so we can
        # observe forwarding + aclose without real I/O.
        items = [(b"a", 0), (b"b", 0), (b"c", 0)]

        async def _fake_stream(method, url, *, request=None, **kw):
            return _FakeStreamCtx(items, closed)
        pool.stream = _fake_stream

        out = []
        async with pool.relay("GET", "https://x.invalid", request=None) as up:
            async for chunk in up:
                out.append(chunk)
        await pool.aclose()
        return out

    out = asyncio.run(go())
    assert out == [b"a", b"b", b"c"], out
    assert closed["closed"], "relay did not aclose the upstream on clean end"


def test_pool_stream_request_none_unchanged():
    """Regression: `pool.stream(...)` with NO request= behaves exactly as today
    — it returns an async-CM that enters to an httpx response we can
    aiter_bytes(). (No cancel wiring, zero behavior change.)"""
    from lancelot.llm import TransportPool

    async def go():
        def _handler(req):
            return httpx.Response(200, content=b"hello-stream",
                                  headers={"content-type": "text/plain"})
        pool = TransportPool(transport=httpx.MockTransport(_handler))
        cm = await pool.stream("GET", "https://x.invalid")
        chunks = []
        async with cm as r:
            async for c in r.aiter_bytes():
                chunks.append(c)
        await pool.aclose()
        return b"".join(chunks)

    assert asyncio.run(go()) == b"hello-stream"
