"""Regression guards for the post-90% conformance push.

Each test pins ONE behavior that was unblocked in this push so we don't
quietly lose it as future work touches the surrounding code paths.

Targeted gaps (876 → 910 = +34 Class-A passes):
  - `@app.exception_handler` / `add_exception_handler` /
    `FastAPI(exception_handlers={ExcCls: fn})` surface
  - strict_content_type defaulting True with per-router / per-route override
  - Annotated metadata preservation (e.g. Pydantic `Json[T]`)
  - lenient cookie parser (SimpleCookie drops `[`/`]`/`"`)
  - subclass-class-attr media_type honored in JSONResponse subclasses
  - callable-instance + functools.partial dispatch in DI
  - single-Body scalar pattern (body root IS the value)
  - default 500 → PlainTextResponse "Internal Server Error" (Starlette parity)
  - Optional[list[T]] form / form-model fields wrap single-value as `["x"]`"""
import asyncio
import functools

import pytest

import lancelot
from lancelot.params import Body, Depends, Form
from lancelot.testclient import TestClient


# ---- @app.exception_handler -------------------------------------------

def test_exception_handler_constructor_kwarg():
    from lancelot.exceptions import HTTPException
    from lancelot.responses import JSONResponse

    def h(req, exc):
        return JSONResponse({"caught": "yes"}, status_code=418)

    app = lancelot.App(exception_handlers={HTTPException: h})

    @app.get("/boom")
    def boom():
        raise HTTPException(status_code=400, detail="x")

    with TestClient(app) as c:
        r = c.get("/boom")
    assert r.status_code == 418
    assert r.json() == {"caught": "yes"}


def test_exception_handler_decorator_form():
    from lancelot.exceptions import HTTPException
    from lancelot.responses import JSONResponse

    app = lancelot.App()

    @app.exception_handler(HTTPException)
    def h(req, exc):
        return JSONResponse({"caught": True}, status_code=418)

    @app.get("/x")
    def x():
        raise HTTPException(status_code=400)

    with TestClient(app) as c:
        r = c.get("/x")
    assert r.status_code == 418


# ---- strict_content_type ----------------------------------------------

def test_strict_content_type_default_true_rejects_no_ct():
    """A POST with a JSON body but NO Content-Type header is rejected
    422 (FastAPI 0.110+ default). Note: TestClient with `content=...`
    intentionally omits the Content-Type that `json=...` would set."""
    from pydantic import BaseModel

    class M(BaseModel):
        x: int

    app = lancelot.App()  # default strict_content_type=True

    @app.post("/p")
    def p(m: M):
        return {"ok": True}

    with TestClient(app) as c:
        r = c.post("/p", content='{"x": 1}')  # no content-type
    assert r.status_code == 422


def test_strict_content_type_off_at_app_level_accepts():
    from pydantic import BaseModel

    class M(BaseModel):
        x: int

    app = lancelot.App(strict_content_type=False)

    @app.post("/p")
    def p(m: M):
        return {"x": m.x}

    with TestClient(app) as c:
        r = c.post("/p", content='{"x": 1}')
    assert r.status_code == 200


# ---- Annotated metadata preservation (Json[T]) ------------------------

def test_annotated_json_metadata_survives_extract_field():
    """`Annotated[Json[list[str]], Query()]` MUST preserve the inner
    `Json` marker so Pydantic parses the JSON string into a list."""
    from pydantic import Json
    from typing import Annotated
    from lancelot.params import Query

    app = lancelot.App()

    @app.get("/q")
    def q(items: Annotated[Json[list[str]], Query()]):
        return {"items": items}

    with TestClient(app) as c:
        r = c.get('/q?items=%5B%22a%22%2C%22b%22%5D')  # ?items=["a","b"]
    assert r.status_code == 200
    assert r.json() == {"items": ["a", "b"]}


# ---- lenient cookie parser --------------------------------------------

