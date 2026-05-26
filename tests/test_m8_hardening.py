"""M8 — hardening pass distilled from the multi-agent recheck (2026-05-26).

Each test pins one production-safety guarantee inherited from FastAPI/Starlette
or a byte-path transport hardening surfaced by the Robyn/teardown rechecks.
All items passed the no-bullshit / no-overkill filter: each is a real gap in
Lancelot's code that a peer framework guards against, fixed with the smallest
viable change.

  SEC1 — Set-Cookie CRLF/control-char injection (responses.py)
  SEC2 — multipart field/file/part-size DoS caps (uploads.py)
  SEC3 — query-string param-count DoS guard (asgi.py)
  OPS1 — lifespan startup failure: full traceback + re-raise (asgi.py)
  OPS2 — LANCELOT_MAX_BODY operator knob (asgi.py + src/service.rs)
  OPS3 — App(json_max_depth=N) operator knob (app.py + asgi.py)
  TP1  — TransportPool connection-pool knobs (llm.py)
  TP2  — Idempotency-Key on retried upstream calls (llm.py)
"""
import asyncio
import contextlib

import pytest

from lancelot import App
from lancelot.responses import Response


# ---------- SEC1: Set-Cookie header injection ---------------------------

def test_sec1_set_cookie_strips_crlf_no_header_injection():
    """A CRLF in a cookie value must not be able to split the Set-Cookie
    header into a second header (response-splitting). Matches the existing
    A3 CR-safety pattern already used for media_type / session_affinity."""
    r = Response()
    r.set_cookie("session", "val\r\nSet-Cookie: admin=true")
    hv = r.headers["set-cookie"]
    assert "\r" not in hv and "\n" not in hv, hv


def test_sec1_set_cookie_strips_crlf_in_key():
    r = Response()
    r.set_cookie("inject\r\nX-Evil", "harmless")
    hv = r.headers["set-cookie"]
    assert "\r" not in hv and "\n" not in hv, hv


def test_sec1_set_cookie_rejects_invalid_samesite():
    """samesite is an enum {strict,lax,none}; an arbitrary string is a
    programming error and must raise (Starlette parity)."""
    r = Response()
    with pytest.raises(ValueError):
        r.set_cookie("k", "v", samesite="evil; X-Injected: hdr")


# ---------- SEC2: multipart parse DoS caps ------------------------------

def _mp_body(fields, boundary=b"BOUNDARY"):
    """Build a minimal multipart/form-data body. `fields` is a list of
    (name, value_bytes) — value None marks a file part."""
    out = []
    for name, val in fields:
        out.append(b"--" + boundary + b"\r\n")
        out.append(b'Content-Disposition: form-data; name="'
                   + name.encode() + b'"\r\n\r\n')
        out.append(val + b"\r\n")
    out.append(b"--" + boundary + b"--\r\n")
    ct = "multipart/form-data; boundary=" + boundary.decode()
    return b"".join(out), ct


def test_sec2_multipart_field_count_cap():
    """A flood of tiny fields within the body cap must be rejected before
    building a giant dict (form-parse DoS — Starlette max_fields)."""
    from lancelot.uploads import parse_multipart
    body, ct = _mp_body([("f%d" % i, b"x") for i in range(6)])
    with pytest.raises(ValueError, match="(?i)too many|fields"):
        parse_multipart(body, ct, max_fields=3)


def test_sec2_multipart_part_size_cap():
    """A single part larger than max_part_size must be rejected."""
    from lancelot.uploads import parse_multipart
    body, ct = _mp_body([("big", b"x" * 5000)])
    with pytest.raises(ValueError, match="(?i)too large|part"):
        parse_multipart(body, ct, max_part_size=100)


def test_sec2_multipart_under_caps_still_parses():
    """Regression guard: normal small forms still parse fine under the
    default caps."""
    from lancelot.uploads import parse_multipart
    body, ct = _mp_body([("name", b"alice"), ("city", b"paris")])
    out = parse_multipart(body, ct)
    assert out["name"] == "alice" and out["city"] == "paris"


# ---------- SEC3: query-string param-count guard ------------------------

def _get(app, path):
    import httpx

    async def go():
        t = httpx.ASGITransport(app=app.asgi())
        async with httpx.AsyncClient(transport=t, base_url="http://t") as c:
            return await c.get(path)

    return asyncio.run(go())


def test_sec3_query_string_param_count_capped():
    app = App()

    @app.get("/q")
    def q():
        return {"ok": 1}

    qs = "?" + "&".join("k%d=1" % i for i in range(700))
    r = _get(app, "/q" + qs)
    assert r.status_code == 400, r.status_code


def test_sec3_query_string_normal_count_ok():
    app = App()

    @app.get("/q")
    def q():
        return {"ok": 1}

    r = _get(app, "/q?a=1&b=2&c=3")
    assert r.status_code == 200, r.status_code


# ---------- OPS1: lifespan startup failure diagnostics ------------------

