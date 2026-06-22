"""M20 batch B — observability + lifecycle.

Items (research/bytepath_mine/FINDINGS.md):
  T1.2  three-state /readyz — 503 + Retry-After until lifespan startup COMPLETED
        (code-verified false-ready deploy-and-roll bug; SGLang http_server.py:656)
  T3.6  App(readyz_check=callable) optional readiness predicate (additive)
  T1.7  emit 499 to RED metrics on a WON client-disconnect race (was 500;
        nginx-convention client-closed-request; metrics.py:149 anticipated it)
  T2.7  method-gate RED metrics — exclude OPTIONS/HEAD from the request
        COUNTER + DURATION histogram (in-flight gauge keeps counting all)
  T1.8  gc.freeze() ONCE after lifespan startup completes (vLLM
        server_utils.py:467 freeze_gc_heap); LANCELOT_NO_GC_FREEZE=1 opt-out

The /readyz + readyz_check + method-gate proofs run on the in-process ASGI
seam (TestClient / direct drive). The 499-on-disconnect proof and the
native-run-stays-ready proof are REAL-SERVER subprocess tests (cardinal rule
#2: a client-disconnect / native-lifespan bug does not live on TestClient).
"""
import asyncio
import os
import signal
import socket
import subprocess
import sys
import time

import pytest

import lancelot
from lancelot.testclient import TestClient


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


async def _drive_lifespan_startup(app):
    """Drive ONE lifespan.startup against the app's ASGI callable and return
    the ack type — the same protocol uvicorn / the native seam drive. Lets a
    test observe /readyz state BEFORE vs AFTER startup completes."""
    to_app: asyncio.Queue = asyncio.Queue()
    from_app: asyncio.Queue = asyncio.Queue()
    sent = []

    async def _recv():
        return await to_app.get()

    async def _send(msg):
        sent.append(msg)
        await from_app.put(msg)

    task = asyncio.ensure_future(
        app({"type": "lifespan", "asgi": {"version": "3.0"}}, _recv, _send))
    await to_app.put({"type": "lifespan.startup"})
    ack = await from_app.get()
    return ack, to_app, task


async def _readyz_status(app):
    """Invoke the builtin GET /readyz through the real ASGI seam and return
    (status, body_bytes)."""
    scope = {"type": "http", "method": "GET", "path": "/readyz",
             "headers": [], "query_string": b""}
    out = {"status": None, "body": b""}
    sent_started = asyncio.Event()

    async def _recv():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _send(msg):
        if msg["type"] == "http.response.start":
            out["status"] = msg["status"]
        elif msg["type"] == "http.response.body":
            out["body"] += msg.get("body", b"")
            sent_started.set()

    await app(scope, _recv, _send)
    return out["status"], out["body"]


# ==========================================================================
# T1.2 — three-state /readyz
# ==========================================================================
def test_readyz_503_before_startup_then_200_after():
    """The verified false-ready bug: /readyz must return 503 + Retry-After
    BEFORE lifespan startup completes, and 200 only after. A fresh App has
    `_started=False`, so a probe issued before startup is told to back off."""
    started = {"v": False}

    import contextlib

    @contextlib.asynccontextmanager
    async def lifespan(app):
        started["v"] = True
        yield

    app = lancelot.App(lifespan=lifespan)

    async def _run():
        # Pre-startup: NOT ready -> 503 + Retry-After.
        st, body = await _readyz_status(app)
        assert st == 503, f"readyz should be 503 before startup, got {st}"
        assert b"starting" in body, body

        # Drive lifespan startup; it must complete AND flip _started.
        ack, _to_app, task = await _drive_lifespan_startup(app)
        assert ack["type"] == "lifespan.startup.complete", ack
        assert app._started is True

        # Post-startup: ready -> 200.
        st2, body2 = await _readyz_status(app)
        assert st2 == 200, f"readyz should be 200 after startup, got {st2}"
        assert b"ok" in body2, body2

        task.cancel()
        try:
            await task
        except BaseException:  # noqa: BLE001
            pass

    asyncio.run(_run())
    assert started["v"] is True


