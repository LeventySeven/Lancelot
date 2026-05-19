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
    def __init__(self, lifespan=None, title="Lancelot", version="0.1.0"):
        self.title = title
        self.version = version
        self.router = Router()
        self.state = State()
        self.dependency_overrides: dict = {}
        self._override_plans: dict = {}  # per-app compiled override plans (A2)
        self._lifespan = lifespan
        self._asgi = None
        self._openapi_cache = None
        self._middleware: list = []  # (cls, opts) — pure-ASGI escape hatch (U20)
        self._register_builtin_routes()

    def add_middleware(self, mw_cls, **opts):
        """U20 escape hatch: an unmodified pure-ASGI middleware wrapping the
        app callable. NEVER BaseHTTPMiddleware/call_next. Last-added is the
        outermost wrapper."""
        self._middleware.append((mw_cls, opts))
        self._asgi = None  # rebuild the wrapped callable

    def _register_builtin_routes(self):
        """OpenAPI JSON endpoint (doc UIs added in M1-P3 T8). FastAPI does the
        same — every app exposes /openapi.json."""
        from lancelot.docs import redoc_html, scalar_html, swagger_ui_html
        from lancelot.responses import HTMLResponse, JSONResponse
        from lancelot.validation import _wire_json

        def _openapi_json():
            return JSONResponse(_wire_json(self.openapi()))

        def _docs():
            return HTMLResponse(swagger_ui_html("/openapi.json", "Lancelot - Swagger UI"))

        def _redoc():
            return HTMLResponse(redoc_html("/openapi.json", "Lancelot - ReDoc"))

        def _scalar():
            return HTMLResponse(scalar_html("/openapi.json", "Lancelot - Scalar"))

        for path, fn in (("/openapi.json", _openapi_json), ("/docs", _docs),
                         ("/redoc", _redoc), ("/scalar", _scalar)):
            self.router.add(Route("GET", path, fn, include_in_schema=False))

    # --- registration -----------------------------------------------------
    @property
    def routes(self) -> list[Route]:
        return self.router.routes

    def _add(self, method: str, path: str):
        def deco(fn):
            self.router.add(Route(method, path, fn))
            return fn

        return deco

    def websocket(self, path: str):
        def deco(fn):
            self.router.add(Route("GET", path, fn, is_ws=True))
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
        """Built once at first access, then memoized (FastAPI semantics:
        the schema is built once; routes added after first access are not
        reflected — documented contract)."""
        if self._openapi_cache is None:
            from lancelot.openapi import build_openapi

            self._openapi_cache = build_openapi(
                self, title=self.title, version=self.version
            )
        return self._openapi_cache

    def asgi(self):
        if self._asgi is None:
            from lancelot.asgi import build_asgi

            app = build_asgi(self)
            # last-added wraps outermost (FastAPI/Starlette order)
            for mw_cls, opts in reversed(self._middleware):
                app = mw_cls(app, **opts)
            self._asgi = app
        return self._asgi


# M0 compatibility: `lancelot.Lancelot` + `lancelot.run` keep working.
Lancelot = App


def run(app: "App", host: str = "127.0.0.1", port: int = 8000):
    routes = [(r.method, r.path, r.handler) for r in app.routes]
    _native.run(routes, host, port)
