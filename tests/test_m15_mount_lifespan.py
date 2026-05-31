"""M15 T4 (#3) — mounted sub-app lifespans run under serve()/the ASGI lifespan
protocol, and the dispatcher propagates `root_path` to the sub-app's scope.

REAL-SERVER tests (cardinal rule #2): the parent Lancelot App is served under
uvicorn in a SUBPROCESS (uvicorn drives the ASGI `lifespan` protocol — the Rust
`_native.run` server does NOT, so this MUST be exercised over a real socket with
a real lifespan + SIGTERM, not an in-process TestClient).

Confirmed pre-fix bug: `_lifespan` (asgi.py) drove ONLY `app._lifespan`; a
mounted sub-app's startup/shutdown NEVER ran. The dispatcher (asgi.py) stripped
the mount prefix but never set `sub_scope["root_path"]`.
"""
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time

import pytest

httpx = pytest.importorskip("httpx")


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _spawn(src, statefile):
    port = _free_port()
    env = {**os.environ, "LANCELOT_STATEFILE": statefile}
    proc = subprocess.Popen(
        [sys.executable, "-c", src, str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    deadline = time.time() + 12
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"server exited rc={proc.returncode}: "
                f"{proc.stderr.read().decode()[:800]}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return proc, port
        except OSError:
            time.sleep(0.05)
    proc.kill()
    raise RuntimeError("server did not start")


def _read_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


# A parent Lancelot App with a mounted sub-app whose lifespan increments a
# startup counter (written to a sidecar file) and decrements on shutdown.
_LIFESPAN_SERVER = r"""
import contextlib, json, os, sys
import uvicorn
import lancelot

_STATE = os.environ["LANCELOT_STATEFILE"]

def _write(**kw):
    cur = {}
    if os.path.exists(_STATE):
        try:
            with open(_STATE) as f:
                cur = json.load(f)
        except Exception:
            cur = {}
    cur.update(kw)
    with open(_STATE, "w") as f:
        json.dump(cur, f)

@contextlib.asynccontextmanager
async def sub_lifespan(sub):
    _write(sub_started=True, sub_shutdown=False)
    try:
        yield
    finally:
        _write(sub_shutdown=True)

sub = lancelot.App(lifespan=sub_lifespan)

@sub.get("/ping")
async def sub_ping():
    return {"sub": True}

parent = lancelot.App()
parent.mount("/sub", sub)

@parent.get("/ping")
async def ping():
    return {"ok": True}

uvicorn.run(parent.asgi(), host="127.0.0.1", port=int(sys.argv[1]),
            log_level="warning", access_log=False, lifespan="on")
"""


def test_mounted_subapp_lifespan_runs():
    statefile = tempfile.mktemp(suffix=".json")
    proc, port = _spawn(_LIFESPAN_SERVER, statefile)
    try:
        base = f"http://127.0.0.1:{port}"
        assert httpx.get(f"{base}/ping").status_code == 200
        assert httpx.get(f"{base}/sub/ping").json() == {"sub": True}
        # Sub-app startup must have fired (today: it does NOT).
        st = _read_state(statefile)
        assert st.get("sub_started") is True, f"sub startup did not run: {st}"
        assert st.get("sub_shutdown") is False, f"sub shut down early: {st}"

        # SIGTERM → uvicorn drives lifespan.shutdown → sub-app teardown runs.
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
        st = _read_state(statefile)
        assert st.get("sub_shutdown") is True, \
            f"sub shutdown did not run on SIGTERM: {st}"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        try:
            os.remove(statefile)
        except OSError:
            pass


# A mounted sub-app that records the root_path it was dispatched with.
_ROOTPATH_SERVER = r"""
import json, os, sys
import uvicorn
import lancelot

_STATE = os.environ["LANCELOT_STATEFILE"]

async def echo_root(scope, receive, send):
    # Bare ASGI app: capture scope["root_path"] for the test to assert.
    assert scope["type"] == "http"
    while True:
        m = await receive()
        if not m.get("more_body", False):
            break
    rp = scope.get("root_path", "<missing>")
    with open(_STATE, "w") as f:
        json.dump({"root_path": rp, "path": scope.get("path")}, f)
    body = json.dumps({"root_path": rp}).encode()
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})

parent = lancelot.App()
parent.mount("/mounted", echo_root)

@parent.get("/ping")
async def ping():
    return {"ok": True}

uvicorn.run(parent.asgi(), host="127.0.0.1", port=int(sys.argv[1]),
            log_level="warning", access_log=False, lifespan="on")
"""


def test_mounted_subapp_root_path():
    statefile = tempfile.mktemp(suffix=".json")
    proc, port = _spawn(_ROOTPATH_SERVER, statefile)
    try:
        base = f"http://127.0.0.1:{port}"
        r = httpx.get(f"{base}/mounted/thing")
        assert r.status_code == 200
        # The dispatcher must set root_path = the stripped mount prefix so the
        # sub-app can emit correct absolute URLs (openapi server URL,
        # url_path_for). The sub-app sees path stripped of the prefix.
        assert r.json()["root_path"] == "/mounted", r.json()
        st = _read_state(statefile)
        assert st.get("root_path") == "/mounted", st
        assert st.get("path") == "/thing", st
    finally:
        _stop(proc)
        try:
            os.remove(statefile)
        except OSError:
            pass


# A parent with TWO mounted sub-apps: the SECOND raises during startup. Only
# the first (already-entered) must tear down; the parent must shut down cleanly
# (no orphaned/never-torn-down sub-app, no crash on shutdown).
_PARTIAL_SERVER = r"""
import contextlib, json, os, sys
import uvicorn
import lancelot

_STATE = os.environ["LANCELOT_STATEFILE"]

def _write(**kw):
    cur = {}
    if os.path.exists(_STATE):
        try:
            with open(_STATE) as f:
                cur = json.load(f)
        except Exception:
            cur = {}
    cur.update(kw)
    with open(_STATE, "w") as f:
        json.dump(cur, f)

@contextlib.asynccontextmanager
async def good_lifespan(sub):
    _write(good_started=True, good_torn_down=False)
    try:
        yield
    finally:
        _write(good_torn_down=True)

@contextlib.asynccontextmanager
async def bad_lifespan(sub):
    _write(bad_entered=True)
    raise RuntimeError("sub startup boom")
    yield  # unreachable

good = lancelot.App(lifespan=good_lifespan)
bad = lancelot.App(lifespan=bad_lifespan)

parent = lancelot.App()
parent.mount("/good", good)
parent.mount("/bad", bad)

uvicorn.run(parent.asgi(), host="127.0.0.1", port=int(sys.argv[1]),
            log_level="warning", access_log=False, lifespan="on")
"""


def test_partial_startup_safety():
    statefile = tempfile.mktemp(suffix=".json")
    port = _free_port()
    env = {**os.environ, "LANCELOT_STATEFILE": statefile}
    proc = subprocess.Popen(
        [sys.executable, "-c", _PARTIAL_SERVER, str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    try:
        # A sub-app raising on startup must abort lifespan startup (uvicorn
        # exits non-zero / never becomes ready). The already-entered "good"
        # sub-app must be torn down (AsyncExitStack LIFO guarantee).
        rc = proc.wait(timeout=12)
        # uvicorn exits non-zero when lifespan.startup fails.
        assert rc != 0, "startup failure should abort the server"
        st = _read_state(statefile)
        assert st.get("good_started") is True, f"good never started: {st}"
        assert st.get("bad_entered") is True, f"bad never entered: {st}"
        # The good sub-app's teardown ran even though `bad` blew up after it.
        assert st.get("good_torn_down") is True, \
            f"partial-startup did not tear down the entered sub-app: {st}"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        try:
            os.remove(statefile)
        except OSError:
            pass


def _stop(proc):
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