def test_readyz_503_before_startup_bare_app():
    """Even a lifespan-LESS app gates: the ASGI seam still drives the startup
    protocol (and flips _started at startup.complete). Before that drive, a
    bare App is `_started=False` -> 503."""
    app = lancelot.App()

    async def _run():
        st, _ = await _readyz_status(app)
        assert st == 503, f"bare app readyz before startup should be 503, got {st}"
        ack, _to_app, task = await _drive_lifespan_startup(app)
        assert ack["type"] == "lifespan.startup.complete", ack
        assert app._started is True
        st2, _ = await _readyz_status(app)
        assert st2 == 200
        task.cancel()
        try:
            await task
        except BaseException:  # noqa: BLE001
            pass

    asyncio.run(_run())


def test_readyz_200_after_testclient_lifespan():
    """Through TestClient (which enters the lifespan in __enter__), /readyz is
    200 inside the context — the normal serving state."""
    app = lancelot.App()

    with TestClient(app) as c:
        r = c.get("/readyz")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


def test_user_readyz_route_overrides_builtin():
    """M19b lesson: a user-registered GET /readyz must WIN over the builtin
    three-state probe (builtin routes are registered with builtin=True so a
    user route shadows them). The user's body + status are served verbatim,
    even before/without the startup gate."""
    app = lancelot.App()

    @app.get("/readyz")
    def my_readyz():
        from lancelot.responses import JSONResponse
        return JSONResponse(b'{"status":"custom"}')

    with TestClient(app) as c:
        r = c.get("/readyz")
        assert r.status_code == 200
        assert r.json() == {"status": "custom"}, r.text


def test_readyz_503_while_draining_takes_precedence():
    """Draining still 503s (existing behavior preserved) — and outranks the
    starting/ready states."""
    app = lancelot.App()
    with TestClient(app) as c:
        assert c.get("/readyz").status_code == 200  # started
        app.initiate_shutdown()
        r = c.get("/readyz")
        assert r.status_code == 503
        assert r.headers.get("retry-after") == "1"
        assert r.json() == {"status": "draining"}


# ==========================================================================
# T3.6 — App(readyz_check=callable)
# ==========================================================================
def test_readyz_check_sync_predicate_gates_readiness():
    """A sync `readyz_check` predicate is ANDed with startup: ready only when
    startup done AND the predicate is truthy."""
    ready = {"v": False}
    app = lancelot.App(readyz_check=lambda: ready["v"])

    with TestClient(app) as c:
        # Startup is complete, but the predicate says not-ready.
        r = c.get("/readyz")
        assert r.status_code == 503, r.text
        assert r.json() == {"status": "not_ready"}
        # Flip the predicate -> ready.
        ready["v"] = True
        r2 = c.get("/readyz")
        assert r2.status_code == 200
        assert r2.json() == {"status": "ok"}


def test_readyz_check_async_predicate():
    """`readyz_check` may return an awaitable (DB ping / model-warm probe)."""
    ready = {"v": False}

    async def check():
        return ready["v"]

    app = lancelot.App(readyz_check=check)
    with TestClient(app) as c:
        assert c.get("/readyz").status_code == 503
        ready["v"] = True
        assert c.get("/readyz").status_code == 200


def test_readyz_check_raising_predicate_is_not_ready():
    """A predicate that RAISES is treated as not-ready (503), never a 500 —
    a failing health probe must not crash the probe endpoint."""
    def check():
        raise RuntimeError("db down")

    app = lancelot.App(readyz_check=check)
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/readyz")
        assert r.status_code == 503, r.text
        assert r.json() == {"status": "not_ready"}


