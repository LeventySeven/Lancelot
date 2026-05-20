from lancelot import _native
from lancelot.routing import APIRouter, Route, Router


class NoMatchFound(Exception):
    """Starlette-parity: raised by App.url_path_for(name) on miss."""


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
    def __init__(self, lifespan=None, title="Lancelot", version="0.1.0",
                 *, redirect_slashes: bool = True, **kwargs):
        """FastAPI-shape constructor: accepts the documented kwargs verbatim.
        Implemented: lifespan, title, version, redirect_slashes. Other
        FastAPI kwargs (docs_url, default_response_class, dependencies,
        generate_unique_id_function, separate_input_output_schemas, …)
        are accepted at registration so user code that passes them does
        not crash; their behavior is ledgered until needed."""
        self.title = title
        self.version = version
        self.redirect_slashes = redirect_slashes
        self.router = Router()
        self.state = State()
        self.dependency_overrides: dict = {}
        self._override_plans: dict = {}  # per-app compiled override plans (A2)
        self._lifespan = lifespan
        self._asgi = None
        self._openapi_cache = None
        self._middleware: list = []  # (cls, opts) — pure-ASGI escape hatch (U20)
        self._mounts: list = []  # (prefix, asgi_app) — Starlette-shape Mount
        self._extra_kwargs = kwargs  # accept-and-store; ledgered behavior
        self._tools: dict = {}  # M2-P5: name → ToolSpec
        self._user_http_middleware: list = []  # @app.middleware("http") fns
        # FastAPI parity: `FastAPI(default_response_class=ORJSONResponse)`
        # picks the response class for any route that didn't override.
        self._default_response_class = kwargs.pop(
            "default_response_class", None)
        # Apply to routes registered directly on the app, too — same
        # hierarchical resolution as include_router.
        self._register_builtin_routes()

    async def __call__(self, scope, receive, send):
        """FastAPI/Starlette parity: `App` is directly callable as ASGI.
        `TestClient(app)` from starlette/anywhere works without an explicit
        `app.asgi()` unwrap. Internally lazy-builds the wrapped ASGI callable."""
        if self._asgi is None:
            self._asgi = self.asgi()
        await self._asgi(scope, receive, send)

    def mount(self, prefix: str, app) -> None:
        """Mount a sub-ASGI app (e.g. StaticFiles) at a path prefix —
        Starlette's Mount shape. The dispatcher strips the prefix from
        scope['path'] before delegating."""
        self._mounts.append((prefix.rstrip("/"), app))
        self._asgi = None

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

    def _add(self, method: str, path: str, **kwargs):
        """Route decorator factory accepting FastAPI's documented kwargs.
        Implemented: status_code. Accepted-and-stored (behavior ledgered):
        response_model, response_class, responses, operation_id, summary,
        description, tags, dependencies, deprecated, name, ...
        Unknown kwargs are accepted (collection-unblock) and dropped."""
        import dataclasses as _dc

        known = {f.name for f in _dc.fields(Route)}
        kw = {k: v for k, v in kwargs.items() if k in known}
        # App-level default_response_class fallback (route didn't override).
        if kw.get("response_class") is None and \
                self._default_response_class is not None:
            kw["response_class"] = self._default_response_class

        def deco(fn):
            self.router.add(Route(method, path, fn, **kw))
            return fn

        return deco

    def tool(self, *, name: str | None = None, description: str | None = None,
             read_only: bool = False, scopes: list | None = None,
             **_ignored):
        """M2-P5: register a callable as an LLM tool. Reuses the existing
        Pydantic/Dependant compiler — input_schema auto-derived from type
        hints (Anthropic-tool-format shape; OpenAI/Google convert to it)."""
        from lancelot.tool import make_tool_spec

        def deco(fn):
            spec = make_tool_spec(fn, name=name, description=description,
                                  read_only=read_only, scopes=scopes)
            self._tools[spec.name] = spec
            return fn

        return deco

    def tool_schemas(self) -> list[dict]:
        """Return the list of registered tools' {name, description,
        input_schema} dicts — exactly the shape Anthropic/MCP expect."""
        return [{"name": s.name, "description": s.description,
                 "input_schema": s.input_schema}
                for s in self._tools.values()]

    def mcp(self, path: str = "/mcp", *, allowed_origins=None):
        """M2-P5b: mount a Streamable-HTTP MCP server at `path` that
        exposes the `@app.tool` registry as JSON-RPC 2.0 over HTTP. Pass
        `allowed_origins=[...]` to restrict (Claude Desktop / claude.ai).

        Spec: modelcontextprotocol.io/specification/2025-06-18 — closes
        the 5 gaps in Robyn's reference impl (Mcp-Session-Id roundtrip,
        MCP-Protocol-Version validation, Origin allow-list)."""
        from lancelot.mcp import make_handler

        h = make_handler(self, allowed_origins=allowed_origins)

        async def _mcp_endpoint(request: "Request"):
            return await h(request)

        # Manual route registration — needs the live Request injected.
        from lancelot.requests import Request

        _mcp_endpoint.__annotations__ = {"request": Request}
        self.router.add(Route("POST", path, _mcp_endpoint,
                              include_in_schema=False))

    def call_tool(self, name: str, **kwargs):
        """Invoke a registered tool by name. Inputs are Pydantic-validated
        (same machinery as HTTP request validation)."""
        spec = self._tools[name]
        validated = spec._validator(**kwargs).model_dump()
        return spec.func(**validated)

    def stream(self, path: str, **kwargs):
        """M2-P4: typed event-stream sugar. The handler is an async
        generator yielding `lancelot.stream.Event` items; the framework
        owns SSE framing + cancellation propagation."""
        import dataclasses as _dc

        known = {f.name for f in _dc.fields(Route)}
        kw = {k: v for k, v in kwargs.items() if k in known}
        kw["is_stream"] = True

        def deco(fn):
            self.router.add(Route("GET", path, fn, **kw))
            return fn

        return deco

    def websocket(self, path: str, **kwargs):
        import dataclasses as _dc

        known = {f.name for f in _dc.fields(Route)}
        kw = {k: v for k, v in kwargs.items() if k in known}

        def deco(fn):
            self.router.add(Route("GET", path, fn, is_ws=True, **kw))
            return fn

        return deco

    def api_route(self, path: str, *, methods=None, **kwargs):
        """FastAPI's generic route decorator — registers one Route per
        method in `methods` (default ['GET'])."""
        import dataclasses as _dc

        known = {f.name for f in _dc.fields(Route)}
        kw = {k: v for k, v in kwargs.items() if k in known}

        def deco(fn):
            for m in (methods or ["GET"]):
                self.router.add(Route(m.upper(), path, fn, **kw))
            return fn

        return deco

    def add_api_route(self, path: str, endpoint, *, methods=None, **kwargs):
        """Function-based registration (no decorator). Same kwargs as
        api_route; default methods=['GET']."""
        import dataclasses as _dc

        known = {f.name for f in _dc.fields(Route)}
        kw = {k: v for k, v in kwargs.items() if k in known}
        for m in (methods or ["GET"]):
            self.router.add(Route(m.upper(), path, endpoint, **kw))

    def get(self, p, **kw): return self._add("GET", p, **kw)
    def post(self, p, **kw): return self._add("POST", p, **kw)
    def put(self, p, **kw): return self._add("PUT", p, **kw)
    def delete(self, p, **kw): return self._add("DELETE", p, **kw)
    def patch(self, p, **kw): return self._add("PATCH", p, **kw)
    def head(self, p, **kw): return self._add("HEAD", p, **kw)
    def options(self, p, **kw): return self._add("OPTIONS", p, **kw)

    def include_router(self, router: APIRouter, prefix: str = "", **kwargs):
        # Accepts FastAPI's documented include_router kwargs (dependencies,
        # default_response_class, responses, tags, deprecated, ...).
        # Lancelot acts on `dependencies` (prepended) and
        # `default_response_class` (applied to child routes without own).
        prefix = prefix.rstrip("/")
        extra_deps = list(kwargs.pop("dependencies", None) or [])
        # Resolve effective default response class — include arg > router's
        # own default > app's own default.
        default_rc = kwargs.pop(
            "default_response_class",
            getattr(router, "default_response_class", None)
            or self._default_response_class,
        )
        import dataclasses as _dc

        _copy = {f.name for f in _dc.fields(Route)} - {
            "method", "path", "handler", "name"}
        for r in router.routes:
            kw = {k: getattr(r, k) for k in _copy}
            if extra_deps:
                kw["dependencies"] = list(extra_deps) + list(
                    kw.get("dependencies", []) or [])
            if kw.get("response_class") is None and default_rc is not None:
                kw["response_class"] = default_rc
            self.router.add(Route(r.method, prefix + r.path, r.handler,
                                  r.name, **kw))

    def middleware(self, kind: str = "http"):
        """FastAPI `@app.middleware("http")` decorator. The wrapped fn
        becomes a Starlette-shape `async def mw(request, call_next)` that
        Lancelot adapts to a pure-ASGI wrapper at build time."""

        def deco(fn):
            self._user_http_middleware.append(fn)
            self._asgi = None
            return fn

        return deco

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

    def url_path_for(self, name: str, /, **path_params) -> str:
        """Starlette parity: resolve a route's path template by handler
        name and substitute `{param}`/`{param:conv}` placeholders. Raises
        NoMatchFound on miss."""
        import re

        for r in self.router.routes:
            if r.name != name:
                continue
            # Replace each {param} or {param:conv} with the supplied value.
            def _sub(m):
                key = m.group(1)
                if key not in path_params:
                    raise NoMatchFound(name)
                return str(path_params[key])

            return re.sub(r"\{([^}:]+)(?::[^}]+)?\}", _sub, r.path)
        raise NoMatchFound(name)

    def asgi(self):
        if self._asgi is None:
            from lancelot.asgi import build_asgi
            from lancelot.middleware import HTTPMiddlewareAdapter

            app = build_asgi(self)
            # @app.middleware("http") fns wrap the inner ASGI app, then
            # add_middleware-registered pure-ASGI middlewares wrap THAT.
            # Last-added is outermost (FastAPI/Starlette order).
            for fn in reversed(self._user_http_middleware):
                app = HTTPMiddlewareAdapter(app, fn)
            for mw_cls, opts in reversed(self._middleware):
                app = mw_cls(app, **opts)
            self._asgi = app
        return self._asgi


# M0 compatibility: `lancelot.Lancelot` + `lancelot.run` keep working.
Lancelot = App


def run(app: "App", host: str = "127.0.0.1", port: int = 8000):
    routes = [(r.method, r.path, r.handler) for r in app.routes]
    _native.run(routes, host, port)
