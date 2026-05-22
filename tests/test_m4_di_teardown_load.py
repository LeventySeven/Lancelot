"""M4 sweep — yield-dependency TEARDOWN under REAL concurrent load. A yield-dep's
`finally:` (DB rollback, file/conn close) MUST run on EVERY exit path — 200,
handler exception (500), AND per-route timeout cancellation (504, B5) — with no
leak under concurrency. Leak detector: a dep increments `open` on enter and
`closed` on teardown; after a concurrent mix, open must equal closed must equal
the request count (every resource acquired was released).
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
import asyncio
import sys
import uvicorn
import lancelot
from lancelot.params import Depends

app = lancelot.App()
_stat = {"open": 0, "closed": 0, "peak": 0}

def resource():
    _stat["open"] += 1
    _stat["peak"] = max(_stat["peak"], _stat["open"] - _stat["closed"])
    try:
        yield "r"
    finally:
        _stat["closed"] += 1   # MUST run on every exit path

@app.get("/ok")
async def ok(r: str = Depends(resource)):
    return {"ok": True}

@app.get("/boom")
async def boom(r: str = Depends(resource)):
    raise RuntimeError("boom")          # 500 — teardown must still run

@app.get("/slow", timeout=0.1)
async def slow(r: str = Depends(resource)):
    await asyncio.sleep(2.0)            # 504 (cancelled) — teardown must still run
    return {"never": True}

@app.get("/stats")
async def stats():
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


def test_yield_dep_teardown_runs_on_every_path_no_leak_under_load(server):
    # A concurrent mix across all three exit paths.
    paths = (["/ok"] * 15) + (["/boom"] * 10) + (["/slow"] * 5)
    expected = {"/ok": 200, "/boom": 500, "/slow": 504}

    def hit(p):
        return p, httpx.get(f"{server}{p}", timeout=10).status_code

    with cf.ThreadPoolExecutor(max_workers=len(paths)) as ex:
        results = list(ex.map(hit, paths))

    for p, code in results:
        assert code == expected[p], f"{p} -> {code} (expected {expected[p]})"

    # Poll until teardown has drained (timeout/cancel teardown is async).
    deadline = time.time() + 10
    st = {}
    while time.time() < deadline:
        st = httpx.get(f"{server}/stats").json()
        if st["closed"] >= len(paths):
            break
        time.sleep(0.05)

    # Every acquired resource was released — no leak — across 200/500/504.
    assert st["open"] == len(paths), f"opened {st['open']} != {len(paths)}"
    assert st["closed"] == st["open"], f"LEAK: {st['open']} opened, {st['closed']} closed"
    assert st["peak"] > 1, f"deps did not overlap (peak={st['peak']}); not a real concurrency test"
