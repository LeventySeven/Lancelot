import asyncio
import contextlib

from lancelot import _native
from lancelot.routing import APIRouter, Route, Router


class State:
    """Mutable attribute bag (FastAPI/Starlette app.state shape)."""

    def __init__(self):
        self.__dict__["_d"] = {}

    def __getattr__(self, k):
        try:
            return self.__dict__["_d"][k]
        except KeyError as e:
            raise AttributeError(k) from e

    def __setattr__(self, k, v):
        self.__dict__["_d"][k] = v


class App:
    def __init__(self, lifespan=None):
        self.router = Router()
        self.state = State()
        self.dependency_overrides: dict = {}
        self._lifespan = lifespan
        self._asgi = None

    # --- registration -----------------------------------------------------
    @property
    def routes(self) -> list[Route]:
        return self.router.routes

    def _add(self, method: str, path: str):
        def deco(fn):
            self.router.add(Route(method, path, fn))
            return fn

        return deco

    def get(self, p): return self._add("GET", p)
    def post(self, p): return self._add("POST", p)
    def put(self, p): return self._add("PUT", p)
    def delete(self, p): return self._add("DELETE", p)
    def patch(self, p): return self._add("PATCH", p)
    def head(self, p): return self._add("HEAD", p)
    def options(self, p): return self._add("OPTIONS", p)

    def include_router(self, router: APIRouter, prefix: str = ""):
        prefix = prefix.rstrip("/")
        for r in router.routes:
            self.router.add(Route(r.method, prefix + r.path, r.handler, r.name))

    def openapi(self) -> dict:
        # M1-P3 (U17) replaces this with the real pydantic-FFI generator.
        return {
            "openapi": "3.1.0",
            "info": {"title": "Lancelot", "version": "0"},
            "paths": {},
        }

    def asgi(self):
        if self._asgi is None:
            from lancelot.asgi import build_asgi

            self._asgi = build_asgi(self)
        return self._asgi


# M0 compatibility: `lancelot.Lancelot` + `lancelot.run` keep working.
Lancelot = App


def run(app: "App", host: str = "127.0.0.1", port: int = 8000):
    routes = [(r.method, r.path, r.handler) for r in app.routes]
    _native.run(routes, host, port)


@contextlib.contextmanager
def _run_lifespan_sync(app: "App"):
    """Drive the app's async lifespan from sync code (TestClient + the ASGI
    lifespan protocol). Startup exceptions propagate (fail-fast, not a runtime
    500). No-op if no lifespan was provided."""
    if app._lifespan is None:
        yield
        return
    loop = asyncio.new_event_loop()
    try:
        cm = app._lifespan(app)
        loop.run_until_complete(cm.__aenter__())  # startup; raises -> fail-fast
        try:
            yield
        finally:
            loop.run_until_complete(cm.__aexit__(None, None, None))  # shutdown
    finally:
        loop.close()
