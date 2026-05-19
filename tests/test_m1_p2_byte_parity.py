"""The moat, actually verified: build the SAME app on REAL FastAPI and on
Lancelot (identical pydantic-core), drive both, and compare RAW RESPONSE
BYTES (not parsed dicts). This is the only test that proves DoD #1
("byte-identical 422 wire-shape"). If this passes, the thesis holds on the
wire; if it fails, it does not — no self-reference, no dict-equality."""
import pydantic
import pytest

import fastapi
from fastapi.testclient import TestClient as FATestClient

import lancelot
from lancelot.params import Depends as LDepends
from lancelot.testclient import TestClient as LTestClient


class Item(pydantic.BaseModel):
    name: str
    age: int


def _fa_app():
    app = fastapi.FastAPI()

    @app.get("/q")
    def q(query: int):
        return {"query": query}

    @app.get("/p/{item_id}")
    def p(item_id: int):
        return {"item_id": item_id}

    @app.get("/c/{item_id}")
    def c(item_id: str = fastapi.Path(min_length=3)):
        return {"item_id": item_id}

    @app.post("/body")
    def body(items: list[Item]):
        return {"n": len(items)}

    def dep(skip: int = 0):
        return {"skip": skip}

    @app.get("/d")
    def d(info: dict = fastapi.Depends(dep)):
        return info

    return app


def _lc_app():
    app = lancelot.App()

    @app.get("/q")
    def q(query: int):
        return {"query": query}

    @app.get("/p/{item_id}")
    def p(item_id: int):
        return {"item_id": item_id}

    @app.get("/c/{item_id}")
    def c(item_id: str = lancelot.params.Path(min_length=3)):
        return {"item_id": item_id}

    @app.post("/body")
    def body(items: list[Item]):
        return {"n": len(items)}

    def dep(skip: int = 0):
        return {"skip": skip}

    @app.get("/d")
    def d(info: dict = LDepends(dep)):
        return info

    return app


import lancelot.params  # noqa: E402  (after _lc_app def, used inside it)

_JSON = {"content-type": "application/json"}
# Each case: (method, url, httpx-kwargs). Body cases send a real JSON
# content-type (the M1-P2 scope: JSON APIs). The no-content-type / form /
# multipart content-type matrix is deep/01 §3.2 — P6/P7 ratchet, out of P2.
CASES = [
    ("GET", "/q", {}),                                  # missing query -> 422
    ("GET", "/q?query=4.5", {}),                        # int_parsing query
    ("GET", "/p/foo", {}),                              # int_parsing path
    ("GET", "/c/fo", {}),                               # string_too_short+ctx
    ("POST", "/body", {"json": [{"age": "five"}]}),     # nested body list idx
    ("POST", "/body", {"content": b"{bad", "headers": _JSON}),  # json_invalid
    ("GET", "/d", {}),                                   # Depends happy 200
    ("GET", "/d?skip=5", {}),                            # Depends + query 200
]


@pytest.fixture(scope="module")
def clients():
    with FATestClient(_fa_app()) as fa, LTestClient(_lc_app()) as lc:
        yield fa, lc


@pytest.mark.parametrize("method,url,kw", CASES)
def test_byte_identical_to_real_fastapi(clients, method, url, kw):
    fa, lc = clients
    fr = fa.request(method, url, **kw)
    lr = lc.request(method, url, **kw)
    assert lr.status_code == fr.status_code, (
        f"{method} {url}: status {lr.status_code} != FastAPI {fr.status_code}")
    assert lr.content == fr.content, (
        f"{method} {url}: BYTES differ\nFastAPI : {fr.content!r}\nLancelot: {lr.content!r}")
