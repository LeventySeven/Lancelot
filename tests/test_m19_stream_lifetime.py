"""T1 #1 (2026-06-02 byte-path build order) — per-route absolute wall-clock
`max_stream_lifetime` cap + terminal `reconnect` sentinel.

The genuine gap the founder/pattern-mine workflows surfaced (Polar's 10-min cap,
Letta): `max_stream_silence` (M16) only catches a stream that goes DATA-silent.
A stream that keeps *trickling* one byte every `(silence-1)s` never trips the
idle watchdog yet lives forever — its bounded mpsc + upstream client +
ContextVars are pinned open indefinitely. `max_stream_lifetime=N` caps each
stream at an ABSOLUTE wall-clock deadline measured from stream start (NOT
last-data), cancels the generator (its `finally`/`aclose` runs), emits a
terminal `event: reconnect` frame so the client re-establishes, and closes.

Real-server tests (cardinal rule #2): subprocess uvicorn + a generator that
emits data continuously (so silence can NEVER be the trigger). A sidecar file
records whether the generator's `finally` ran on the lifetime cut.
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


def _recv_until_close(host, port, path, max_seconds):
    """Open an SSE GET on a raw socket; read until the response stream ENDS
    (chunked end-marker `0\\r\\n\\r\\n` or a TCP close) or `max_seconds`
    elapses. Returns (ended, elapsed, raw)."""
    s = socket.create_connection((host, port), timeout=5)
    s.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\n"
              f"Accept: text/event-stream\r\n\r\n".encode())
    s.settimeout(0.5)
    t0 = time.time()
    raw = b""
    ended = False
    while time.time() - t0 < max_seconds:
        try:
            chunk = s.recv(65536)
            if chunk == b"":
                ended = True
                break
            raw += chunk
            if raw.endswith(b"0\r\n\r\n"):
                ended = True
                break
        except socket.timeout:
            continue
        except OSError:
            ended = True
            break
    elapsed = time.time() - t0
    s.close()
    return ended, elapsed, raw


# Data flows CONTINUOUSLY (every 0.4s) so the silence watchdog can NEVER be the
# cause — only the absolute lifetime cap can end this. keepalive on (realistic
# Polar shape). max_stream_lifetime=3 must cut at ~3s, emit a reconnect frame,
# and run the generator's finally (recorded to a sidecar file).
_ACTIVE_WITH_KEEPALIVE = r"""
import sys, asyncio, lancelot

app = lancelot.App()
_final = sys.argv[1] + ".final"

@app.stream("/live", sse_keepalive=1, max_stream_lifetime=3)
async def live(request: lancelot.Request):
    try:
        while True:
            yield b"data: tick\n\n"      # never goes data-silent
            await asyncio.sleep(0.4)
    finally:
        with open(_final, "w") as f:
            f.write("closed")

@app.get("/ping")
async def ping(request: lancelot.Request):
    return {"ok": True}

import uvicorn
uvicorn.run(app.asgi(), host="127.0.0.1", port=int(sys.argv[1]),
            log_level="warning", access_log=False, loop="uvloop", http="httptools")
"""


# NO keepalive, NO silence cap — only max_stream_lifetime is set. Proves the
# watchdog ARMS on the lifetime cap alone (not just when keepalive/silence are on).
_ACTIVE_NO_KEEPALIVE = r"""
import sys, asyncio, lancelot

app = lancelot.App()
_final = sys.argv[1] + ".final"

@app.stream("/live2", sse_keepalive=None, max_stream_lifetime=3)
async def live2(request: lancelot.Request):
    try:
        while True:
            yield b"data: tick\n\n"
            await asyncio.sleep(0.4)
    finally:
        with open(_final, "w") as f:
            f.write("closed")

import uvicorn
uvicorn.run(app.asgi(), host="127.0.0.1", port=int(sys.argv[1]),
            log_level="warning", access_log=False, loop="uvloop", http="httptools")
