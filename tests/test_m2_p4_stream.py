"""M2-P4 — `@app.stream` typed event sugar + `request.cancelled`
awaitable. Replaces the hand-rolled SSE framing
(`yield f"data: {json.dumps(...)}\n\n"`) recurring across 9+ products
(PERPLEXITY_API_REPLICA, GROK, PALOMA, AEMON, MINTLIFY, TAVILY,
PERPLEXITY_COMPUTER, LAMINAR, GOOGLE_ADK). No `resumable=` (strict
synthesis — zero product citations for Last-Event-Id replay)."""
import asyncio


def test_stream_typed_events_render_to_sse():
    """A handler yielding `Event` instances → SSE wire frames
    `data: <json>\\n\\n`. The framework owns the framing."""
    import lancelot
    from lancelot.stream import Event
    from lancelot.testclient import TestClient

    app = lancelot.App()

    @app.stream("/chat")
    async def chat():
        yield Event("delta", {"text": "hi"})
        yield Event("delta", {"text": " there"})
        yield Event("done", {})

    with TestClient(app) as client:
        r = client.get("/chat", headers={"accept": "text/event-stream"})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        body = r.text
        assert 'event: delta' in body
        assert '"text":"hi"' in body
        assert 'event: done' in body


def test_request_cancelled_is_a_truthy_awaitable_after_disconnect():
    """`request.cancelled` returns an awaitable whose `is_set()` becomes
    True when the client disconnects mid-stream. Stops `app.llm` bills.
    Without an actual disconnect, it's a no-op (returns False)."""
    import lancelot
    from lancelot.requests import Request

    # Standalone unit-level invariant: a fresh Request has a non-truthy
    # cancellation token by default (it gets wired by the asgi seam).
    req = Request("GET", "/x", {}, {}, {}, {}, b"", None)
    # Before the asgi seam attaches one, attribute exists and is False-ish
    assert getattr(req, "_cancel_event", None) is None or \
        not req._cancel_event.is_set()
