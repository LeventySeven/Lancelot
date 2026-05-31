"""M15 T5 (#6-core) — `app.lifetime_task(coro_fn)`: a supervised app-lifetime
daemon started at lifespan startup, retained by a strong ref, and cancelled +
awaited (bounded by LANCELOT_DRAIN_SECS) on drain/shutdown.

REAL-SERVER tests (cardinal rule #2): parent App served under uvicorn in a
SUBPROCESS so the ASGI lifespan protocol + SIGTERM path are actually exercised
over a real socket — a green TestClient would not prove the SIGTERM-drain
behavior. Must not regress B1 graceful drain (in-flight requests still finish).
"""
import os
import signal
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


def _spawn(src, env_extra=None):
    port = _free_port()
    env = {**os.environ, **(env_extra or {})}
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


# A lifetime_task that loops incrementing a shared counter (exposed via /count),
# with a slow in-flight route to prove B1 drain still holds.
_LIFETIME_SERVER = r"""
import asyncio, os, sys
import uvicorn
import lancelot

_CANCEL_MARKER = os.environ["LANCELOT_CANCEL_MARKER"]
app = lancelot.App()
_state = {"n": 0}

async def daemon():
    try:
        while True:
            _state["n"] += 1
            await asyncio.sleep(0.02)
    except asyncio.CancelledError:
        # Prove the supervised cancel actually reached the daemon body.
        with open(_CANCEL_MARKER, "w") as f:
            f.write("cancelled")
        raise

app.lifetime_task(daemon)

@app.get("/count")
async def count():
    return {"n": _state["n"]}

@app.get("/slow")
async def slow():
    await asyncio.sleep(0.4)
    return {"slow": True}

# info level so the "Application shutdown complete" line is emitted (the
# clean-shutdown signal the test asserts); warnings (orphan tasks) print
# at any level.
uvicorn.run(app.asgi(), host="127.0.0.1", port=int(sys.argv[1]),
            log_level="info", access_log=False, lifespan="on")
"""


def test_lifetime_task_runs_and_is_cancelled_on_drain():
    import tempfile
    marker = tempfile.mktemp(suffix=".marker")
    proc, port = _spawn(_LIFETIME_SERVER,
                        {"LANCELOT_DRAIN_SECS": "5",
                         "LANCELOT_CANCEL_MARKER": marker})
    try:
        base = f"http://127.0.0.1:{port}"
        # The daemon is running: the counter advances over time.
        n1 = httpx.get(f"{base}/count").json()["n"]
        time.sleep(0.2)
        n2 = httpx.get(f"{base}/count").json()["n"]
        assert n2 > n1, f"lifetime_task not advancing: {n1} -> {n2}"

        # Start an in-flight request, THEN SIGTERM — B1: it must still finish.
        import threading
        result = {}

        def inflight():
            try:
                result["status"] = httpx.get(f"{base}/slow", timeout=10).status_code
            except Exception as e:  # noqa: BLE001
                result["error"] = repr(e)

        th = threading.Thread(target=inflight)
        th.start()
        time.sleep(0.1)
        proc.send_signal(signal.SIGTERM)
        th.join(timeout=10)

        # B1 NOT regressed: the in-flight request drained to 200.
        assert result.get("status") == 200, f"B1 drain regressed: {result}"

        # uvicorn finishes graceful shutdown then exits via the signal
        # (rc=-SIGTERM is uvicorn's own clean-shutdown exit code — NOT a
        # wedge). The meaningful contract: application shutdown COMPLETED
        # within the drain bound (a daemon that wasn't cancelled+awaited would
        # hang past the bound or emit the orphan warning below).
        proc.wait(timeout=8)
        err = proc.stderr.read().decode()
        assert "Application shutdown complete" in err, err[-1500:]
        # The daemon was cancelled + awaited; no orphan / destroyed-task noise.
        assert "Task was destroyed but it is pending" not in err, err[-1500:]
        assert "was never retrieved" not in err, err[-1500:]
        # Direct proof the supervised cancel reached the daemon body (its
        # CancelledError handler wrote the marker).
        assert os.path.exists(marker), \
            "lifetime_task was not cancelled on drain (marker absent)"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        try:
            os.remove(marker)
        except OSError:
            pass


# A lifetime_task whose strong ref must survive a GC triggered mid-run.
_STRONGREF_SERVER = r"""
import asyncio, gc, sys
import uvicorn
import lancelot

app = lancelot.App()
_state = {"n": 0}

async def daemon():
    while True:
        _state["n"] += 1
        await asyncio.sleep(0.02)

app.lifetime_task(daemon)

@app.get("/gc")
async def do_gc():
    gc.collect()
    gc.collect()
    return {"n": _state["n"]}

uvicorn.run(app.asgi(), host="127.0.0.1", port=int(sys.argv[1]),
            log_level="warning", access_log=False, lifespan="on")
"""


def test_drain_cancels_lifetime_tasks_in_process():
    """`app.drain()` (the explicit graceful-drain entrypoint, distinct from the
    lifespan-shutdown path) also cancels + awaits app-lifetime daemons, bounded
    by the deadline. In-process: directly drives the methods the ASGI seam uses."""
    import asyncio

    import lancelot

    async def go():
        app = lancelot.App(drain_timeout=2.0)
        cancelled = {"v": False}

        async def daemon():
            try:
                while True:
                    await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                cancelled["v"] = True
                raise

        app.lifetime_task(daemon)
        app._start_lifetime_tasks()
        await asyncio.sleep(0.05)
        assert app._lifetime_tasks and not app._lifetime_tasks[0].done()
        t = app._lifetime_tasks[0]
        ok = await app.drain(timeout=2.0)
        assert ok is True
        assert cancelled["v"] is True, "drain() did not cancel the daemon"
        assert t.done(), "drain() did not await the daemon to completion"
        assert app._lifetime_tasks == [], "task refs not cleared after drain"

    asyncio.run(go())


def test_lifetime_task_rejects_non_callable():
    import lancelot

    app = lancelot.App()
    with pytest.raises(TypeError):
        app.lifetime_task(object())  # not a coroutine function


def test_lifetime_task_strong_ref():
    proc, port = _spawn(_STRONGREF_SERVER)
    try:
        base = f"http://127.0.0.1:{port}"
        # Force a GC; if the task weren't strongly referenced it could be
        # collected and stop advancing.
        n1 = httpx.get(f"{base}/gc").json()["n"]
        time.sleep(0.25)
        n2 = httpx.get(f"{base}/gc").json()["n"]
        assert n2 > n1, f"lifetime_task GC'd mid-run (stopped advancing): {n1} -> {n2}"
        # And a third sample after another GC keeps moving.
        time.sleep(0.2)
        n3 = httpx.get(f"{base}/gc").json()["n"]
        assert n3 > n2, f"lifetime_task stopped after second GC: {n2} -> {n3}"
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
