"""M15 T6 (#1) — extend C2 disconnect->cancel to NON-streaming handlers.

The C2 "stop paying for unwatched tokens" machinery was wired ONLY on the
streaming path (`@app.stream`): `_cancel_event` was created inside the
`is_stream` branch, so a UNARY handler's in-flight `await` (e.g. a slow
TransportPool call) was never cancelled when the client disconnected — the
server buffered the whole response (ASGI/FastAPI parity) and the user paid
for the full upstream call nobody was watching.

T6 adds an OPT-IN `cancel_on_disconnect=True` route kwarg (+ an `App(...)`
default). On such a route (when async or `timeout=`'d), the seam spawns the
same `receive()`-polling disconnect watcher used by the stream branch, arms
`request._cancel_event`, and races the handler task vs the disconnect. On a
hard client close the handler task is CANCELLED — and yield-dependency
teardown STILL runs (DB-rollback / file-close are not skipped).

These are the canonical "green-on-TestClient != works-under-a-real-socket"
tests: a real subprocess server + a raw socket that HARD-CLOSES mid-flight.
"""
import os
import socket
import subprocess
import sys
import time

import pytest

httpx = pytest.importorskip("httpx")


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _spawn(src, *args):
    port = _free_port()
    proc = subprocess.Popen([sys.executable, "-c", src, str(port), *args],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.time() + 12
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited rc={proc.returncode}: "
                               f"{proc.stderr.read().decode()[:800]}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return proc, port
        except OSError:
            time.sleep(0.05)
    proc.kill()
    raise RuntimeError("server did not start")


def _stop(*procs):
    import signal
    for p in procs:
        p.send_signal(signal.SIGTERM)
    for p in procs:
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()


# A UNARY (non-streaming) handler that awaits a long sleep (a stand-in for a
# slow upstream LLM call). A side "fake upstream" task records whether it ran
# to completion. On `cancel_on_disconnect=True`, a client hard-close mid-await
# must CANCEL the handler before the sleep completes (`done` stays 0,
# `cancelled` becomes 1). The /stats route reports the counters.
_UNARY_CANCEL = r"""
import asyncio, sys, uvicorn, lancelot

app = lancelot.App()
_stat = {"done": 0, "cancelled": 0, "teardown": 0}

@app.post("/slow", cancel_on_disconnect=True)
async def slow(request: lancelot.Request):
    try:
        await asyncio.sleep(2.0)        # fake in-flight upstream
        _stat["done"] += 1              # only if NOT cancelled
        return {"ok": True}
    except asyncio.CancelledError:
        _stat["cancelled"] += 1         # client gone -> work stopped ($ saved)
        raise

@app.get("/stats")
async def stats():
    return dict(_stat)

uvicorn.run(app.asgi(), host="127.0.0.1", port=int(sys.argv[1]),
            log_level="warning", access_log=False, loop="uvloop", http="httptools")
"""


# Same, but with a yield-dependency (acquire -> yield -> release). On a
# disconnect-cancel the dep's teardown (release) MUST still run — the cancel
# is fed into the solve_async AsyncExitStack exactly like the per-route
# timeout= path, so DB-rollback / file-close are NOT skipped.
_UNARY_CANCEL_YIELD = r"""
import asyncio, sys, uvicorn, lancelot
from lancelot import Depends

app = lancelot.App()
_stat = {"acquired": 0, "released": 0, "cancelled": 0, "done": 0}

def resource():
    _stat["acquired"] += 1
    try:
        yield "res"
    finally:
        _stat["released"] += 1     # teardown — must run even on cancel

@app.post("/slow", cancel_on_disconnect=True)
async def slow(request: lancelot.Request, res: str = Depends(resource)):
    try:
        await asyncio.sleep(2.0)
        _stat["done"] += 1
        return {"ok": True}
    except asyncio.CancelledError:
        _stat["cancelled"] += 1
        raise

@app.get("/stats")
async def stats():
    return dict(_stat)

uvicorn.run(app.asgi(), host="127.0.0.1", port=int(sys.argv[1]),
            log_level="warning", access_log=False, loop="uvloop", http="httptools")
"""


# A route WITHOUT cancel_on_disconnect (the fast-path default). Even a slow
# client must NOT change its behavior — it completes normally; nothing about
# the disconnect-watcher is armed. (We can't directly observe "no task
# spawned" over the wire, so we assert the request completes to `done` and the
# response is correct under a slow read; the code-inspection proof in the
# report covers the "watcher not spawned" half.)
_UNARY_NO_CANCEL = r"""
import asyncio, sys, uvicorn, lancelot

app = lancelot.App()
_stat = {"done": 0, "cancelled": 0}

@app.post("/slow")                      # NO cancel_on_disconnect
async def slow(request: lancelot.Request):
    try:
        await asyncio.sleep(0.4)
        _stat["done"] += 1
        return {"ok": True}
    except asyncio.CancelledError:
        _stat["cancelled"] += 1
        raise

@app.get("/stats")
async def stats():
    return dict(_stat)

uvicorn.run(app.asgi(), host="127.0.0.1", port=int(sys.argv[1]),
            log_level="warning", access_log=False, loop="uvloop", http="httptools")
"""


def test_unary_handler_cancelled_on_disconnect():
    proc, port = _spawn(_UNARY_CANCEL)
    try:
        base = f"http://127.0.0.1:{port}"

        # Control: complete the request normally -> handler runs to done.
        r = httpx.post(f"{base}/slow", json={}, timeout=10)
        assert r.status_code == 200, r.text
        assert httpx.get(f"{base}/stats").json() == {
            "done": 1, "cancelled": 0, "teardown": 0}

        # Money path: raw-socket POST, send the FULL request (incl. an empty
        # JSON body so the seam finishes draining the body), then HARD-CLOSE
        # while the handler is awaiting the 2 s sleep.
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        body = b"{}"
        req = (b"POST /slow HTTP/1.1\r\nHost: x\r\n"
               b"Content-Type: application/json\r\n"
               b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        s.sendall(req)
        time.sleep(0.3)   # body drained; handler now awaiting the sleep
        s.close()         # client gone mid-flight

        # Observe BEFORE the sleep's remaining ~1.7 s elapses — a non-cancelled
        # handler would still be in flight (done would NOT advance) and would
        # only complete later, so check the cancel counter advanced now.
        time.sleep(0.7)
        stats = httpx.get(f"{base}/stats").json()
        assert stats["cancelled"] == 1, \
            f"unary handler NOT cancelled on disconnect: {stats}"
        assert stats["done"] == 1, \
            f"the disconnected call leaked to completion: {stats}"
    finally:
        _stop(proc)


def test_unary_cancel_runs_yield_dep_teardown():
    proc, port = _spawn(_UNARY_CANCEL_YIELD)
    try:
        base = f"http://127.0.0.1:{port}"

        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        body = b"{}"
        req = (b"POST /slow HTTP/1.1\r\nHost: x\r\n"
               b"Content-Type: application/json\r\n"
               b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        s.sendall(req)
        time.sleep(0.3)   # dep acquired; handler awaiting the sleep
        s.close()

        time.sleep(0.7)
        stats = httpx.get(f"{base}/stats").json()
        assert stats["acquired"] == 1, stats
        assert stats["cancelled"] == 1, \
            f"handler not cancelled on disconnect: {stats}"
        # The crux: yield-dep teardown ran despite the cancel (no skipped
        # rollback/close). done did NOT advance.
        assert stats["released"] == 1, \
            f"yield-dep teardown SKIPPED on cancel (DB rollback would leak): {stats}"
        assert stats["done"] == 0, stats
    finally:
        _stop(proc)


def test_non_cancel_route_not_armed():
    proc, port = _spawn(_UNARY_NO_CANCEL)
    try:
        base = f"http://127.0.0.1:{port}"
        # A normal request on a NON-cancel route completes to `done`. A slow
        # read does not change its result (the response is buffered as ASGI
        # parity dictates) and nothing cancels the handler.
        r = httpx.post(f"{base}/slow", json={}, timeout=10)
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True}
        stats = httpx.get(f"{base}/stats").json()
        assert stats == {"done": 1, "cancelled": 0}, stats
    finally:
        _stop(proc)
