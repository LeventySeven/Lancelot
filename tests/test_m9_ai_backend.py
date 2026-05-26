"""M9 — AI-backend transport patterns (2026-05-26 deep recheck of serving
engines + LLM gateways + production transcripts, backend-framework lens).

Most AI-backend patterns were already shipped, Tier-D, or AI-logic. Three
net-new BYTE-PATH wins cleared the no-bullshit/no-overkill filter:

  S1 — mid-stream handler error emits a final SSE error frame instead of a
       silently-truncated stream that looks complete (Ollama streamResponse).
  T1 — TransportPool retry budget: a wall-clock cap so a runaway Retry-After
       chain can't hold an outbound call open indefinitely (Portkey
       MAX_RETRY_LIMIT_MS).
  T2 — transport-level timeouts (httpx.TimeoutException) are classified into
       the upstream metric taxonomy (408 -> "timeout") so they're not
       invisible vs HTTP 408/504 responses.
"""
import asyncio

import pytest

import lancelot


# ---------- S1: mid-stream error frame (no silent truncation) -----------

def _drive_http_collect(app, path, method="GET", body=b""):
    """Drive one HTTP request through the raw ASGI callable, collecting every
    `send` message and capturing any exception the app propagates. Avoids
    TestClient's post-EOF exception handling so we can assert the exact
    stream-close contract."""
    asgi = app.asgi()
    scope = {
        "type": "http", "method": method, "path": path,
        "raw_path": path.encode(), "query_string": b"", "headers": [],
        "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", 80),
        "scheme": "http",
    }
    msgs = []
    state = {"body_sent": False}

    async def receive():
        if not state["body_sent"]:
            state["body_sent"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        # The disconnect watcher parks here until the stream's finally
        # cancels it (no client disconnect in this test).
        await asyncio.sleep(3600)

    async def send(m):
        msgs.append(m)

    exc = {"e": None}

    async def go():
        try:
            await asgi(scope, receive, send)
        except Exception as e:  # noqa: BLE001
            exc["e"] = e

    asyncio.run(go())
    bodies = b"".join(m.get("body", b"") for m in msgs
                      if m.get("type") == "http.response.body")
    return msgs, bodies, exc["e"]


def test_s1_midstream_error_emits_error_frame_not_silent_truncation():
    app = lancelot.App()

    @app.stream("/boom")
    async def boom():
        yield b"data: chunk1\n\n"
        raise RuntimeError("midstream-fail")

    msgs, bodies, exc = _drive_http_collect(app, "/boom")

    # The good chunk that was already produced is delivered...
    assert b"data: chunk1\n\n" in bodies, bodies
    # ...and a final SSE error frame signals the failure (not a clean EOF that
    # makes the truncated stream look complete).
    assert b"event: error" in bodies and b"error" in bodies.lower(), bodies
    # Post-commit, the error is reported to the client + logged, not propagated
    # as an unhandled app exception.
    assert exc is None, f"stream error should be handled, not propagated: {exc!r}"


def test_s1_clean_stream_has_no_error_frame():
    """Regression guard: a normal stream must NOT gain an error frame."""
    app = lancelot.App()

    @app.stream("/ok")
    async def ok():
        yield b"data: a\n\n"
        yield b"data: b\n\n"

    _msgs, bodies, exc = _drive_http_collect(app, "/ok")
    assert b"data: a\n\n" in bodies and b"data: b\n\n" in bodies
    assert b"event: error" not in bodies, bodies
    assert exc is None


# ---------- T1: TransportPool retry budget wall-clock cap ---------------

def test_t1_retry_budget_caps_total_wall_clock():
    """A small retry budget must stop the retry chain before a long
    Retry-After would blow past it — even with max_retries high."""
    import httpx
    from lancelot.llm import TransportPool

    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(503, headers={"retry-after": "30"})  # 30s each

    pool = TransportPool(transport=httpx.MockTransport(handler),
                         max_retries=10, retry_budget_ms=1000)  # 1s budget
    r = asyncio.run(pool.request("GET", "https://x/y"))
    assert r.status_code == 503
    # 1s budget, each Retry-After is 30s → the first retry alone exceeds the
    # budget → bail after the initial attempt (no sleeps, fast test).
    assert attempts["n"] == 1, attempts
    asyncio.run(pool.aclose())


def test_t1_retry_budget_default_allows_normal_retries():
    """Regression guard: with the default budget, short backoffs still retry."""
    import httpx
    from lancelot.llm import TransportPool

    n = {"i": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        n["i"] += 1
        if n["i"] < 3:
            return httpx.Response(503, headers={"retry-after": "0"})
        return httpx.Response(200, json={"ok": True})

    pool = TransportPool(transport=httpx.MockTransport(handler),
                         max_retries=5, backoff_base=0.001)
    r = asyncio.run(pool.request("GET", "https://x/y"))
    assert r.status_code == 200 and n["i"] == 3
    asyncio.run(pool.aclose())


# ---------- T2: transport timeout → upstream metric taxonomy ------------

def test_t2_timeout_exception_recorded_in_upstream_metrics(monkeypatch):
    """A transport-level ReadTimeout must be classified into the upstream
    metric taxonomy (408 → 'timeout'), not vanish vs an HTTP 408 response.
    Behavior preserved: the timeout still propagates to the caller."""
    import httpx
    import lancelot.upstream_metrics as um
    from lancelot.llm import TransportPool

    recorded = []
    real = um.record_upstream

    def spy(url, status, duration_s, **kw):
        recorded.append((url, status))
        return real(url, status, duration_s, **kw)

    monkeypatch.setattr(um, "record_upstream", spy)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    pool = TransportPool(transport=httpx.MockTransport(handler), max_retries=0)
    with pytest.raises(httpx.TimeoutException):
        asyncio.run(pool.request("GET", "https://x/y"))
    assert any(s == 408 for (_u, s) in recorded), recorded
    asyncio.run(pool.aclose())