def test_ops1_lifespan_startup_failure_emits_full_traceback():
    """On startup failure the ASGI seam must emit lifespan.startup.failed with
    a FULL traceback (not just repr(e)) so the server logs a real stack and the
    operator can diagnose the crash. Lancelot's deliberate contract is
    fail-fast-not-raise (see test_startup_exception_is_failfast_not_swallowed):
    the traceback rides in the protocol message rather than propagating."""
    @contextlib.asynccontextmanager
    async def ls(app):
        raise RuntimeError("boom-during-startup")
        yield  # pragma: no cover

    app = App(lifespan=ls)
    sent = []
    inbox = ["lifespan.startup"]

    async def receive():
        return {"type": inbox.pop(0)}

    async def send(m):
        sent.append(m)

    async def go():
        await app.asgi()({"type": "lifespan", "state": {}}, receive, send)

    asyncio.run(go())  # fail-fast, does NOT propagate (existing contract)

    assert sent and sent[0]["type"] == "lifespan.startup.failed"
    msg = sent[0]["message"]
    # The win: a real multi-line traceback, not the old one-line repr(e).
    assert "Traceback" in msg and "boom-during-startup" in msg, msg
    assert "RuntimeError" in msg, msg


# ---------- OPS2: LANCELOT_MAX_BODY operator knob -----------------------

def test_ops2_max_body_env_knob_parsing(monkeypatch):
    from lancelot import asgi as _asgi
    default = 16 * 1024 * 1024
    monkeypatch.setenv("LANCELOT_MAX_BODY", "2048")
    assert _asgi._resolve_max_body() == 2048
    # invalid / out-of-range → fall back to the default
    monkeypatch.setenv("LANCELOT_MAX_BODY", "-5")
    assert _asgi._resolve_max_body() == default
    monkeypatch.setenv("LANCELOT_MAX_BODY", "notanint")
    assert _asgi._resolve_max_body() == default
    monkeypatch.delenv("LANCELOT_MAX_BODY", raising=False)
    assert _asgi._resolve_max_body() == default


# ---------- OPS3: App(json_max_depth=) operator knob --------------------

def _post_json(app, path, raw):
    import httpx

    async def go():
        t = httpx.ASGITransport(app=app.asgi())
        async with httpx.AsyncClient(transport=t, base_url="http://t") as c:
            return await c.post(path, content=raw,
                                headers={"content-type": "application/json"})

    return asyncio.run(go())


def test_ops3_json_max_depth_knob_rejects_deep():
    app = App(json_max_depth=3)

    @app.post("/d")
    def d():
        return {"ok": 1}

    deep = b'{"a":{"b":{"c":{"d":1}}}}'  # depth 4 > 3
    r = _post_json(app, "/d", deep)
    assert r.status_code == 400, r.status_code


def test_ops3_json_max_depth_knob_allows_shallow():
    app = App(json_max_depth=3)

    @app.post("/d")
    def d():
        return {"ok": 1}

    shallow = b'{"a":{"b":1}}'  # depth 2 < 3
    r = _post_json(app, "/d", shallow)
    assert r.status_code == 200, r.status_code


# ---------- TP1: TransportPool connection-pool knobs --------------------

def test_tp1_connection_pool_knobs_forwarded():
    from lancelot.llm import TransportPool
    pool = TransportPool(max_connections=7, max_keepalive_connections=3,
                         keepalive_expiry=12.0)
    assert pool.limits.max_connections == 7
    assert pool.limits.max_keepalive_connections == 3
    assert pool.limits.keepalive_expiry == 12.0
    asyncio.run(pool.aclose())


def test_tp1_connection_pool_env_fallback(monkeypatch):
    from lancelot.llm import TransportPool
    monkeypatch.setenv("LANCELOT_UPSTREAM_MAX_CONNECTIONS", "256")
    pool = TransportPool()
    assert pool.limits.max_connections == 256
    asyncio.run(pool.aclose())


# ---------- TP2: Idempotency-Key on retried upstream calls --------------

def test_tp2_idempotency_key_stable_across_retries():
    """A retried idempotent call must carry the SAME Idempotency-Key on
    every attempt so the upstream can dedupe (Turbopuffer/Stainless
    pattern). Without a stable key the server can't dedup a retry against
    the original."""
    import httpx
    from lancelot.llm import TransportPool

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("idempotency-key"))
        if len(seen) < 3:
            return httpx.Response(503, headers={"retry-after": "0"})
        return httpx.Response(200, json={"ok": True})

    pool = TransportPool(transport=httpx.MockTransport(handler),
                         max_retries=5, backoff_base=0.001)
    r = asyncio.run(pool.request("GET", "https://api.x/v1/m"))
    assert r.status_code == 200 and len(seen) == 3
    assert all(k for k in seen), seen
    assert len(set(seen)) == 1, f"key changed across retries: {seen}"
    asyncio.run(pool.aclose())


def test_tp2_idempotency_key_caller_value_preserved():
    """A caller-supplied Idempotency-Key is never overwritten."""
    import httpx
    from lancelot.llm import TransportPool

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("idempotency-key"))
        return httpx.Response(200, json={})

    pool = TransportPool(transport=httpx.MockTransport(handler))
    asyncio.run(pool.request("GET", "https://x/y",
                             headers={"Idempotency-Key": "caller-123"}))
    assert seen == ["caller-123"], seen
    asyncio.run(pool.aclose())