def test_readyz_check_default_none_is_todays_behavior():
    """Default readyz_check=None -> exactly the startup-gated behavior (200
    once started). Additive: no predicate means no extra condition."""
    app = lancelot.App()
    assert app.readyz_check is None
    with TestClient(app) as c:
        assert c.get("/readyz").status_code == 200


# ==========================================================================
# T2.7 — method-gate RED metrics
# ==========================================================================
def _install_reader():
    """Single process-wide InMemoryMetricReader (set_meter_provider is once
    per process). Recover whatever provider is active so collection order
    can't orphan us — mirrors test_m4_observability / test_m15_metric_cardinality."""
    from opentelemetry import metrics as om
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    provider = om.get_meter_provider()
    readers = getattr(getattr(provider, "_sdk_config", None),
                      "metric_readers", None)
    reader = None
    if readers:
        for r in readers:
            if isinstance(r, InMemoryMetricReader):
                reader = r
                break
    if reader is None:
        reader = InMemoryMetricReader()
        try:
            om.set_meter_provider(MeterProvider(metric_readers=[reader]))
        except Exception:  # noqa: BLE001
            pass
    from lancelot import metrics as lmetrics
    lmetrics.reset_for_tests()
    return reader


def _points(reader, name):
    pts = []
    _data = reader.get_metrics_data()
    for rm in ((_data.resource_metrics if _data else None) or []):
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == name:
                    pts.extend(m.data.data_points)
    return pts


def _scoped(reader, name, routes):
    return [p for p in _points(reader, name)
            if p.attributes.get("http.route") in routes]


def test_method_gate_excludes_options_from_counter_and_duration():
    pytest.importorskip("opentelemetry.sdk.metrics")
    reader = _install_reader()
    app = lancelot.App()

    @app.api_route("/m27", methods=["GET", "POST", "OPTIONS"])
    async def m27():
        return {"ok": True}

    mine = {"/m27"}

    def _counts():
        cnt = sum(p.value for p in _scoped(reader, "lancelot.http.requests", mine))
        dur = sum(p.count for p in
                  _scoped(reader, "lancelot.http.request.duration", mine))
        return cnt, dur

    base = _counts()
    with TestClient(app) as c:
        assert c.get("/m27").status_code == 200
        assert c.post("/m27").status_code == 200
        # 5 OPTIONS preflights — these must NOT be counted (default-gated).
        for _ in range(5):
            assert c.options("/m27").status_code == 200

    cnt, dur = (a - b for a, b in zip(_counts(), base))
    # Only the GET + POST were counted; the 5 OPTIONS were gated out.
    assert cnt == 2, f"counter counted {cnt}, expected 2 (OPTIONS gated)"
    assert dur == 2, f"duration recorded {dur}, expected 2 (OPTIONS gated)"


def test_method_gate_inflight_gauge_still_counts_all_methods():
    """The in-flight gauge (in_flight_delta) is a SEPARATE call and must keep
    counting EVERY method — concurrency/saturation accuracy is not gated.
    Unit-check the gate function directly: in_flight_delta has no method arg,
    so it cannot be filtered."""
    import inspect

    from lancelot import metrics as lmetrics
    # in_flight_delta takes only `delta` — there is no method to gate on.
    assert list(inspect.signature(lmetrics.in_flight_delta)
                .parameters) == ["delta"]
    # And OPTIONS is in the default exclude set while GET is not.
    lmetrics.reset_for_tests()
    os.environ.pop("LANCELOT_METRICS_EXCLUDE_METHODS", None)
    excl = lmetrics._excluded_methods()
    assert "OPTIONS" in excl and "HEAD" in excl and "GET" not in excl


