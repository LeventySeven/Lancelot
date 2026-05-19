"""Two-tier middleware (U20/U21). `app.add_middleware(cls, **opts)` is the
pure-ASGI escape hatch — an unmodified ASGI middleware wrapping the app
callable, NEVER BaseHTTPMiddleware/call_next. CORSMiddleware (U21) lands
in M1-P4 T8."""

class CORSMiddleware:
    """Pure-ASGI CORS — byte-faithful to Starlette's CORSMiddleware for the
    covered config (the M1-P4 byte-differential oracle calibrates; full
    preflight header-set parity / regex origins → P7 quarantine)."""

    def __init__(self, app, *, allow_origins=(), allow_methods=("GET",),
                 allow_headers=(), allow_credentials=False, max_age=600):
        self.app = app
        self.allow_origins = set(allow_origins)
        self.allow_all_origins = "*" in self.allow_origins
        self.allow_methods = list(allow_methods)
        self.allow_headers = list(allow_headers)
        self.allow_credentials = allow_credentials
        self.max_age = max_age

    def _allowed(self, origin):
        return self.allow_all_origins or origin in self.allow_origins

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode()
                   for k, v in scope.get("headers", [])}
        origin = headers.get("origin")
        if origin is None:
            await self.app(scope, receive, send)
            return
        if scope["method"] == "OPTIONS" and "access-control-request-method" in headers:
            h = [(b"content-type", b"text/plain; charset=utf-8")]
            if self._allowed(origin):
                h += [
                    (b"access-control-allow-origin", origin.encode()),
                    (b"access-control-allow-methods",
                     ", ".join(self.allow_methods).encode()),
                    (b"access-control-allow-headers",
                     ", ".join(self.allow_headers).encode()),
                    (b"access-control-max-age", str(self.max_age).encode()),
                ]
                if self.allow_credentials:
                    h.append((b"access-control-allow-credentials", b"true"))
            await send({"type": "http.response.start", "status": 200,
                        "headers": h})
            await send({"type": "http.response.body", "body": b"OK"})
            return

        async def send2(msg):
            if msg["type"] == "http.response.start" and self._allowed(origin):
                msg = dict(msg)
                hh = list(msg.get("headers", []))
                hh.append((b"access-control-allow-origin", origin.encode()))
                if self.allow_credentials:
                    hh.append((b"access-control-allow-credentials", b"true"))
                msg["headers"] = hh
            await send(msg)

        await self.app(scope, receive, send2)
