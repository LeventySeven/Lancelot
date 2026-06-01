"""M18 Task#7 — the NATIVE `app.run()` server drives the ASGI lifespan protocol.

REAL-SERVER tests (cardinal rule #2): the app is served by the Rust `_native.run`
server in a SUBPROCESS that ends in `app.run()` — NOT TestClient, NOT uvicorn.
TestClient/uvicorn drive the lifespan today and would HIDE the very bug this
fixes (the native server used to silently no-op `@app.on_event` / `lifespan=` /
`@app.lifetime_task` / mounted sub-app lifespans). The proof must run over a real
socket against the real native server, with a real SIGTERM.

GATE 1 (functional, the primary proof). Asserts:
 (a) the first `GET /probe` sees startup state (`app.state.client == "READY"`) —
     today an AttributeError -> 500;
 (b) lifespan-enter + >=2 lifetime-daemon heartbeats appear BEFORE the first
     request (the LOOP-IDENTITY proof: the daemon runs on the server's own loop);
 (c) the mounted sub-app's lifespan marker exists;
 (d) SIGTERM -> lifespan-EXIT + sub-app teardown written AND the heartbeat STOPS
     within LANCELOT_DRAIN_SECS, with NO "Task was destroyed but pending" on
     stderr;
 (e) a startup that RAISES -> the process exits NON-ZERO WITHOUT binding
     (connection-refused + nonzero rc);
 (f) lifespan drives exactly once.
"""
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