def test_method_gate_env_empty_counts_all_methods():
    pytest.importorskip("opentelemetry.sdk.metrics")
    reader = _install_reader()
    old = os.environ.get("LANCELOT_METRICS_EXCLUDE_METHODS")
    os.environ["LANCELOT_METRICS_EXCLUDE_METHODS"] = ""  # count everything
    try:
        from lancelot import metrics as lmetrics
        lmetrics.reset_for_tests()  # re-read the env
        # Re-bind reader to the active provider after the reset.
        reader = _install_reader()
        os.environ["LANCELOT_METRICS_EXCLUDE_METHODS"] = ""
        lmetrics.reset_for_tests()

        app = lancelot.App()

        @app.api_route("/m27e", methods=["GET", "OPTIONS"])
        async def m27e():
            return {"ok": True}

        mine = {"/m27e"}

        def _cnt():
            return sum(p.value for p in
                       _scoped(reader, "lancelot.http.requests", mine))

        base = _cnt()
        with TestClient(app) as c:
            assert c.get("/m27e").status_code == 200
            assert c.options("/m27e").status_code == 200
        # With the exclude set empty, BOTH the GET and the OPTIONS count.
        assert _cnt() - base == 2, "empty exclude set should count all methods"
    finally:
        if old is None:
            os.environ.pop("LANCELOT_METRICS_EXCLUDE_METHODS", None)
        else:
            os.environ["LANCELOT_METRICS_EXCLUDE_METHODS"] = old
        from lancelot import metrics as lmetrics
        lmetrics.reset_for_tests()


