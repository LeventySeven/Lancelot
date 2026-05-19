"""M1-P4 moat: same app on real FastAPI vs Lancelot — assert byte parity
for the COVERED subset (HTTPBearer 401 body+WWW-Authenticate;
securitySchemes structural; CORS simple-request ACAO). Genuine long-tail
(full preflight header-set ordering, OAuth2 form 422, OpenID, digest,
scope ENFORCEMENT [M4]) → conformance/vendor/quarantine/ + ledger,
never silently passing."""
import fastapi
from fastapi.testclient import TestClient as FA
from fastapi.security import HTTPBearer as FAHTTPBearer
from fastapi.middleware.cors import CORSMiddleware as FACORS

import lancelot
from lancelot.testclient import TestClient as LC
from lancelot.params import Depends as LDepends
from lancelot.security import HTTPBearer as LHTTPBearer
from lancelot.middleware import CORSMiddleware as LCORS


def _build_fa():
    app = fastapi.FastAPI(title="T")
    sec = FAHTTPBearer()

    @app.get("/me")
    def me(c=fastapi.Depends(sec)):
        return {"s": c.scheme}

    app.add_middleware(FACORS, allow_origins=["https://ex.com"],
                       allow_methods=["GET"], allow_headers=["x-test"])
    return app


def _build_lc():
    app = lancelot.App(title="T")
    sec = LHTTPBearer()

    @app.get("/me")
    def me(c=LDepends(sec)):
        return {"s": c.scheme}

    app.add_middleware(LCORS, allow_origins=["https://ex.com"],
                       allow_methods=["GET"], allow_headers=["x-test"])
    return app


def test_p4_byte_parity_covered_subset():
    with FA(_build_fa()) as fa, LC(_build_lc()) as lc:
        # 401 body + WWW-Authenticate byte-identical
        f0, l0 = fa.get("/me"), lc.get("/me")
        assert l0.status_code == f0.status_code == 401
        assert l0.content == f0.content, (
            f"\nFA : {f0.content!r}\nLC : {l0.content!r}")
        assert l0.headers.get("www-authenticate") == \
            f0.headers.get("www-authenticate") == "Bearer"
        # securitySchemes structurally identical (FastAPI's own bar)
        assert lc.get("/openapi.json").json()["components"]["securitySchemes"] == \
            fa.get("/openapi.json").json()["components"]["securitySchemes"]
        # CORS simple-request ACAO parity
        fh = fa.get("/me", headers={"Origin": "https://ex.com",
                                    "Authorization": "Bearer x"})
        lh = lc.get("/me", headers={"Origin": "https://ex.com",
                                    "Authorization": "Bearer x"})
        assert lh.headers.get("access-control-allow-origin") == \
            fh.headers.get("access-control-allow-origin") == "https://ex.com"


def _star_app(F, CORS):
    app = F.FastAPI() if hasattr(F, "FastAPI") else F.App()

    @app.get("/")
    def root():
        return {"ok": True}

    app.add_middleware(CORS, allow_origins=["*"], allow_methods=["GET"])
    return app


def test_cors_wildcard_and_preflight_byte_parity():
    """V1/A3: `allow_origins=["*"]` must emit literal `*` (Starlette), NOT
    reflect Origin (open-reflector vuln); preflight on a disallowed origin
    must 400 like Starlette; duplicate Authorization = first-wins."""
    with FA(_star_app(fastapi, FACORS)) as fa, LC(_star_app(lancelot, LCORS)) as lc:
        f = fa.get("/", headers={"Origin": "https://attacker.evil"})
        l = lc.get("/", headers={"Origin": "https://attacker.evil"})
        assert l.headers.get("access-control-allow-origin") == \
            f.headers.get("access-control-allow-origin") == "*"  # NOT reflected

    # explicit-list preflight: disallowed origin -> Starlette 400
    with FA(_build_fa()) as fa, LC(_build_lc()) as lc:
        fp = fa.request("OPTIONS", "/me", headers={
            "Origin": "https://evil.com",
            "Access-Control-Request-Method": "GET"})
        lp = lc.request("OPTIONS", "/me", headers={
            "Origin": "https://evil.com",
            "Access-Control-Request-Method": "GET"})
        assert lp.status_code == fp.status_code  # 400 in Starlette
        # duplicate Authorization header -> FIRST wins (no smuggling)
        fr = fa.get("/me", headers=[("authorization", "Bearer aaa"),
                                    ("authorization", "Bearer bbb")])
        lr = lc.get("/me", headers=[("authorization", "Bearer aaa"),
                                    ("authorization", "Bearer bbb")])
        assert lr.json() == fr.json()


