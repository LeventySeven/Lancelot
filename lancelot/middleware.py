"""Two-tier middleware (U20/U21). `app.add_middleware(cls, **opts)` wraps the
app callable with an ASGI middleware — a plain pure-ASGI middleware OR a
Starlette `BaseHTTPMiddleware` subclass (it is itself an ASGI app; T2#6a
verified it streams + propagates `scope['state']` to the handler). The
`@app.middleware("http")` decorator is adapted by `HTTPMiddlewareAdapter`
below using a pure-asyncio streaming task-group model (T2#6b) so it streams
the inner response instead of buffering it.

CORSMiddleware — byte-faithful to Starlette's CORSMiddleware (the one
FastAPI re-exports) for the covered config, verified by
tests/test_m1_p4_byte_parity.py against the real installed stack
(fastapi 0.136.1 / starlette 1.0.0): literal `*` for allow_origins=["*"]
(NOT an
Origin reflector — V1 review blocker); `Vary: Origin` for explicit-list;
preflight 400 "Disallowed CORS origin|method|headers" (incl. ACRH
validation); `sorted(SAFELISTED ∪ allow_headers)` Allow-Headers; ACAC
emitted unconditionally on the simple path (Starlette parity). Wire
header ORDER also matches Starlette (init_headers: content-length then
content-type LAST; ACAC before ACAO/Vary) — gated by the ordered
raw-ASGI differential test_wire_header_ORDER_matches_fastapi.
Genuinely-unimplemented tail (ledgered, conformance/vendor/quarantine/
test_p4_longtail.py): allow_origin_regex / wildcard-subdomain origins,
`expose_headers`, `access-control-request-private-network`."""

class Middleware:
    """FastAPI/Starlette config-shape Middleware spec — a deferred
    constructor. `Middleware(SomeMW, **opts)` is unpacked at app-build
    time to `SomeMW(app, **opts)`. Iteration yields (cls, args, kwargs)
    for parity with Starlette's API."""

    def __init__(self, cls, *args, **kwargs):
        self.cls = cls
        self.args = args
        self.kwargs = kwargs

    def __iter__(self):
        return iter((self.cls, self.args, self.kwargs))


class HTTPMiddlewareAdapter:
    """Adapter for `@app.middleware("http")` user fns (Starlette's
    `async def mw(request, call_next)` shape). T2#6b: a pure-asyncio STREAMING
    task-group model (no anyio dependency — cardinal rule #3 keeps the runtime
    pydantic-only). The inner app runs as a task whose ASGI messages flow
    through a queue; `call_next` returns a `StreamingResponse` the moment the
    response START arrives (carrying the inner status + headers, content-length
    dropped), and the body STREAMS through chunk-by-chunk instead of being
    buffered. This fixes the old sync-capture bugs: a corrupted DUPLICATED
    content-type (`text/event-stream, text/plain`) and full-body buffering of
    streaming endpoints. The inner task is started lazily inside `call_next`, so
    a middleware that short-circuits (never calls call_next) does NOT run the
    inner handler. Lifespan/websocket scopes pass through untouched."""

    def __init__(self, app, fn):
        self.app = app
        self.fn = fn

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        import asyncio
        import urllib.parse as _up

        from lancelot.requests import Request
        from lancelot.responses import StreamingResponse

        # Inspection Request from the scope (body empty — the inner app gets the
        # real receive(); this is for header/method/path inspection only).
        hdrs = {k.decode("latin-1").lower(): v.decode("latin-1")
                for k, v in scope.get("headers", [])}
        qp = dict(_up.parse_qsl(scope.get("query_string", b"").decode()))
        req = Request(
            method=scope.get("method", "GET"),
            path=scope.get("path", "/"),
            query_params=qp,
            headers=hdrs,
            cookies={},
            path_params={},
            body=b"",
            app=None,
        )

        queue: asyncio.Queue = asyncio.Queue()
        state = {"task": None, "exc": None}

        async def _inner_send(message):
            await queue.put(message)

        async def _run_inner():
            try:
                await self.app(scope, receive, _inner_send)
            except Exception as e:  # noqa: BLE001 - surfaced via call_next
                state["exc"] = e
            finally:
                await queue.put(None)  # sentinel: inner finished

        async def call_next(_request):
            # Lazily start the inner app on first call_next so a short-circuit
            # (no call_next) never runs the inner handler (old semantics).
            if state["task"] is None:
                state["task"] = asyncio.ensure_future(_run_inner())
            message = await queue.get()
            if message is None:  # inner ended before emitting a response
                if state["exc"] is not None:
                    raise state["exc"]
                raise RuntimeError("inner app returned no response")
            # message is http.response.start
            status = message.get("status", 200)
            resp_headers = {}
            for k, v in message.get("headers", []):
                resp_headers[k.decode("latin-1").lower()] = v.decode("latin-1")
            resp_headers.pop("content-length", None)  # re-streamed; len unknown

            async def _body():
                while True:
                    m = await queue.get()
                    if m is None:
                        if state["exc"] is not None:
                            raise state["exc"]
                        return
                    if m.get("type") == "http.response.body":
                        chunk = m.get("body", b"")
                        if chunk:
                            yield chunk
                        if not m.get("more_body", False):
                            return

            resp = StreamingResponse(_body(), status_code=status)
            resp.headers = resp_headers  # carries the inner content-type
            return resp

        try:
            response = await self.fn(req, call_next)
            await _emit_middleware_response(response, send)
        finally:
            # Let the inner app finish (teardown) if it was started; if the user
            # short-circuited without call_next, there is no task to await.
            t = state["task"]
            if t is not None and not t.done():
                try:
                    await t
                except Exception:  # noqa: BLE001
                    pass
        # Transparency to inner exceptions (Starlette BaseHTTPMiddleware app_exc
        # parity): the inner app may SEND a 500 response AND re-raise (the
        # ServerErrorMiddleware pattern) so the ASGI server logs / the test
        # client re-raises. If the inner task raised after fully emitting its
        # response (so `_body` returned on more_body=False without surfacing it),
        # propagate it now. If it raised BEFORE sending a start, call_next already
        # re-raised it above and we never reach here.
        if state["exc"] is not None:
            raise state["exc"]


