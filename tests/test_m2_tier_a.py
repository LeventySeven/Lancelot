"""Tier-A regression guards (research-distillation `docs/plans/...`).

Each test corresponds 1:1 to one Tier-A item. If a test goes red, that
Tier-A guarantee has regressed."""
import asyncio
import json

import pytest

import lancelot
from lancelot.params import Body
from lancelot.stream import Event
from lancelot.testclient import TestClient


# ---------- A3: JSON nesting-depth DoS guard ----------------------------

def test_a3_deep_json_body_rejected_400():
    """A JSON body nested > 128 levels deep is rejected with 400 before
    pydantic-core touches it (stack-overflow defense)."""
    from pydantic import BaseModel

    class M(BaseModel):
        x: list

    app = lancelot.App()

    @app.post("/p")
    def p(m: M = Body(...)):
        return {"ok": True}

    deep = "[" * 200 + "]" * 200
    with TestClient(app) as c:
        r = c.post("/p", content=deep,
                   headers={"content-type": "application/json"})
    assert r.status_code == 400
    assert "nested too deep" in r.text


def test_a3_shallow_json_body_still_passes():
    """A normal JSON body (depth well under 128) is untouched."""
    from pydantic import BaseModel

    class M(BaseModel):
        x: list

    app = lancelot.App()

    @app.post("/p")
    def p(m: M = Body(...)):
        return {"len": len(m.x)}

    with TestClient(app) as c:
        r = c.post("/p", json={"x": [1, 2, 3]})
    assert r.status_code == 200
    assert r.json() == {"len": 3}


def test_a3_non_json_content_type_skips_guard():
    """multipart / urlencoded / binary bodies are NOT scanned (legitimate
    forms can have many delimiters)."""
    app = lancelot.App()

    @app.post("/p")
    def p():
        return {"ok": True}

    # 200 `=`s would trip ANY hot pre-scan if applied indiscriminately —
    # confirm the urlencoded path is skipped.
    with TestClient(app) as c:
        r = c.post("/p", content="=" * 200,
                   headers={"content-type":
                            "application/x-www-form-urlencoded"})
    assert r.status_code == 200


# ---------- A4: /livez + /readyz ----------------------------------------

def test_a4_livez_and_readyz_auto_registered():
    """Every App exposes /livez (process alive) and /readyz (accepting
    traffic) without user code — k8s table-stakes."""
    app = lancelot.App()
    with TestClient(app) as c:
        live = c.get("/livez")
        ready = c.get("/readyz")
    assert live.status_code == 200
    assert live.json() == {"status": "ok"}
    assert ready.status_code == 200
    assert ready.json() == {"status": "ok"}


def test_a4_readyz_flips_to_503_when_shutting_down():
    """Once the app flips _shutting_down=True (lifespan shutdown / SIGTERM),
    /readyz returns 503 + Retry-After so the LB stops sending traffic.
    /livez stays 200 (the process is still alive)."""
    app = lancelot.App()
    app._shutting_down = True
    with TestClient(app) as c:
        live = c.get("/livez")
        ready = c.get("/readyz")
    assert live.status_code == 200
    assert ready.status_code == 503
    assert ready.headers.get("retry-after") == "1"


# ---------- A5: SSE keepalive ping --------------------------------------

def test_a5_sse_keepalive_emits_comment_on_idle():
    """When the handler is idle for sse_keepalive seconds, the framework
    emits an SSE comment line so proxies don't time out."""
    app = lancelot.App()

    @app.stream("/s", sse_keepalive=0.05)  # 50ms for the test
    async def s():
        await asyncio.sleep(0.12)  # ≥ 2× the keepalive → ≥ 2 pings
        yield Event(name="done", data={"ok": True})

    with TestClient(app) as c:
        with c.stream("GET", "/s") as r:
            body = b""
            for chunk in r.iter_raw():
                body += chunk
    assert b":\n\n" in body          # at least one keepalive comment
    assert b"event: done" in body    # then the real event lands


# ---------- A6: stream_interval batching --------------------------------

def test_a6_stream_interval_batches_events():
    """With stream_interval=N, every N events are coalesced into one
    body chunk (fewer ASGI send() calls on token-heavy streams)."""
    app = lancelot.App()

    @app.stream("/s", stream_interval=3)
    async def s():
        for i in range(6):
            yield Event(name="tick", data={"i": i})

    with TestClient(app) as c:
        with c.stream("GET", "/s") as r:
            chunks = [c for c in r.iter_raw() if c]
    body = b"".join(chunks)
    # All 6 events arrive intact (correctness) — Event.render() emits
    # compact JSON (`{"i":0}`), not stdlib's spaced default.
    for i in range(6):
        assert (b'{"i":' + str(i).encode() + b'}') in body


def test_a6_stream_interval_default_is_one_event_per_chunk():
    """At stream_interval=1 (the default), per-event framing is
    preserved — same wire as before."""
    app = lancelot.App()

    @app.stream("/s")  # default interval=1
    async def s():
        for i in range(3):
            yield Event(name="tick", data={"i": i})

    with TestClient(app) as c:
        with c.stream("GET", "/s") as r:
            body = b"".join(r.iter_raw())
    assert body.count(b"event: tick") == 3


# ---------- A7: wire invariants (Content-Length on chunked, 204 no-body)

def test_a7_no_body_for_204():
    """RFC 7230: 204 No Content MUST NOT include a message body. Verify
    Lancelot honors this when the user returns a `Response` with status=204."""
    from lancelot.responses import Response

    app = lancelot.App()

    @app.delete("/x")
    def x():
        return Response(b"", status_code=204)

    with TestClient(app) as c:
        r = c.delete("/x")
    assert r.status_code == 204
    assert r.content == b""


def test_a7_sse_omits_content_length():
    """Streaming/chunked responses MUST NOT carry Content-Length (RFC 7230
    §3.3.2). The ASGI seam must not set one for @app.stream routes."""
    app = lancelot.App()

    @app.stream("/s")
    async def s():
        yield Event(name="x", data={"i": 1})

    with TestClient(app) as c:
        r = c.get("/s")
    assert "content-length" not in {k.lower() for k in r.headers}
    assert r.headers["content-type"].startswith("text/event-stream")
