"""M18 R1 — GIL-releasing gzip middleware (opt-in, ASGI seam only).

These tests cover the Python ASGI seam (`lancelot.compress.GZipMiddleware`)
and the Rust codec (`lancelot._native.gzip_encode`). They prove:
  * byte-valid gzip output (round-trips to the original);
  * Starlette's exclusions are honored (small body / SSE / already-encoded
    pass through identity);
  * content-encoding + vary headers + recomputed content-length on compress;
  * streaming (`more_body=True`) passes through UNCOMPRESSED (the Tier-D
    per-chunk ban — no stream compression, preserves TTFT).

The middleware is driven directly via the ASGI 3-callable protocol (through
`asyncio.run`, matching the repo's no-pytest-asyncio convention) so the test
needs no HTTP server — the bench (`benchmarks/compress_bench.sh`) covers the
real-server / concurrency path.
"""
import asyncio
import gzip

import pytest

from lancelot.compress import GZipMiddleware


# ---- the Rust codec directly --------------------------------------------------
def test_native_gzip_encode_roundtrips():
    import lancelot._native as _native

    payload = (b'{"items":[{"id":1,"name":"alpha","val":3.14},'
               b'{"id":2,"name":"beta","val":6.28}]}') * 64
    out = _native.gzip_encode(payload, 6)
    assert isinstance(out, (bytes, bytearray))
    assert gzip.decompress(bytes(out)) == payload
    # gzip magic bytes.
    assert bytes(out)[:2] == b"\x1f\x8b"


def test_native_gzip_encode_level_9_roundtrips():
    import lancelot._native as _native

    payload = bytes(range(256)) * 200  # 51200 B, low redundancy
    out = _native.gzip_encode(payload, 9)
    assert gzip.decompress(bytes(out)) == payload


def test_native_gzip_encode_empty():
    import lancelot._native as _native

    out = _native.gzip_encode(b"", 6)
    assert gzip.decompress(bytes(out)) == b""


def test_native_gzip_encode_invalid_level_raises():
    import lancelot._native as _native

    # libdeflate's valid level range is 0-12 (0 = stored / no compression).
    # Out-of-range levels must surface as a ValueError, not a panic/UB.
    with pytest.raises(ValueError):
        _native.gzip_encode(b"abc", 13)
    with pytest.raises(ValueError):
        _native.gzip_encode(b"abc", 255)


# ---- ASGI harness -------------------------------------------------------------
def _run(mw, scope, body_messages):
    """Run an ASGI app/middleware once, collecting the outgoing messages."""
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    # Build an inner app that emits the supplied body_messages.
    async def app(scope, receive, send):
        for m in body_messages:
            await send(m)

    async def go():
        wrapped = mw(app)
        await wrapped(scope, receive, send)

    asyncio.run(go())
    return sent


def _http_scope(accept_encoding="gzip"):
    headers = []
    if accept_encoding is not None:
        headers.append((b"accept-encoding", accept_encoding.encode()))
    return {"type": "http", "headers": headers}


def _headers_dict(message):
    return {k.decode("latin1").lower(): v.decode("latin1")
            for k, v in message.get("headers", [])}


def test_large_body_compressed_with_headers():
    body = (b'{"k":"' + b"x" * 50 + b'-%d"}' % 0) * 200  # ~12 KB
    start = {"type": "http.response.start", "status": 200,
             "headers": [(b"content-type", b"application/json"),
                         (b"content-length", str(len(body)).encode())]}
    body_msg = {"type": "http.response.body", "body": body, "more_body": False}

    sent = _run(
        lambda app: GZipMiddleware(app, minimum_size=500),
        _http_scope("gzip"), [start, body_msg])

    start_out = next(m for m in sent if m["type"] == "http.response.start")
    body_out = next(m for m in sent if m["type"] == "http.response.body")
    hdrs = _headers_dict(start_out)
    assert hdrs.get("content-encoding") == "gzip"
    assert "accept-encoding" in hdrs.get("vary", "").lower()
    # content-length recomputed to the compressed size.
    assert hdrs["content-length"] == str(len(body_out["body"]))
    assert len(body_out["body"]) < len(body)
    assert gzip.decompress(body_out["body"]) == body


def test_compressed_no_content_length_in_source_still_sets_it():
    # An inner app that doesn't pre-set content-length (chunked-style one-shot)
    # must still get a correct compressed content-length.
    body = b"z" * 4000
    start = {"type": "http.response.start", "status": 200,
             "headers": [(b"content-type", b"text/plain")]}
    body_msg = {"type": "http.response.body", "body": body, "more_body": False}

    sent = _run(
        lambda app: GZipMiddleware(app, minimum_size=500),
        _http_scope("gzip"), [start, body_msg])

    start_out = next(m for m in sent if m["type"] == "http.response.start")
    body_out = next(m for m in sent if m["type"] == "http.response.body")
    hdrs = _headers_dict(start_out)
    assert hdrs.get("content-encoding") == "gzip"
    assert hdrs["content-length"] == str(len(body_out["body"]))
    assert gzip.decompress(body_out["body"]) == body


