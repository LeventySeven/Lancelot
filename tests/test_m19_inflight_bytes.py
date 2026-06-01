"""T1 #4 (2026-06-02 byte-path build order) — aggregate in-flight body-bytes cap.

Verified gap (Rust pattern-mine, LanceDB two-budget): the native server admits on
a COUNT-only semaphore (LANCELOT_MAX_CONNECTIONS=1024) + a PER-REQUEST body cap
(LANCELOT_MAX_BODY=16 MiB). 1024 conns each buffering a large body ≈ 16 GiB of
transient RAM — the count cap does not bound AGGREGATE memory.

`LANCELOT_MAX_INFLIGHT_BYTES` (default 0 = OFF) bounds the SUM of in-flight request
bodies process-wide. Design decision (vs LanceDB's internal min-in-flight WAITING
admission): an HTTP front door SHEDS load — reject excess with 503 + Retry-After,
do NOT queue — which is deadlock-free (no waiting → no min-in-flight subtlety).
A forward-progress guarantee keeps it from wedging: when nothing is in flight, a
request is ALWAYS admitted even if it alone exceeds the budget.

This budget lives in the Rust transport (`service.rs::handle`), so these are
NATIVE-server tests (`lancelot.run`), NOT the uvicorn ASGI path. Real-server,
adversarial (cardinal rule #2): flood concurrent large bodies, assert some shed
(503) while others succeed (200, forward progress) and the server stays up.
"""
import concurrent.futures
import signal
import socket
import subprocess
import sys
import time

import pytest

httpx = pytest.importorskip("httpx")

_BODY = b"x" * (1024 * 1024)  # 1 MiB per request


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


# A handler that holds each request in flight (sleep) so concurrent 1 MiB bodies
# pile up against the budget. `_CAP` is the value of LANCELOT_MAX_INFLIGHT_BYTES
# set in-process before serving ("" = leave it unset = default OFF).
_SERVER = r"""
import os, sys, asyncio
_cap = {cap!r}
if _cap:
    os.environ["LANCELOT_MAX_INFLIGHT_BYTES"] = _cap
import lancelot

app = lancelot.App()

@app.post("/upload")
async def upload(request: lancelot.Request):
    await asyncio.sleep(0.4)   # keep the in-flight claim held
    return {{"ok": True}}

@app.get("/ping")
async def ping(request: lancelot.Request):
    return {{"ok": True}}

lancelot.run(app, "127.0.0.1", int(sys.argv[1]))
"""


def _flood(port, n=16):
    def _one(_):
        try:
            return httpx.post(f"http://127.0.0.1:{port}/upload",
                              content=_BODY, timeout=15).status_code
        except Exception as e:  # noqa: BLE001
            return repr(e)
    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as ex:
        return list(ex.map(_one, range(n)))


def test_inflight_budget_sheds_excess_with_503():
    """With a ~4 MB budget and 16 concurrent 1 MiB bodies, some requests are
    shed with 503 (the aggregate cap engaged) while others still succeed (200,
    forward progress), and the server stays responsive afterward."""
    proc, port = _spawn(_SERVER.format(cap="4000000"))
    try:
        codes = _flood(port, 16)
        assert 503 in codes, (
            f"expected aggregate-byte budget to shed some requests with 503; "
            f"got {codes}")
        assert 200 in codes, (
            f"forward-progress guarantee violated — every request shed: {codes}")
        # Server is still alive after the flood.
        r = httpx.get(f"http://127.0.0.1:{port}/ping", timeout=5)
        assert r.status_code == 200 and r.json() == {"ok": True}
    finally:
        _stop(proc)


def test_inflight_budget_off_by_default():
    """No LANCELOT_MAX_INFLIGHT_BYTES → the same flood is NOT shed (byte-identical
    to pre-M19): every request gets 200, none 503."""
    proc, port = _spawn(_SERVER.format(cap=""))
    try:
        codes = _flood(port, 16)
        assert 503 not in codes, (
            f"in-flight budget shed requests with NO cap set (default must be "
            f"OFF / byte-identical); got {codes}")
        assert all(c == 200 for c in codes), f"unexpected codes with cap off: {codes}"
    finally:
        _stop(proc)
