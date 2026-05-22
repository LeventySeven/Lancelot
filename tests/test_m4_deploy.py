"""M4 Tier-V3.3 — multiprocess deployment (the established scaling model).

The Rust server anti-scales on tokio threads (one GIL/loop per process), so the
scaling model is N PROCESSES × 1 thread sharing the port via SO_REUSEPORT
(`LANCELOT_WORKERS=1` each). This smoke test pins that N processes co-bind the
same port and all serve.

PLATFORM CAVEAT (validated here): SO_REUSEPORT *load-balancing* is a LINUX
feature. macOS/BSD SO_REUSEPORT lets N sockets bind but delivers connections to
ONE — it does NOT distribute. So on Linux we assert distribution; elsewhere we
assert only co-bind + serve, and the deploy recipe (docs/deploy.md) tells macOS
users to front N procs (on distinct ports) with a proxy. This is also why the
benchmark's macOS multiprocess numbers are a no-distribution FLOOR, not the
Linux ceiling — see benchmarks/README.md.
"""
import os
import signal
import socket
import subprocess
import sys
import time

import pytest

httpx = pytest.importorskip("httpx")

_PID_SERVER = r"""
import os, sys, lancelot._native as n
async def whoami(req):
    return {"pid": os.getpid()}
n.run([("GET", "/whoami", whoami)], host="127.0.0.1", port=int(sys.argv[1]))
"""


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_v3_3_multiprocess_so_reuseport_cobind_and_serve():
    port = _free_port()
    n_workers = 4
    env = {**os.environ, "LANCELOT_WORKERS": "1"}
    procs = [subprocess.Popen([sys.executable, "-c", _PID_SERVER, str(port)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
             for _ in range(n_workers)]
    try:
        # Wait for the port to accept, then let all N finish binding.
        deadline = time.time() + 12
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), 0.2).close()
                break
            except OSError:
                time.sleep(0.05)
        time.sleep(1.0)

        # Every worker co-bound the same port via SO_REUSEPORT (no "address in
        # use" crash) — the core of the multiprocess model.
        for p in procs:
            assert p.poll() is None, \
                f"a worker died (port co-bind failed): {p.stderr.read().decode()[:300]}"

        # All requests are served 200, across distinct connections.
        pids = set()
        for _ in range(40):
            with httpx.Client(limits=httpx.Limits(max_keepalive_connections=0)) as c:
                r = c.get(f"http://127.0.0.1:{port}/whoami")
                assert r.status_code == 200
                pids.add(r.json()["pid"])

        # Distribution is a LINUX SO_REUSEPORT feature; macOS/BSD delivers to one.
        if sys.platform.startswith("linux"):
            assert len(pids) > 1, f"SO_REUSEPORT did not load-balance on Linux: {pids}"
    finally:
        for p in procs:
            p.send_signal(signal.SIGTERM)
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
