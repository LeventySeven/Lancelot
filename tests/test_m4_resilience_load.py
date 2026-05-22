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
import time

import pytest

httpx = pytest.importorskip("httpx")

_SERVER = r"""
import sys
import lancelot._native as _native

async def echo(req):
    return {"ok": True, "n": len(req.body)}

_native.run([("GET", "/echo", echo), ("POST", "/echo", echo)],
            host="127.0.0.1", port=int(sys.argv[1]))
"""


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _spawn(env_extra=None):
    port = _free_port()
    env = {**os.environ, **(env_extra or {})}
    proc = subprocess.Popen([sys.executable, "-c", _SERVER, str(port)],
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
