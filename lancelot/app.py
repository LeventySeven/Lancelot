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
                 *, redirect_slashes: bool = True,
                 max_concurrent_requests: int | None = None,
                 drain_timeout: float = 30.0,
                 exception_handlers: dict | None = None,
                 strict_content_type: bool = True,
                 dependencies: list | None = None,
                 json_max_depth: int = 128,
                 **kwargs):
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
        # M15 T5 (#6-core): app-lifetime supervised daemons. Factories are
        # registered via `app.lifetime_task(coro_fn)`; the lifespan-startup
        # seam starts one task per factory and STRONG-refs it here (so it
        # isn't GC'd mid-run); drain()/lifespan-shutdown cancels + awaits each
        # bounded by the drain deadline.
        self._lifetime_task_factories: list = []
        self._lifetime_tasks: list = []
        self._extra_kwargs = kwargs  # accept-and-store; ledgered behavior
        # M15 T2 (#11): FastAPI's per-app operationId factory. Was swallowed
        # into _extra_kwargs and ignored; now consumed by build_openapi as the
        # fallback when a route has no explicit operation_id. None = use the
        # built-in MUST-32 formula. (kwargs is the same dict stored above, so
        # popping here also removes it from _extra_kwargs.)
        self._generate_unique_id_function = kwargs.pop(
            "generate_unique_id_function", None)
        self._tools: dict = {}  # M2-P5: name → ToolSpec
        self._user_http_middleware: list = []  # @app.middleware("http") fns
        # @app.on_event("startup"/"shutdown") deprecated decorator handlers.
        self._on_event_handlers: dict = {"startup": [], "shutdown": []}
        # FastAPI parity: app.add_exception_handler / @app.exception_handler /
        # FastAPI(exception_handlers={ExcCls: fn}). MRO-walked at exception
        # time so a `Exception:` catch-all also fires for subclasses.
        self.exception_handlers: dict = dict(exception_handlers or {})
        # FastAPI 0.110+ feature: app-wide default for content-type checking
        # on JSON-body routes. Per-route / per-router override allowed.
        self.strict_content_type: bool = strict_content_type
        # OPS3: JSON-nesting-depth DoS guard limit (read by the ASGI seam).
        # 128 = Robyn's value; raise for deep config payloads, lower to
        # tighten protection on shallow-schema APIs.
        self._json_max_depth: int = json_max_depth
        # App-wide dependencies — prepended to every route's dep list.
        # Same semantics as `APIRouter(dependencies=[])`: validation /
        # auth runs before the handler; the return value is discarded.
        self.dependencies: list = list(dependencies or [])
        # Graceful-shutdown flag — flipped by lifespan_shutdown,
        # `app.initiate_shutdown()`, or `app.install_signal_handlers()`.
        # /readyz returns 503 + Retry-After while True so the LB stops
        # sending new traffic; the ASGI seam refuses NEW requests with
        # 503 but lets in-flight ones complete.
        self._shutting_down: bool = False
        # Tier-B.B1 + B4: in-flight request counter + global cap. asyncio
        # is cooperative single-threaded so int += 1 is safe here without
        # a lock. `max_concurrent_requests=None` means unlimited (FastAPI
        # default behavior).
        self._active_requests: int = 0
        self._max_concurrent_requests: int | None = max_concurrent_requests
        self._drain_timeout: float = drain_timeout
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

    def lifetime_task(self, coro_fn):
        """M15 T5 (#6-core): register a coroutine FACTORY to run for the
        lifetime of the app — a supervised background daemon (metrics flusher,
        cache warmer, queue poller, heartbeat). `coro_fn` is called with no
        args at lifespan startup; the resulting task is strong-referenced (so
        it isn't GC'd) and, on graceful drain / lifespan shutdown, CANCELLED
        and awaited bounded by the drain deadline (LANCELOT_DRAIN_SECS, else
        `drain_timeout`), suppressing CancelledError — no orphan, no
        "Task was destroyed" warning.

        Lancelot owns the bounded-cancel-on-drain plumbing; the coroutine body
        (what the daemon DOES) is the user's — positioning lock. Usable as a
        decorator or a direct call:

            @app.lifetime_task
            async def flush_metrics():
                while True:
                    await push(); await asyncio.sleep(5)

        Returns `coro_fn` unchanged so it composes as a decorator."""
        if not callable(coro_fn):
            raise TypeError(
                "app.lifetime_task expects a zero-arg coroutine FUNCTION "
                f"(called at startup), got {coro_fn!r}")
        self._lifetime_task_factories.append(coro_fn)
        return coro_fn

    def _start_lifetime_tasks(self) -> None:
        """Start one asyncio task per registered factory (called from the
        lifespan-startup seam). Idempotent: re-entry won't double-start while
        tasks are already live."""
        import asyncio

        if self._lifetime_tasks:
            return
        for fn in self._lifetime_task_factories:
            self._lifetime_tasks.append(asyncio.ensure_future(fn()))

    async def _drain_lifetime_tasks(self, timeout: float | None = None) -> None:
        """Cancel every running lifetime task and await it, bounded by the
        drain deadline so a daemon ignoring CancelledError can't wedge
        shutdown. CancelledError is suppressed (expected); any other exception
        from the daemon's teardown is swallowed (we're already shutting down).
        Mirrors the graceful-teardown discipline in `serve()`."""
        import asyncio

        tasks = [t for t in self._lifetime_tasks if not t.done()]
        # Also surface (and discard) results of already-finished tasks so a
        # crashed daemon doesn't leave an un-retrieved exception warning.
        for t in self._lifetime_tasks:
            if t.done() and not t.cancelled():
                try:
                    t.exception()
                except Exception:  # noqa: BLE001
                    pass
        self._lifetime_tasks = []
        if not tasks:
            return
        for t in tasks:
            t.cancel()
        deadline = (timeout if timeout is not None
                    else _resolve_drain_secs(self._drain_timeout))
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=max(deadline, 0.0))
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            # Bound exceeded (a daemon swallowing CancelledError) — proceed;
            # the loop teardown / process exit reclaims it. Don't wedge.
            pass

    def run(self, host: str | None = None, port: int | None = None) -> None:
        """Serve on the native Rust server. host/port default from env
        (LANCELOT_HOST, PORT/LANCELOT_PORT) so `CMD ["python","app.py"]` with
        a trailing `app.run()` deploys cleanly on any PaaS — set
        LANCELOT_HOST=0.0.0.0 in the container. For the FastAPI-parity path
        (works on every ASGI platform) run `uvicorn yourmod:app` instead."""
        run(self, host, port)

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

        # /livez: cheapest possible "process alive" probe — must NEVER touch
        # app state or DB. Returns 200 if the event loop can answer.
        # /readyz: "ready for traffic" — 503 once graceful shutdown has been
        # signalled; 200 otherwise. Both are k8s table-stakes and FastAPI
        # makes you write them by hand (research/core 02_STRUCTURE 200-230).
        def _livez():
            return JSONResponse(b'{"status":"ok"}')

        def _readyz():
            if self._shutting_down:
                from lancelot.responses import Response as _R

                r = _R(b'{"status":"draining"}', status_code=503,
                       media_type="application/json")
                r.headers["retry-after"] = "1"
                return r
            return JSONResponse(b'{"status":"ok"}')

        # M7 T2.3 (Conductor pattern): /fleet/status surfaces this worker's
        # region + worker_id + in-flight count + drain state — so an LB
        # aggregator (HAProxy / nginx Lua / Conductor's "head of
        # engineering" dashboard) can roll up N workers' state without
        # speaking per-process OTel. Cheap; just reads in-memory state.
        def _fleet_status():
            from lancelot.fleet import fleet_info
            info = fleet_info(self)
            return JSONResponse(info)

        for path, fn in (("/openapi.json", _openapi_json), ("/docs", _docs),
                         ("/redoc", _redoc), ("/scalar", _scalar),
                         ("/livez", _livez), ("/readyz", _readyz),
                         ("/fleet/status", _fleet_status)):
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
        # App-level strict_content_type fallback (route didn't override).
        if kw.get("strict_content_type") is None:
            kw["strict_content_type"] = self.strict_content_type
        # App-level dependencies are PREPENDED to the route's own deps
        # (FastAPI: outermost runs first).
        if self.dependencies:
            kw["dependencies"] = list(self.dependencies) + list(
                kw.get("dependencies", []) or [])
        # Honor a custom `app.router.route_class` (FastAPI custom-route).
        _rc = getattr(self.router, "route_class", Route)

        def deco(fn):
            self.router.add(_rc(method, path, fn, **kw))
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
        input_schema [, output_schema]} dicts — exactly the shape
        Anthropic / OpenAI / Google / MCP expect. `output_schema` is
        included ONLY when the tool function has a meaningful return-type
        annotation (Tier-C.C3) — its absence is meaningful too (means
        "the model doesn't need to know the response shape")."""
        out = []
        for s in self._tools.values():
            d = {"name": s.name, "description": s.description,
                 "input_schema": s.input_schema}
            if s.output_schema is not None:
                d["output_schema"] = s.output_schema
            out.append(d)
        return out

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

    def call_tool(self, name, /, **kwargs):
        """Invoke a registered tool by name. Inputs are Pydantic-validated
        (same machinery as HTTP request validation).

        `name` is positional-only (PEP 570) so tools whose parameter
        names include `name` don't collide with this method's signature
        — required for MCP tools/call which passes `arguments` as
        **kwargs (M7 T1.4: the greet(name='...') tool case)."""
        spec = self._tools[name]
        validated = spec._validator(**kwargs).model_dump()
        return spec.func(**validated)

    def batch(self, path: str, *, batch_window_ms: int = 50,
              max_batch: int = 16, max_keys: int = 1024,
              key=None, **route_kwargs):
        """Tier-C.C5 (marquee): prefix-aware request grouping for AI
        workloads. The HTTP route accepts ONE payload per request; the
        wrapped handler receives a `list[Payload]` containing every
        request that arrived during `batch_window_ms` (or up to
        `max_batch`, whichever fires first). The framework fans the
        handler's `list[Result]` back to each waiter by index.

        Surface:
            @app.batch("/chat", batch_window_ms=50, max_batch=10,
                       key=lambda item: item.system_prompt)
            async def chat(items: list[ChatRequest]) -> list[ChatResponse]:
                # ONE upstream call covering every item in `items`
                return await call_upstream(items)

        Failure semantics: a handler exception fans out to every waiter
        in that batch (no half-success); a wrong-length return list
        raises RuntimeError on every waiter (contract failure).

        AI logic stays user-side (cardinal rule). We just batch + fanout."""
        import inspect as _inspect
        import typing as _typing
        from lancelot.batch import _BatchRegistry

        window_s = batch_window_ms / 1000.0

        def deco(fn):
            # The user handler is `(items: list[T]) -> list[U]`. The HTTP
            # route accepts ONE `T`. Extract T from `list[T]` so the
            # synthesized route handler has the right signature for
            # pydantic validation.
            sig = _inspect.signature(fn)
            params = list(sig.parameters.values())
            if len(params) != 1:
                raise TypeError(
                    f"@app.batch handler must take exactly one "
                    f"(items: list[T]) parameter; got {len(params)}"
                )
            hints = _typing.get_type_hints(fn)
            list_ann = hints.get(params[0].name)
            if list_ann is None or _typing.get_origin(list_ann) not in (
                    list, _typing.List):  # noqa: UP006
                raise TypeError(
                    f"@app.batch handler's parameter must be `list[T]`, "
                    f"got {list_ann!r}"
                )
            item_T = _typing.get_args(list_ann)[0]

            registry = _BatchRegistry(fn, window_s, max_batch, key,
                                      max_keys=max_keys)

            # Synthesize an HTTP-level handler with one-payload signature.
            # The body name `item` becomes the JSON-body field; matches
            # FastAPI's `@app.post` shape exactly.
            async def _http_handler(item):
                return await registry.submit(item)

            _http_handler.__annotations__ = {"item": item_T}
            _http_handler.__name__ = fn.__name__
            _http_handler.__doc__ = fn.__doc__
            try:
                _http_handler.__signature__ = _inspect.Signature(
                    parameters=[_inspect.Parameter(
                        "item", _inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        annotation=item_T)]
                )
            except Exception:  # noqa: BLE001
                pass

            import dataclasses as _dc

            known = {f.name for f in _dc.fields(Route)}
            kw = {k: v for k, v in route_kwargs.items() if k in known}
            self.router.add(Route("POST", path, _http_handler, **kw))
            return fn  # original is still directly callable

        return deco

    def stream(self, path: str, *, stream_interval: int = 1,
               sse_keepalive: float = 15.0, **kwargs):
        """M2-P4 + Tier-A.A5/A6: typed event-stream sugar. The handler is
        an async generator yielding `lancelot.stream.Event` items; the
        framework owns SSE framing + cancellation propagation.

        `stream_interval` (default 1, unbuffered): batch N events per SSE
        frame — useful for high-rate token streams (vLLM 0.7 uses 5). At
        =1 the wire is exactly one event per send (FastAPI parity).

        `sse_keepalive` (default 15.0 s): emit an SSE comment line on idle
        so CDNs / nginx / browsers don't time out during slow LLM streams.
        Set to None or 0 to disable (not recommended)."""
        import dataclasses as _dc

        known = {f.name for f in _dc.fields(Route)}
        kw = {k: v for k, v in kwargs.items() if k in known}
        kw["is_stream"] = True
        kw["stream_interval"] = stream_interval
        kw["sse_keepalive"] = sse_keepalive

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
    def trace(self, p, **kw): return self._add("TRACE", p, **kw)

    def on_event(self, event_type: str):
        """FastAPI 0.92-era startup/shutdown decorator. Deprecated in
        favor of lifespan handlers; we keep the decorator + emit the
        FastAPIDeprecationWarning so tests asserting the warning still
        pass. The registered fn runs at lifespan-startup / shutdown."""
        import warnings

        from lancelot.exceptions import FastAPIDeprecationWarning as _FDW

        warnings.warn(
            "on_event is deprecated, use lifespan event handlers instead.\n"
            "Read more about it in the docs: "
            "https://fastapi.tiangolo.com/advanced/events/",
            _FDW,
            stacklevel=2,
        )

        if event_type not in ("startup", "shutdown"):
            raise ValueError(
                f"on_event expects 'startup' or 'shutdown', got {event_type!r}"
            )

        def deco(fn):
            self._on_event_handlers.setdefault(event_type, []).append(fn)
            return fn

        return deco

    def include_router(self, router: APIRouter, prefix: str = "", **kwargs):
        # Accepts FastAPI's documented include_router kwargs (dependencies,
        # default_response_class, responses, tags, deprecated, ...).
        # Lancelot acts on `dependencies` (prepended) and
        # `default_response_class` (applied to child routes without own).
        prefix = prefix.rstrip("/")
        # App-level dependencies prepend to include-time `dependencies`
        # which prepend to each route's own.
        extra_deps = list(self.dependencies) + list(
            kwargs.pop("dependencies", None) or [])
        # Resolve effective default response class — include arg > router's
        # own default > app's own default.
        default_rc = kwargs.pop(
            "default_response_class",
            getattr(router, "default_response_class", None)
            or self._default_response_class,
        )
        # Resolve effective strict_content_type — include kwarg > router's
        # own > app's default.
        sct_kw = kwargs.pop("strict_content_type", None)
        effective_sct = sct_kw if sct_kw is not None else \
            (getattr(router, "strict_content_type", None)
             if getattr(router, "strict_content_type", None) is not None
             else self.strict_content_type)
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
            # Collapse to a concrete bool at App level (no more inheritance
            # to chase after this point).
            if kw.get("strict_content_type") is None:
                kw["strict_content_type"] = effective_sct
            # FastAPI parity: an empty final path (no prefix AND empty
            # route path) is invalid — raise FastAPIError at include time
            # so the bug surfaces immediately, not at first request.
            _final_path = prefix + r.path
            if _final_path == "":
                from lancelot.exceptions import FastAPIError

                raise FastAPIError(
                    "Prefix and path cannot be both empty (path operation"
                    f" {r.method} '{r.path}'). Add a prefix to the "
                    "router or change the path."
                )
            # Preserve the source route's class (custom APIRoute survives
            # include_router).
            self.router.add(type(r)(r.method, _final_path, r.handler,
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

    # --- FastAPI-parity exception handler surface -----------------------
    def add_exception_handler(self, exc_class_or_status_code,
                              handler) -> None:
        """Register a handler for an exception CLASS (FastAPI/Starlette
        shape). Integer status-code handlers (Starlette's other form) are
        accepted for surface parity but not behaviorally honored — pass a
        class. The handler receives `(request, exc)` and returns a
        Response. Most-specific class in the exception's MRO wins."""
        self.exception_handlers[exc_class_or_status_code] = handler

    def exception_handler(self, exc_class):
        """Decorator form: `@app.exception_handler(HTTPException)`."""
        def deco(fn):
            self.exception_handlers[exc_class] = fn
            return fn

        return deco

    # --- Tier-B.B1: graceful shutdown surface ----------------------------
    def initiate_shutdown(self) -> None:
        """Flip the drain flag. /readyz immediately returns 503 +
        Retry-After; new requests are refused with 503; in-flight
        requests run to completion. Call from a SIGTERM handler, a
        lifespan shutdown, or a /shutdown route."""
        self._shutting_down = True

    async def drain(self, timeout: float | None = None) -> bool:
        """Wait until all in-flight requests have completed (or `timeout`
        seconds, whichever is first), THEN cancel + await app-lifetime
        daemons (M15 T5) bounded by the same deadline. Returns True if the
        in-flight drain succeeded in time. Caller is responsible for
        `initiate_shutdown()` first."""
        import asyncio

        deadline = (timeout if timeout is not None else self._drain_timeout)
        _loop = asyncio.get_running_loop()  # async ctx → always present (no deprecation)
        end = _loop.time() + max(deadline, 0.0)
        ok = True
        while self._active_requests > 0:
            remaining = end - _loop.time()
            if remaining <= 0:
                ok = False
                break
            await asyncio.sleep(min(0.05, remaining))
        # M15 T5: stop app-owned daemons (cancel + bounded await). Use the
        # remaining budget so the whole drain stays inside `deadline`.
        remaining = max(end - _loop.time(), 0.0)
        await self._drain_lifetime_tasks(timeout=remaining)
        return ok

    def install_signal_handlers(self) -> bool:
        """Best-effort SIGTERM / SIGINT wiring on the running event loop.
        Returns True if handlers were installed. NO-OP (and returns False)
        if we're not on the main thread or on Windows where loop signal
        handling is limited — the user can still call initiate_shutdown()
        from their own handler in those cases."""
        import asyncio
        import signal

        try:
            loop = asyncio.get_running_loop()  # not get_event_loop (deprecated 3.12+)
        except RuntimeError:
            return False  # no running loop → caller wires its own handler
        try:
            loop.add_signal_handler(signal.SIGTERM, self.initiate_shutdown)
            loop.add_signal_handler(signal.SIGINT, self.initiate_shutdown)
            return True
        except (NotImplementedError, RuntimeError, ValueError):
            # NotImplementedError: loop on a non-main thread on POSIX,
            # or Windows ProactorEventLoop. RuntimeError: loop closed.
            # ValueError: signal.signal() rejecting a non-main thread.
            return False

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


def _resolve_drain_secs(fallback: float) -> float:
    """M15 T5: graceful-drain deadline for app-lifetime daemons. Honors the
    SAME `LANCELOT_DRAIN_SECS` env knob the Rust server reads (one knob, one
    meaning across the deployable paths); falls back to the app's
    `drain_timeout`. Invalid/negative → fallback."""
    import os

    raw = os.environ.get("LANCELOT_DRAIN_SECS")
    if raw is None:
        return fallback
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return fallback
    return v if v >= 0 else fallback


def _resolve_bind(host, port):
    """Resolve the bind address: explicit arg > env > default. `PORT` is the
    universal PaaS convention (Render / Railway / Heroku / Cloud Run / Fly all
    inject it); `LANCELOT_PORT` is the namespaced override. Host defaults to the
    SAFE `127.0.0.1` (loopback) — set `LANCELOT_HOST=0.0.0.0` to accept external
    traffic in a container (the deploy/Dockerfile does this). We deliberately do
    NOT read bare `$HOST`: many shells/PaaS set it to the machine hostname, which
    would bind to the wrong interface (the Robyn ROBYN_HOST-namespacing reason)."""
    import os
    if host is None:
        host = os.environ.get("LANCELOT_HOST", "127.0.0.1")
    if port is None:
        raw = (os.environ.get("LANCELOT_PORT")
               or os.environ.get("PORT") or "")
        port = int(raw) if raw.strip().isdigit() else 8000
    return host, int(port)


def run(app: "App", host: str | None = None, port: int | None = None):
    """Serve `app` on the native Rust server, binding from env when host/port
    are omitted (`LANCELOT_HOST`, `PORT`/`LANCELOT_PORT`) so it deploys cleanly
    on PaaS — set `LANCELOT_HOST=0.0.0.0` in a container. For the FastAPI-parity
    deploy path (works on every ASGI platform) use instead:
        `uvicorn yourmod:app --host 0.0.0.0 --port $PORT --workers N`."""
    host, port = _resolve_bind(host, port)
    routes = [(r.method, r.path, r.handler) for r in app.routes]
    _native.run(routes, host, port)
