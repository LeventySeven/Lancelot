"""M17 P7 — streamed-response cost/token/TTFT accounting (the parity bug).

Confirmed gap (verify live): `pool.request` -> `_emit_upstream` (llm.py:424)
records `upstream.*` metrics AND accumulates the per-call `cost_usd` into the
`lancelot.context.cost_usd` ContextVar (:441-444) — so the shipped
`X-Lancelot-Cost-USD` trailer covers unary sub-LLM calls. But `pool.stream`
(:482) and `_relay_forward` (:579) record NOTHING, so a STREAMED LLM call's
cost/tokens/upstream-TTFT are silently 0 (r2r re-runs tiktoken to recover the
lost number).

Fix (opt-in, byte-identical when off): `track_usage=True` on
`pool.stream`/`pool.relay`. As chunks flow, retain the LAST SSE `data:` frame;
on clean end, parse it and run the EXISTING `extract_tokens` (3 provider shapes
— NO tiktoken, NO new dep), compute cost via the same rate path
`_emit_upstream` uses, record the `upstream.*` token/cost metrics, accumulate
into the `cost_usd` ContextVar, and record the upstream-TTFT (first chunk).
Default `track_usage=False` -> NO frame retention/scan (zero overhead).
Provider-reported usage only — no usage in the terminal frame -> record nothing.

Cardinal rule #2: streaming feature -> REAL-SERVER tests (subprocess uvicorn +
HTTP), exercising the relay path the bug lives on. The handler exposes the
post-stream `cost_usd` ContextVar via a response header so the test (a real
HTTP client) reads the accumulated number off the wire. Mirrors
tests/test_m16_relay_cancel.py.
"""
import socket
import subprocess
import sys
import time

import pytest