def _hdr_app(F, CORS):
    app = F.FastAPI() if hasattr(F, "FastAPI") else F.App()

    @app.get("/")
    def root():
        return {"ok": True}

    app.add_middleware(CORS, allow_origins=["https://ex.com"],
                       allow_methods=["GET"], allow_headers=["Z-Last", "A-Token"])
    return app


def _cred_app(F, CORS):
    app = F.FastAPI() if hasattr(F, "FastAPI") else F.App()

    @app.get("/")
    def root():
        return {"ok": True}

    app.add_middleware(CORS, allow_origins=["https://ex.com"],
                       allow_credentials=True, allow_methods=["GET"])
    return app


def test_cors_preflight_acrh_and_sorted_headers_byte_parity():
    with FA(_hdr_app(fastapi, FACORS)) as fa, LC(_hdr_app(lancelot, LCORS)) as lc:
        def pf(c, acrh):
            return c.request("OPTIONS", "/", headers={
                "Origin": "https://ex.com",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": acrh})

        f1, l1 = pf(fa, "a-token"), pf(lc, "a-token")
        assert l1.status_code == f1.status_code == 200
        assert l1.content == f1.content
        assert l1.headers.get("access-control-allow-headers") == \
            f1.headers.get("access-control-allow-headers")
        f2, l2 = pf(fa, "authorization, x-nope"), pf(lc, "authorization, x-nope")
        assert l2.status_code == f2.status_code == 400  # Disallowed CORS headers
        assert l2.content == f2.content
    with FA(_cred_app(fastapi, FACORS)) as fa, LC(_cred_app(lancelot, LCORS)) as lc:
        f = fa.get("/", headers={"Origin": "https://evil.com"})
        l = lc.get("/", headers={"Origin": "https://evil.com"})
        assert l.headers.get("access-control-allow-credentials") == \
            f.headers.get("access-control-allow-credentials") == "true"
        assert l.headers.get("access-control-allow-origin") == \
            f.headers.get("access-control-allow-origin")


def _star_methods_app(F, CORS):
    app = F.FastAPI() if hasattr(F, "FastAPI") else F.App()

    @app.get("/")
    def root():
        return {"ok": True}

    app.add_middleware(CORS, allow_origins=["https://ex.com"],
                       allow_methods=["*"], allow_headers=["*"])
    return app


def test_cors_wildcard_methods_headers_and_content_length():
    # F1 (*-methods expand) + F2 (*-headers: verbatim ACRH / none w/o ACRH)
    with FA(_star_methods_app(fastapi, FACORS)) as fa, \
         LC(_star_methods_app(lancelot, LCORS)) as lc:
        def pf(c, extra):
            h = {"Origin": "https://ex.com",
                 "Access-Control-Request-Method": "DELETE"}
            h.update(extra)
            return c.request("OPTIONS", "/", headers=h)
        f1, l1 = pf(fa, {}), pf(lc, {})
        assert l1.headers.get("access-control-allow-methods") == \
            f1.headers.get("access-control-allow-methods") == \
            "DELETE, GET, HEAD, OPTIONS, PATCH, POST, PUT"
        assert l1.headers.get("access-control-allow-headers") == \
            f1.headers.get("access-control-allow-headers")  # both None
        f2, l2 = pf(fa, {"Access-Control-Request-Headers": "x-y, z-w"}), \
            pf(lc, {"Access-Control-Request-Headers": "x-y, z-w"})
        assert l2.headers.get("access-control-allow-headers") == \
            f2.headers.get("access-control-allow-headers") == "x-y, z-w"
    # F3: content-length byte-identical to real FastAPI (the 401 body is
    # byte-identical across the two; /openapi.json is byte-identical for
    # this security-only app once the 422 predicate is FastAPI-faithful).
    with FA(_build_fa()) as fa, LC(_build_lc()) as lc:
        fj, lj = fa.get("/openapi.json"), lc.get("/openapi.json")
        assert lj.content == fj.content  # full byte parity (security-only)
        assert lj.headers.get("content-length") == fj.headers.get("content-length")
        assert lj.headers.get("content-length") == str(len(lj.content))  # honest
        f0, l0 = fa.get("/me"), lc.get("/me")  # 401
        assert l0.headers.get("content-length") == f0.headers.get("content-length")


def _raw_headers(app_callable, method, path, req_headers=None):
    """Drive the ASGI callable directly and capture the ORDERED
    http.response.start header list — header order is part of the wire
    bytes (ASGI servers write the list verbatim); .headers.get() is
    order-blind and cannot gate this."""
    import anyio

    out = {}

    async def rcv():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def snd(m):
        if m["type"] == "http.response.start":
            out["h"] = [(k.decode(), v.decode()) for k, v in m["headers"]]
            out["s"] = m["status"]

    scope = {"type": "http", "method": method, "path": path,
             "raw_path": path.encode(), "query_string": b"",
             "headers": [(k.lower().encode(), v.encode())
                         for k, v in (req_headers or {}).items()],
             "scheme": "http", "server": ("t", 80), "client": ("c", 1),
             "http_version": "1.1"}

    async def _run():
        await app_callable(scope, rcv, snd)

    anyio.run(_run)
    return out["s"], [k for k, _ in out["h"]]


def test_wire_header_ORDER_matches_fastapi():
    """Raw-ASGI differential: the ordered http.response.start header list
    must equal real fastapi 0.136.1 on every JSON/security/CORS path
    (Starlette init_headers: explicit headers, then content-length, then
    content-type LAST). The order-blind .headers.get() asserts could not
    catch this."""
    fasgi = _build_fa()         # FastAPI app instance IS the ASGI callable
    lasgi = _build_lc().asgi()  # Lancelot: build the ASGI callable
    cases = [
        ("GET", "/me", None),                                    # 401 sec
        ("GET", "/nope", None),                                   # 404
        ("OPTIONS", "/me", {"origin": "https://ex.com",
                             "access-control-request-method": "GET"}),
        ("GET", "/me", {"origin": "https://ex.com",
                        "authorization": "Bearer x"}),            # simple CORS
        ("GET", "/openapi.json", None),                           # Response
    ]
    for m, p, h in cases:
        fs, fo = _raw_headers(fasgi, m, p, h)
        ls, lo = _raw_headers(lasgi, m, p, h)
        assert ls == fs, (m, p, "status", ls, fs)
        assert lo == fo, (m, p, "HEADER ORDER", lo, fo)
    # credentials simple path: Starlette emits ACAC before ACAO/Vary
    fcs, fco = _raw_headers(_cred_app(fastapi, FACORS), "GET", "/",
                            {"origin": "https://ex.com"})
    lcs, lco = _raw_headers(_cred_app(lancelot, LCORS).asgi(), "GET", "/",
                            {"origin": "https://ex.com"})
    assert lcs == fcs and lco == fco, ("cred simple ORDER", lco, fco)


def _p_app(F):
    """One app, six route shapes — the exact predicate FastAPI uses to emit
    the 422 response + HTTPValidationError/ValidationError schemas."""
    from pydantic import BaseModel

    is_fa = hasattr(F, "FastAPI")
    app = F.FastAPI() if is_fa else F.App()
    Dep = (F.Depends if is_fa else __import__(
        "lancelot.params", fromlist=["Depends"]).Depends)
    if is_fa:
        from fastapi.security import HTTPBearer as B
        from fastapi import Request as Rq
    else:
        from lancelot.security import HTTPBearer as B
        from lancelot.requests import Request as Rq
    sec = B()

    class Body(BaseModel):
        x: int

    @app.get("/secdep")
    def secdep(c=Dep(sec)):
        return {}

    @app.get("/none")
    def none():
        return {}

    @app.get("/req")
    def req(r: Rq):
        return {}

    @app.get("/query")
    def q(limit: int = 0):
        return {}

    @app.post("/body")
    def body(b: Body):
        return {}

    def _inner(page: int = 1):
        return page

    @app.get("/depquery")
    def depquery(v=Dep(_inner)):
        return {}

    return app


def test_openapi_422_predicate_matches_fastapi():
    """FastAPI emits `422` + the validation-error schemas IFF a route has
    >=1 transitive validatable field (query/path/header/cookie) or a body.
    Security-only / no-param / Request-only routes get NEITHER. Lancelot
    previously over-emitted (keyed off `bool(plan.dependencies)`, and a
    security scheme IS a SubDep) — the byte oracle caught it."""
    with FA(_p_app(fastapi)) as fa, LC(_p_app(lancelot)) as lc:
        fd = fa.get("/openapi.json").json()
        ld = lc.get("/openapi.json").json()

        def has422(d, p, m="get"):
            return "422" in d["paths"][p][m]["responses"]

        for p, m in [("/secdep", "get"), ("/none", "get"), ("/req", "get")]:
            assert has422(fd, p, m) is False
            assert has422(ld, p, m) == has422(fd, p, m), (p, "must NOT 422")
        for p, m in [("/query", "get"), ("/body", "post"),
                     ("/depquery", "get")]:
            assert has422(fd, p, m) is True
            assert has422(ld, p, m) == has422(fd, p, m), (p, "MUST 422")
        # schemas presence parity (some route 422s here → both have them)
        assert ("schemas" in ld.get("components", {})) == \
            ("schemas" in fd.get("components", {})) is True
