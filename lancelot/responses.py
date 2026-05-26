"""Minimal response objects so a handler can return pre-serialized content
(doc HTML, the openapi JSON) without the ASGI seam re-`_to_json`-ing it."""


def _strip_ctl(s) -> str:
    """Drop CR/LF and other control chars (< 0x20 and DEL) so user-supplied
    cookie data can't split the Set-Cookie header into a second response
    header (HTTP response-splitting). Same A3 CR-safety pattern already used
    for media_type / session_affinity, generalized to all control chars."""
    return "".join(ch for ch in str(s) if ch >= " " and ch != "\x7f")


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
        list value under the `set-cookie` key (asgi seam emits each).

        Security: key/value/path/domain are stripped of control chars so a
        CRLF in user-supplied cookie data can't inject a second header
        (response-splitting). `samesite` is validated against the spec enum."""
        parts = [f"{_strip_ctl(key)}={_strip_ctl(value)}"]
        if max_age is not None:
            parts.append(f"Max-Age={int(max_age)}")
        if expires is not None:
            parts.append(f"Expires={_strip_ctl(expires)}")
        if path:
            parts.append(f"Path={_strip_ctl(path)}")
        if domain:
            parts.append(f"Domain={_strip_ctl(domain)}")
        if secure:
            parts.append("Secure")
        if httponly:
            parts.append("HttpOnly")
        if samesite:
            if str(samesite).lower() not in ("strict", "lax", "none"):
                raise ValueError(
                    f"invalid samesite {samesite!r}; "
                    "expected one of 'strict', 'lax', 'none'")
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

    def set_retry_hint(self, should_retry: bool) -> None:
        """M7 T4.9 (Turbopuffer pattern): tell the client whether THIS
        specific response is retry-safe. Server-controlled retry overrides
        the client SDK's status-code defaults. Use cases:
          - status=500 BUT should_retry=False ("permanent server error,
            don't pound us with retries on this") — back-pressure to the
            caller during a real outage instead of thundering-herd retries.
          - status=409 (conflict) WITH should_retry=True ("transient
            lock-conflict, retry after backoff").
          - status=429 (rate-limit) WITH should_retry=True paired with the
            standard Retry-After header.

        Emits `X-Should-Retry: true` or `X-Should-Retry: false`. Clients
        that don't read it fall back to the SDK's status-code defaults.
        Pairs with @app.batch: surface upstream-LLM backpressure cleanly
        to callers without thundering-herd retries on a real outage."""
        self.headers["x-should-retry"] = "true" if should_retry else "false"


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


# Starlette-name pass-throughs for vendored tests that import these by
# name. We don't ship orjson/ujson (Tier-D: pydantic-only runtime); if
# the user installs them they get the speed, but the CONTRACT — class
# exists, returns JSON — works regardless.
class ORJSONResponse(JSONResponse):
    media_type = "application/json"


class UJSONResponse(JSONResponse):
    media_type = "application/json"
