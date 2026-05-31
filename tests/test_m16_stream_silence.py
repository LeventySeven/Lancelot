"""M16 P2 — stuck-stream silence-abort + keepalive self-reset fix.

Confirmed latent bug (CLAUDE.md cardinal rule #2): `_send_serialized` bumped a
SINGLE `_last_send` clock on EVERY send — including its own keepalive ping — and
`_keepalive_loop` checked only that clock. So a generator that has gone silent
but whose keepalive pings keep firing **resets its own idle timer forever**: a
zombie / stuck stream is never detected or closed, and there was no max-silence
cap at all.

Fix:
  * Track the last-DATA send SEPARATELY from the last-PING send. The keepalive
    cadence is unchanged (it pings on "time since ANY send"), but the
    silence-abort is measured from last-DATA only — so a ping no longer resets
    the data-silence timer.
  * `@app.stream(max_stream_silence=N)` (default None=off): when only keepalive
    pings (no real data) have flowed for N seconds, CANCEL the generator (its
    `finally`/`aclose` runs — teardown preserved) and close the connection.

These are real-server tests: a subprocess uvicorn + a generator that emits ONLY
keepalive-triggering silence (no data). A sidecar file records whether the
generator's `finally` ran on the abort.
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


# A generator that NEVER yields data (it just sleeps forever). With
# sse_keepalive=1 the watchdog pings every ~1s; without the fix those pings
# would reset the idle clock forever. max_stream_silence=3 must drop the
# connection at ~3s of DATA-silence AND run the generator's finally (recorded
# to a sidecar file the test reads after the drop).
_PING_ONLY = r"""
import sys, asyncio, lancelot

app = lancelot.App()
_final = sys.argv[1] + ".final"

@app.stream("/zombie", sse_keepalive=1, max_stream_silence=3)
async def zombie(request: lancelot.Request):
    try:
        while True:
            await asyncio.sleep(0.5)   # NEVER yields a data chunk
            if False:                  # pragma: no cover - makes this an async-gen
                yield b""
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


# Data flows for a while, THEN goes silent while keepalive pings continue. The
# silence must be measured from the LAST DATA, not the last ping — so the abort
# fires ~max_stream_silence after the last real chunk, NOT never. Records the
# count of data chunks emitted (must be exactly the 2 early ones) + finally.
_DATA_THEN_SILENT = r"""
import sys, asyncio, lancelot

app = lancelot.App()
_final = sys.argv[1] + ".final"

@app.stream("/ds", sse_keepalive=1, max_stream_silence=3)
async def ds(request: lancelot.Request):
    try:
        yield b"data: one\n\n"
        yield b"data: two\n\n"
        while True:                    # then silent forever (pings keep firing)
            await asyncio.sleep(0.5)
    finally:
        with open(_final, "w") as f:
            f.write("closed")

import uvicorn
uvicorn.run(app.asgi(), host="127.0.0.1", port=int(sys.argv[1]),
            log_level="warning", access_log=False, loop="uvloop", http="httptools")
"""


# max_stream_silence OFF (default None): a long quiet-but-pinging stream stays
# open. We open it, wait well past where a 3s cap would have fired, and assert
# the connection is STILL receiving pings (not dropped) — byte-identical to the
# pre-M16 keepalive behavior. The generator is then torn down on server stop.
_NO_CAP = r"""
import sys, asyncio, lancelot

app = lancelot.App()

@app.stream("/quiet", sse_keepalive=1)   # NO max_stream_silence
async def quiet(request: lancelot.Request):
    while True:
        await asyncio.sleep(0.5)
        if False:                        # pragma: no cover
            yield b""

import uvicorn
uvicorn.run(app.asgi(), host="127.0.0.1", port=int(sys.argv[1]),
            log_level="warning", access_log=False, loop="uvloop", http="httptools")
"""


