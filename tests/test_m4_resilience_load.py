"""M4 Tier-V1 — resilience under ADVERSARIAL load, against the REAL Rust server
(lancelot._native.run) in a subprocess. Unit tests proved these defenses fire
ONCE; this proves they hold under a flood / a hostile client and that the server
stays healthy. Thesis of M4: shipped + green unit tests != works under load.

  V1.1 slow-loris   — a client dribbling an incomplete request head is dropped at
                      the header_read_timeout, and does NOT block healthy traffic.
  V1.2 body-cap     — a flood of >16 MiB uploads all 413 without crashing or
                      growing RSS unboundedly; the server keeps serving.
"""
import concurrent.futures as cf
import os
import signal
import socket
import subprocess
import sys
import threading
import time

import pytest

httpx = pytest.importorskip("httpx")

_SERVER = r"""
import asyncio
import sys
import lancelot._native as _native

async def echo(req):
    return {"ok": True, "n": len(req.body)}

async def slow(req):
    await asyncio.sleep(0.3)  # an in-flight handler with real work
    return {"ok": True, "slow": True}

_native.run([("GET", "/echo", echo), ("POST", "/echo", echo),
             ("GET", "/slow", slow)],
            host="127.0.0.1", port=int(sys.argv[1]))
"""


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# Python ASGI seam (lancelot.App under uvicorn) — the deployable path — with a
# per-route timeout + a yield-dep whose teardown we count, to prove the timeout
# fires AND cleans up under concurrency (V1.5).
_TIMEOUT_SERVER = r"""
import asyncio
import sys
import uvicorn
import lancelot
from lancelot.params import Depends

app = lancelot.App()
_td = {"n": 0}

def db():
    try:
        yield "session"
    finally:
        _td["n"] += 1

@app.get("/slow", timeout=0.2)
async def slow(s: str = Depends(db)):
    await asyncio.sleep(5.0)  # far exceeds the 0.2 s route timeout
    return {"never": True}

@app.get("/teardowns")
async def teardowns():
    return {"n": _td["n"]}

@app.get("/ping")
async def ping():
    return {"ok": True}

uvicorn.run(app.asgi(), host="127.0.0.1", port=int(sys.argv[1]),
            log_level="warning", access_log=False, loop="uvloop", http="httptools")
"""


