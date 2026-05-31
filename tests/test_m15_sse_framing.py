"""M15 T7 (#5) — SSE-of-models framing + `done_sentinel='[DONE]'`.

`@app.stream(sse=True, done_sentinel="[DONE]")` makes a yielded Pydantic
model / dict arrive as ONE `data: <json>\n\n` SSE frame (the shape an
EventSource / browser client parses), and on clean generator exhaustion
auto-emits a terminal `data: [DONE]\n\n` sentinel (OpenAI-streaming
convention). Event/bytes/str yields stay BYTE-IDENTICAL to before
(backward compatible) — only the bare-model path and the EOF sentinel
change, and only under `sse=True`.

The framing + sentinel are produced by a shared Python wrapper generator
(`lancelot.stream.sse_adapter`) that emits already-framed `bytes`, so BOTH
the uvicorn ASGI seam (`_stream_chunk_bytes`) AND the native R3 pump
(`src/stream.rs` `chunk_to_bytes`) pass those bytes through UNCHANGED —
byte-identical output on both transports, by construction, with the R3
pump (on-loop `_drain` + same-thread `try_push` + bounded backpressure)
untouched (Tier-D: do NOT regress R3).

Cardinal rule #2: a streaming feature gets REAL-SERVER tests (subprocess +
raw socket / EventSource), exercising the path the bug would live on —
the disconnecting client, both the uvicorn AND the native transport.
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
            out, err = p.communicate(timeout=6)
        except subprocess.TimeoutExpired:
            p.kill()
            p.communicate()


def _read_sse_frames(raw: bytes) -> list[bytes]:
    """Split a raw SSE body into its `...\n\n`-terminated frames (keeping the
    trailing blank line on each, so a frame is exactly what was sent)."""
    parts = raw.split(b"\n\n")
    # Re-attach the separators; the final element after a trailing \n\n is "".
    return [p + b"\n\n" for p in parts if p]


# --- native R3 server (app.run() -> _native.run) serving SSE-of-models ------
_NATIVE_SSE = r"""
import sys
import lancelot
from pydantic import BaseModel

app = lancelot.App()

class Delta(BaseModel):
    text: str
    idx: int

@app.stream("/sse", sse=True)
async def sse(request: lancelot.Request):
    yield Delta(text="hi", idx=0)
    yield Delta(text="there", idx=1)

app.run(host="127.0.0.1", port=int(sys.argv[1]))
"""


# --- uvicorn ASGI seam serving the SAME SSE-of-models stream ----------------
_UVICORN_SSE = r"""
import sys, uvicorn, lancelot
from pydantic import BaseModel

app = lancelot.App()

class Delta(BaseModel):
    text: str
    idx: int

@app.stream("/sse", sse=True)
async def sse(request: lancelot.Request):
    yield Delta(text="hi", idx=0)
    yield Delta(text="there", idx=1)

uvicorn.run(app.asgi(), host="127.0.0.1", port=int(sys.argv[1]),
            log_level="warning", access_log=False, loop="uvloop", http="httptools")
"""


# --- done_sentinel=None disables the terminal sentinel ----------------------
_UVICORN_NO_SENTINEL = r"""
import sys, uvicorn, lancelot
from pydantic import BaseModel

app = lancelot.App()

class Delta(BaseModel):
    text: str

@app.stream("/sse", sse=True, done_sentinel=None)
async def sse(request: lancelot.Request):
    yield Delta(text="a")

uvicorn.run(app.asgi(), host="127.0.0.1", port=int(sys.argv[1]),
            log_level="warning", access_log=False, loop="uvloop", http="httptools")
"""


# --- a long native SSE stream for the disconnect test -----------------------
_NATIVE_SSE_DISCONNECT = r"""
import sys, asyncio, lancelot
from pydantic import BaseModel

app = lancelot.App()
# Record progress to a file the test reads after the disconnect.
_path = sys.argv[1] + ".progress"

class Tok(BaseModel):
    i: int

@app.stream("/sse", sse=True)
async def sse(request: lancelot.Request):
    try:
        for i in range(10000):
            yield Tok(i=i)
            with open(_path, "w") as f:
                f.write(str(i))
            await asyncio.sleep(0.02)
    finally:
        with open(_path + ".final", "w") as f:
            f.write("closed")

@app.get("/ping")
async def ping(request: lancelot.Request):
    return {"ok": True}

