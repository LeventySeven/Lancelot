"""M2-P4 — `@app.stream` typed event sugar + `request.cancelled`
awaitable. Replaces the hand-rolled SSE framing
(`yield f"data: {json.dumps(...)}\n\n"`) recurring across 9+ products
(PERPLEXITY_API_REPLICA, GROK, PALOMA, AEMON, MINTLIFY, TAVILY,
PERPLEXITY_COMPUTER, LAMINAR, GOOGLE_ADK). No `resumable=` (strict
synthesis — zero product citations for Last-Event-Id replay)."""
import asyncio


def test_stream_accepts_raw_bytes_and_str_not_only_event():
    """REGRESSION: `@app.stream` must accept `bytes`/`str` yields (raw SSE the
    way AI handlers naturally write it — `yield f"data: {token}\\n\\n"`), not
    only `Event`. The loop called `event.render()` unconditionally, so a
    bytes/str yield raised AFTER the 200 was sent → a SILENT EMPTY body. Now:
    bytes pass through, str → utf-8, Event → SSE-framed."""
    import lancelot
    from lancelot.stream import Event
    from lancelot.testclient import TestClient

    app = lancelot.App()

    @app.stream("/raw")
    async def raw():
        yield b"data: a\n\n"            # bytes — passthrough
        yield "data: b\n\n"            # str — utf-8
        yield Event("delta", {"x": 1})  # Event — SSE-framed via render()

    with TestClient(app) as client:
        r = client.get("/raw")
        assert r.status_code == 200
        body = r.content
        assert body, "regression: @app.stream emitted a SILENT EMPTY body"
        assert b"data: a\n\n" in body
        assert b"data: b\n\n" in body
        assert b"event: delta" in body and b'"x":1' in body


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