def _recv_until_close(host, port, path, max_seconds):
    """Open an SSE GET on a raw socket; read until the response stream ENDS
    (the server closes the socket OR sends the HTTP/1.1 chunked end-marker
    `0\\r\\n\\r\\n`, which terminates the body even though keep-alive leaves the
    TCP socket open) or `max_seconds` elapses. Returns (ended, elapsed, raw).

    NOTE: we detect the chunked terminator, not just a TCP FIN — under HTTP/1.1
    keep-alive the server legitimately holds the connection open for reuse after
    the stream ends, so waiting for a socket close would hang."""
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
                ended = True        # server closed the connection
                break
            raw += chunk
            # HTTP/1.1 chunked end-of-body marker — the SSE stream is over.
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


def test_ping_only_stream_aborts_at_max_silence():
    """A ping-only zombie stream (no data, keepalive firing) is DROPPED at
    ~max_stream_silence, and the generator's finally/aclose RAN. Pre-M16 it
    would never be dropped (the ping reset its own idle timer forever)."""
    proc, port = _spawn(_PING_ONLY)
    final = f"{port}.final"
    try:
        os.remove(final)
    except OSError:
        pass
    try:
        # max_stream_silence=3; give it 8s of headroom. If the self-reset bug
        # were present, the connection would stay open the whole window.
        ended, elapsed, raw = _recv_until_close("127.0.0.1", port, "/zombie", 8)
        assert ended, (
            f"ping-only zombie stream was NOT dropped within 8s "
            f"(self-reset bug); raw tail={raw[-120:]!r}")
        # Dropped around the 3s cap (allow keepalive-tick + scheduling slack).
        assert 2.0 <= elapsed <= 6.5, f"abort timing off: {elapsed:.2f}s"
        # The generator's teardown ran on the silence-abort (no leaked work).
        time.sleep(0.4)
        assert os.path.exists(final), \
            "generator finally/aclose did NOT run on silence-abort"
        # The server still serves the next request (the seam didn't wedge).
        r = httpx.get(f"http://127.0.0.1:{port}/ping", timeout=5)
        assert r.status_code == 200 and r.json() == {"ok": True}
    finally:
        _stop(proc)
        try:
            os.remove(final)
        except OSError:
            pass


def test_keepalive_does_not_reset_data_silence():
    """Data flows, then the stream goes silent while pings keep firing. The
    silence is measured from the last DATA, so the abort still fires (~3s after
    the last chunk) even though pings reset the keepalive cadence. The early
    DATA frames were delivered; the generator's finally ran."""
    proc, port = _spawn(_DATA_THEN_SILENT)
    final = f"{port}.final"
    try:
        os.remove(final)
    except OSError:
        pass
    try:
        ended, elapsed, raw = _recv_until_close("127.0.0.1", port, "/ds", 9)
        assert ended, (
            f"stream NOT dropped after data went silent (ping kept resetting "
            f"the data-silence timer); raw tail={raw[-160:]!r}")
        # The two real data frames were delivered before the silence.
        assert b"data: one\n\n" in raw and b"data: two\n\n" in raw, raw[:300]
        # Pings DID fire during the silent window (proves keepalive ran yet did
        # not prevent the abort).
        assert b":\n\n" in raw, ("expected keepalive pings in the silent "
                                 f"window; raw={raw[:300]!r}")
        time.sleep(0.4)
        assert os.path.exists(final), \
            "generator finally did NOT run on data-silence abort"
    finally:
        _stop(proc)
        try:
            os.remove(final)
        except OSError:
            pass


def test_max_silence_off_default():
    """max_stream_silence=None (default): a long quiet-but-pinging stream stays
    OPEN (byte-identical to pre-M16). We watch for ~6s — well past where a 3s
    cap would fire — and assert the connection is still alive and receiving
    pings (never dropped by us)."""
    proc, port = _spawn(_NO_CAP)
    try:
        # Read for 6s; the stream must NOT be closed by the server.
        ended, elapsed, raw = _recv_until_close("127.0.0.1", port, "/quiet", 6)
        assert not ended, (
            "a quiet stream with NO max_stream_silence was dropped — the "
            "default-off behavior changed (must stay byte-identical)")
        # Keepalive pings flowed (the stream is alive, just data-quiet).
        assert raw.count(b":\n\n") >= 2, (
            f"expected ongoing keepalive pings on the open stream; "
            f"raw={raw[:200]!r}")
    finally:
        _stop(proc)