app.run(host="127.0.0.1", port=int(sys.argv[1]))
"""


def test_sse_model_framing():
    """A `@app.stream(sse=True)` yielding Pydantic models arrives as exactly
    one `data: <json>\n\n` frame per model on the NATIVE R3 transport."""
    proc, port = _spawn(_NATIVE_SSE)
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/sse", timeout=10)
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("text/event-stream"), \
            r.headers["content-type"]
        body = r.content
        frames = _read_sse_frames(body)
        # Two model frames + the [DONE] sentinel frame.
        assert frames[0] == b'data: {"text":"hi","idx":0}\n\n', frames
        assert frames[1] == b'data: {"text":"there","idx":1}\n\n', frames
        assert frames[-1] == b"data: [DONE]\n\n", frames
    finally:
        _stop(proc)


def test_sse_done_sentinel():
    """Clean exhaustion auto-emits a terminal `data: [DONE]\n\n`;
    `done_sentinel=None` disables it (uvicorn path)."""
    # Default sentinel present.
    proc, port = _spawn(_UVICORN_SSE)
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/sse", timeout=10)
        assert r.status_code == 200, r.text
        frames = _read_sse_frames(r.content)
        assert frames[-1] == b"data: [DONE]\n\n", frames
    finally:
        _stop(proc)

    # done_sentinel=None -> NO terminal sentinel frame.
    proc, port = _spawn(_UVICORN_NO_SENTINEL)
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/sse", timeout=10)
        assert r.status_code == 200, r.text
        body = r.content
        assert b"[DONE]" not in body, body
        frames = _read_sse_frames(body)
        assert frames[-1] == b'data: {"text":"a"}\n\n', frames
    finally:
        _stop(proc)


def test_sse_backward_compat():
    """Event/bytes/str yields under EXISTING usage are byte-identical to the
    pre-T7 behavior (no `sse=` -> plain @app.stream). Regression guard: the new
    code must not touch the Event/bytes/str paths."""
    import lancelot
    from lancelot.stream import Event
    from lancelot.testclient import TestClient

    app = lancelot.App()

    @app.stream("/raw")
    async def raw():
        yield b"data: a\n\n"
        yield "data: b\n\n"
        yield Event("delta", {"x": 1})

    with TestClient(app) as client:
        r = client.get("/raw")
        assert r.status_code == 200
        body = r.content
        # Exact bytes, in order, with NO [DONE] sentinel (sse not enabled).
        assert body == (b"data: a\n\n" + b"data: b\n\n"
                        + Event("delta", {"x": 1}).render()), body
        assert b"[DONE]" not in body


def test_sse_backward_compat_event_under_sse():
    """Even with sse=True, an Event/bytes/str yield stays byte-identical
    (only the BARE-MODEL path changes). The sentinel is still appended."""
    import lancelot
    from lancelot.stream import Event
    from lancelot.testclient import TestClient

    app = lancelot.App()

    @app.stream("/mixed", sse=True)
    async def mixed():
        yield b"data: raw\n\n"          # bytes -> passthrough
        yield Event("delta", {"x": 1})  # Event -> render() unchanged

    with TestClient(app) as client:
        r = client.get("/mixed")
        assert r.status_code == 200
        body = r.content
        assert body == (b"data: raw\n\n"
                        + Event("delta", {"x": 1}).render()
                        + b"data: [DONE]\n\n"), body


def test_sse_both_paths_identical_bytes():
    """THE byte-parity crux: the uvicorn ASGI seam and the native R3 pump emit
    BYTE-IDENTICAL output for the same sse=True model-yielding generator
    (framing + [DONE] sentinel produced by the shared Python wrapper)."""
    nat_proc, nat_port = _spawn(_NATIVE_SSE)
    uv_proc, uv_port = _spawn(_UVICORN_SSE)
    try:
        nat = httpx.get(f"http://127.0.0.1:{nat_port}/sse", timeout=10).content
        uv = httpx.get(f"http://127.0.0.1:{uv_port}/sse", timeout=10).content
        assert nat == uv, (
            "uvicorn vs native R3 SSE framing diverged:\n"
            f"native={nat!r}\nuvicorn={uv!r}")
        # And the bytes are the expected SSE-of-models shape.
        assert nat == (b'data: {"text":"hi","idx":0}\n\n'
                       b'data: {"text":"there","idx":1}\n\n'
                       b"data: [DONE]\n\n"), nat
    finally:
        _stop(nat_proc, uv_proc)


def test_sse_disconnect_midstream():
    """Client disconnect mid-stream -> the generator is left suspended (R3
    contract: at most ~1 extra chunk after the socket closes), and the server
    still serves the NEXT request. Native R3 transport (the pump under test)."""
    proc, port = _spawn(_NATIVE_SSE_DISCONNECT)
    progress = f"{port}.progress"
    final = f"{port}.progress.final"
    for p in (progress, final):
        try:
            os.remove(p)
        except OSError:
            pass
    try:
        # Open the SSE GET on a raw socket, read a few frames, then HARD-CLOSE.
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        s.sendall(b"GET /sse HTTP/1.1\r\nHost: x\r\nAccept: text/event-stream\r\n\r\n")
        time.sleep(0.25)            # let a handful of chunks flow
        # Read whatever is buffered, then close.
        s.setblocking(False)
        try:
            s.recv(65536)
        except BlockingIOError:
            pass
        s.close()                   # client gone mid-stream

        # Capture progress shortly after the disconnect, then again later: the
        # generator must STOP advancing (<= ~1 extra chunk), not run to 10000.
        time.sleep(0.2)
        try:
            with open(progress) as f:
                after_close = int(f.read())
        except (OSError, ValueError):
            after_close = -1
        time.sleep(0.6)             # ~30 more chunks would have fired if alive
        try:
            with open(progress) as f:
                later = int(f.read())
        except (OSError, ValueError):
            later = -1
        # R3 contract: at most one more chunk after the disconnect is observed.
        assert later - after_close <= 1, (
            f"generator kept producing after disconnect: {after_close} -> {later}")

        # The server still serves the next request (the pump didn't wedge).
        r = httpx.get(f"http://127.0.0.1:{port}/ping", timeout=5)
        assert r.status_code == 200 and r.json() == {"ok": True}
    finally:
        _stop(proc)
        for p in (progress, final):
            try:
                os.remove(p)
            except OSError:
                pass