def test_existing_vary_header_merged_not_clobbered():
    body = b"q" * 4000
    start = {"type": "http.response.start", "status": 200,
             "headers": [(b"content-type", b"application/json"),
                         (b"vary", b"Origin")]}
    body_msg = {"type": "http.response.body", "body": body, "more_body": False}

    sent = _run(
        lambda app: GZipMiddleware(app, minimum_size=500),
        _http_scope("gzip"), [start, body_msg])

    start_out = next(m for m in sent if m["type"] == "http.response.start")
    vary = _headers_dict(start_out).get("vary", "").lower()
    assert "origin" in vary and "accept-encoding" in vary


def test_small_body_passthrough():
    body = b'{"ok":true}'  # < minimum_size
    start = {"type": "http.response.start", "status": 200,
             "headers": [(b"content-type", b"application/json")]}
    body_msg = {"type": "http.response.body", "body": body, "more_body": False}

    sent = _run(
        lambda app: GZipMiddleware(app, minimum_size=500),
        _http_scope("gzip"), [start, body_msg])

    start_out = next(m for m in sent if m["type"] == "http.response.start")
    body_out = next(m for m in sent if m["type"] == "http.response.body")
    assert "content-encoding" not in _headers_dict(start_out)
    assert body_out["body"] == body


def test_no_accept_encoding_passthrough():
    body = b"x" * 5000
    start = {"type": "http.response.start", "status": 200,
             "headers": [(b"content-type", b"application/json")]}
    body_msg = {"type": "http.response.body", "body": body, "more_body": False}

    sent = _run(
        lambda app: GZipMiddleware(app, minimum_size=500),
        _http_scope(accept_encoding=None), [start, body_msg])

    start_out = next(m for m in sent if m["type"] == "http.response.start")
    body_out = next(m for m in sent if m["type"] == "http.response.body")
    assert "content-encoding" not in _headers_dict(start_out)
    assert body_out["body"] == body


def test_sse_content_type_excluded():
    body = b"data: " + b"x" * 5000 + b"\n\n"
    start = {"type": "http.response.start", "status": 200,
             "headers": [(b"content-type", b"text/event-stream")]}
    body_msg = {"type": "http.response.body", "body": body, "more_body": False}

    sent = _run(
        lambda app: GZipMiddleware(app, minimum_size=500),
        _http_scope("gzip"), [start, body_msg])

    start_out = next(m for m in sent if m["type"] == "http.response.start")
    body_out = next(m for m in sent if m["type"] == "http.response.body")
    assert "content-encoding" not in _headers_dict(start_out)
    assert body_out["body"] == body


def test_already_encoded_passthrough():
    body = b"x" * 5000
    start = {"type": "http.response.start", "status": 200,
             "headers": [(b"content-type", b"application/json"),
                         (b"content-encoding", b"br")]}
    body_msg = {"type": "http.response.body", "body": body, "more_body": False}

    sent = _run(
        lambda app: GZipMiddleware(app, minimum_size=500),
        _http_scope("gzip"), [start, body_msg])

    start_out = next(m for m in sent if m["type"] == "http.response.start")
    body_out = next(m for m in sent if m["type"] == "http.response.body")
    # original content-encoding preserved, body untouched.
    assert _headers_dict(start_out).get("content-encoding") == "br"
    assert body_out["body"] == body


def test_streaming_more_body_passthrough_uncompressed():
    """more_body=True (streaming) MUST pass through identity — no per-chunk
    compression (Tier-D ban; preserves TTFT)."""
    chunk1 = b"x" * 5000
    chunk2 = b"y" * 5000
    start = {"type": "http.response.start", "status": 200,
             "headers": [(b"content-type", b"application/json")]}
    msg1 = {"type": "http.response.body", "body": chunk1, "more_body": True}
    msg2 = {"type": "http.response.body", "body": chunk2, "more_body": False}

    sent = _run(
        lambda app: GZipMiddleware(app, minimum_size=500),
        _http_scope("gzip"), [start, msg1, msg2])

    start_out = next(m for m in sent if m["type"] == "http.response.start")
    bodies = [m for m in sent if m["type"] == "http.response.body"]
    assert "content-encoding" not in _headers_dict(start_out)
    # Both chunks delivered verbatim, uncompressed, in order.
    assert bodies[0]["body"] == chunk1
    assert bodies[1]["body"] == chunk2


def test_non_http_scope_passthrough():
    """Non-http scopes (websocket/lifespan) pass straight through."""
    seen = {"called": False}

    async def app(scope, receive, send):
        seen["called"] = True

    async def receive():
        return {"type": "lifespan.startup"}

    async def send(message):
        pass

    async def go():
        mw = GZipMiddleware(app)
        await mw({"type": "lifespan"}, receive, send)

    asyncio.run(go())
    assert seen["called"] is True