def _spawn(src, port, statefile, extra_env=None):
    env = {**os.environ, "LANCELOT_STATEFILE": statefile,
           "LANCELOT_HOST": "127.0.0.1"}
    if extra_env:
        env.update(extra_env)
    return subprocess.Popen(
        [sys.executable, "-c", src, str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)


def _wait_listen(proc, port, deadline_s=12):
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"server exited rc={proc.returncode}: "
                f"{proc.stderr.read().decode()[:1200]}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    proc.kill()
    raise RuntimeError("server did not start listening in time")


def _readlines(path):
    try:
        with open(path) as f:
            return [ln.rstrip("\n") for ln in f if ln.strip()]
    except OSError:
        return []


# A full-fat lifespan app served by the NATIVE server. The side-channel file
# records, in order: the root lifespan ENTER/EXIT (which sets app.state.client —
# the classic footgun: the first request relies on it), each lifetime-task
# HEARTBEAT, and the mounted sub-app's ENTER/EXIT.
#
# NOTE: `lifespan=` and `@app.on_event` are MUTUALLY EXCLUSIVE (FastAPI parity —
# `_lifespan` ignores on_event when a root lifespan is set), so the resource
# setup goes in the `lifespan=` CM here; the `@app.on_event` surface is proven
# separately in test_native_server_runs_on_event_hooks below.
_SERVER = r"""
import asyncio, contextlib, os, sys
import lancelot

_STATE = os.environ["LANCELOT_STATEFILE"]

def _append(line):
    with open(_STATE, "a") as f:
        f.write(line + "\n")
        f.flush()

@contextlib.asynccontextmanager
async def root_lifespan(app):
    app.state.client = "READY"
    _append("ROOT_ENTER")
    try:
        yield
    finally:
        _append("ROOT_EXIT")

@contextlib.asynccontextmanager
async def sub_lifespan(sub):
    _append("SUB_ENTER")
    try:
        yield
    finally:
        _append("SUB_EXIT")

app = lancelot.App(lifespan=root_lifespan)
sub = lancelot.App(lifespan=sub_lifespan)
app.mount("/sub", sub)

@app.lifetime_task
async def heartbeat():
    try:
        while True:
            _append("HEARTBEAT")
            await asyncio.sleep(0.05)
    finally:
        _append("HEARTBEAT_FINALLY")

@app.get("/probe")
async def probe(req):
    # Reads the SAME app object the lifespan mutated. Under the old no-op
    # server `app.state.client` is unset -> AttributeError -> 500.
    return {"client": app.state.client}

app.run(host="127.0.0.1", port=int(sys.argv[1]))
"""


def test_native_server_drives_lifespan_and_drains():
    statefile = tempfile.mktemp(suffix=".log")
    port = _free_port()
    # A small but non-zero drain window so shutdown has time to run + we can
    # assert the heartbeat stops within it.
    proc = _spawn(_SERVER, port, statefile, {"LANCELOT_DRAIN_SECS": "5"})
    try:
        _wait_listen(proc, port)
        base = f"http://127.0.0.1:{port}"

        # (a) startup state is visible to the FIRST request.
        r = httpx.get(f"{base}/probe", timeout=5)
        assert r.status_code == 200, (r.status_code, r.text)
        assert r.json() == {"client": "READY"}, r.json()

        # (b) LOOP-IDENTITY proof: the root lifespan entered before serving AND
        # the @app.lifetime_task daemon is ALIVE on the server's own loop (not a
        # dead throwaway loop) — proven by the heartbeat count GROWING while the
        # server handles traffic. A daemon bound to a dead loop would never tick.
        lines = _readlines(statefile)
        assert "ROOT_ENTER" in lines, lines
        assert lines.count("HEARTBEAT") >= 1, lines  # at least one before serving
        # Poll until the heartbeat advances (the daemon keeps running) — bounded.
        first = lines.count("HEARTBEAT")
        deadline = time.time() + 3
        while time.time() < deadline:
            cur = _readlines(statefile).count("HEARTBEAT")
            if cur >= first + 2:
                break
            time.sleep(0.05)
        lines = _readlines(statefile)
        assert lines.count("HEARTBEAT") >= first + 2, (
            "lifetime daemon did not keep ticking on the server loop "
            f"(loop-identity): {lines}")
        # (c) mounted sub-app lifespan ran, AFTER the root entered (LIFO order).
        assert "SUB_ENTER" in lines, lines
        assert lines.index("ROOT_ENTER") < lines.index("SUB_ENTER"), lines

        # (d) SIGTERM -> graceful drain -> lifespan shutdown runs; heartbeat
        # stops; sub-app + root tear down; no orphaned-task warning.
        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=10)
        err = proc.stderr.read().decode()

        lines = _readlines(statefile)
        assert "ROOT_EXIT" in lines, lines
        assert "SUB_EXIT" in lines, lines
        # Teardown order: daemons stop FIRST, then sub-apps (LIFO), then root.
        assert lines.index("SUB_EXIT") < lines.index("ROOT_EXIT"), lines
        assert "HEARTBEAT_FINALLY" in lines, lines
        # The heartbeat actually STOPPED (no new HEARTBEAT after the FINALLY).
        fin = lines.index("HEARTBEAT_FINALLY")
        assert "HEARTBEAT" not in lines[fin:], lines[fin:]
        assert "Task was destroyed but it is pending" not in err, err
        # (f) lifespan drove exactly once (single enter, single exit).
        assert lines.count("ROOT_ENTER") == 1, lines
        assert lines.count("ROOT_EXIT") == 1, lines
        assert lines.count("SUB_ENTER") == 1, lines
        assert lines.count("SUB_EXIT") == 1, lines
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        try:
            os.remove(statefile)
        except OSError:
            pass


# The `@app.on_event` surface (NO root lifespan, so on_event fires — FastAPI
# parity). startup sets app.state.client; shutdown writes a marker on SIGTERM.
_ONEVENT_SERVER = r"""
import os, sys
import lancelot

_STATE = os.environ["LANCELOT_STATEFILE"]

def _append(line):
    with open(_STATE, "a") as f:
        f.write(line + "\n")
        f.flush()

app = lancelot.App()

@app.on_event("startup")
async def _startup():
    app.state.client = "READY"
    _append("ONEVENT_STARTUP")

@app.on_event("shutdown")
async def _shutdown():
    _append("ONEVENT_SHUTDOWN")

@app.get("/probe")
async def probe(req):
    return {"client": app.state.client}

app.run(host="127.0.0.1", port=int(sys.argv[1]))
"""


