"""M3 end-to-end HTTP integration test of the Rust server (lancelot._native.run
= hyper+tokio+pyo3). Unlike test_m3_rust_swaps.py (in-process _drive helpers),
this stands up the REAL server in a subprocess and drives it over real HTTP —
the only test that exercises the full vertical slice: socket → hyper → matchit
router → R2 lazy request → GIL dispatch → pydantic-core serialize / R3 stream.

Validates the hot-path swaps are transparent + correct over the wire:
  R2  real method/path/query/headers/body reach the handler
  R3  async-gen → chunked streaming response
  body cap → 413 ; routing → 404 / 405 ; connection cap holds under load.
"""
import os
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
    return {
        "method": req.method,
        "path": req.path,
        "q": dict(req.query_params),
        "probe": req.headers.get("x-probe", ""),
        "body": req.body.decode("utf-8", "replace"),
    }


async def stream_handler(req):
    for i in range(3):
        yield f"chunk{i}\n".encode()


port = int(sys.argv[1])
_native.run(
    [("GET", "/echo", echo), ("POST", "/echo", echo), ("GET", "/stream", stream_handler)],
    host="127.0.0.1",
    port=port,
)
"""


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _wait_until_listening(port: int, proc: subprocess.Popen, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"server exited early (rc={proc.returncode})\n"
                f"{proc.stderr.read().decode() if proc.stderr else ''}"
            )
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("server did not start listening in time")


@pytest.fixture()
def server():
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-c", _SERVER, str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_until_listening(port, proc)
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.send_signal(signal.SIGTERM)  # serve() breaks the accept loop
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def test_r2_get_real_method_path_query_headers(server):
    r = httpx.get(f"{server}/echo", params={"a": "1", "b": "2"},
                  headers={"X-Probe": "hello"})
    assert r.status_code == 200
    body = r.json()
    # R2: the REAL request data reaches the handler (M0 hardcoded POST/echo).
    assert body["method"] == "GET"
    assert body["path"] == "/echo"
    assert body["q"] == {"a": "1", "b": "2"}
    assert body["probe"] == "hello"


def test_r2_post_body(server):
    r = httpx.post(f"{server}/echo", content=b'{"k":"v"}')
    assert r.status_code == 200
    body = r.json()
    assert body["method"] == "POST"
    assert body["body"] == '{"k":"v"}'


def test_r3_streaming_response(server):
    with httpx.stream("GET", f"{server}/stream") as r:
        assert r.status_code == 200
        chunks = list(r.iter_bytes())
    assert b"".join(chunks) == b"chunk0\nchunk1\nchunk2\n"


def test_404_unknown_path(server):
    assert httpx.get(f"{server}/nope").status_code == 404


def test_405_wrong_method(server):
    # /echo exists for GET/POST but not DELETE → 405 (path exists), not 404.
    assert httpx.delete(f"{server}/echo").status_code == 405


def test_413_body_cap(server):
    # >16 MiB body → 413 (the Rust path's Limited cap; CRITICAL DoS fix).
    big = b"x" * (16 * 1024 * 1024 + 1)
    r = httpx.post(f"{server}/echo", content=big)
    assert r.status_code == 413


def test_concurrent_requests_hold(server):
    # The connection cap + GIL-released dispatch must serve concurrent
    # requests without deadlock. Honest sanity check (not a perf claim).
    import concurrent.futures as cf

    def hit(_):
        return httpx.get(f"{server}/echo", params={"i": str(_)}).status_code

    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        codes = list(ex.map(hit, range(64)))
    assert all(c == 200 for c in codes)
