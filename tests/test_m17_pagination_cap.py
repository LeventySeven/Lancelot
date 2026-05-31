"""M17 P9-cap — safe pagination / sort DEPENDENCIES (DoS-cap + sort-allowlist).

Pins the cap-only pagination surface (the panel FENCED the Page[T] envelope —
NONE is shipped here; fastapi-pagination already owns that and it has zero
byte-path angle). What we DO ship:

  * `lancelot.PageParams` — a `Depends()`-style dependency that parses
    page/size (or limit/offset) query params and CAPS size by
    `LANCELOT_PAGE_MAX_SIZE` (safe default 100). NO `-1`/unbounded escape
    hatch (the corpus footgun: `if items_per_page == -1: -> full-table read`).
    Over-cap or negative -> 422.
  * `lancelot.SortParam(allowed=[...])` — parses a `sort` query param against
    an allowlist; a field NOT in `allowed` -> 422 (anti sort-injection). The
    user still runs their own SQL with the validated/capped values.

If a test goes red, the DoS cap or the sort-allowlist has regressed."""
import importlib

import pytest

import lancelot
from lancelot import Depends, PageParams, SortParam
from lancelot.testclient import TestClient


# ---------------------------------------------------------------------------
# PageParams: caps size, rejects the -1 unbounded footgun, passes within range.
# ---------------------------------------------------------------------------

def test_pageparams_under_limit_passes():
    app = lancelot.App()

    @app.get("/items")
    def items(p=Depends(PageParams())):
        return {"page": p.page, "size": p.size, "offset": p.offset}

    with TestClient(app) as c:
        r = c.get("/items?page=2&size=25")
        assert r.status_code == 200
        body = r.json()
        assert body["page"] == 2
        assert body["size"] == 25
        assert body["offset"] == 25  # (page-1)*size


def test_pageparams_caps_size():
    """size over LANCELOT_PAGE_MAX_SIZE (default 100) -> 422, NOT clamped or
    honored. The cap is the whole point."""
    app = lancelot.App()

    @app.get("/items")
    def items(p=Depends(PageParams())):
        return {"size": p.size}

    with TestClient(app) as c:
        r = c.get("/items?size=100000")
        assert r.status_code == 422


def test_pageparams_rejects_negative_one():
    """THE footgun: `size=-1` (the corpus full-table-read escape) is rejected,
    never honored as 'unbounded'."""
    app = lancelot.App()

    @app.get("/items")
    def items(p=Depends(PageParams())):
        return {"size": p.size}

    with TestClient(app) as c:
        r = c.get("/items?size=-1")
        assert r.status_code == 422


def test_pageparams_rejects_zero_and_negative_page():
    app = lancelot.App()

    @app.get("/items")
    def items(p=Depends(PageParams())):
        return {"page": p.page}

    with TestClient(app) as c:
        assert c.get("/items?page=0").status_code == 422
        assert c.get("/items?size=0").status_code == 422
        assert c.get("/items?page=-3").status_code == 422


def test_pageparams_defaults_when_omitted():
    app = lancelot.App()

    @app.get("/items")
    def items(p=Depends(PageParams())):
        return {"page": p.page, "size": p.size, "offset": p.offset}

    with TestClient(app) as c:
        r = c.get("/items")
        assert r.status_code == 200
        body = r.json()
        assert body["page"] == 1
        assert body["size"] == PageParams.default_size
        assert body["offset"] == 0


def test_pageparams_limit_offset_mode():
    """Opt-in limit/offset style (style='limit_offset'): limit capped, offset
    floored at 0; limit=-1 rejected too."""
    app = lancelot.App()

    @app.get("/items")
    def items(p=Depends(PageParams(style="limit_offset"))):
        return {"limit": p.size, "offset": p.offset}

    with TestClient(app) as c:
        r = c.get("/items?limit=10&offset=40")
        assert r.status_code == 200
        assert r.json() == {"limit": 10, "offset": 40}
        # limit=-1 (the bitcart footgun) -> rejected.
        assert c.get("/items?limit=-1").status_code == 422
        # over-cap limit -> rejected.
        assert c.get("/items?limit=99999").status_code == 422


def test_pageparams_explicit_max_size_kwarg():
    """An explicit max_size= kwarg overrides the env default per-dependency."""
    app = lancelot.App()

    @app.get("/items")
    def items(p=Depends(PageParams(max_size=10))):
        return {"size": p.size}

    with TestClient(app) as c:
        assert c.get("/items?size=10").status_code == 200
        assert c.get("/items?size=11").status_code == 422


def test_pageparams_env_max_size(monkeypatch):
    """LANCELOT_PAGE_MAX_SIZE sets the default cap (read at call time)."""
    monkeypatch.setenv("LANCELOT_PAGE_MAX_SIZE", "5")
    # Re-import so the module-level default reflects the env (it's read at
    # call time, so a fresh dependency picks it up regardless).
    app = lancelot.App()

    @app.get("/items")
    def items(p=Depends(PageParams())):
        return {"size": p.size}

    with TestClient(app) as c:
        assert c.get("/items?size=5").status_code == 200
        assert c.get("/items?size=6").status_code == 422


# ---------------------------------------------------------------------------
# SortParam: allowlist -> 422 on a disallowed field; an allowed field passes.
# ---------------------------------------------------------------------------

def test_sortparam_allowlist_422():
    app = lancelot.App()

    @app.get("/items")
    def items(s=Depends(SortParam(allowed=["name", "created_at"]))):
        return {"field": s.field, "descending": s.descending}

    with TestClient(app) as c:
        # Allowed field -> passes.
        r = c.get("/items?sort=name")
        assert r.status_code == 200
        assert r.json() == {"field": "name", "descending": False}
        # Disallowed field (sort-injection attempt) -> 422.
        assert c.get("/items?sort=password").status_code == 422
        assert c.get("/items?sort=id;DROP TABLE users").status_code == 422


def test_sortparam_descending_prefix():
    """A leading '-' means descending (the field is still allowlist-checked)."""
    app = lancelot.App()

    @app.get("/items")
    def items(s=Depends(SortParam(allowed=["name"]))):
        return {"field": s.field, "descending": s.descending}

    with TestClient(app) as c:
        r = c.get("/items?sort=-name")
        assert r.status_code == 200
        assert r.json() == {"field": "name", "descending": True}
        # '-' on a disallowed field is still rejected.
        assert c.get("/items?sort=-secret").status_code == 422


def test_sortparam_omitted_is_none():
    app = lancelot.App()

    @app.get("/items")
    def items(s=Depends(SortParam(allowed=["name"]))):
        return {"is_none": s.field is None}

    with TestClient(app) as c:
        r = c.get("/items")
        assert r.status_code == 200
        assert r.json() == {"is_none": True}


# ---------------------------------------------------------------------------
# PANEL FENCE: NO Page[T] envelope is shipped.
# ---------------------------------------------------------------------------

def test_no_page_envelope_shipped():
    mod = importlib.import_module("lancelot.pagination")
    assert not hasattr(mod, "Page"), \
        "PANEL FENCE: the Page[T] response envelope must NOT be shipped"
    # Top-level surface exports the cap helpers but no envelope.
    assert not hasattr(lancelot, "Page")