"""


# max_stream_lifetime OFF (default None): an active stream stays open well past
# where a 3s cap would have fired — byte-identical to pre-M19.
_NO_CAP = r"""
import sys, asyncio, lancelot

app = lancelot.App()

@app.stream("/forever", sse_keepalive=1)   # NO max_stream_lifetime
async def forever(request: lancelot.Request):
    while True:
        yield b"data: tick\n\n"
        await asyncio.sleep(0.4)

import uvicorn
uvicorn.run(app.asgi(), host="127.0.0.1", port=int(sys.argv[1]),
            log_level="warning", access_log=False, loop="uvloop", http="httptools")
"""


def test_max_stream_lifetime_caps_active_data_stream():
    """A stream emitting data continuously (silence can never fire) is cut at
    ~max_stream_lifetime, a terminal `event: reconnect` frame is delivered, and
    the generator's finally ran. The server still serves the next request."""
    proc, port = _spawn(_ACTIVE_WITH_KEEPALIVE)
    final = f"{port}.final"
    try:
        os.remove(final)
    except OSError:
        pass
    try:
        ended, elapsed, raw = _recv_until_close("127.0.0.1", port, "/live", 8)
        assert ended, (
            f"active data stream was NOT cut at the lifetime cap within 8s; "
            f"raw tail={raw[-160:]!r}")
        assert 2.0 <= elapsed <= 6.5, f"lifetime cut timing off: {elapsed:.2f}s"
        # Real data did flow (this is NOT a silence abort).
        assert b"data: tick\n\n" in raw, raw[:300]
        # The terminal reconnect sentinel was delivered so the client reconnects.
        assert b"event: reconnect" in raw, (
            f"expected a terminal `event: reconnect` frame on the lifetime cut; "
            f"raw tail={raw[-200:]!r}")
        time.sleep(0.4)
        assert os.path.exists(final), \
            "generator finally/aclose did NOT run on the lifetime cut"
        r = httpx.get(f"http://127.0.0.1:{port}/ping", timeout=5)
        assert r.status_code == 200 and r.json() == {"ok": True}
    finally:
        _stop(proc)
        try:
            os.remove(final)
        except OSError:
            pass


def test_max_stream_lifetime_arms_without_keepalive():
    """With NO keepalive and NO silence cap, the lifetime cap alone arms the
    watchdog and cuts the stream at ~3s, running the generator's finally."""
    proc, port = _spawn(_ACTIVE_NO_KEEPALIVE)
    final = f"{port}.final"
    try:
        os.remove(final)
    except OSError:
        pass
    try:
        ended, elapsed, raw = _recv_until_close("127.0.0.1", port, "/live2", 8)
        assert ended, (
            f"lifetime cap did not arm the watchdog without keepalive; "
            f"raw tail={raw[-160:]!r}")
        assert 2.0 <= elapsed <= 6.5, f"lifetime cut timing off: {elapsed:.2f}s"
        assert b"event: reconnect" in raw, raw[-200:]
        time.sleep(0.4)
        assert os.path.exists(final), \
            "generator finally did NOT run on lifetime cut (no keepalive)"
    finally:
        _stop(proc)
        try:
            os.remove(final)
        except OSError:
            pass


def test_max_stream_lifetime_off_default():
    """max_stream_lifetime=None (default): an active stream stays OPEN past
    where a 3s cap would fire (byte-identical to pre-M19)."""
    proc, port = _spawn(_NO_CAP)
    try:
        ended, elapsed, raw = _recv_until_close("127.0.0.1", port, "/forever", 6)
        assert not ended, (
            "a stream with NO max_stream_lifetime was cut — the default-off "
            "behavior changed (must stay byte-identical)")
        assert raw.count(b"data: tick\n\n") >= 3, (
            f"expected ongoing data on the open stream; raw={raw[:200]!r}")
        assert b"event: reconnect" not in raw, \
            "reconnect frame emitted with no lifetime cap set"
    finally:
        _stop(proc)
