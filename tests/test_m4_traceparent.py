"""W3C trace-context propagation (Tier-B cloud observability). Lightweight, no
OTel dep: ingest an incoming `traceparent`, expose its trace-id for log/metric
correlation, and forward it on outbound TransportPool calls so the upstream LLM
joins the SAME distributed trace. Full span export remains the user's OTel job.
"""
import asyncio

import pytest

import lancelot
from lancelot.context import parse_traceparent, trace_id, trace_parent
from lancelot.testclient import TestClient

httpx = pytest.importorskip("httpx")

_VALID = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
_TRACE = "0af7651916cd43dd8448eb211c80319c"


def test_parse_traceparent_valid_invalid_and_zero():
    assert parse_traceparent(_VALID) == _TRACE
    assert parse_traceparent(_VALID.upper()) == _TRACE          # case-insensitive
    assert parse_traceparent("  " + _VALID + "  ") == _TRACE    # whitespace-tolerant
    assert parse_traceparent(None) is None
    assert parse_traceparent("") is None
    assert parse_traceparent("garbage") is None
    assert parse_traceparent("00-" + "z" * 32 + "-b7ad6b7169203331-01") is None
    assert parse_traceparent("00-" + "0" * 32 + "-b7ad6b7169203331-01") is None  # zero trace
    assert parse_traceparent("00-" + _TRACE + "-" + "0" * 16 + "-01") is None     # zero span


def test_incoming_traceparent_exposed_to_handler_and_defaults():
    app = lancelot.App()

    @app.get("/t")
    async def t():
        return {"tid": trace_id.get(), "tp": trace_parent.get()}

    with TestClient(app) as c:
        r = c.get("/t", headers={"traceparent": _VALID})
        assert r.json() == {"tid": _TRACE, "tp": _VALID.lower()}
        # No / invalid header → defaults (no LookupError, no bleed from above).
        assert c.get("/t").json() == {"tid": "-", "tp": None}
        assert c.get("/t", headers={"traceparent": "nope"}).json() == {"tid": "-", "tp": None}


def test_transportpool_forwards_traceparent_when_in_trace_context():
    seen = []  # one entry per outbound request (per-request, no overwrite)

    def handler(req):
        seen.append(req.headers.get("traceparent"))
        return httpx.Response(200, json={"ok": True})

    from lancelot.llm import TransportPool
    pool = TransportPool(transport=httpx.MockTransport(handler))

    async def go():
        tok = trace_parent.set(_VALID.lower())
        try:
            await pool.request("GET", "http://upstream/x")   # in trace context
        finally:
            trace_parent.reset(tok)
        await pool.request("GET", "http://upstream/y")        # no trace context
        await pool.aclose()

    asyncio.run(go())
    assert seen == [_VALID.lower(), None]  # forwarded in-context; absent otherwise


def test_transportpool_caller_traceparent_wins():
    captured = {}

    def handler(req):
        captured["tp"] = req.headers.get("traceparent")
        return httpx.Response(200)

    from lancelot.llm import TransportPool
    pool = TransportPool(transport=httpx.MockTransport(handler))

    async def go():
        tok = trace_parent.set(_VALID.lower())
        try:
            await pool.request("GET", "http://upstream/x",
                               headers={"traceparent": "00-" + "a" * 32 + "-" + "b" * 16 + "-01"})
        finally:
            trace_parent.reset(tok)
        await pool.aclose()

    asyncio.run(go())
    assert captured["tp"] == "00-" + "a" * 32 + "-" + "b" * 16 + "-01"  # explicit wins
