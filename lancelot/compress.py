"""M18 R1 — GIL-releasing gzip response compression (`GZipMiddleware`).

An OPT-IN, pure-ASGI middleware (NEVER `BaseHTTPMiddleware` — cardinal rule)
that gzip-compresses **one-shot buffered** response bodies, with the actual
zlib pass running in Rust UNDER A RELEASED GIL on a thread-pool thread. Wired
via the existing `add_middleware` escape hatch, matching the M17 house style
(`ratelimit.py`, `auth.py`)::

    app = lancelot.App()
    app.add_middleware(GZipMiddleware, minimum_size=1000, compresslevel=6)

Default behavior is byte-identical when the middleware is NOT added.

================================================================================
WHERE THIS HELPS — read before you rely on it (the honest, narrow envelope)
================================================================================
This is the **ASGI seam only**. Lancelot's native Rust server (`App.run()` →
`_native.run`, the marquee ~183k const-tier / large-payload path) emits a
finished body with no post-serialization send/negotiation seam — it **cannot**
host this middleware at all. R1 helps only the uvicorn-hosted `lancelot.App`
ASGI path, and only when you are NOT already behind a compressing proxy
(nginx / Cloudflare / a TLS terminator all compress for you — the mandated
multi-process deploy almost always has one). The win it targets is purely a
single-process **concurrency** effect: Starlette's `GZipMiddleware` runs zlib
holding the GIL on the loop thread, stalling every other coroutine for the
duration; this version releases the GIL across the encode so peers keep making
progress. Behind multiple uvicorn workers (separate GILs) the effect shrinks
or vanishes. NEVER describe this as a blanket "faster compression".
================================================================================

Scope discipline (forced by the no-bullshit + Tier-D anti-patterns):
  * ONE-SHOT buffered bodies only. A streaming response (`more_body=True`)
    passes through IDENTITY — no per-chunk gzip. Per-chunk compression is the
    documented Tier-D anti-pattern and would wreck TTFT (the AI-streaming UX
    metric). The Rust codec is therefore stateless (one buffer in, one out).
  * Exclusions mirror Starlette's `gzip.py` exactly: require `gzip` in
    Accept-Encoding; skip if `content-encoding` is already set; skip excluded
    content-types (default `text/event-stream`); skip bodies < `minimum_size`.

There is no `App(compress=)` constructor kwarg, deliberately: a constructor
flag would imply the native `app.run()` server compresses, which it provably
cannot. Use `add_middleware` so the wiring states plainly that this is the
ASGI path.
"""
import asyncio
from typing import Iterable, Optional, Tuple

__all__ = ["GZipMiddleware"]

DEFAULT_EXCLUDED_CONTENT_TYPES: Tuple[str, ...] = ("text/event-stream",)


def _header_get(headers, name: bytes) -> Optional[bytes]:
    """First value for a header name (raw ASGI header list), case-insensitive."""
    low = name.lower()
    for k, v in headers:
        if k.lower() == low:
            return v
    return None


class GZipMiddleware:
    """Pure-ASGI gzip middleware (opt-in) with an off-loop, GIL-releasing encode.

    Args:
        app: the inner ASGI app (set by ``add_middleware``).
        minimum_size: bodies smaller than this (bytes) pass through
            uncompressed (gzip framing + CPU aren't worth it on tiny payloads).
            Default 1000.
        compresslevel: libdeflate level 1-12. Default 6 (libdeflate's level 6
            already beats zlib level 9 on ratio for most JSON while staying
            fast); the bench compares at a matched level 9 for fairness.
        exclude_content_types: content-type prefixes to never compress.
            Defaults to ``("text/event-stream",)`` (SSE).
    """

    def __init__(self, app, *,
                 minimum_size: int = 1000,
                 compresslevel: int = 6,
                 exclude_content_types: Iterable[str] = DEFAULT_EXCLUDED_CONTENT_TYPES):
        self.app = app
        self.minimum_size = int(minimum_size)
        self.compresslevel = int(compresslevel)
        # Tuple for str.startswith (accepts a tuple of prefixes).
        self.excluded = tuple(exclude_content_types)

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        # Only engage the responder when the client advertised gzip; otherwise
        # this is a pure passthrough (mirrors Starlette's negotiation at the
        # middleware boundary — no per-message branching on the hot path).
        accept = b""
        for k, v in scope.get("headers", ()):
            if k.lower() == b"accept-encoding":
                accept = v
                break
        if b"gzip" not in accept.lower():
            await self.app(scope, receive, send)
            return
        responder = _GZipResponder(
            self.app, self.minimum_size, self.compresslevel, self.excluded)
        await responder(scope, receive, send)


