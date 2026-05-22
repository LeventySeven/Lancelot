"""M4 Tier-V2 — AI primitives under load, real servers + subprocess.

V2.2 TransportPool cancellation (the "stops paying for unwatched tokens" money
claim): a Lancelot handler calls a SLOW upstream via TransportPool.request(
request=request); when the client disconnects mid-call, the in-flight upstream
HTTP call must be CANCELLED (not run to completion = wasted spend). Validated
against a real upstream over a real socket, with a real client disconnect.
"""
import socket
import subprocess
import sys
import time

import pytest

httpx = pytest.importorskip("httpx")

# A slow upstream "LLM": 2 s per call. (uvicorn + uvloop, like the deployable path.)
_UPSTREAM = r"""
import asyncio, sys, uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

async def slow(request):
    await asyncio.sleep(2.0)
    return JSONResponse({"upstream": "done"})

uvicorn.run(Starlette(routes=[Route("/slow", slow)]), host="127.0.0.1",
            port=int(sys.argv[1]), log_level="warning", access_log=False,
            loop="uvloop", http="httptools")
"""

# Lancelot proxy: a STREAMING handler (the realistic LLM case) calls the upstream
# via TransportPool bound to the Request, then yields. On client disconnect the
# framework cancels the stream → the in-flight upstream call is cancelled. NOTE:
# disconnect-cancellation is wired on the STREAMING path (the framework owns its
# cancellation); a plain non-streaming handler does NOT cancel its upstream on
# disconnect (ASGI/FastAPI parity — the server buffers the response). The C2
# "stop paying for unwatched tokens" claim is a STREAMING claim, which is the
# actual LLM token-stream shape; this validates it on that path.
_PROXY = r"""
import asyncio, sys, uvicorn, lancelot
from lancelot.llm import TransportPool
from lancelot.stream import Event

UP = sys.argv[2]
app = lancelot.App()
pool = TransportPool()
_stat = {"done": 0, "cancelled": 0}

@app.stream("/proxy_stream")
async def proxy_stream(request: lancelot.Request):
    try:
        await pool.request("GET", UP + "/slow", request=request)
        _stat["done"] += 1
        yield Event("ok", {})
    except asyncio.CancelledError:
        _stat["cancelled"] += 1   # the upstream call was cancelled — $ saved
        raise

@app.get("/proxy_stats")
async def proxy_stats():
    return dict(_stat)

uvicorn.run(app.asgi(), host="127.0.0.1", port=int(sys.argv[1]),
            log_level="warning", access_log=False, loop="uvloop", http="httptools")
"""


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
                               f"{proc.stderr.read().decode()[:500]}")
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


def test_v2_2_streaming_disconnect_cancels_upstream_call():
    up_proc, up_port = _spawn(_UPSTREAM)
    proxy_proc, proxy_port = _spawn(_PROXY, f"http://127.0.0.1:{up_port}")
    try:
        base = f"http://127.0.0.1:{proxy_port}"

        # Control: consume the whole stream → the upstream call completes (done=1).
        with httpx.stream("GET", f"{base}/proxy_stream", timeout=10) as r:
            assert r.status_code == 200
            b"".join(r.iter_bytes())
        assert httpx.get(f"{base}/proxy_stats").json() == {"done": 1, "cancelled": 0}

        # Money path: open the stream on a raw socket, get the 200 head, then
        # DISCONNECT while the handler is awaiting the 2 s upstream.
        s = socket.create_connection(("127.0.0.1", proxy_port), timeout=5)
        s.sendall(b"GET /proxy_stream HTTP/1.1\r\nHost: x\r\n\r\n")
        time.sleep(0.3)  # 200 head sent; handler now awaiting the upstream
        s.close()        # client gone mid-stream

        # Observe the cancel, but BEFORE the upstream's remaining ~1.7 s — so a
        # non-cancelled call would still be in flight (done would NOT advance).
        time.sleep(0.7)
        stats = httpx.get(f"{base}/proxy_stats").json()

        # The disconnected stream's upstream call was cancelled, not completed.
        assert stats["cancelled"] == 1, f"upstream not cancelled on disconnect: {stats}"
        assert stats["done"] == 1, f"the disconnected call leaked to completion: {stats}"
    finally:
        _stop(up_proc, proxy_proc)
