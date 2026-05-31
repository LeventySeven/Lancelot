"""M17 P3 — inbound rate-limiter middleware (`lancelot.ratelimit.RateLimit`).

Real-server (subprocess uvicorn + raw HTTP) flood tests: the bug lives under a
real socket where a flood of requests races the token bucket. A `TestClient`
in-process would not exercise the per-connection client-host extraction nor a
genuine burst.

What we prove:
  - Flooding past the configured rate returns 429 with a valid `Retry-After`
    and `RateLimit-Limit` / `RateLimit-Remaining` / `RateLimit-Reset` headers.
  - `key='ip'` vs `key='api_key'` bucket INDEPENDENTLY (two api-keys on the
    same socket each get their own allowance).
  - Requests within the rate pass through (200) untouched.
"""
import socket
import subprocess
import sys
import time

import pytest

httpx = pytest.importorskip("httpx")


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
                               f"{proc.stderr.read().decode()[:800]}")
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


# A tiny app guarded by RateLimit keyed on the client IP. burst=5 → the bucket
# starts with 5 tokens; the 6th request inside the same second is shed with 429.
# The refill rate is low (5/minute) so the flood definitely exhausts it.
_RL_IP = r"""
import sys, uvicorn, lancelot
from lancelot.ratelimit import RateLimit

app = lancelot.App()

@app.get("/ping")
async def ping():
    return {"ok": True}

app.add_middleware(RateLimit, rate="5/minute", key="ip", burst=5)

uvicorn.run(app.asgi(), host="127.0.0.1", port=int(sys.argv[1]),
            log_level="warning", access_log=False, loop="uvloop", http="httptools")
"""


# Keyed on the api-key header: two different keys must NOT share a bucket even
# over the same TCP source. burst=3 each.
_RL_APIKEY = r"""
import sys, uvicorn, lancelot
from lancelot.ratelimit import RateLimit

app = lancelot.App()

@app.get("/ping")
async def ping():
    return {"ok": True}

app.add_middleware(RateLimit, rate="3/minute", key="api_key", burst=3)

uvicorn.run(app.asgi(), host="127.0.0.1", port=int(sys.argv[1]),
            log_level="warning", access_log=False, loop="uvloop", http="httptools")
"""


# Generous rate so nothing is shed: every request inside the window passes.
_RL_UNDER = r"""
import sys, uvicorn, lancelot
from lancelot.ratelimit import RateLimit

app = lancelot.App()

@app.get("/ping")
async def ping():
    return {"ok": True}

app.add_middleware(RateLimit, rate="1000/minute", key="ip", burst=1000)

uvicorn.run(app.asgi(), host="127.0.0.1", port=int(sys.argv[1]),
            log_level="warning", access_log=False, loop="uvloop", http="httptools")
"""


def test_ratelimit_429_and_retry_after():
    proc, port = _spawn(_RL_IP)
    try:
        base = f"http://127.0.0.1:{port}"
        statuses = []
        last = None
        for _ in range(12):
            r = httpx.get(f"{base}/ping", timeout=10)
            statuses.append(r.status_code)
            last = r
        # First 5 (burst) succeed; the rest are shed with 429.
        assert statuses[:5] == [200] * 5, statuses
        assert 429 in statuses, statuses
        # The 429 response carries Retry-After + the RateLimit-* family.
        shed = None
        for _ in range(20):
            r = httpx.get(f"{base}/ping", timeout=10)
            if r.status_code == 429:
                shed = r
                break
        assert shed is not None, "expected a 429 under flood"
        ra = shed.headers.get("retry-after")
        assert ra is not None, dict(shed.headers)
        assert int(ra) >= 0          # delta-seconds, parseable int
        assert shed.headers.get("ratelimit-limit") == "5", dict(shed.headers)
        assert shed.headers.get("ratelimit-remaining") == "0", dict(shed.headers)
        rst = shed.headers.get("ratelimit-reset")
        assert rst is not None and int(rst) >= 0, dict(shed.headers)
    finally:
        _stop(proc)


def test_ratelimit_key_by_ip_vs_api_key():
    proc, port = _spawn(_RL_APIKEY)
    try:
        base = f"http://127.0.0.1:{port}"
        # Exhaust key "alpha" (burst=3 → 3 ok, 4th shed).
        s_alpha = [httpx.get(f"{base}/ping", headers={"x-api-key": "alpha"},
                             timeout=10).status_code for _ in range(4)]
        assert s_alpha[:3] == [200, 200, 200], s_alpha
        assert s_alpha[3] == 429, s_alpha
        # Key "beta" has its OWN full bucket — first request must still be 200
        # even though alpha is exhausted on the same socket.
        r_beta = httpx.get(f"{base}/ping", headers={"x-api-key": "beta"},
                           timeout=10)
        assert r_beta.status_code == 200, (r_beta.status_code, r_beta.text)
    finally:
        _stop(proc)


def test_ratelimit_under_limit_passes():
    proc, port = _spawn(_RL_UNDER)
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(30):
            r = httpx.get(f"{base}/ping", timeout=10)
            assert r.status_code == 200, (r.status_code, r.text)
            assert r.json() == {"ok": True}
    finally:
        _stop(proc)


def test_ratelimit_import_is_pydantic_only():
    # `import lancelot.ratelimit` must NOT eagerly pull httpx / redis (the
    # pydantic-only runtime rule). Run in a clean subprocess.
    src = (
        "import sys\n"
        "import lancelot.ratelimit\n"
        "assert 'httpx' not in sys.modules, 'httpx eagerly imported'\n"
        "assert 'redis' not in sys.modules, 'redis eagerly imported'\n"
        "print('clean')\n"
    )
    out = subprocess.run([sys.executable, "-c", src],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "clean" in out.stdout