async def _emit_middleware_response(response, send):
    """Serialize a Response / StreamingResponse returned from an
    `@app.middleware("http")` fn to ASGI send() events, WITHOUT duplicating
    content-type/content-length (the old adapter's bug). A streaming response
    (has `body_iterator`) is emitted chunked; anything else is one-shot."""
    out_hdrs = []
    have_ct = have_cl = False
    for k, v in (getattr(response, "headers", None) or {}).items():
        kb = str(k).lower().encode("latin-1")
        if kb == b"content-type":
            have_ct = True
        elif kb == b"content-length":
            have_cl = True
        if isinstance(v, list):
            for vv in v:
                out_hdrs.append((kb, str(vv).encode("latin-1")))
        else:
            out_hdrs.append((kb, str(v).encode("latin-1")))
    media_type = getattr(response, "media_type", None) or "text/plain"
    status = getattr(response, "status_code", 200)
    iterator = getattr(response, "body_iterator", None)
    if iterator is not None:
        if not have_ct:
            out_hdrs.append((b"content-type", media_type.encode("latin-1")))
        await send({"type": "http.response.start",
                    "status": status, "headers": out_hdrs})
        import inspect as _isp
        if _isp.isasyncgen(iterator) or hasattr(iterator, "__aiter__"):
            async for chunk in iterator:
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8")
                await send({"type": "http.response.body",
                            "body": chunk, "more_body": True})
        else:
            for chunk in iterator:
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8")
                await send({"type": "http.response.body",
                            "body": chunk, "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})
        return
    body = getattr(response, "body", b"")
    body = body if isinstance(body, (bytes, bytearray)) else str(body).encode()
    if not have_cl:
        out_hdrs.append((b"content-length", str(len(body)).encode()))
    if not have_ct:
        out_hdrs.append((b"content-type", media_type.encode("latin-1")))
    await send({"type": "http.response.start",
                "status": status, "headers": out_hdrs})
    await send({"type": "http.response.body", "body": body})


_SAFELISTED = ["Accept", "Accept-Language", "Content-Language", "Content-Type"]


class CORSMiddleware:
    def __init__(self, app, *, allow_origins=(), allow_methods=("GET",),
                 allow_headers=(), allow_credentials=False, max_age=600):
        self.app = app
        # SEC: wildcard origin + credentials reflects ANY Origin back with
        # Access-Control-Allow-Credentials: true → cross-origin credentialed
        # access (SOP bypass). Fail loud at construction (Starlette parity)
        # rather than silently serving the dangerous config.
        if "*" in allow_origins and allow_credentials:
            raise ValueError(
                "CORSMiddleware: allow_credentials=True cannot be combined "
                "with allow_origins=['*'] (it reflects every Origin with "
                "credentials — a same-origin-policy bypass). List explicit "
                "allowed origins instead.")
        self.allow_origins = list(allow_origins)
        self.allow_all_origins = "*" in self.allow_origins
        self.allow_methods = list(allow_methods)
        if "*" in self.allow_methods:  # F1: Starlette expands to ALL_METHODS
            self.allow_methods = ["DELETE", "GET", "HEAD", "OPTIONS",
                                  "PATCH", "POST", "PUT"]
        self.allow_headers = list(allow_headers)
        self.allow_credentials = allow_credentials
        self.max_age = max_age
        self.allow_all_headers = "*" in self.allow_headers
        # Starlette: sorted(SAFELISTED ∪ allow_headers), case-sensitive.
        self._preflight_allow_headers = sorted(
            set(_SAFELISTED) | {h for h in self.allow_headers if h != "*"}
        )
        self._allow_headers_lc = {h.lower() for h in self._preflight_allow_headers}

    def _is_allowed(self, origin):
        return self.allow_all_origins or origin in self.allow_origins

    def _acao(self, origin):
        # Starlette: literal "*" when wildcard AND no credentials; else the
        # (allowed) origin, which makes the response Origin-dependent → Vary.
        if self.allow_all_origins and not self.allow_credentials:
            return "*", False
        return origin, True  # (value, needs Vary: Origin)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        h = {}
        for k, v in scope.get("headers", []):  # first-wins (no smuggling)
            h.setdefault(k.decode().lower(), v.decode())
        origin = h.get("origin")
        if origin is None:
            await self.app(scope, receive, send)
            return

        allowed = self._is_allowed(origin)

        if scope["method"] == "OPTIONS" and "access-control-request-method" in h:
            failures = []
            if not allowed:
                failures.append("origin")
            acrm = h.get("access-control-request-method")
            if "*" not in self.allow_methods and acrm not in self.allow_methods:
                failures.append("method")
            acrh = h.get("access-control-request-headers")
            if acrh and not self.allow_all_headers:
                req = [x.strip().lower() for x in acrh.split(",") if x.strip()]
                if any(r not in self._allow_headers_lc for r in req):
                    failures.append("headers")
            # Starlette PlainTextResponse wire order (present on BOTH 200 and
            # 400): vary, ACAM, max-age, [ACAH], [ACAC], [ACAO], then
            # content-length, then content-type LAST (header order is part
            # of the wire bytes — init_headers appends ctype/clen last).
            hdrs = [
                (b"vary", b"Origin"),
                (b"access-control-allow-methods",
                 ", ".join(self.allow_methods).encode()),
                (b"access-control-max-age", str(self.max_age).encode()),
            ]
            if self.allow_all_headers:
                # F2: Starlette echoes ACRH verbatim and emits NOTHING when
                # there is no ACRH (does NOT fall back to the safelist).
                if acrh:
                    hdrs.append((b"access-control-allow-headers", acrh.encode()))
            elif self._preflight_allow_headers:
                hdrs.append((b"access-control-allow-headers",
                             ", ".join(self._preflight_allow_headers).encode()))
            if self.allow_credentials:
                hdrs.append((b"access-control-allow-credentials", b"true"))
            if allowed:  # ACAO only when the origin is allowed
                acao, _ = self._acao(origin)
                hdrs.append((b"access-control-allow-origin", acao.encode()))
            body = (("Disallowed CORS " + ", ".join(failures)).encode()
                    if failures else b"OK")
            # Starlette PlainTextResponse.init_headers: content-length then
            # content-type appended LAST.
            hdrs.append((b"content-length", str(len(body)).encode()))
            hdrs.append((b"content-type", b"text/plain; charset=utf-8"))
            await send({"type": "http.response.start",
                        "status": 400 if failures else 200, "headers": hdrs})
            await send({"type": "http.response.body", "body": body})
            return

        acao, vary = self._acao(origin)

        async def send2(msg):
            if msg["type"] == "http.response.start":
                msg = dict(msg)
                hh = list(msg.get("headers", []))
                # Starlette simple_response wire order: headers.update(
                # simple_headers) puts ACAC FIRST (unconditional — inert
                # without ACAO), THEN the explicit-origin branch appends
                # ACAO and the Vary header.
                if self.allow_credentials:
                    hh.append((b"access-control-allow-credentials", b"true"))
                if allowed:
                    hh.append((b"access-control-allow-origin", acao.encode()))
                    if vary:
                        hh.append((b"vary", b"Origin"))
                msg["headers"] = hh
            await send(msg)

        await self.app(scope, receive, send2)