def test_cookie_with_brackets_is_parsed():
    """SimpleCookie silently DROPS cookies with `[` / `]` / `"`. Our
    lenient parser keeps them so JSON-shaped cookies survive."""
    from typing import Annotated
    from lancelot.params import Cookie

    app = lancelot.App()

    @app.get("/c")
    def c(items: Annotated[str, Cookie()]):
        return {"got": items}

    with TestClient(app) as c:
        c.cookies.set("items", '["abc","def"]')
        r = c.get("/c")
    assert r.status_code == 200
    assert r.json() == {"got": '["abc","def"]'}


# ---- response_class subclass media_type --------------------------------

def test_subclass_class_attr_media_type_honored():
    """A `class X(JSONResponse): media_type='application/x-foo'` MUST be
    visible on the INSTANCE (the seam reads `_r.media_type`)."""
    from lancelot.responses import JSONResponse

    class X(JSONResponse):
        media_type = "application/x-foo"

    app = lancelot.App()

    @app.get("/x", response_class=X)
    def x():
        return {"ok": True}

    with TestClient(app) as c:
        r = c.get("/x")
    assert r.headers["content-type"] == "application/x-foo"


# ---- callable-instance dispatch ---------------------------------------

def test_callable_instance_async_call_is_awaited():
    """A Depends(callable_instance) where `__call__` is `async def`
    MUST be awaited (not return a coroutine to the JSON serializer)."""

    class A:
        async def __call__(self, value: str) -> str:
            return value.upper()

    inst = A()
    app = lancelot.App()

    @app.get("/x")
    async def x(v: str = Depends(inst)):
        return {"v": v}

    with TestClient(app) as c:
        r = c.get("/x?value=hello")
    assert r.status_code == 200
    assert r.json() == {"v": "HELLO"}


def test_partial_over_async_dep_is_awaited():
    """A Depends(functools.partial(async_fn, …)) MUST be awaited too."""

    async def base(value: str, extra: str) -> str:
        return f"{value}-{extra}"

    bound = functools.partial(base, extra="X")
    app = lancelot.App()

    @app.get("/x")
    async def x(v: str = Depends(bound)):
        return {"v": v}

    with TestClient(app) as c:
        r = c.get("/x?value=a")
    assert r.status_code == 200
    assert r.json() == {"v": "a-X"}


# ---- single-Body scalar pattern ---------------------------------------

def test_single_body_scalar_takes_raw_json_value():
    """`name: str = Body()` with EXACTLY ONE Body param + no embed:
    the raw JSON body IS the value (deep/01 §3.1.3)."""
    app = lancelot.App()

    @app.put("/x")
    def x(name: str = Body()):
        return {"name": name}

    with TestClient(app) as c:
        r = c.put("/x", json="Morty")
    assert r.status_code == 200
    assert r.json() == {"name": "Morty"}


# ---- Starlette-shape default 500 --------------------------------------

def test_default_500_is_plaintext_internal_server_error():
    """The default 500 body is `Internal Server Error` as PLAIN TEXT
    (Starlette parity — NOT our old JSON `{"detail": ...}` shape)."""
    app = lancelot.App()

    @app.get("/boom")
    def boom():
        raise RuntimeError("oops")

    # raise_server_exceptions=False so we receive the response.
    with TestClient(app) as c:
        r = c.get("/boom")
    assert r.status_code == 500
    assert r.text == "Internal Server Error"
    assert r.headers["content-type"].startswith("text/plain")


# ---- Optional[list[T]] form ------------------------------------------

def test_optional_list_form_single_value_wraps_to_list():
    """`p: Annotated[list[str] | None, Form()] = None` posted with one
    value `p=world` MUST become `["world"]`, not the bare string."""
    from typing import Annotated

    app = lancelot.App()

    @app.post("/p")
    async def p(p: Annotated[list[str] | None, Form()] = None):
        return {"p": p}

    with TestClient(app) as c:
        r = c.post("/p", data={"p": "world"})
    assert r.status_code == 200
    assert r.json() == {"p": ["world"]}
