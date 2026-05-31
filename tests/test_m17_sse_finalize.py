"""M17 P6 — guaranteed SSE terminal sentinel + crash/cancel -> ProblemDetail
error-frame (opt-in `@app.stream(sse=True, error_frame=True)`).

Confirmed state (verify live, post-M16): the mid-stream error frame is a
HARDCODED `event: error\ndata: {"error": "internal server error", ...}`
(asgi.py:1198-1200), NOT the shipped RFC-9457 ProblemDetail; the `finally`
sends a clean EOF with NO terminal `[DONE]`; the CancelledError arm emits NO
error frame.

Change (opt-in, default-off byte-identical): `error_frame=True` makes the seam
(a) route the mid-stream error frame through a SCRUBBED ProblemDetail shape
(never leaks `str(exc)`); (b) GUARANTEE the terminal `done_sentinel` ([DONE])
on every non-disconnect end (clean / raise / internal silence-abort); (c) emit
the error frame on a genuine INTERNAL cancel (the M16 `max_stream_silence`
silence-abort) but NEVER on a client disconnect.

Cardinal rule #2: streaming feature -> REAL-SERVER tests (subprocess uvicorn +
raw socket / HTTP), exercising the raise / disconnect / clean paths. Mirrors
tests/test_m15_sse_framing.py + tests/test_m16_relay_cancel.py.
"""
import json
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
                               f"{proc.stderr.read().decode()[:2000]}")
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


def _read_sse_frames(raw: bytes) -> list[bytes]:
    parts = raw.split(b"\n\n")
    return [p + b"\n\n" for p in parts if p]


# --- error_frame=True: a handler that raises AFTER its first yield -----------
_RAISE_WITH_FRAME = r"""
import sys, uvicorn, lancelot
from pydantic import BaseModel

app = lancelot.App()

class Tok(BaseModel):
    i: int

@app.stream("/boom", sse=True, error_frame=True)
async def boom(request: lancelot.Request):
    yield Tok(i=0)
    yield Tok(i=1)
    raise RuntimeError("super secret internal detail leaked? no")

uvicorn.run(app.asgi(), host="127.0.0.1", port=int(sys.argv[1]),
            log_level="error", access_log=False, loop="uvloop", http="httptools")
"""


# --- error_frame DEFAULT (off): same raising handler -> hardcoded frame, no
#     terminal [DONE] (byte-identical to today) -------------------------------
_RAISE_DEFAULT = r"""
import sys, uvicorn, lancelot
from pydantic import BaseModel

app = lancelot.App()

class Tok(BaseModel):
    i: int

@app.stream("/boom", sse=True)
async def boom(request: lancelot.Request):
    yield Tok(i=0)
    raise RuntimeError("boom")

uvicorn.run(app.asgi(), host="127.0.0.1", port=int(sys.argv[1]),
            log_level="error", access_log=False, loop="uvloop", http="httptools")
"""


# --- error_frame=True + an INTERNAL silence-abort (client STILL connected) ---
# A stream that emits one real frame then goes silent forever (only keepalive
# pings). max_stream_silence=2 fires an INTERNAL cancel while the client is
# still reading -> under error_frame the client MUST get a ProblemDetail error
# frame + the terminal [DONE] (the positive case of requirement (c): the
# internal-cancel frame DOES fire, because the client can read it).
_SILENCE_ABORT_WITH_FRAME = r"""
import sys, asyncio, uvicorn, lancelot

app = lancelot.App()

@app.stream("/zombie", sse_keepalive=1, max_stream_silence=2, error_frame=True)
async def zombie(request: lancelot.Request):
    yield b"data: first\n\n"          # one real chunk
    await asyncio.sleep(3600)         # then silent forever (pings only)
    yield b"data: never\n\n"

uvicorn.run(app.asgi(), host="127.0.0.1", port=int(sys.argv[1]),
            log_level="error", access_log=False, loop="uvloop", http="httptools")
"""


# --- error_frame=True + a client that disconnects mid-stream -----------------
# An error_frame=True relay over an endless fake upstream (the realistic
# inference-proxy shape; mirrors test_m16_relay_cancel.py). The test opens a raw
# socket, lets a couple frames flow, then HARD-CLOSES. On the relay path a
# client disconnect reliably cancels the upstream (M16 P8 cancel race) -> the
# seam takes its CancelledError `else: raise` branch. P6 must NOT emit an error
# frame on that branch (a disconnected client can't read it). The upstream's
# finally records that aclose ran (teardown preserved).
_DISCONNECT_WITH_FRAME = r"""
import sys, asyncio, uvicorn, lancelot, httpx

CLOSED = sys.argv[1] + ".final"

async def _upstream_body():
    try:
        i = 0
        while True:
            yield f"data: tok{i}\n\n".encode()
            i += 1
            await asyncio.sleep(0.05)
    finally:
        with open(CLOSED, "w") as f:
            f.write("closed")

def _handler(request):
    return httpx.Response(200, content=_upstream_body(),
                          headers={"content-type": "text/event-stream"})

pool = lancelot.llm.TransportPool(transport=httpx.MockTransport(_handler))
app = lancelot.App()

@app.stream("/sse", sse=True, error_frame=True)
async def sse(request: lancelot.Request):
    async with pool.relay("POST", "https://upstream.invalid/v1/stream",
                          request=request) as upstream:
        async for chunk in upstream:
            yield chunk

@app.get("/ping")
async def ping(request: lancelot.Request):
    return {"ok": True}

uvicorn.run(app.asgi(), host="127.0.0.1", port=int(sys.argv[1]),
            log_level="error", access_log=False, loop="uvloop", http="httptools")
"""


