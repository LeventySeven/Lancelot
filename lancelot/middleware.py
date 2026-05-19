"""Two-tier middleware (U20/U21). `app.add_middleware(cls, **opts)` is the
pure-ASGI escape hatch — an unmodified ASGI middleware wrapping the app
callable, NEVER BaseHTTPMiddleware/call_next.

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

_SAFELISTED = ["Accept", "Accept-Language", "Content-Language", "Content-Type"]


class CORSMiddleware:
    def __init__(self, app, *, allow_origins=(), allow_methods=("GET",),
                 allow_headers=(), allow_credentials=False, max_age=600):
        self.app = app
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
