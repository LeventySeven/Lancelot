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
