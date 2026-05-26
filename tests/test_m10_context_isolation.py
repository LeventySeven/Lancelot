"""M10 — per-request ContextVar isolation on the NATIVE Rust server path.

A multi-agent recheck flagged that the native `_native.run` path shares one
server-wide TaskLocals/context (lib.rs/service.rs), so a ContextVar written by
one handler could leak into the next request — a correctness hazard FastAPI
does not have (asyncio copies the context per task). This drives a REAL server
(subprocess + HTTP) and asserts isolation: every request must see the default,
never a prior request's write.
"""
import os
import socket
import subprocess
import sys
import time

import pytest

httpx = pytest.importorskip("httpx")


_CTX_SERVER = r"""
import sys
import contextvars
import lancelot._native as _native

# A user-defined ContextVar (the common case: request-scoped state a handler
# stashes for helpers to read). Default proves isolation when read fresh.
_cv = contextvars.ContextVar("leak_probe", default="UNSET")


async def probe(req):
    # What does THIS handler see before it writes anything? With per-request
    # context isolation it must always be the default "UNSET"; if the context
    # is shared across requests it will be the PREVIOUS request's write.
    seen = _cv.get()
    body = req.body.decode() if req.body else "empty"
    _cv.set("REQ-" + body)
    return {"seen_before": seen}


_native.run([("POST", "/probe", probe)],
            host="127.0.0.1", port=int(sys.argv[1]))
"""


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _spawn(src, env_extra=None):
    port = _free_port()
    env = {**os.environ, **(env_extra or {})}
    proc = subprocess.Popen([sys.executable, "-c", src, str(port)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=env)
    deadline = time.time() + 10
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"server exited rc={proc.returncode}: "
                f"{proc.stderr.read().decode()[:500]}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return proc, port
        except OSError:
            time.sleep(0.05)
    proc.kill()
    raise RuntimeError("server did not start")


def _stop(proc):
    import signal
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def test_contextvar_does_not_leak_across_requests_native_server():
    proc, port = _spawn(_CTX_SERVER)
    try:
        base = f"http://127.0.0.1:{port}"
        # Sequential requests with distinct bodies. Each handler writes
        # "REQ-<body>"; the NEXT request must NOT see it.
        seen = []
        for body in ("A", "B", "C", "D"):
            r = httpx.post(f"{base}/probe", content=body.encode(), timeout=5)
            assert r.status_code == 200, r.text
            seen.append(r.json()["seen_before"])
        # Every request must see the fresh default — never a prior write.
        assert seen == ["UNSET", "UNSET", "UNSET", "UNSET"], (
            f"ContextVar leaked across requests on the native path: {seen}")
    finally:
        _stop(proc)


_LOOP_SERVER = r"""
import sys
import lancelot._native as _native

async def ping(req):
    return {"ok": True}

_native.run([("GET", "/ping", ping)], host="127.0.0.1", port=int(sys.argv[1]))
"""


def test_native_server_serves_under_uvloop_and_stdlib_fallback():
    """The embedded loop prefers uvloop (perf) but must serve correctly with
    the stdlib-asyncio fallback too (uvloop absent / Windows / LANCELOT_LOOP=
    asyncio escape hatch)."""
    # Default (uvloop when installed).
    proc, port = _spawn(_LOOP_SERVER)
    try:
        assert httpx.get(f"http://127.0.0.1:{port}/ping", timeout=5).json() \
            == {"ok": True}
    finally:
        _stop(proc)
    # Forced stdlib asyncio — the soft-dep fallback path.
    proc, port = _spawn(_LOOP_SERVER, env_extra={"LANCELOT_LOOP": "asyncio"})
    try:
        assert httpx.get(f"http://127.0.0.1:{port}/ping", timeout=5).json() \
            == {"ok": True}
    finally:
        _stop(proc)