def test_error_frame_midstream_raise_is_problemdetail():
    """A mid-stream raise under error_frame=True -> a ProblemDetail-shaped error
    frame (RFC-9457 members: type/title/status; scrubbed -- NO str(exc)) THEN a
    terminal `data: [DONE]\n\n` so the client can tell the stream ended."""
    proc, port = _spawn(_RAISE_WITH_FRAME)
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/boom", timeout=10)
        assert r.status_code == 200, r.text  # 200 already committed pre-raise
        body = r.content
        frames = _read_sse_frames(body)
        # The good model frames came first.
        assert frames[0] == b'data: {"i":0}\n\n', frames
        assert frames[1] == b'data: {"i":1}\n\n', frames
        # An error frame, ProblemDetail-shaped, scrubbed.
        err_frames = [f for f in frames if f.startswith(b"event: error")]
        assert len(err_frames) == 1, frames
        ef = err_frames[0]
        # data: line carries the RFC-9457 problem object.
        data_line = [ln for ln in ef.split(b"\n")
                     if ln.startswith(b"data:")][0]
        problem = json.loads(data_line[len(b"data:"):].strip())
        assert problem.get("status") == 500, problem
        assert problem.get("title"), problem            # human-readable title
        assert "type" in problem, problem               # RFC-9457 member
        # SCRUBBED: the raw exception message must NOT leak into the wire.
        assert b"super secret internal detail" not in body, body
        # And the terminal sentinel is GUARANTEED after the error frame.
        assert frames[-1] == b"data: [DONE]\n\n", frames
    finally:
        _stop(proc)


def test_error_frame_off_default_byte_identical():
    """Default (error_frame off) -> the legacy hardcoded `{"error": "internal
    server error"}` frame and NO terminal [DONE] (byte-identical to today)."""
    proc, port = _spawn(_RAISE_DEFAULT)
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/boom", timeout=10)
        assert r.status_code == 200, r.text
        body = r.content
        # Good frame first, then the legacy hardcoded error frame.
        assert b'data: {"i":0}\n\n' in body, body
        assert b"event: error" in body, body
        assert b'"error": "internal server error"' in body, body
        # The legacy path does NOT append a terminal [DONE] after the error
        # (sse_adapter only emits [DONE] on CLEAN exhaustion, which didn't
        # happen here). This is the byte-identity anchor.
        assert b"[DONE]" not in body, body
    finally:
        _stop(proc)


def test_error_frame_internal_silence_abort_emits_frame():
    """An INTERNAL silence-abort (M16 max_stream_silence) under error_frame=True,
    with the client STILL connected -> the client gets a ProblemDetail error
    frame + the terminal [DONE] (so it can tell "we aborted you" from a clean
    end). The positive half of requirement (c): the internal-cancel frame DOES
    fire (the disconnect case below proves it does NOT fire when the client is
    gone)."""
    proc, port = _spawn(_SILENCE_ABORT_WITH_FRAME)
    try:
        # Read the full response (the abort closes the stream within ~2-3s).
        r = httpx.get(f"http://127.0.0.1:{port}/zombie", timeout=12)
        assert r.status_code == 200, r.text
        body = r.content
        assert b"data: first\n\n" in body, body
        frames = _read_sse_frames(body)
        err_frames = [f for f in frames if f.startswith(b"event: error")]
        assert len(err_frames) == 1, body
        data_line = [ln for ln in err_frames[0].split(b"\n")
                     if ln.startswith(b"data:")][0]
        problem = json.loads(data_line[len(b"data:"):].strip())
        assert problem.get("status") == 500 and "type" in problem, problem
        # Terminal sentinel guaranteed after the internal-abort error frame.
        assert frames[-1] == b"data: [DONE]\n\n", frames
    finally:
        _stop(proc)


def test_error_frame_client_disconnect_emits_nothing():
    """Client disconnects mid-stream under error_frame=True -> NO error frame is
    emitted (a disconnected client can't read it); the generator is left
    suspended (its finally runs), and the server still serves the next request.
    Reuses the M16 disconnect harness shape."""
    proc, port = _spawn(_DISCONNECT_WITH_FRAME)
    final = f"{port}.final"
    try:
        os.remove(final)
    except OSError:
        pass
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        s.sendall(b"GET /sse HTTP/1.1\r\nHost: x\r\n"
                  b"Accept: text/event-stream\r\n\r\n")
        time.sleep(0.25)            # let a few frames flow
        # Read whatever buffered (so we can assert it has no error frame yet),
        # then HARD-CLOSE.
        s.setblocking(False)
        seen = b""
        try:
            while True:
                part = s.recv(65536)
                if not part:
                    break
                seen += part
        except BlockingIOError:
            pass
        s.close()                   # client gone mid-stream
        # Nothing read so far is an error frame (we disconnected cleanly).
        assert b"event: error" not in seen, seen
        # The generator's finally ran on the disconnect (teardown preserved).
        deadline = time.time() + 3.0
        while time.time() < deadline and not os.path.exists(final):
            time.sleep(0.05)
        assert os.path.exists(final), (
            "generator finally did not run on client disconnect")
        # The server still serves the next request (the seam didn't wedge and
        # did NOT try to push an error frame down the dead socket).
        r = httpx.get(f"http://127.0.0.1:{port}/ping", timeout=5)
        assert r.status_code == 200 and r.json() == {"ok": True}
    finally:
        _stop(proc)
        try:
            os.remove(final)
        except OSError:
            pass
