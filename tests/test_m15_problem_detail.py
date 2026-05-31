"""M15 T11 — opt-in RFC 9457 ProblemDetail + retry-taxonomy convenience.

The conformance story is the byte-identical `{"detail": ...}` default error
shape. T11 does NOT touch that: it adds an OPT-IN
`lancelot.exceptions.ProblemDetail` (RFC 9457 `application/problem+json`:
type/title/status/detail/instance) the user explicitly raises (or returns
via `.to_response()`), plus a small `retry_taxonomy(...)` convenience that
wires `set_retry_hint` / `Retry-After` to a status taxonomy.

`test_default_error_unchanged` is the GUARD — a plain `HTTPException` must
still emit exactly `{"detail":"nope"}`.
"""
import json

import pytest

import lancelot
from lancelot.exceptions import HTTPException, ProblemDetail
from lancelot.testclient import TestClient


# ---------- ProblemDetail shape (raise + return) -----------------------

def test_problem_detail_shape_when_raised():
    """Raising ProblemDetail emits application/problem+json with RFC 9457
    fields and the right status."""
    app = lancelot.App()

    @app.get("/p")
    async def p():
        raise ProblemDetail(
            status=403,
            type="https://example.com/probs/forbidden",
            title="Forbidden",
            detail="You shall not pass",
            instance="/p/42",
        )

    with TestClient(app) as c:
        r = c.get("/p")

    assert r.status_code == 403, r.text
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["type"] == "https://example.com/probs/forbidden"
    assert body["title"] == "Forbidden"
    assert body["status"] == 403
    assert body["detail"] == "You shall not pass"
    assert body["instance"] == "/p/42"


def test_problem_detail_shape_when_returned():
    """Returning ProblemDetail(...).to_response() emits the same
    application/problem+json shape (the return path)."""
    app = lancelot.App()

    @app.get("/p")
    async def p():
        return ProblemDetail(status=409, title="Conflict",
                             detail="version mismatch").to_response()

    with TestClient(app) as c:
        r = c.get("/p")

    assert r.status_code == 409
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["status"] == 409
    assert body["title"] == "Conflict"
    assert body["detail"] == "version mismatch"


def test_problem_detail_type_defaults_to_about_blank():
    """RFC 9457 §4.1: when `type` is omitted it defaults to 'about:blank'
    and `title` SHOULD be the status phrase."""
    app = lancelot.App()

    @app.get("/p")
    async def p():
        raise ProblemDetail(status=404)

    with TestClient(app) as c:
        r = c.get("/p")
    body = r.json()
    assert body["type"] == "about:blank"
    assert body["status"] == 404
    # title falls back to a phrase for the status
    assert body["title"] == "Not Found"


def test_problem_detail_extension_members():
    """RFC 9457 §3.2: a problem object MAY carry extension members. The
    helper passes through arbitrary extra fields."""
    app = lancelot.App()

    @app.get("/p")
    async def p():
        raise ProblemDetail(status=429, title="Too Many Requests",
                            detail="slow down",
                            extensions={"balance": 30, "retry_in_s": 5})

    with TestClient(app) as c:
        r = c.get("/p")
    body = r.json()
    assert body["balance"] == 30
    assert body["retry_in_s"] == 5
    assert body["status"] == 429


def test_problem_detail_carries_headers():
    """ProblemDetail can carry response headers (e.g. Retry-After) that
    reach the wire."""
    app = lancelot.App()

    @app.get("/p")
    async def p():
        raise ProblemDetail(status=503, title="Service Unavailable",
                            headers={"Retry-After": "7"})

    with TestClient(app) as c:
        r = c.get("/p")
    assert r.status_code == 503
    assert r.headers["retry-after"] == "7"
    assert r.headers["content-type"].startswith("application/problem+json")


# ---------- the conformance GUARD: default path unchanged --------------

def test_default_error_unchanged():
    """A plain HTTPException(404, "nope") still emits EXACTLY
    `{"detail":"nope"}` with content-type application/json — byte-identical
    to before T11 (the 99.6% conformance story must not move)."""
    app = lancelot.App()

    @app.get("/x")
    async def x():
        raise HTTPException(404, "nope")

    with TestClient(app) as c:
        r = c.get("/x")

    assert r.status_code == 404
    # Byte-identical body + the JSON (not problem+json) content-type.
    assert r.content == b'{"detail":"nope"}', r.content
    assert r.headers["content-type"] == "application/json"


def test_default_error_with_headers_unchanged():
    """HTTPException(headers=...) wire shape is unchanged (headers first,
    then content-length, then content-type) and still {"detail":...}."""
    app = lancelot.App()

    @app.get("/x")
    async def x():
        raise HTTPException(401, "no", headers={"WWW-Authenticate": "Bearer"})

    with TestClient(app) as c:
        r = c.get("/x")
    assert r.status_code == 401
    assert r.content == b'{"detail":"no"}'
    assert r.headers["www-authenticate"] == "Bearer"
    assert r.headers["content-type"] == "application/json"


# ---------- retry-taxonomy convenience ---------------------------------

def test_retry_taxonomy_sets_headers():
    """`retry_taxonomy(response, status)` sets X-Should-Retry (and a
    sensible Retry-After for the rate-limit class) per a documented status
    taxonomy: 429/503 → retryable + Retry-After; 5xx → retryable; 4xx
    (client) → not retryable."""
    from lancelot.exceptions import retry_taxonomy
    from lancelot.responses import JSONResponse

    # 429 → retryable + Retry-After.
    r1 = JSONResponse({"detail": "slow"}, status_code=429)
    retry_taxonomy(r1, 429, retry_after=12)
    assert r1.headers["x-should-retry"] == "true"
    assert r1.headers["retry-after"] == "12"

    # 503 → retryable + a default Retry-After.
    r2 = JSONResponse({"detail": "down"}, status_code=503)
    retry_taxonomy(r2, 503)
    assert r2.headers["x-should-retry"] == "true"
    assert "retry-after" in r2.headers

    # 500 → retryable, no Retry-After mandated.
    r3 = JSONResponse({"detail": "boom"}, status_code=500)
    retry_taxonomy(r3, 500)
    assert r3.headers["x-should-retry"] == "true"

    # 400 (client error) → NOT retryable.
    r4 = JSONResponse({"detail": "bad"}, status_code=400)
    retry_taxonomy(r4, 400)
    assert r4.headers["x-should-retry"] == "false"
    assert "retry-after" not in r4.headers


def test_problem_detail_with_retry_taxonomy_end_to_end():
    """ProblemDetail composed with the retry taxonomy: a raised 503
    ProblemDetail carries problem+json AND the retry headers."""
    from lancelot.exceptions import retry_taxonomy

    app = lancelot.App()

    @app.get("/p")
    async def p():
        pd = ProblemDetail(status=503, title="Service Unavailable",
                           detail="upstream LLM at capacity")
        retry_taxonomy(pd, 503, retry_after=3)
        raise pd

    with TestClient(app) as c:
        r = c.get("/p")
    assert r.status_code == 503
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.headers["x-should-retry"] == "true"
    assert r.headers["retry-after"] == "3"
    assert r.json()["title"] == "Service Unavailable"
