"""M15 T12 (#8) — live route add/remove with all-tier eviction + OpenAPI
cache-bust.

REAL-SERVER tests (cardinal rule #2): the stale-const-tier-hit bug is exactly
the class of "green in a unit test, serves a dead route in prod" failure — a
route dropped from `app.routes` alone would still be served by the O(1) const
tier. So the add/remove behavior is exercised over a real socket under uvicorn
in a SUBPROCESS (the native `_native.run` server builds its router once at
startup and does NOT support live mutation — that is the documented scope; see
`App.add_route_live`/`remove_route`). The in-process unit tests below pin the
per-tier eviction edge cases (const vs dyn, multi-method, WS) that a single
real-server flow can't all reach.
"""
import os
import signal
import socket
import subprocess
import sys
import time

import pytest

httpx = pytest.importorskip("httpx")


# ----------------------------------------------------------------------------
# In-process unit tests: Router.remove evicts from EVERY tier.
# ----------------------------------------------------------------------------

def _route(method, path, **kw):
    from lancelot.routing import Route
    return Route(method, path, lambda: {"ok": True}, **kw)


def test_remove_evicts_const_tier():
    """A static-shape (no {param}) route lives in _const/_const_paths. Removing
    it must drop it from BOTH derived tiers and routes — not leave a stale O(1)
    const hit."""
    from lancelot.routing import Router
    r = Router()
    r.add(_route("GET", "/health"))
    assert ("GET", "/health") in r._const
    assert "/health" in r._const_paths
    assert r.match("GET", "/health")[0] is not None

    n = r.remove("/health")
    assert n == 1
    # All tiers cleared.
    assert ("GET", "/health") not in r._const
    assert "/health" not in r._const_paths
    assert all(rt.path != "/health" for rt in r.routes)
    # match() now 404s (NOT a stale const hit).
    assert r.match("GET", "/health") == (None, 404)


def test_remove_evicts_dyn_tier():
    """A {param} route lives in _dyn. Removing it drops the regex entry."""
    from lancelot.routing import Router
    r = Router()
    r.add(_route("GET", "/items/{id}"))
    assert r.match("GET", "/items/42")[0] is not None
    assert r.remove("/items/{id}") == 1
    assert all(rt.path != "/items/{id}" for rt in r._dyn)
    assert r.match("GET", "/items/42") == (None, 404)


def test_remove_specific_method_keeps_sibling():
    """remove(path, method) drops only that method; a sibling method on the
    SAME static path stays routable AND keeps its 405 detection (the shared
    _const_paths entry must survive)."""
    from lancelot.routing import Router
    r = Router()
    r.add(_route("GET", "/x"))
    r.add(_route("POST", "/x"))
    assert r.remove("/x", method="GET") == 1
    # GET gone, POST survives.
    assert r.match("GET", "/x")[0] is None
    assert r.match("POST", "/x")[0] is not None
    # The path is still "known" → a wrong method is 405, not 404.
    assert "/x" in r._const_paths
    assert r.match("DELETE", "/x") == (None, 405)


def test_remove_all_methods_clears_path():
    """remove(path) with method=None drops every method; the path becomes
    wholly unknown (404, not 405)."""
    from lancelot.routing import Router
    r = Router()
    r.add(_route("GET", "/x"))
    r.add(_route("POST", "/x"))
    assert r.remove("/x") == 2
    assert "/x" not in r._const_paths
    assert r.match("GET", "/x") == (None, 404)
    assert r.match("POST", "/x") == (None, 404)


def test_remove_ws_route():
    """A WebSocket route lives in _dyn; remove evicts it."""
    from lancelot.routing import Router
    r = Router()
    r.add(_route("GET", "/ws", is_ws=True))
    assert r.match_ws("/ws")[0] is not None
    assert r.remove("/ws") == 1
    assert r.match_ws("/ws") == (None, 404)


def test_remove_nonexistent_is_zero():
    from lancelot.routing import Router
    r = Router()
    r.add(_route("GET", "/x"))
    assert r.remove("/nope") == 0
    assert r.remove("/x", method="DELETE") == 0
    # Untouched.
    assert r.match("GET", "/x")[0] is not None


def test_remove_then_readd_works():
    """After removing a route the same path can be re-added (no stale state)."""
    from lancelot.routing import Router
    r = Router()
    r.add(_route("GET", "/x"))
    assert r.remove("/x") == 1
    r.add(_route("GET", "/x"))
    assert r.match("GET", "/x")[0] is not None


