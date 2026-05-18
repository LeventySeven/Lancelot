from lancelot import App
from lancelot.routing import APIRouter


def test_include_router_prefix_and_match():
    # App.include_router(prefix=) — exercised here (built in T1's routing.py).
    app = App()
    r = APIRouter(prefix="/v1")

    @r.get("/users/{uid}")
    def get_user(uid): ...

    app.include_router(r, prefix="/api")

    route, params = app.router.match("GET", "/api/v1/users/42")
    assert route is not None and route.name == "get_user"
    assert params == {"uid": "42"}
    assert app.router.match("POST", "/api/v1/users/42") == (None, 405)
    assert app.router.match("GET", "/nope") == (None, 404)


def test_nested_router():
    # The genuine RED for T2: APIRouter.include_router (nested routers).
    inner = APIRouter(prefix="/inner")

    @inner.get("/x")
    def x(): ...

    outer = APIRouter(prefix="/outer")
    outer.include_router(inner)
    app = App()
    app.include_router(outer)
    assert app.router.match("GET", "/outer/inner/x")[0] is not None
