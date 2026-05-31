"""M16 P1 — pre-200 streaming status (`@app.stream(defer_status=True)`).

Confirmed latent bug (CLAUDE.md cardinal rule #2: green TestClient != works
under a real socket): the ASGI seam builds the async generator, then sends
`http.response.start` with `status: 200` + the SSE headers BEFORE it pulls the
first `__anext__`. A handler that RAISES BEFORE its first `yield` (e.g. an auth
/ quota check that fails) therefore commits a `200` and can only emit an SSE
*error frame* — a half-open 200. A client / SDK that keys off the HTTP status
sees success.

`@app.stream(defer_status=True)` (default **False** -> byte-identical to today)
makes the seam pull the FIRST item inside a `try` BEFORE `response.start`:
  * a pre-yield `HTTPException` / `ProblemDetail` -> a REAL 4xx/5xx + JSON body
    (no half-open 200);
  * any other pre-yield exception -> a real 500 + plain body;
  * otherwise -> 200 + the stashed first chunk, then the stream continues.

Native-path decision (see `docs/plans/2026-05-31-m16-streaming-fixes.md` P1 and
the note in `src/stream.rs`): `defer_status` is ASGI-seam (uvicorn) ONLY — the
native `app.run()` server forwards only `(method, path, handler)` and has no
per-route config to key off (same documented limitation as native lifespan/DI).
So these tests run on the uvicorn subprocess.

These are real-server tests: a subprocess uvicorn + raw socket, asserting on
the actual status line the disconnecting/erroring path produces.
"""
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


def _raw_status_line(host: str, port: int, path: str) -> tuple[int, bytes]:
    """Send a bare GET on a raw socket and return (status_code, full_raw)."""
    s = socket.create_connection((host, port), timeout=5)
    s.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\n"
              f"Accept: text/event-stream\r\nConnection: close\r\n\r\n"
              .encode())
    s.settimeout(5)
    raw = b""
    try:
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            raw += chunk
    except (TimeoutError, OSError):
        pass
    finally:
        s.close()
    # Status line: "HTTP/1.1 409 Conflict\r\n..."
    first = raw.split(b"\r\n", 1)[0]
    parts = first.split(b" ", 2)
    status = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else -1
    return status, raw


# A handler that RAISES BEFORE its first yield, with defer_status=True. Must
# produce a real 409 + JSON body, NOT a half-open 200 + SSE error frame.
_DEFER_RAISE = r"""
import sys, uvicorn, lancelot
from lancelot import HTTPException

app = lancelot.App()

@app.stream("/s", defer_status=True)
async def s(request: lancelot.Request):
    raise HTTPException(status_code=409, detail="precondition failed")
    yield b"never\n"   # pragma: no cover - unreachable; marks this an async-gen

uvicorn.run(app.asgi(), host="127.0.0.1", port=int(sys.argv[1]),
            log_level="warning", access_log=False, loop="uvloop", http="httptools")
"""


# Same handler shape but defer_status is OFF (default). This reproduces the
# legacy half-open behavior: the 200 + SSE headers are already committed when
# the handler raises, so the status is 200 (the bug). This pins the contrast.
_NODEFER_RAISE = r"""
import sys, uvicorn, lancelot
from lancelot import HTTPException

app = lancelot.App()

@app.stream("/s")
async def s(request: lancelot.Request):
    raise HTTPException(status_code=409, detail="precondition failed")
    yield b"never\n"   # pragma: no cover

uvicorn.run(app.asgi(), host="127.0.0.1", port=int(sys.argv[1]),
            log_level="warning", access_log=False, loop="uvloop", http="httptools")
"""


# defer_status=True happy path: yields normally -> 200 + all chunks in order.
_DEFER_HAPPY = r"""
import sys, uvicorn, lancelot

app = lancelot.App()

@app.stream("/s", defer_status=True)
async def s(request: lancelot.Request):
    yield b"data: a\n\n"
    yield b"data: b\n\n"
    yield b"data: c\n\n"

uvicorn.run(app.asgi(), host="127.0.0.1", port=int(sys.argv[1]),
            log_level="warning", access_log=False, loop="uvloop", http="httptools")
"""


# Default-off stream (no defer_status): byte-identical regression target.
_DEFAULT_STREAM = r"""
import sys, uvicorn, lancelot

app = lancelot.App()

@app.stream("/s")
async def s(request: lancelot.Request):
    yield b"data: a\n\n"
    yield b"data: b\n\n"
    yield b"data: c\n\n"

uvicorn.run(app.asgi(), host="127.0.0.1", port=int(sys.argv[1]),
            log_level="warning", access_log=False, loop="uvloop", http="httptools")
"""


def test_defer_status_pre_yield_raise_sends_real_4xx():
    """THE money path: a defer_status=True handler that raises HTTPException(409)
    BEFORE its first yield -> the client gets a REAL 409 + JSON body, NOT a
    half-open 200 + SSE error frame."""
    proc, port = _spawn(_DEFER_RAISE)
    try:
        status, raw = _raw_status_line("127.0.0.1", port, "/s")
        assert status == 409, (
            f"pre-yield raise did NOT produce a real 409 status "
            f"(half-open 200 bug); raw={raw[:300]!r}")
        # The body is the canonical {"detail": ...} JSON, not an SSE frame.
        assert b'"detail"' in raw and b"precondition failed" in raw, raw[:400]
        assert b"text/event-stream" not in raw, raw[:400]
    finally:
        _stop(proc)


def test_defer_status_off_reproduces_halfopen_200():
    """Contrast / regression pin: WITHOUT defer_status, the SAME pre-yield raise
    commits a 200 first (the legacy behavior we are deliberately preserving when
    the kwarg is off). The error surfaces as an SSE error frame, not the status.
    """
    proc, port = _spawn(_NODEFER_RAISE)
    try:
        status, raw = _raw_status_line("127.0.0.1", port, "/s")
        assert status == 200, (
            f"default (off) path changed — must stay byte-identical "
            f"(committed 200 then error frame); raw={raw[:300]!r}")
        assert b"text/event-stream" in raw, raw[:400]
        # The legacy error-frame path emits `event: error`.
        assert b"event: error" in raw, raw[:400]
    finally:
        _stop(proc)


def test_defer_status_happy_path():
    """A defer_status=True stream that yields normally still emits 200 + all
    chunks in order (the stashed first chunk is not lost)."""
    proc, port = _spawn(_DEFER_HAPPY)
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/s", timeout=10)
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.content == b"data: a\n\ndata: b\n\ndata: c\n\n", r.content
    finally:
        _stop(proc)


def test_defer_status_off_is_byte_identical():
    """Regression: with defer_status off (default), a normal stream is exactly
    what it was pre-M16 — same 200, same headers, same body bytes."""
    proc, port = _spawn(_DEFAULT_STREAM)
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/s", timeout=10)
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.content == b"data: a\n\ndata: b\n\ndata: c\n\n", r.content
    finally:
        _stop(proc)