httpx = pytest.importorskip("httpx")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _spawn(src: str):
    port = _free_port()
    proc = subprocess.Popen([sys.executable, "-c", src, str(port)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.time() + 12
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited rc={proc.returncode}: "
                               f"{proc.stderr.read().decode()[:2000]}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return proc, port
        except OSError:
            time.sleep(0.05)
    proc.kill()
    raise RuntimeError("server did not start")


def _stop(*procs):
    import signal
    for p in procs:
        p.send_signal(signal.SIGTERM)
    for p in procs:
        try:
            p.communicate(timeout=6)
        except subprocess.TimeoutExpired:
            p.kill()
            p.communicate()


# A relay over a fake OpenAI-style streaming upstream whose TERMINAL SSE frame
# carries a `usage` block (the `stream_options={"include_usage": true}` shape).
# The handler relays with `track_usage=True`; after the stream drains it reads
# the `cost_usd` ContextVar the relay accumulated and returns it (the SECOND
# response on a keep-alive conn so the test can read it cleanly). We expose it
# via a separate /cost endpoint that reads a module global the relay handler
# stashed — ContextVars don't survive across requests, so the relay handler
# copies the post-stream value into a module global.
_RELAY_USAGE = r"""
import sys, lancelot, httpx
from lancelot import upstream_metrics

# Provider rate so cost computes to a known non-zero number.
upstream_metrics.set_rate_table({
    "openai": {"gpt-4o": {"input_per_1k": 1.0, "output_per_1k": 2.0}},
})

# Fake OpenAI streaming upstream: a few content deltas, then a final frame that
# carries usage (prompt_tokens=10, completion_tokens=5), then [DONE].
_BODY = (
    b'data: {"model":"gpt-4o","choices":[{"delta":{"content":"he"}}]}\n\n'
    b'data: {"model":"gpt-4o","choices":[{"delta":{"content":"llo"}}]}\n\n'
    b'data: {"model":"gpt-4o","choices":[],'
    b'"usage":{"prompt_tokens":10,"completion_tokens":5}}\n\n'
    b'data: [DONE]\n\n'
)

def _handler(request):
    return httpx.Response(200, content=_BODY,
                          headers={"content-type": "text/event-stream"})

pool = lancelot.llm.TransportPool(transport=httpx.MockTransport(_handler))

app = lancelot.App()

# Module global the relay handler stashes the post-stream cost into (a
# ContextVar does not survive to the NEXT request).
LAST = {"cost": None}

@app.stream("/relay")
async def relay(request: lancelot.Request):
    from lancelot.context import cost_usd
    async with pool.relay("POST", "https://api.openai.com/v1/chat/completions",
                          request=request, track_usage=True) as upstream:
        async for chunk in upstream:
            yield chunk
    # Stream drained -> the relay accumulated cost into the cost_usd ContextVar.
    LAST["cost"] = cost_usd.get()

@app.get("/cost")
async def cost(request: lancelot.Request):
    return {"cost": LAST["cost"]}

import uvicorn
uvicorn.run(app.asgi(), host="127.0.0.1", port=int(sys.argv[1]),
            log_level="warning", access_log=False, loop="uvloop", http="httptools")
"""


# Same upstream/handler but track_usage is OFF (the default) -> cost must stay 0
# (no frame scan, byte-identical relay).
_RELAY_NO_TRACK = r"""
import sys, lancelot, httpx
from lancelot import upstream_metrics
upstream_metrics.set_rate_table({
    "openai": {"gpt-4o": {"input_per_1k": 1.0, "output_per_1k": 2.0}},
})
_BODY = (
    b'data: {"model":"gpt-4o","choices":[{"delta":{"content":"he"}}]}\n\n'
    b'data: {"model":"gpt-4o","choices":[],'
    b'"usage":{"prompt_tokens":10,"completion_tokens":5}}\n\n'
    b'data: [DONE]\n\n'
)
def _handler(request):
    return httpx.Response(200, content=_BODY,
                          headers={"content-type": "text/event-stream"})
pool = lancelot.llm.TransportPool(transport=httpx.MockTransport(_handler))
app = lancelot.App()
LAST = {"cost": None, "body": None}
@app.stream("/relay")
async def relay(request: lancelot.Request):
    from lancelot.context import cost_usd
    out = []
    async with pool.relay("POST", "https://api.openai.com/v1/chat/completions",
                          request=request) as upstream:  # track_usage defaults OFF
        async for chunk in upstream:
            out.append(chunk)
            yield chunk
    LAST["cost"] = cost_usd.get()
    LAST["body"] = b"".join(out)
@app.get("/cost")
async def cost(request: lancelot.Request):
    return {"cost": LAST["cost"], "len": len(LAST["body"] or b"")}
import uvicorn
uvicorn.run(app.asgi(), host="127.0.0.1", port=int(sys.argv[1]),
            log_level="warning", access_log=False, loop="uvloop", http="httptools")
"""


# track_usage on, but the upstream NEVER reports usage -> cost must stay 0 (we
# do not guess; provider-reported usage only).
_RELAY_NO_USAGE = r"""
import sys, lancelot, httpx
from lancelot import upstream_metrics
upstream_metrics.set_rate_table({
    "openai": {"gpt-4o": {"input_per_1k": 1.0, "output_per_1k": 2.0}},
})
_BODY = (
    b'data: {"model":"gpt-4o","choices":[{"delta":{"content":"he"}}]}\n\n'
    b'data: {"model":"gpt-4o","choices":[{"delta":{"content":"llo"}}]}\n\n'
    b'data: [DONE]\n\n'
)
def _handler(request):
    return httpx.Response(200, content=_BODY,
                          headers={"content-type": "text/event-stream"})
pool = lancelot.llm.TransportPool(transport=httpx.MockTransport(_handler))
app = lancelot.App()
LAST = {"cost": None}
@app.stream("/relay")
async def relay(request: lancelot.Request):
    from lancelot.context import cost_usd
    async with pool.relay("POST", "https://api.openai.com/v1/chat/completions",
                          request=request, track_usage=True) as upstream:
        async for chunk in upstream:
            yield chunk
    LAST["cost"] = cost_usd.get()
@app.get("/cost")
async def cost(request: lancelot.Request):
    return {"cost": LAST["cost"]}
import uvicorn
uvicorn.run(app.asgi(), host="127.0.0.1", port=int(sys.argv[1]),
            log_level="warning", access_log=False, loop="uvloop", http="httptools")
"""


def test_stream_track_usage_accumulates_cost():
    """A `pool.relay(..., track_usage=True)` over a fake OpenAI stream whose
    terminal frame carries `usage` -> the relay parses it, computes cost via the
    SAME rate path the unary `_emit_upstream` uses, and accumulates it into the
    `cost_usd` ContextVar. cost = 10/1000*1.0 + 5/1000*2.0 = 0.01 + 0.01 = 0.02.
    """
    proc, port = _spawn(_RELAY_USAGE)
    try:
        # Drive the stream to completion (drains the relay -> usage accounted).
        r = httpx.get(f"http://127.0.0.1:{port}/relay", timeout=10)
        assert r.status_code == 200, r.text
        assert b"[DONE]" in r.content, r.content
        # Read the accumulated cost the relay stashed.
        c = httpx.get(f"http://127.0.0.1:{port}/cost", timeout=10).json()
        assert c["cost"] is not None, "relay never accounted usage"
        assert abs(c["cost"] - 0.02) < 1e-9, (
            f"streamed cost not accumulated correctly: {c['cost']} (want 0.02)")
    finally:
        _stop(proc)


def test_stream_track_usage_off_zero_overhead():
    """Default (track_usage off) -> the relay forwards every byte unchanged and
    accumulates NO cost (no frame scan). Byte-identical to today's relay."""
    proc, port = _spawn(_RELAY_NO_TRACK)
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/relay", timeout=10)
        assert r.status_code == 200, r.text
        # All upstream bytes forwarded verbatim (incl. the usage frame + [DONE]).
        assert b'"usage"' in r.content and b"[DONE]" in r.content, r.content
        forwarded_len = len(r.content)
        c = httpx.get(f"http://127.0.0.1:{port}/cost", timeout=10).json()
        assert c["cost"] == 0.0, (
            f"track_usage OFF must not accumulate cost, got {c['cost']}")
        assert c["len"] == forwarded_len, (
            "relay forwarded body differs from what client received")
    finally:
        _stop(proc)


def test_stream_no_usage_frame_records_nothing():
    """track_usage on but the upstream never reports usage -> cost stays 0 (we
    record provider-reported usage only; we do NOT guess via tiktoken)."""
    proc, port = _spawn(_RELAY_NO_USAGE)
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/relay", timeout=10)
        assert r.status_code == 200, r.text
        c = httpx.get(f"http://127.0.0.1:{port}/cost", timeout=10).json()
        assert c["cost"] == 0.0, (
            f"no upstream usage -> must record nothing, got {c['cost']}")
    finally:
        _stop(proc)


# ---- in-process unit tests for the frame scanner (chunk-boundary + shapes) --

def test_scan_last_data_frame_split_across_chunks():
    """The SSE frame scanner buffers partial trailing bytes: a usage frame that
    arrives split across two byte chunks is still recovered intact."""
    from lancelot.llm import _scan_last_data_frame

    pending, last = b"", None
    # First chunk ends mid-frame (no terminating \n\n yet).
    pending, last = _scan_last_data_frame(
        pending, b'data: {"usage":{"prompt_tokens":3,', last)
    assert last is None, "incomplete frame must not be retained yet"
    # Second chunk completes the frame.
    pending, last = _scan_last_data_frame(
        pending, b'"completion_tokens":7}}\n\n', last)
    import json
    assert json.loads(last) == {"usage": {"prompt_tokens": 3,
                                          "completion_tokens": 7}}, last


def test_scan_retains_last_usage_frame_not_done_sentinel():
    """OpenAI shape: content deltas, then a usage frame, then `[DONE]`. The
    scanner retains the USAGE frame (the cheap `usage` substring gate skips
    deltas and the `[DONE]` sentinel)."""
    from lancelot.llm import _scan_last_data_frame

    pending, last = b"", None
    body = (b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            b'data: {"choices":[],"usage":{"prompt_tokens":10,'
            b'"completion_tokens":5}}\n\n'
            b'data: [DONE]\n\n')
    pending, last = _scan_last_data_frame(pending, body, last)
    import json
    parsed = json.loads(last)
    assert parsed["usage"] == {"prompt_tokens": 10, "completion_tokens": 5}, last


def test_scan_anthropic_message_delta_shape():
    """Anthropic streams usage in a `message_delta` event's data frame
    (`usage.output_tokens`); extract_tokens reads it."""
    from lancelot.llm import _scan_last_data_frame
    from lancelot.upstream_metrics import extract_tokens
    import json

    pending, last = b"", None
    body = (b'event: message_delta\n'
            b'data: {"type":"message_delta","usage":{"output_tokens":42}}\n\n')
    pending, last = _scan_last_data_frame(pending, body, last)
    tin, tout = extract_tokens(json.loads(last))
    assert (tin, tout) == (0, 42), (tin, tout)
