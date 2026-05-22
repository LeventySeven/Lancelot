"""M4 resilience (cloud) — the `max_concurrent_requests` overload path under REAL
concurrent load. The B4 cap → 503 + Retry-After is correctness-tested in
test_m2_tier_b, but that test never PARSED the body — so it missed that the 503
body was truncated to invalid JSON (`{"detail":"server overloaded` — the `[:28]`
slice cut a 30-byte string). This drives a real uvicorn server with a flood that
exceeds the cap and asserts every 503 carries Retry-After AND a VALID JSON body,
and that the server recovers. (Cardinal rule #2: validate resilience under load.)
"""
import concurrent.futures as cf
import json
import signal
import socket
import subprocess
import sys
import time

import pytest

httpx = pytest.importorskip("httpx")

_SERVER = r"""
import asyncio
import sys
import uvicorn
import lancelot

app = lancelot.App(max_concurrent_requests=4)

@app.get("/slow")
async def slow():
    await asyncio.sleep(0.5)   # hold the slot so the cap is actually exceeded
    return {"ok": True}

uvicorn.run(app.asgi(), host="127.0.0.1", port=int(sys.argv[1]),
            log_level="warning", access_log=False, loop="uvloop", http="httptools")
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
    deadline = time.time() + 12
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


def test_overload_503_has_retry_after_and_valid_json_under_load(server):
    n = 24  # >> the cap of 4

    def hit(_):
        r = httpx.get(f"{server}/slow", timeout=10)
        return r.status_code, r.headers.get("retry-after"), r.text

    with cf.ThreadPoolExecutor(max_workers=n) as ex:
        results = list(ex.map(hit, range(n)))

    codes = [c for c, _, _ in results]
    # The flood exceeds the cap → a mix of 200 (got a slot) and 503 (refused).
    assert 200 in codes, codes
    assert 503 in codes, f"cap never triggered a 503: {codes}"

    for code, retry_after, text in results:
        if code == 503:
            assert retry_after == "1", f"503 missing Retry-After: {retry_after!r}"
            # Must be VALID JSON (the truncation bug emitted `{"detail":"server overloaded`).
            assert json.loads(text) == {"detail": "server overloaded"}, text

    # Server recovers once the flood drains.
    assert httpx.get(f"{server}/slow", timeout=10).status_code == 200
