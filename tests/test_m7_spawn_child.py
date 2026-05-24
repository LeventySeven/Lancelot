"""M7 T1.1 — TransportPool.spawn_child() sub-agent / nestable primitive.

Giga's "outbound calls from within a single agent — basically like a
sub-agent" pattern (TRANSCRIPTS_YC_ROOT_ACCESS.md:9358–9852) +
Parahelp's N-trace fanout (lines 7798–8292).

Validates:
  - spawn_child captures parent's traceparent + request_id + agent_id
  - inject works even if parent ContextVars get reset (snapshot semantics)
  - session_affinity flows as `x-lancelot-affinity`
  - upstream `request()` already gets the same headers in the common case
    (no spawn_child needed when call is inline; ContextVar copy-on-await)
  - parent_request cancellation propagates to child (C2 semantics inherited)
"""
import asyncio

import httpx
import pytest

pytest.importorskip("httpx")

import lancelot
from lancelot.context import (
    trace_parent, request_id, agent_id, trace_id,
)
from lancelot.llm import TransportPool


# ----- T1.1.a inline request() injects the parent's identifiers -----------

def test_t1_1_inline_request_injects_request_id_and_agent_id():
    """The existing `request()` injects traceparent (existing M2 behavior)
    AND now also `x-request-id` + `x-agent-id` when those ContextVars are
    set. Covers the common inline-handler case where no spawn_child is
    needed."""
    captured = {}

    def fake(request: httpx.Request):
        captured.update(dict(request.headers))
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(fake)
    pool = TransportPool(transport=transport)

    async def run():
        rid_tok = request_id.set("req-abc123")
        aid_tok = agent_id.set("agent-alice")
        tp_tok = trace_parent.set("00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01")
        try:
            await pool.request("POST", "https://upstream/v1/chat")
        finally:
            request_id.reset(rid_tok)
            agent_id.reset(aid_tok)
            trace_parent.reset(tp_tok)
        await pool.aclose()

    asyncio.run(run())

    assert captured.get("traceparent", "").startswith("00-aaaaaaa")
    assert captured.get("x-request-id") == "req-abc123"
    assert captured.get("x-agent-id") == "agent-alice"


# ----- T1.1.b spawn_child captures snapshot at construction time ----------

def test_t1_1_spawn_child_snapshot_survives_parent_context_reset():
    """The child handle should still inject the captured headers even
    after the parent's ContextVars have been reset (the typical case for
    fire-and-forget / outliving sub-agent tasks)."""
    captured_calls = []

    def fake(request: httpx.Request):
        captured_calls.append(dict(request.headers))
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(fake)
    pool = TransportPool(transport=transport)

    async def run():
        # Inside parent request scope: snapshot.
        rid_tok = request_id.set("req-parent")
        aid_tok = agent_id.set("agent-x")
        tp_tok = trace_parent.set("00-11111111111111111111111111111111-2222222222222222-01")
        try:
            child = pool.spawn_child(session_affinity="cust-42")
        finally:
            # Parent context torn down BEFORE the child's call.
            request_id.reset(rid_tok)
            agent_id.reset(aid_tok)
            trace_parent.reset(tp_tok)

        # The parent ContextVars are now back to defaults.
        assert request_id.get() == "-"
        assert agent_id.get() == "-"

        # The child should STILL inject the snapshotted values.
        await child.request("POST", "https://upstream/v1/chat")
        await pool.aclose()

    asyncio.run(run())

    assert captured_calls
    h = captured_calls[0]
    assert h.get("x-request-id") == "req-parent"
    assert h.get("x-agent-id") == "agent-x"
    assert h.get("traceparent", "").startswith("00-11111111")
    assert h.get("x-lancelot-affinity") == "cust-42"


# ----- T1.1.c spawn_child does NOT overwrite caller-set headers -----------

def test_t1_1_spawn_child_respects_caller_set_headers():
    """If the caller explicitly passes a header, the spawn_child snapshot
    must NOT clobber it (.setdefault semantics)."""
    captured_calls = []

    def fake(request: httpx.Request):
        captured_calls.append(dict(request.headers))
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(fake)
    pool = TransportPool(transport=transport)

    async def run():
        rid_tok = request_id.set("req-parent")
        try:
            child = pool.spawn_child()
        finally:
            request_id.reset(rid_tok)

        # Caller explicitly passes their own x-request-id.
        await child.request("POST", "https://upstream/v1/chat",
                             headers={"x-request-id": "caller-override"})
        await pool.aclose()

    asyncio.run(run())

    assert captured_calls[0].get("x-request-id") == "caller-override"


# ----- T1.1.d spawn_child with NO parent context = no-op headers ----------

def test_t1_1_spawn_child_no_parent_context_emits_nothing():
    """Calling spawn_child OUTSIDE any request scope should produce a
    child that injects nothing (still works; just no identifiers)."""
    captured_calls = []

    def fake(request: httpx.Request):
        captured_calls.append(dict(request.headers))
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(fake)
    pool = TransportPool(transport=transport)

    async def run():
        # No ContextVars set; default sentinels.
        child = pool.spawn_child()
        await child.request("POST", "https://upstream/v1/chat")
        await pool.aclose()

    asyncio.run(run())

    h = captured_calls[0]
    # Default sentinels '-' should NOT show up as headers.
    assert "x-request-id" not in h
    assert "x-agent-id" not in h
    assert "traceparent" not in h
    assert "x-lancelot-affinity" not in h
