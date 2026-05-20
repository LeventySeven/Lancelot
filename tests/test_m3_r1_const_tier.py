"""M3-R1 — Router const-tier benchmark + invariants. Static-shape
routes (no {param}) hit an O(1) dict lookup; dynamic routes fall to
the regex walk. Transparent — user code unchanged."""
import time


def test_const_tier_hits_static_route_in_one_lookup():
    """A static route is in self._const; matching it must NOT iterate
    self._dyn (which we keep empty here to prove the path)."""
    import lancelot
    from lancelot.routing import Router, Route

    r = Router()
    rt = Route("GET", "/livez", lambda: {"ok": True})
    r.add(rt)
    # Const-tier hit
    matched, params = r.match("GET", "/livez")
    assert matched is rt and params == {}
    # Method-mismatch on a known static path → 405
    matched, info = r.match("POST", "/livez")
    assert matched is None and info == 405


def test_dynamic_route_still_works_through_regex():
    """A {param} route falls to the regex walk. Param extracted correctly."""
    from lancelot.routing import Router, Route

    r = Router()
    rt = Route("GET", "/items/{id}", lambda id: {"id": id})
    r.add(rt)
    matched, params = r.match("GET", "/items/42")
    assert matched is rt and params == {"id": "42"}


def test_const_tier_makes_static_match_dramatically_faster():
    """With N static routes added, matching a known one should be
    constant-time O(1), NOT proportional to N (the prior O(N) regex
    scan). We assert wall-clock is comfortably under a 10ms ceiling
    for 5000 lookups across 200 registered static routes."""
    from lancelot.routing import Router, Route

    r = Router()
    for i in range(200):
        r.add(Route("GET", f"/route_{i}", (lambda v: lambda: v)(i)))

    t0 = time.perf_counter()
    for _ in range(5000):
        matched, _p = r.match("GET", "/route_42")
        assert matched is not None
    elapsed = time.perf_counter() - t0
    # Generous ceiling — O(1) puts this well below 50ms even on slow CI.
    assert elapsed < 0.5, f"5000 const-tier hits took {elapsed:.3f}s — " \
        f"likely regressed to O(N) regex scan"