# ==========================================================================
# T1.7 — emit 499 on a won client-disconnect race
# ==========================================================================
def test_t1_7_status_selection_499_on_disconnect_flag():
    """Unit-level proof of the status selection: when the disconnect flag is
    set on the scope and no status was committed (still the 500 default),
    record_request is called with 499 — NOT 500. Captures the exact arguments
    the asgi `finally` passes by monkeypatching record_request."""
    from lancelot import asgi as lasgi

    captured = {}

    def _fake_record(route, method, status, dur, **kw):
        captured["route"] = route
        captured["status"] = status

    async def _run():
        # Build a minimal app with a route so the seam matches + sets the
        # route label, then a handler that blocks until the client disconnects.
        app = lancelot.App(cancel_on_disconnect=True)

        ev = asyncio.Event()

        @app.get("/slow")
        async def slow():
            await ev.wait()  # never completes; the disconnect cancels it
            return {"ok": True}

        # Drive lifespan so _started is set (not required for this path, but
        # keeps the app in a serving state).
        asgi_app = app.asgi()

        scope = {"type": "http", "method": "GET", "path": "/slow",
                 "headers": [], "query_string": b""}

        disconnected = {"v": False}

        async def _recv():
            # First call: empty body (drained). Then: simulate the client
            # leaving so the disconnect watcher fires.
            if not disconnected["v"]:
                disconnected["v"] = True
                return {"type": "http.request", "body": b"",
                        "more_body": False}
            await asyncio.sleep(0.02)
            return {"type": "http.disconnect"}

        sends = []

        async def _send(msg):
            sends.append(msg)

        # Patch record_request as imported inside the asgi finally.
        import lancelot.metrics as lmetrics
        orig = lmetrics.record_request
        lmetrics.record_request = _fake_record
        try:
            # The disconnect cancels the handler -> CancelledError propagates
            # out of _http; its finally records the status. Swallow the
            # CancelledError that re-raises out of the seam.
            try:
                await asyncio.wait_for(
                    asgi_app(scope, _recv, _send), timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        finally:
            lmetrics.record_request = orig

        return captured, sends

    cap, sends = asyncio.run(_run())
    # No http.response.start was ever sent (the handler was cancelled pre-emit).
    assert not any(m.get("type") == "http.response.start" for m in sends), sends
    # The RED metric recorded 499 (client-closed-request), NOT 500.
    assert cap.get("status") == 499, (
        f"expected status 499 on won client-disconnect, got {cap.get('status')}")
    assert cap.get("route") == "/slow", cap


# --- real-server proof: an actual mid-flight disconnect records 499 ---------
_DISCONNECT_SERVER = r"""
import sys, asyncio, lancelot

# In-process OTel reader so the subprocess can report what status the RED
# metric recorded for the cancelled request.
from opentelemetry import metrics as om
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
_READER = InMemoryMetricReader()
om.set_meter_provider(MeterProvider(metric_readers=[_READER]))

import lancelot.metrics as lm
lm.reset_for_tests()  # bind the lancelot meter to our provider

app = lancelot.App(cancel_on_disconnect=True)

@app.get("/slow")
async def slow(request: lancelot.Request):
    # Block long enough that the client can hang up mid-flight; the unary
    # cancel_on_disconnect race cancels this the moment the socket closes.
    await asyncio.sleep(30)
    return {"ok": True}

def _statuses_for(route):
    out = []
    data = _READER.get_metrics_data()
    for rm in (data.resource_metrics if data else []):
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == "lancelot.http.requests":
                    for p in m.data.data_points:
                        if p.attributes.get("http.route") == route:
                            out.append((p.attributes.get("http.status_code"),
                                        p.value))
    return out

@app.get("/_status_dump")
async def dump(request: lancelot.Request):
    # Report the status_code attrs recorded for /slow so the test can assert
    # 499 (client-closed-request) was emitted, not 500.
    return {"slow": _statuses_for("/slow")}

import uvicorn
uvicorn.run(app.asgi(), host="127.0.0.1", port=int(sys.argv[1]),
            log_level="warning", access_log=False, loop="uvloop", http="httptools")
"""


def test_t1_7_real_disconnect_records_499():
    """REAL-SERVER proof (cardinal rule #2): a subprocess uvicorn server, a raw
    socket that opens GET /slow and HARD-CLOSES before the handler returns. The
    server's own InMemoryMetricReader must show the RED metric for /slow was
    recorded with status_code 499 (not 500). Then /_status_dump reports it."""
    httpx = pytest.importorskip("httpx")
    pytest.importorskip("opentelemetry.sdk.metrics")
    pytest.importorskip("uvloop")

    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-c", _DISCONNECT_SERVER, str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ})
    try:
        # Wait for listen.
        deadline = time.time() + 12
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"server exited rc={proc.returncode}: "
                    f"{proc.stderr.read().decode()[:1200]}")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            raise RuntimeError("server did not start")

        # Open /slow, send the request, read nothing, then HARD-CLOSE while the
        # handler is still sleeping (the won-disconnect race).
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        s.sendall(b"GET /slow HTTP/1.1\r\nHost: x\r\n\r\n")
        time.sleep(0.4)   # handler is now awaiting inside the race
        s.close()         # client gone mid-flight
        time.sleep(0.6)   # let the seam observe the disconnect + record 499

        r = httpx.get(f"http://127.0.0.1:{port}/_status_dump", timeout=5)
        assert r.status_code == 200, r.text
        slow = r.json()["slow"]
        # Exactly the cancelled request, recorded as 499 (client-closed).
        codes = [c for c, _v in slow]
        assert "499" in codes, (
            f"client-disconnect was not recorded as 499 (got {slow}) — "
            f"dashboards would miscount the abort as 5xx")
        assert "500" not in codes, (
            f"client-disconnect mislabeled as 500 server error: {slow}")
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.communicate(timeout=6)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()


# ==========================================================================
# T1.8 — gc.freeze() after startup completes
# ==========================================================================
def test_t1_8_gc_freeze_called_after_startup():
    """`_on_startup_complete` freezes the GC heap: the permanent (frozen) gen
    count grows across the call. gc.get_freeze_count() reports the number of
    objects in the frozen generation."""
    import gc

    app = lancelot.App()
    # Make sure nothing is frozen from a prior test (best-effort: unfreeze).
    gc.unfreeze()
    before = gc.get_freeze_count()
    app._on_startup_complete()
    after = gc.get_freeze_count()
    assert app._started is True
    assert after > before, (
        f"gc.freeze() did not move objects to the permanent gen "
        f"({before} -> {after})")
    gc.unfreeze()  # don't leak frozen objects into other tests


