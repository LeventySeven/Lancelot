"""The NATIVE `app.run()` server must serve SYNC handlers, not just `async def`.

REAL-SERVER test (cardinal rule #2): the app is served by the Rust `_native.run`
server in a SUBPROCESS that ends in `app.run()` — NOT TestClient, NOT uvicorn.
TestClient/uvicorn threadpool a sync handler, so they HIDE the very bug this
fixes: the native dispatch (`src/dispatch.rs::build_result`) called the handler
INLINE (`handler.call1((req,))`) — for an `async def` the result is a coroutine
(fine), but for a `def h(request): return {...}` the result is the plain return
VALUE, which the old code blindly fed to `into_future_with_locals` →
`asyncio.ensure_future(<dict>)` → `TypeError` → service.rs mapped it to a 500.

The fix detects a non-awaitable result and serializes it RIGHT THERE in the GIL
scope (no bridge, no await) via `Dispatched::Ready`.

This test proves, over a real socket:
 (a) a SYNC handler returns 200 with a byte-correct JSON body (was 500);
 (b) a SYNC handler returning a LIST serializes correctly;
 (c) an ASYNC handler still returns 200 (the async path is untouched — regression);
 (d) a const-tier route is still served from Rust (the fast path is untouched).
"""
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time

import pytest

httpx = pytest.importorskip("httpx")


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _wait_listen(proc, port, deadline_s=12):
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"server exited rc={proc.returncode}: "
                f"{proc.stderr.read().decode()[:1200]}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    proc.kill()
    raise RuntimeError("server did not start listening in time")


# A native-server app mixing a SYNC handler, an ASYNC handler, a SYNC handler
# returning a list, and a const route. The native server calls every handler
# as `handler(request)` (ONE positional arg, no DI), so each takes `request`.
_SERVER = r"""
import sys
import lancelot

app = lancelot.App()

# SYNC handler — `def`, not `async def`. The body runs INLINE during call1();
# the returned dict must be serialized directly (Dispatched::Ready), not fed to
# asyncio.ensure_future (the bug: TypeError -> 500).
@app.get("/sync")
def sync_handler(request):
    return {"ok": True, "n": 1}

# ASYNC handler — the coroutine path stays byte-identical (regression guard).
@app.get("/async")
async def async_handler(request):
    return {"ok": True}

# SYNC handler returning a LIST — exercises non-dict serialization on Ready.
@app.get("/synclist")
def sync_list(request):
    return [1, 2, 3]

# Const tier — served straight from Rust, zero GIL (fast path untouched).
app.const_route("GET", "/livez", b'{"status":"ok"}', "application/json")

app.run(host="127.0.0.1", port=int(sys.argv[1]))
"""


def test_native_server_serves_sync_handlers():
    port = _free_port()
    env = {**os.environ, "LANCELOT_HOST": "127.0.0.1"}
    proc = subprocess.Popen(
        [sys.executable, "-c", _SERVER, str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    try:
        _wait_listen(proc, port)
        base = f"http://127.0.0.1:{port}"

        # (a) SYNC handler -> 200 + byte-correct JSON (was 500 before the fix).
        r = httpx.get(f"{base}/sync", timeout=5)
        assert r.status_code == 200, (r.status_code, r.text)
        assert r.json() == {"ok": True, "n": 1}, r.json()
        # Byte-correctness: pydantic-core compact JSON (no spaces).
        assert r.content == b'{"ok":true,"n":1}', r.content

        # (b) SYNC handler returning a LIST serializes correctly.
        r = httpx.get(f"{base}/synclist", timeout=5)
        assert r.status_code == 200, (r.status_code, r.text)
        assert r.json() == [1, 2, 3], r.json()
        assert r.content == b"[1,2,3]", r.content

        # (c) ASYNC handler still 200 (the coroutine path is unchanged).
        r = httpx.get(f"{base}/async", timeout=5)
        assert r.status_code == 200, (r.status_code, r.text)
        assert r.json() == {"ok": True}, r.json()

        # (d) const tier still served from Rust.
        r = httpx.get(f"{base}/livez", timeout=5)
        assert r.status_code == 200, (r.status_code, r.text)
        assert r.content == b'{"status":"ok"}', r.content
        assert r.headers["content-type"] == "application/json"
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
