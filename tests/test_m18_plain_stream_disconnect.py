"""M18 follow-up (Task a) — a PLAIN `@app.stream` async-gen is proactively
CANCELLED on client disconnect on the uvicorn/ASGI seam.

Confirmed gap (CLAUDE.md cardinal rule #2): the stream branch's `_consume()`
task is cancelled by the watchdog on a `max_stream_silence` breach, and the
relay path (`idle_guarded_chunks`) races the request's `_cancel_event` per
chunk — BUT a PLAIN stream (a handler that just `yield`s data, no relay) had
NOTHING acting on `_cancel_event`. So a client hangup mid plain-stream did NOT
cancel the generator: it kept running (and, e.g., kept driving an upstream
nobody was watching) until a later `send()` happened to fail.

Fix: a ONE-TIME (per-stream, NOT per-chunk) event-driven watcher awaits the
request's `_cancel_event` (set by the disconnect watcher) and cancels the
`_consume_task` — so the suspended `__anext__` raises CancelledError, throwing
into the generator at its yield point so its `finally`/`aclose` RUNS. This must
NOT add a per-chunk `wait_for`/race (that would reintroduce the M6-T1.1 tax).
A client-disconnect cancel emits NO SSE error frame (the client is gone — M17
P6: only an internal silence-abort emits one).

These are REAL-SERVER tests: a subprocess uvicorn + a raw socket that HARD-
CLOSES mid-stream; a sidecar file records whether the generator's `finally`
ran (cancelled) vs whether it ran to natural completion. Mirrors
tests/test_m16_stream_silence.py / tests/test_m16_relay_cancel.py.
"""
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


# A PLAIN stream that yields a data chunk every 0.1s FOREVER. Because real DATA
# keeps flowing, the silence-abort watchdog never fires (this isolates the
# disconnect-cancel path from the M16 silence path). A sidecar records the
# generator's furthest-reached index + a marker for HOW it ended:
#   "cancelled" -> the finally ran via a CancelledError (client disconnect)
#   "completed" -> the generator returned normally (must NOT happen here)
# Keepalive is OFF (sse_keepalive=0) so the ONLY thing that can cancel the gen
# on disconnect is the new one-time cancel watcher (NOT the keepalive watchdog).
_PLAIN_FOREVER = r"""
import sys, asyncio, lancelot

app = lancelot.App()
PROGRESS = sys.argv[1] + ".progress"
ENDED = sys.argv[1] + ".ended"

@app.stream("/forever", sse_keepalive=0)
async def forever(request: lancelot.Request):
    i = 0
    cancelled = False
    try:
        while True:
            yield f"data: tok{i}\n\n".encode()
            with open(PROGRESS, "w") as f:
                f.write(str(i))
            i += 1
            await asyncio.sleep(0.1)
    except asyncio.CancelledError:
        cancelled = True
        raise
    finally:
        with open(ENDED, "w") as f:
            f.write("cancelled" if cancelled else "completed")

@app.get("/ping")
async def ping(request: lancelot.Request):
    return {"ok": True}

import uvicorn
uvicorn.run(app.asgi(), host="127.0.0.1", port=int(sys.argv[1]),
            log_level="warning", access_log=False, loop="uvloop", http="httptools")
"""


# A PLAIN stream that emits a FIXED, small number of chunks and then returns
# normally. Regression guard: the new disconnect-cancel watcher must NOT touch
# a stream the client reads to completion — every chunk is delivered and the
# generator's finally runs via natural completion ("completed", not
# "cancelled").
_PLAIN_FINITE = r"""
import sys, asyncio, lancelot

app = lancelot.App()
ENDED = sys.argv[1] + ".ended"

@app.stream("/finite", sse_keepalive=0)
async def finite(request: lancelot.Request):
    cancelled = False
    try:
        for i in range(5):
            yield f"data: tok{i}\n\n".encode()
            await asyncio.sleep(0.02)
    except asyncio.CancelledError:
        cancelled = True
        raise
    finally:
        with open(ENDED, "w") as f:
            f.write("cancelled" if cancelled else "completed")

import uvicorn
uvicorn.run(app.asgi(), host="127.0.0.1", port=int(sys.argv[1]),
            log_level="warning", access_log=False, loop="uvloop", http="httptools")
"""