def test_t1_8_gc_freeze_opt_out_env():
    """LANCELOT_NO_GC_FREEZE=1 skips the freeze (but still flips _started, so
    /readyz still becomes ready — the opt-out is GC-only, not readiness)."""
    import gc

    old = os.environ.get("LANCELOT_NO_GC_FREEZE")
    os.environ["LANCELOT_NO_GC_FREEZE"] = "1"
    try:
        gc.unfreeze()
        before = gc.get_freeze_count()
        app = lancelot.App()
        app._on_startup_complete()
        after = gc.get_freeze_count()
        assert app._started is True, "opt-out must still flip readiness"
        assert after == before, (
            f"LANCELOT_NO_GC_FREEZE=1 should skip gc.freeze() "
            f"({before} -> {after})")
    finally:
        if old is None:
            os.environ.pop("LANCELOT_NO_GC_FREEZE", None)
        else:
            os.environ["LANCELOT_NO_GC_FREEZE"] = old
        gc.unfreeze()


def test_t1_8_on_startup_complete_idempotent():
    """Calling the hook twice is safe (native + lifespan paths could both fire
    in pathological setups; it must not error)."""
    import gc
    app = lancelot.App()
    gc.unfreeze()
    app._on_startup_complete()
    app._on_startup_complete()
    assert app._started is True
    gc.unfreeze()


# ==========================================================================
# T1.2 CRITICAL CAVEAT — native app.run() with NO lifespan stays READY
# ==========================================================================
_NATIVE_BARE_SERVER = r"""
import sys, lancelot
# A bare app: NO lifespan, NO on_event, NO mounts, NO lifetime tasks. The
# native server therefore has NO _lifespan seam to flip _started -> without the
# run()-path _on_startup_complete the /readyz gate would sit 503 FOREVER. This
# server proves the caveat: native deploys stay ready.
app = lancelot.App()

@app.get("/probe")
async def probe(request: lancelot.Request):
    return {"started": app._started}

app.run("127.0.0.1", int(sys.argv[1]))
"""


def test_t1_2_native_run_no_lifespan_stays_ready():
    """REAL-SERVER caveat proof: a NATIVE `app.run()` server with NO lifespan
    work must still flip `_started`. The native server drives NO `_lifespan`
    seam for a bare app, so a naive startup gate would leave the worker
    not-ready FOREVER; `run()` flips `_started` unconditionally (its
    `_native_on_startup`). `/probe` reads the in-process flag and proves it is
    True — i.e. the readiness gate is satisfied on the native path.

    NOTE: this asserts the flag (the thing T1.2 owns), NOT a 200 from the
    builtin /readyz over the native socket. The native server's value-serialize
    path does not render a Lancelot `Response`/`JSONResponse` return (it serves
    plain values) — `/readyz` (and the unchanged `/livez`) return 500 there on
    `main` already, a PRE-EXISTING native limitation orthogonal to this batch
    (the Python/uvicorn seam, exercised by the tests above, serves /readyz
    correctly). The requirement here is solely that native deploys are not
    stuck not-ready, which the flag proves."""
    httpx = pytest.importorskip("httpx")

    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-c", _NATIVE_BARE_SERVER, str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, "LANCELOT_HOST": "127.0.0.1"})
    try:
        deadline = time.time() + 12
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"native server exited rc={proc.returncode}: "
                    f"{proc.stderr.read().decode()[:1200]}")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            raise RuntimeError("native server did not start")

        # /probe proves the in-process flag is True (run() flipped it) — the
        # readiness gate is satisfied; the native deploy is NOT stuck not-ready.
        rp = httpx.get(f"http://127.0.0.1:{port}/probe", timeout=5)
        assert rp.status_code == 200, rp.text
        assert rp.json() == {"started": True}, (
            f"native run did not flip _started (lifespan-less native deploy "
            f"would be stuck not-ready forever): {rp.text}")
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.communicate(timeout=6)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
