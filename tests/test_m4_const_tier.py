"""Const-bytes tier (Robyn-style ConstRouter) — a route served as PREBAKED bytes
straight from Rust: zero GIL, zero Request alloc, zero handler call. Flips the
small-response regime (health probes, static/cached endpoints) from a loss
vs uvicorn+uvloop into a decisive win (~183k vs ~66k req/s, per-core).

API: `lancelot._native.run(routes, host, port, const_routes=[(method, path,
body: bytes, content_type: str, status: int), ...])`.
"""
import concurrent.futures as cf
import signal
import socket
import subprocess
import sys
import time

import pytest

httpx = pytest.importorskip("httpx")

_SERVER = r"""
import sys
import lancelot._native as _native

async def echo(req):
    return {"h": "handler"}

_native.run(
    [("GET", "/echo", echo)],
    host="127.0.0.1",
    port=int(sys.argv[1]),
    const_routes=[
        ("GET", "/livez", b'{"status":"ok"}', "application/json", 200),
        ("GET", "/readyz", b"draining", "text/plain", 503),
    ],
)
"""

# A const route with an invalid content-type must fail at STARTUP with a clean
# error, not a panic across the FFI seam.
_BAD_SERVER = r"""
import sys
import lancelot._native as _native
_native.run([], host="127.0.0.1", port=int(sys.argv[1]),
            const_routes=[("GET", "/x", b"y", "bad\nctype", 200)])
"""


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture()
def server():
    port = _free_port()
    proc = subprocess.Popen([sys.executable, "-c", _SERVER, str(port)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.time() + 10
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(proc.stderr.read().decode()[:500])
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def test_const_route_serves_prebaked_bytes(server):
    r = httpx.get(f"{server}/livez")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/json"
    assert r.json() == {"status": "ok"}


def test_const_route_honors_custom_status_and_content_type(server):
    r = httpx.get(f"{server}/readyz")
    assert r.status_code == 503
    assert r.text == "draining"
    assert r.headers["content-type"].startswith("text/plain")


def test_const_and_handler_routes_coexist(server):
    # The handler route still goes through Python; the const route does not.
    assert httpx.get(f"{server}/echo").json() == {"h": "handler"}
    assert httpx.get(f"{server}/livez").json() == {"status": "ok"}
    # Unknown path still 404s (const tier didn't break routing).
    assert httpx.get(f"{server}/nope").status_code == 404


def test_const_route_holds_under_concurrent_flood(server):
    # The zero-GIL const path must serve a concurrent flood cleanly.
    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        codes = list(ex.map(lambda _: httpx.get(f"{server}/livez").status_code, range(200)))
    assert all(c == 200 for c in codes)


def test_const_route_bad_content_type_rejected_at_startup():
    # Prebaked::new validates the content-type → clean Python error at startup,
    # never a panic across the FFI seam.
    proc = subprocess.Popen([sys.executable, "-c", _BAD_SERVER, str(_free_port())],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    rc = proc.wait(timeout=10)
    err = proc.stderr.read().decode()
    assert rc != 0, "server should have failed to start on a bad const content-type"
    assert "ValueError" in err and "content-type" in err, err[-400:]