def test_app_remove_route_busts_openapi_cache():
    """App.remove_route nulls the memoized OpenAPI schema so the next
    /openapi.json rebuilds without the removed path. App.add_route_live the
    same on add."""
    import lancelot
    app = lancelot.App()

    @app.get("/keep")
    def keep():
        return {"ok": True}

    @app.get("/drop")
    def drop():
        return {"ok": True}

    # Prime the cache.
    schema1 = app.openapi()
    assert "/drop" in schema1["paths"]
    assert app._openapi_cache is not None

    # Remove → cache busted → next build omits /drop.
    assert app.remove_route("/drop") == 1
    assert app._openapi_cache is None
    schema2 = app.openapi()
    assert "/drop" not in schema2["paths"]
    assert "/keep" in schema2["paths"]

    # Add live → cache busted → next build includes the new path.
    app.add_route_live("/added", lambda: {"ok": True}, methods=["GET"])
    assert app._openapi_cache is None
    schema3 = app.openapi()
    assert "/added" in schema3["paths"]


def test_app_remove_nonexistent_does_not_bust_cache():
    """A no-op remove must not needlessly invalidate the cache."""
    import lancelot
    app = lancelot.App()

    @app.get("/x")
    def x():
        return {"ok": True}

    app.openapi()
    assert app._openapi_cache is not None
    assert app.remove_route("/no-such") == 0
    # Cache preserved (nothing changed).
    assert app._openapi_cache is not None


# ----------------------------------------------------------------------------
# REAL-SERVER subprocess tests (uvicorn): live add/remove over a socket.
# ----------------------------------------------------------------------------

def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _spawn(src):
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-c", src, str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={**os.environ})
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


def _stop(proc):
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


# A server that adds a route at runtime on first hit of /admin/add, then the
# new route serves and appears in /openapi.json.
_ADD_SERVER = r"""
import sys
import uvicorn
import lancelot

app = lancelot.App()

@app.get("/admin/add")
async def admin_add():
    app.add_route_live("/dynamic", lambda: {"dynamic": True}, methods=["GET"])
    return {"added": True}

uvicorn.run(app.asgi(), host="127.0.0.1", port=int(sys.argv[1]),
            log_level="warning", access_log=False, lifespan="on")
"""


def test_add_route_live():
    proc, port = _spawn(_ADD_SERVER)
    try:
        base = f"http://127.0.0.1:{port}"
        # Not present yet → 404.
        assert httpx.get(f"{base}/dynamic").status_code == 404
        assert "/dynamic" not in httpx.get(f"{base}/openapi.json").json()["paths"]
        # Add it at runtime.
        assert httpx.get(f"{base}/admin/add").json() == {"added": True}
        # Now it serves over the socket.
        r = httpx.get(f"{base}/dynamic")
        assert r.status_code == 200, r.text
        assert r.json() == {"dynamic": True}
        # And it appears in /openapi.json (cache was busted).
        assert "/dynamic" in httpx.get(f"{base}/openapi.json").json()["paths"]
    finally:
        _stop(proc)


# A server with a STATIC-shape route (lands in the O(1) const tier) that is
# removed at runtime. The stale-const-tier-hit bug would keep serving it.
_REMOVE_SERVER = r"""
import sys
import uvicorn
import lancelot

app = lancelot.App()

# Static path → const tier (the exact tier a naive `routes`-only removal misses).
@app.get("/feature")
async def feature():
    return {"feature": True}

@app.get("/admin/remove")
async def admin_remove():
    n = app.remove_route("/feature")
    return {"removed": n}

uvicorn.run(app.asgi(), host="127.0.0.1", port=int(sys.argv[1]),
            log_level="warning", access_log=False, lifespan="on")
"""


def test_remove_route_evicts_all_tiers():
    proc, port = _spawn(_REMOVE_SERVER)
    try:
        base = f"http://127.0.0.1:{port}"
        # Present + in schema.
        assert httpx.get(f"{base}/feature").json() == {"feature": True}
        assert "/feature" in httpx.get(f"{base}/openapi.json").json()["paths"]
        # Remove it at runtime.
        assert httpx.get(f"{base}/admin/remove").json() == {"removed": 1}
        # 404 over the socket — NOT a stale const-tier hit serving the dead route.
        assert httpx.get(f"{base}/feature").status_code == 404
        # Gone from /openapi.json too (cache busted).
        assert "/feature" not in httpx.get(f"{base}/openapi.json").json()["paths"]
    finally:
        _stop(proc)
