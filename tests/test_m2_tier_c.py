"""Tier-C regression guards (research-distillation `docs/plans/...`).

Each test pins one Tier-C guarantee:
  C1 — KV-cache-friendly canonical JSON + X-Session-Affinity header
  C2 — Streaming-cancellation propagation into TransportPool
  C3 — @app.tool auto-derives output_schema from the return-type annotation
  C4 — (audit-and-ledger only — already covered by existing seam structure)
"""
import asyncio

import pytest

import lancelot
from lancelot.testclient import TestClient


# ---------- C1: canonical JSON + X-Session-Affinity ---------------------

def test_c1_canonical_json_is_stable_across_runs():
    """`canonical.dumps_canonical(obj)` MUST emit identical bytes regardless
    of dict-insertion order — so the resulting payload hashes the same and
    upstream LLM prompt caches hit."""
    a = {"b": 1, "a": 2, "z": [3, {"y": 4, "x": 5}]}
    b = {"z": [3, {"x": 5, "y": 4}], "a": 2, "b": 1}
    assert lancelot.canonical.dumps_canonical(a) \
        == lancelot.canonical.dumps_canonical(b)
    # Compact format: no whitespace between separators.
    assert b" " not in lancelot.canonical.dumps_canonical(a)


def test_c1_canonical_rejects_nan_and_infinity():
    """RFC 8259 forbids NaN / Infinity in JSON. We MUST raise rather than
    silently emit non-portable bytes (Python's json default IS to emit
    them, which is a footgun for cross-system caching)."""
    import pytest

    with pytest.raises(ValueError):
        lancelot.canonical.dumps_canonical({"x": float("nan")})
    with pytest.raises(ValueError):
        lancelot.canonical.dumps_canonical({"x": float("inf")})


def test_c1_set_session_affinity_emits_header():
    """`response.set_session_affinity(key)` adds an `X-Session-Affinity`
    response header so a sticky LB can route the next request from this
    client to the same instance (KV-cache hit)."""
    from lancelot.responses import Response

    app = lancelot.App()

    @app.get("/chat")
    def chat():
        r = Response(b'{"ok":true}', media_type="application/json")
        r.set_session_affinity("user-42")
        return r

    with TestClient(app) as c:
        r = c.get("/chat")
    assert r.status_code == 200
    assert r.headers.get("x-session-affinity") == "user-42"


def test_c1_set_session_affinity_strips_crlf_injection():
    """Defense-in-depth: a user-provided affinity key MUST be sanitized
    of CR/LF so it can't smuggle a header injection."""
    from lancelot.responses import Response

    r = Response(b"x")
    r.set_session_affinity("evil\r\nX-Injected: yes")
    val = r.headers["x-session-affinity"]
    assert "\r" not in val and "\n" not in val
    assert "X-Injected" in val  # the LITERAL chars are kept; only the
    # CRLF that would terminate the header was stripped


# ---------- C2: TransportPool cancellation on client disconnect --------

def test_c2_transport_pool_await_cancellable_passthrough():
    """No request → no cancel event → zero-overhead pass-through.
    The TransportPool's _await_cancellable helper is the contract surface
    we pin (the full client-disconnect path is integration-level)."""
    from lancelot import llm as _llm  # lazy import (httpx dep)

    pool = _llm.TransportPool.__new__(_llm.TransportPool)

    async def go():
        return await pool._await_cancellable(_ok_coro(), request=None)

    assert asyncio.run(go()) == 42


def test_c2_transport_pool_cancels_when_event_fires():
    """If the request's _cancel_event fires before the upstream coroutine
    finishes, the upstream task IS cancelled and CancelledError propagates
    so the handler stops paying for tokens the client isn't watching."""
    from lancelot import llm as _llm

    pool = _llm.TransportPool.__new__(_llm.TransportPool)

    async def go():
        class _FakeReq:
            _cancel_event = asyncio.Event()

        req = _FakeReq()

        async def slow():
            await asyncio.sleep(5.0)
            return "should never return"

        async def trigger():
            await asyncio.sleep(0.02)
            req._cancel_event.set()

        asyncio.create_task(trigger())
        try:
            await pool._await_cancellable(slow(), request=req)
            return "no-cancel"
        except asyncio.CancelledError:
            return "cancelled"

    assert asyncio.run(go()) == "cancelled"


async def _ok_coro():
    return 42


# ---------- C3: @app.tool output_schema -------------------------------

def test_c3_tool_output_schema_derived_from_return_type():
    """A tool with a typed return value (`-> ResultModel`) emits an
    output_schema. Anthropic/OpenAI/Google all accept (and use) this."""
    from pydantic import BaseModel

    app = lancelot.App()

    class Hit(BaseModel):
        title: str
        score: float

    @app.tool(description="Search the index")
    def search(q: str, k: int = 5) -> list[Hit]:
        return []

    [spec] = app.tool_schemas()
    assert spec["name"] == "search"
    assert "input_schema" in spec
    assert "output_schema" in spec
    # The output is `list[Hit]`, which Pydantic serializes as
    # {"items": {...Hit schema...}, "type": "array"}.
    assert spec["output_schema"]["type"] == "array"


def test_c3_tool_no_return_annotation_skips_output_schema():
    """No annotation OR `-> None` MUST NOT add an output_schema (we
    don't invent one). MCP/Anthropic clients use absence as "unknown"."""
    app = lancelot.App()

    @app.tool(description="Side-effect only")
    def emit(msg: str):
        print(msg)

    @app.tool(description="Explicitly void")
    def void(msg: str) -> None:
        return None

    schemas = {s["name"]: s for s in app.tool_schemas()}
    assert "output_schema" not in schemas["emit"]
    assert "output_schema" not in schemas["void"]


# ---------- C4: dynamic writer dispatch — AUDIT-AND-LEDGER -------------

def test_c4_audit_no_per_chunk_branching_on_stream():
    """C4 audit (research-distillation Tier-C.C4): Blazeio's win is binding
    the per-chunk write function at prepare time. Lancelot's SSE seam in
    asgi.py is ALREADY a dedicated code path (separate from the JSON/
    StreamingResponse paths) — no per-chunk content-type branching. This
    test just pins that structural property by exercising a stream and
    asserting wire correctness; the real audit is at the code level (see
    asgi.py @app.stream path)."""
    from lancelot.stream import Event

    app = lancelot.App()

    @app.stream("/s")
    async def s():
        for i in range(4):
            yield Event(name="tick", data={"i": i})

    with TestClient(app) as c:
        with c.stream("GET", "/s") as r:
            body = b"".join(r.iter_raw())
    # All 4 events arrive, correctly framed, with the SSE content-type.
    assert r.headers["content-type"].startswith("text/event-stream")
    assert body.count(b"event: tick") == 4