def test_native_server_runs_on_event_hooks():
    statefile = tempfile.mktemp(suffix=".log")
    port = _free_port()
    proc = _spawn(_ONEVENT_SERVER, port, statefile, {"LANCELOT_DRAIN_SECS": "5"})
    try:
        _wait_listen(proc, port)
        base = f"http://127.0.0.1:{port}"
        r = httpx.get(f"{base}/probe", timeout=5)
        assert r.status_code == 200, (r.status_code, r.text)
        assert r.json() == {"client": "READY"}, r.json()
        assert "ONEVENT_STARTUP" in _readlines(statefile)

        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
        lines = _readlines(statefile)
        assert "ONEVENT_SHUTDOWN" in lines, lines
        # Exactly once.
        assert lines.count("ONEVENT_STARTUP") == 1, lines
        assert lines.count("ONEVENT_SHUTDOWN") == 1, lines
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        try:
            os.remove(statefile)
        except OSError:
            pass


# A startup hook that RAISES. The native server must REFUSE TO SERVE: never
# bind the socket, and exit non-zero (uvicorn `startup.failed` parity).
_FAILING_STARTUP = r"""
import os, sys
import lancelot

_STATE = os.environ["LANCELOT_STATEFILE"]

app = lancelot.App()

@app.on_event("startup")
async def _boom():
    with open(_STATE, "a") as f:
        f.write("STARTUP_ATTEMPTED\n")
    raise RuntimeError("startup boom")

@app.get("/probe")
async def probe(req):
    return {"ok": True}

app.run(host="127.0.0.1", port=int(sys.argv[1]))
"""


def test_failing_startup_refuses_to_serve_without_binding():
    statefile = tempfile.mktemp(suffix=".log")
    port = _free_port()
    proc = _spawn(_FAILING_STARTUP, port, statefile)
    try:
        # (e) The process must EXIT (non-zero) on its own, without ever binding.
        rc = proc.wait(timeout=12)
        err = proc.stderr.read().decode()
        assert rc != 0, f"failing startup should exit non-zero (rc={rc})\n{err}"
        # The startup hook DID run (so we know it failed, not skipped) ...
        assert "STARTUP_ATTEMPTED" in _readlines(statefile)
        # ... and the failure traceback is surfaced.
        assert "startup boom" in err, err
        # The socket was NEVER bound: a connect attempt is refused.
        with pytest.raises(OSError):
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                pass
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        try:
            os.remove(statefile)
        except OSError:
            pass


# The escape hatch: LANCELOT_LIFESPAN=0 restores the old silent no-op.
_NO_LIFESPAN_ENV = r"""
import os, sys
import lancelot

_STATE = os.environ["LANCELOT_STATEFILE"]
app = lancelot.App()

@app.on_event("startup")
async def _startup():
    with open(_STATE, "a") as f:
        f.write("STARTUP_HOOK\n")

@app.get("/ping")
async def ping(req):
    return {"ok": True}

app.run(host="127.0.0.1", port=int(sys.argv[1]))
"""


def test_lifespan_escape_hatch_disables_it():
    statefile = tempfile.mktemp(suffix=".log")
    port = _free_port()
    proc = _spawn(_NO_LIFESPAN_ENV, port, statefile, {"LANCELOT_LIFESPAN": "0"})
    try:
        _wait_listen(proc, port)
        base = f"http://127.0.0.1:{port}"
        assert httpx.get(f"{base}/ping", timeout=5).json() == {"ok": True}
        # With the hatch flipped, startup did NOT run (old behavior preserved).
        assert "STARTUP_HOOK" not in _readlines(statefile)
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        try:
            os.remove(statefile)
        except OSError:
            pass
