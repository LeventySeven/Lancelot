"""T2 #5 (2026-06-02 byte-path build order) — runtime / dynamic-surface
conformance matrix.

The honesty audit ranked this #1: the recurring migration BLOCKER for the
highest-coupling FastAPI products (Polar, Hayhooks, Prefect, LangServe, LitServe)
is not a missing feature but UNVERIFIED parity on advanced runtime/dynamic
surfaces that live in the 0.4% conformance gap. This file is the published
matrix — characterization tests that PROVE each surface (or pin and DOCUMENT a
divergence), so "coin-flip drop-in" becomes "proven drop-in (with named
exceptions)".

Matrix (see docs/plans/2026-06-02-byte-path-build-order-...md for the table):
  1. custom APIRoute.get_route_handler + route_class ......... PROVEN
  2. runtime route registration (add_route_live) ............ PROVEN
  3. dynamic create_model body annotation at registration ... PROVEN
  4. duplicate-path resolution .............................. DIVERGENT (documented)
  5. mounted sub-app lifespan (startup + shutdown) .......... PROVEN
"""
import contextlib

import lancelot
from lancelot import APIRouter, TestClient
from lancelot.routing import Route


# 1 — Custom APIRoute.get_route_handler via route_class (Polar 8-mixin shape).
def test_custom_route_class_get_route_handler_is_invoked():
    class MarkerRoute(Route):
        seen: list = []

        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            # Polar mutates openapi_extra in __init__ of its mixin chain.
            self.openapi_extra = {"x-marker": True}

        def get_route_handler(self):
            MarkerRoute.seen.append(self.path)
            return super().get_route_handler()

    app = lancelot.App()
    r = APIRouter(route_class=MarkerRoute)

    @r.get("/x")
    def x(request: lancelot.Request):
        return {"ok": True}

    app.include_router(r)
    with TestClient(app) as c:
        resp = c.get("/x")
    assert resp.status_code == 200 and resp.json() == {"ok": True}
    # The custom get_route_handler path (not the fast inline path) was taken.
    assert "/x" in MarkerRoute.seen


# 2 — Runtime route registration through the supported live API (Hayhooks).
def test_runtime_route_registration_via_add_route_live():
    app = lancelot.App()

    @app.get("/early")
    def early(request: lancelot.Request):
        return {"e": 1}

    with TestClient(app) as c:
        assert c.get("/early").status_code == 200

    def late(request: lancelot.Request):
        return {"l": 1}

    app.add_route_live("/late", late, methods=["GET"])
    with TestClient(app) as c:
        r = c.get("/late")
    assert r.status_code == 200 and r.json() == {"l": 1}


# 3 — Dynamically built (pydantic.create_model) request body (LitServe).
def test_dynamic_create_model_body_validates():
    from pydantic import create_model

    Dyn = create_model("Dyn", value=(int, ...))
    app = lancelot.App()

    @app.post("/dyn")
    def dyn(request: lancelot.Request, body: Dyn):
        return {"got": body.value}

    with TestClient(app) as c:
        ok = c.post("/dyn", json={"value": 42})
        bad = c.post("/dyn", json={"value": "nope"})
    assert ok.json() == {"got": 42}
    assert bad.status_code == 422  # dynamic annotation IS validated


# 4 — Duplicate same-path/same-method resolution. FIXED in M19b: now
# FIRST-registered-wins (FastAPI/Starlette parity) for STATIC paths too (the
# const-tier dict was last-wins, which 500'd LangServe's add_routes trick;
# routing.py now uses setdefault). Dynamic /param/ routes already matched
# first-wins.
def test_duplicate_static_path_first_registered_wins():
    app = lancelot.App()

    @app.get("/dup")
    def first(request: lancelot.Request):
        return {"who": "first"}

    @app.get("/dup")
    def second(request: lancelot.Request):
        return {"who": "second"}

    with TestClient(app) as c:
        body = c.get("/dup").json()
    assert body == {"who": "first"}, (
        "duplicate-static-path must be first-registered-wins (FastAPI parity)")


# 5 — Mounted sub-app lifespan runs startup AND shutdown (M15 fix; Hayhooks
# mounts sub-apps with their own lifespans).
def test_mounted_subapp_lifespan_runs():
    started = {"v": False}
    stopped = {"v": False}

    @contextlib.asynccontextmanager
    async def life(_a):
        started["v"] = True
        yield
        stopped["v"] = True

    sub = lancelot.App(lifespan=life)

    @sub.get("/ping")
    def ping(request: lancelot.Request):
        return {"ok": True}

    app = lancelot.App()
    app.mount("/sub", sub)

    with TestClient(app) as c:
        assert c.get("/sub/ping").status_code == 200
        assert started["v"] is True
    assert stopped["v"] is True  # shutdown ran on context exit
