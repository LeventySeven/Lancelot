"""Minimal response objects so a handler can return pre-serialized content
(doc HTML, the openapi JSON) without the ASGI seam re-`_to_json`-ing it."""


class Response:
    media_type = "text/plain"

    def __init__(self, content=b"", status_code=200, media_type=None):
        self.status_code = status_code
        if media_type:
            # A3: never let CRLF in a (potentially user-set) media_type
            # inject response headers at the ASGI seam.
            self.media_type = media_type.replace("\r", "").replace("\n", "")
        self.body = (
            content if isinstance(content, (bytes, bytearray))
            else str(content).encode()
        )
        # Multi-value headers for repeats like Set-Cookie. Stored as a
        # plain dict for the simple case; set_cookie appends as a list.
        if not hasattr(self, "headers"):
            self.headers = {}

    def set_cookie(self, key: str, value: str = "", max_age: int | None = None,
                   expires=None, path: str = "/", domain: str | None = None,
                   secure: bool = False, httponly: bool = False,
                   samesite: str | None = "lax"):
        """Build a Set-Cookie header. Multiple cookies accumulate via a
        list value under the `set-cookie` key (asgi seam emits each)."""
        parts = [f"{key}={value}"]
        if max_age is not None:
            parts.append(f"Max-Age={max_age}")
        if expires is not None:
            parts.append(f"Expires={expires}")
        if path:
            parts.append(f"Path={path}")
        if domain:
            parts.append(f"Domain={domain}")
        if secure:
            parts.append("Secure")
        if httponly:
            parts.append("HttpOnly")
        if samesite:
            parts.append(f"SameSite={samesite}")
        header_value = "; ".join(parts)
        existing = self.headers.get("set-cookie")
        if existing is None:
            self.headers["set-cookie"] = header_value
        elif isinstance(existing, list):
            existing.append(header_value)
        else:
            self.headers["set-cookie"] = [existing, header_value]

    def delete_cookie(self, key: str, path: str = "/",
                      domain: str | None = None):
        self.set_cookie(key, value="", max_age=0, expires=0, path=path,
                        domain=domain)

    def set_session_affinity(self, key: str) -> None:
        """Tier-C.C1: hint the load-balancer to route the NEXT request from
        this client to the same backend instance. Emitted as
        `X-Session-Affinity: <key>` so the LB (or a sticky-session reverse
        proxy) can hash on it. Lancelot itself does NOT enforce affinity —
        that's the LB's job; we just expose the hint. Useful for keeping
        an LLM conversation on the same GPU so the KV prefix cache hits."""
        # A3 (Lancelot existing CR-safety pattern): strip CR/LF so a
        # user-provided key can't smuggle a header injection.
        clean = str(key).replace("\r", "").replace("\n", "")
        self.headers["x-session-affinity"] = clean


class HTMLResponse(Response):
    media_type = "text/html; charset=utf-8"


class JSONResponse(Response):
    media_type = "application/json"

    def __init__(self, content=None, status_code=200, media_type=None,
                 headers=None):
        # Accept pre-serialized bytes (existing M0/M1 callers pass
        # `_wire_json(...)` bytes) OR a Python object (FastAPI's
        # JSONResponse(content={...}) shape — serialize here).
        if isinstance(content, (bytes, bytearray)):
            body = bytes(content)
        elif content is None:
            body = b"null"
        else:
            import json as _json

            body = _json.dumps(content, separators=(",", ":")).encode()
        # Honor a SUBCLASS's class-attr `media_type` (FastAPI parity for
        # `class ORJSONResponse(JSONResponse): media_type = "..."`).
        # Resolution: explicit kwarg > subclass class attr > our default.
        super().__init__(body, status_code,
                         media_type or type(self).media_type)
        self.headers = headers or {}


class PlainTextResponse(Response):
    media_type = "text/plain; charset=utf-8"


class StreamingResponse(Response):
    """Minimal SSE/streaming response — accepts an async iterable + a
    media_type. The asgi seam streams chunks via http.response.body
    `more_body=True` until the iterator is exhausted."""

    media_type = "text/plain"

    def __init__(self, content, status_code=200, headers=None,
                 media_type=None, background=None):
        self.status_code = status_code
        self.media_type = (media_type or self.media_type).replace(
            "\r", "").replace("\n", "")
        self.body_iterator = content  # async or sync iterable of chunks
        self.headers = headers or {}
        self.background = background
        self.body = b""  # filled on send
