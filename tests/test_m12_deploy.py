"""M12 — deploy/container friendliness (make `pip install lancelot` + a slim
Docker image + any PaaS Just Work, like FastAPI).

  D1 — bind address resolves from env: PORT (universal PaaS var) / LANCELOT_PORT
       / LANCELOT_HOST, with explicit args winning. Bare $HOST is ignored on
       purpose (shells set it to the hostname).
  D2 — `App` is directly ASGI-callable → `uvicorn yourmod:app` works exactly
       like FastAPI on every ASGI platform. (Regression guard for the deploy
       path; if this breaks, every platform recipe breaks.)
  D3 — the native server actually binds the $PORT the platform injects.
"""
import asyncio
import os
import socket
import subprocess
import sys
import time

import pytest

import lancelot
from lancelot.app import _resolve_bind


# ---------- D1: env-driven bind resolution ------------------------------

def test_d1_resolve_bind_defaults():
    assert _resolve_bind(None, None) == ("127.0.0.1", 8000)


def test_d1_resolve_bind_explicit_args_win(monkeypatch):
    monkeypatch.setenv("PORT", "9999")
    monkeypatch.setenv("LANCELOT_HOST", "0.0.0.0")
    assert _resolve_bind("127.0.0.1", 1234) == ("127.0.0.1", 1234)


def test_d1_resolve_bind_reads_platform_PORT(monkeypatch):
    monkeypatch.delenv("LANCELOT_PORT", raising=False)
    monkeypatch.setenv("PORT", "8080")
    assert _resolve_bind(None, None) == ("127.0.0.1", 8080)


def test_d1_resolve_bind_lancelot_port_wins_over_PORT(monkeypatch):
    monkeypatch.setenv("PORT", "8080")
    monkeypatch.setenv("LANCELOT_PORT", "7070")
    assert _resolve_bind(None, None)[1] == 7070


def test_d1_resolve_bind_lancelot_host(monkeypatch):
    monkeypatch.setenv("LANCELOT_HOST", "0.0.0.0")
    assert _resolve_bind(None, None)[0] == "0.0.0.0"


def test_d1_resolve_bind_ignores_bare_HOST(monkeypatch):
    # Many shells/PaaS set $HOST to the machine hostname — must NOT bind to it.
    monkeypatch.setenv("HOST", "some-host.internal")
    monkeypatch.delenv("LANCELOT_HOST", raising=False)
    assert _resolve_bind(None, None)[0] == "127.0.0.1"


def test_d1_resolve_bind_garbage_port_falls_back(monkeypatch):
    monkeypatch.delenv("LANCELOT_PORT", raising=False)
    monkeypatch.setenv("PORT", "not-a-port")
    assert _resolve_bind(None, None)[1] == 8000


# ---------- D2: App is ASGI-callable (uvicorn yourmod:app) --------------

def test_d2_app_is_asgi_callable():
    app = lancelot.App()
    # ASGI = an async callable taking (scope, receive, send).
    assert asyncio.iscoroutinefunction(app.__call__), (
        "App must be an async ASGI callable for `uvicorn yourmod:app`")
    import inspect
    params = list(inspect.signature(app.__call__).parameters)
    assert params[:3] == ["scope", "receive", "send"], params


def test_d2_app_run_method_exists():
    app = lancelot.App()
    assert callable(getattr(app, "run", None)), \
        "App.run() enables `CMD [\"python\",\"app.py\"]` Docker deploys"


# ---------- D3: native server binds the platform's $PORT ----------------

_PORT_SERVER = r"""
import lancelot
app = lancelot.App()

@app.get("/ping")
async def ping(req):
    return {"ok": True}

app.run()  # binds from env: LANCELOT_HOST (default 127.0.0.1) + $PORT
"""


def test_d3_native_server_binds_PORT_env():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    env = {**os.environ, "PORT": str(port)}
    env.pop("LANCELOT_PORT", None)
    env.pop("LANCELOT_HOST", None)  # default 127.0.0.1 (loopback, testable)
    proc = subprocess.Popen([sys.executable, "-c", _PORT_SERVER],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=env)
    try:
        deadline = time.time() + 10
        bound = False
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"server exited rc={proc.returncode}: "
                    f"{proc.stderr.read().decode()[:400]}")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    bound = True
                    break
            except OSError:
                time.sleep(0.05)
        assert bound, f"native server did not bind $PORT={port} from env"
    finally:
        import signal
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