def test_plain_stream_gen_cancelled_on_client_disconnect():
    """A PLAIN @app.stream gen running a long loop is CANCELLED when the client
    HARD-CLOSES mid-stream: the gen's finally runs via CancelledError (NOT a
    natural completion), it stops advancing within a short window, and the
    server still serves the next request. Without the fix the gen keeps running
    (no per-stream cancel acts on _cancel_event) until a send happens to fail.
    """
    proc, port = _spawn(_PLAIN_FOREVER)
    progress = f"{port}.progress"
    ended = f"{port}.ended"
    for p in (progress, ended):
        try:
            os.remove(p)
        except OSError:
            pass
    try:
        # Open the stream on a raw socket, read one chunk, then HARD-CLOSE.
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        s.sendall(b"GET /forever HTTP/1.1\r\nHost: x\r\n"
                  b"Accept: text/event-stream\r\n\r\n")
        s.settimeout(2.0)
        # Read at least the status line + first data chunk so the stream is
        # genuinely in-flight at the suspended __anext__.
        got = s.recv(65536)
        assert got, "no bytes received from the stream"
        s.setblocking(False)
        try:
            s.recv(65536)
        except BlockingIOError:
            pass
        s.close()                      # client gone mid-stream

        # Capture the gen's index just after disconnect, then later: it must
        # STOP advancing (the gen was cancelled), not run forever.
        time.sleep(0.25)
        try:
            with open(progress) as f:
                after = int(f.read())
        except (OSError, ValueError):
            after = -1
        time.sleep(0.9)                # ~9 more chunks if the gen were alive
        try:
            with open(progress) as f:
                later = int(f.read())
        except (OSError, ValueError):
            later = -1
        assert after >= 0, "stream never started"
        assert later - after <= 1, (
            f"plain-stream gen kept producing after client disconnect "
            f"(NOT cancelled): {after} -> {later}")
        # The gen's finally ran via a CancelledError (client-disconnect cancel),
        # NOT a natural completion.
        assert os.path.exists(ended), \
            "generator finally did NOT run on client disconnect"
        with open(ended) as f:
            how = f.read()
        assert how == "cancelled", (
            f"gen finally ran but the gen was not CANCELLED on disconnect "
            f"(got {how!r}) — the in-flight __anext__ was not interrupted")
        # The server still serves the next request (the seam didn't wedge).
        r = httpx.get(f"http://127.0.0.1:{port}/ping", timeout=5)
        assert r.status_code == 200 and r.json() == {"ok": True}
    finally:
        _stop(proc)
        for p in (progress, ended):
            try:
                os.remove(p)
            except OSError:
                pass


def test_plain_stream_completes_normally_unaffected():
    """Regression: a PLAIN stream the client reads to completion is unaffected
    by the disconnect-cancel watcher — all chunks are delivered and the gen's
    finally runs via NATURAL COMPLETION (not a cancel)."""
    proc, port = _spawn(_PLAIN_FINITE)
    ended = f"{port}.ended"
    try:
        os.remove(ended)
    except OSError:
        pass
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/finite", timeout=10)
        assert r.status_code == 200
        body = r.text
        # All five data frames were delivered.
        for i in range(5):
            assert f"data: tok{i}\n\n" in body, (i, body)
        time.sleep(0.3)
        assert os.path.exists(ended), "gen finally never ran on clean completion"
        with open(ended) as f:
            how = f.read()
        assert how == "completed", (
            f"a cleanly-read stream was CANCELLED instead of completing "
            f"(got {how!r}) — the disconnect watcher fired spuriously")
    finally:
        _stop(proc)
        try:
            os.remove(ended)
        except OSError:
            pass
