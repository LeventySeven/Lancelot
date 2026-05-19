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
