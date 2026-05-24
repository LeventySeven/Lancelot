"""M7 T3.5 — region-aware route registration.

Veriff per-jurisdiction routing pattern (TRANSCRIPTS_YC_ROOT_ACCESS.md:
7297-7798): "always use the best tool available for a given jurisdiction".

A route with `region='eu-west-1'` registers ONLY on workers whose
LANCELOT_REGION matches. Same code → multi-region deploy → each worker
serves its own region's routes.

The check happens at registration time (Router.add) — zero hot-path
overhead. Failover to nearest is the LB's job (HAProxy ip_hash by
region header, etc.), not the framework's.
"""
import lancelot
from lancelot.fleet import reset_for_tests
from lancelot.testclient import TestClient


def setup_function():
    reset_for_tests()


# ----- T3.5.a region=None routes always register -------------------------

def test_t3_5_no_region_registers_everywhere(monkeypatch):
    """Most routes don't specify region → register on every worker
    (backwards-compatible)."""
    monkeypatch.setenv("LANCELOT_REGION", "us-east-1")
    reset_for_tests()
    app = lancelot.App()

    @app.get("/api")
    async def api() -> dict:
        return {"region": "any"}

    with TestClient(app) as c:
        assert c.get("/api").status_code == 200


# ----- T3.5.b matching region registers ----------------------------------

def test_t3_5_matching_region_registers(monkeypatch):
    monkeypatch.setenv("LANCELOT_REGION", "eu-west-1")
    reset_for_tests()
    app = lancelot.App()

    @app.get("/eu", region="eu-west-1")
    async def eu_only() -> dict:
        return {"region": "eu-west-1"}

    with TestClient(app) as c:
        assert c.get("/eu").status_code == 200


# ----- T3.5.c non-matching region SKIPS registration ---------------------

def test_t3_5_non_matching_region_drops_route(monkeypatch):
    """Worker in us-east-1 should NOT register an eu-west-1 route."""
    monkeypatch.setenv("LANCELOT_REGION", "us-east-1")
    reset_for_tests()
    app = lancelot.App()

    @app.get("/eu", region="eu-west-1")
    async def eu_only() -> dict:
        return {"never"}

    with TestClient(app) as c:
        # 404 — the route doesn't exist on this worker.
        assert c.get("/eu").status_code == 404


# ----- T3.5.d two routes at same path, different regions -----------------

def test_t3_5_same_path_different_regions(monkeypatch):
    """Common pattern: handler-A registered for region-A, handler-B for
    region-B, both at the same path. Each worker keeps its local one."""
    monkeypatch.setenv("LANCELOT_REGION", "us-east-1")
    reset_for_tests()
    app = lancelot.App()

    @app.get("/v1/data", region="eu-west-1")
    async def eu() -> dict:
        return {"region": "eu"}

    @app.get("/v1/data", region="us-east-1")
    async def us() -> dict:
        return {"region": "us"}

    with TestClient(app) as c:
        r = c.get("/v1/data")
        assert r.status_code == 200
        assert r.json() == {"region": "us"}


# ----- T3.5.e same code, different region → different handler picked -----

def test_t3_5_swap_region_swaps_handler(monkeypatch):
    """Simulate the same code running in a different region."""
    monkeypatch.setenv("LANCELOT_REGION", "eu-west-1")
    reset_for_tests()
    app = lancelot.App()

    @app.get("/v1/data", region="eu-west-1")
    async def eu() -> dict:
        return {"region": "eu"}

    @app.get("/v1/data", region="us-east-1")
    async def us() -> dict:
        return {"region": "us"}

    with TestClient(app) as c:
        r = c.get("/v1/data")
        assert r.status_code == 200
        assert r.json() == {"region": "eu"}


# ----- T3.5.f default region 'default' matches the implicit default ------

def test_t3_5_default_region_no_env(monkeypatch):
    """When LANCELOT_REGION is unset, region='default' routes register;
    all others drop."""
    monkeypatch.delenv("LANCELOT_REGION", raising=False)
    reset_for_tests()
    app = lancelot.App()

    @app.get("/d", region="default")
    async def d() -> dict:
        return {"ok": True}

    @app.get("/e", region="eu-west-1")
    async def e() -> dict:
        return {"never"}

    with TestClient(app) as c:
        assert c.get("/d").status_code == 200
        assert c.get("/e").status_code == 404