class _GZipResponder:
    """Buffers the start + first body message, then decides:

      * streaming (first body has ``more_body=True``)  -> flush identity,
        pass every remaining chunk through verbatim (no compression);
      * already content-encoded / excluded CT / body < minimum_size
                                                       -> flush identity;
      * otherwise (one-shot, compressible)             -> gzip the body
        OFF-LOOP via ``run_in_executor`` (GIL released in Rust), rewrite
        content-encoding / vary / content-length, then send.
    """

    __slots__ = ("app", "minimum_size", "compresslevel", "excluded",
                 "send", "_start", "_skip", "_started")

    def __init__(self, app, minimum_size, compresslevel, excluded):
        self.app = app
        self.minimum_size = minimum_size
        self.compresslevel = compresslevel
        self.excluded = excluded
        self.send = None
        self._start = None        # buffered http.response.start message
        self._skip = False        # decided to pass through identity
        self._started = False     # have we forwarded the start message yet

    async def __call__(self, scope, receive, send):
        self.send = send
        await self.app(scope, receive, self._send)

    async def _flush_start(self):
        if not self._started:
            self._started = True
            await self.send(self._start)

    async def _send(self, message):
        mtype = message["type"]

        if mtype == "http.response.start":
            # Hold the start until we've inspected the body (we may need to add
            # content-encoding / vary / rewrite content-length).
            self._start = message
            headers = message.get("headers", [])
            ct = _header_get(headers, b"content-type") or b""
            ce_set = _header_get(headers, b"content-encoding") is not None
            ct_excluded = ct.decode("latin1").startswith(self.excluded)
            # Decide skip-vs-maybe-compress at start time for the cheap cases;
            # the size check still happens once we see the (first) body.
            self._skip = ce_set or ct_excluded
            return

        if mtype != "http.response.body":
            # pathsend or any non-body message: flush start, forward as-is.
            await self._flush_start()
            await self.send(message)
            return

        # ---- http.response.body ----
        body = message.get("body", b"")
        more_body = message.get("more_body", False)

        # Already decided to pass through (already-encoded / excluded CT), OR a
        # streaming response (more_body=True on the first chunk → no
        # stream-compress; Tier-D ban). Forward identity from here on.
        if self._skip or more_body:
            self._skip = True
            await self._flush_start()
            await self.send(message)
            return

        # One-shot body, not yet started. Small bodies pass through.
        if len(body) < self.minimum_size:
            await self._flush_start()
            await self.send(message)
            return

        # Compressible one-shot body — THE CRUX: the encode runs OFF-LOOP so the
        # GIL the Rust codec releases is actually picked up by another thread,
        # freeing the single uvloop event loop to serve concurrent requests.
        # Calling _native.gzip_encode INLINE here would release the GIL to
        # nobody (every other coroutine is blocked at the same await) → zero
        # concurrency gain. run_in_executor is mandatory and load-bearing.
        from . import _native  # lazy: keep `import lancelot.compress` cheap
        loop = asyncio.get_event_loop()
        compressed = await loop.run_in_executor(
            None, _native.gzip_encode, body, self.compresslevel)
        compressed = bytes(compressed)

        # Rewrite headers: drop the old content-length, set the new one +
        # content-encoding, add Vary: Accept-Encoding (merge, don't clobber).
        new_headers = []
        vary_seen = False
        for k, v in self._start.get("headers", []):
            lk = k.lower()
            if lk == b"content-length":
                continue  # recomputed below
            if lk == b"vary":
                vary_seen = True
                if b"accept-encoding" not in v.lower():
                    v = v + b", Accept-Encoding"
            new_headers.append((k, v))
        if not vary_seen:
            new_headers.append((b"vary", b"Accept-Encoding"))
        new_headers.append((b"content-encoding", b"gzip"))
        new_headers.append((b"content-length", str(len(compressed)).encode()))
        self._start = {**self._start, "headers": new_headers}

        await self._flush_start()
        out = {**message, "body": compressed}
        out.pop("more_body", None)
        await self.send(out)