def _spawn(env_extra=None, src=_SERVER):
    port = _free_port()
    env = {**os.environ, **(env_extra or {})}
    proc = subprocess.Popen([sys.executable, "-c", src, str(port)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    deadline = time.time() + 10
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


def _stop(proc):
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def test_v1_1_slowloris_dribbled_head_dropped_and_healthy_traffic_survives():
    # 1 s header deadline so the test is fast (real default is 15 s).
    proc, port = _spawn({"LANCELOT_HEADER_TIMEOUT_MS": "1000"})
    try:
        base = f"http://127.0.0.1:{port}"
        # Sanity: healthy request works.
        assert httpx.get(f"{base}/echo").status_code == 200

        # Hostile: open a raw socket, send a PARTIAL request head and stop —
        # never the terminating CRLFCRLF. The server must drop us at ~1 s.
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        s.sendall(b"GET /echo HTTP/1.1\r\nHost: x\r\nX-Dribble: a")
        s.settimeout(5)
        t0 = time.time()
        data = s.recv(4096)  # blocks until the server closes (b'') or 408s
        elapsed = time.time() - t0
        s.close()
        # Connection terminated near the deadline, not hung for the full 5 s.
        assert elapsed < 4.0, f"slow-loris not dropped in time ({elapsed:.1f}s)"
        # Either a clean close (b'') or a 4xx — both end the attack.
        assert data == b"" or data.startswith(b"HTTP/1.1 4"), data[:60]

        # The hostile connection did NOT wedge the server — healthy traffic OK.
        assert httpx.get(f"{base}/echo").status_code == 200
    finally:
        _stop(proc)


def test_v1_3_connection_cap_throttles_flood_without_dropping_or_crashing():
    # Small cap so a flood >> cap is reachable under the OS FD limit.
    proc, port = _spawn({"LANCELOT_MAX_CONNECTIONS": "8"})
    try:
        base = f"http://127.0.0.1:{port}"
        N = 64  # >> cap

        def hit(_):
            # No keep-alive → each request is its own connection (exercises the
            # per-connection permit cap, not request multiplexing).
            with httpx.Client(limits=httpx.Limits(max_keepalive_connections=0),
                              timeout=30) as c:
                return c.get(f"{base}/slow").status_code

        t0 = time.time()
        with cf.ThreadPoolExecutor(max_workers=N) as ex:
            codes = list(ex.map(hit, range(N)))
        elapsed = time.time() - t0

        # Backpressure, not drops: every request is eventually served (200),
        # the server never crashes, and the cap THROTTLES (64 conns / cap 8 ×
        # 0.3 s ≈ 2.4 s of serialization — provably > the ~0.3 s an uncapped
        # server would take, confirming the cap actually gates concurrency).
        assert all(c == 200 for c in codes), codes
        assert proc.poll() is None, "server crashed under connection flood"
        assert elapsed > 1.0, f"cap did not throttle (elapsed {elapsed:.2f}s)"
        assert httpx.get(f"{base}/echo").status_code == 200
    finally:
        _stop(proc)


def test_v1_4_graceful_drain_lets_inflight_request_finish_on_sigterm():
    # B1 claim: SIGTERM drains in-flight work (Robyn hard-kills; we must not).
    proc, port = _spawn()
    try:
        base = f"http://127.0.0.1:{port}"
        assert httpx.get(f"{base}/echo").status_code == 200

        result = {}

        def inflight():
            try:
                r = httpx.get(f"{base}/slow", timeout=10)
                result["status"] = r.status_code
            except Exception as e:  # noqa: BLE001
                result["error"] = repr(e)

        th = threading.Thread(target=inflight)
        th.start()
        time.sleep(0.1)  # ensure the /slow handler is in-flight (0.3 s sleep)
        proc.send_signal(signal.SIGTERM)  # drain, don't hard-kill
        th.join(timeout=10)

        # The in-flight request must COMPLETE (200), not be cancelled/dropped.
        assert result.get("status") == 200, f"in-flight request not drained: {result}"
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_v1_5_per_route_timeout_under_concurrency_504s_and_runs_teardown():
    # Per-route timeout (B5) under concurrent load: every slow request must 504
    # near the deadline (not hang to the 5 s sleep), every yield-dep teardown
    # must run despite the cancellation, and the server stays healthy.
    proc, port = _spawn(src=_TIMEOUT_SERVER)
    try:
        base = f"http://127.0.0.1:{port}"
        assert httpx.get(f"{base}/ping").status_code == 200
        N = 20

        def hit(_):
            return httpx.get(f"{base}/slow", timeout=10).status_code

        t0 = time.time()
        with cf.ThreadPoolExecutor(max_workers=N) as ex:
            codes = list(ex.map(hit, range(N)))
        elapsed = time.time() - t0

        # All time out (504), concurrently — bounded well under the 5 s sleep.
        assert all(c == 504 for c in codes), codes
        assert elapsed < 3.0, f"timeouts did not fire concurrently ({elapsed:.1f}s)"
        # Every yield-dep `finally` ran despite the timeout cancellation.
        td = httpx.get(f"{base}/teardowns").json()["n"]
        assert td == N, f"yield-dep teardown leaked under timeout: {td}/{N}"
        # Server healthy afterward.
        assert httpx.get(f"{base}/ping").status_code == 200
    finally:
        _stop(proc)


def _rss_kb(pid):
    try:
        out = subprocess.check_output(["ps", "-o", "rss=", "-p", str(pid)])
        return int(out.strip())
    except Exception:
        return None


def test_v1_2_body_cap_flood_all_413_and_server_stays_healthy():
    proc, port = _spawn()
    try:
        base = f"http://127.0.0.1:{port}"
        big = b"x" * (16 * 1024 * 1024 + 1)  # just over the 16 MiB cap

        rss_before = _rss_kb(proc.pid)

        def hit(_):
            return httpx.post(f"{base}/echo", content=big, timeout=30).status_code

        # Concurrent flood of oversized uploads.
        with cf.ThreadPoolExecutor(max_workers=12) as ex:
            codes = list(ex.map(hit, range(24)))
        rss_after = _rss_kb(proc.pid)

        # Every oversized upload is rejected (the CRITICAL DoS cap), none crash.
        assert all(c == 413 for c in codes), codes
        # Server is still alive and serving normal traffic.
        assert proc.poll() is None, "server died under the body-cap flood"
        assert httpx.get(f"{base}/echo").status_code == 200

        # RSS did not balloon to flood_size * concurrency (generous bound — the
        # point is "bounded", not a tight number; per-conn Limited caps intake).
        if rss_before is not None and rss_after is not None:
            grew_mib = (rss_after - rss_before) / 1024
            assert grew_mib < 600, f"RSS grew {grew_mib:.0f} MiB under flood"
    finally:
        _stop(proc)
