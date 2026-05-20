"""M2-P3 — auto OTel GenAI spans + cost event. Highest-leverage AI
primitive: every product re-implements token capture, per-model price
table, USD computation (`PERPLEXITY_API_REPLICA_CODE:216-234,1399`;
`GROK_PRODUCT_CODE:2477-2480`; `LAMINAR_PRODUCT_CODE:1152-1186` is the
entire product). Lancelot ships it free.

OTel `gen_ai` semconv: attributes prefixed `gen_ai.*`. Span name follows
`{operation} {model}` convention. Cost event has provider/model/tokens/
usd_cost. Lancelot does NOT depend on opentelemetry at runtime — it
emits to a tiny pluggable sink so users wire Langfuse / Phoenix / OTel
exporter / nothing themselves."""
import asyncio


def test_llm_request_emits_cost_event_with_token_breakdown():
    """A pool.request() to an OpenAI-shaped endpoint auto-parses the
    response usage block + emits a Cost event with tokens + usd."""
    import httpx
    import lancelot
    from lancelot.telemetry import Event, get_sink, set_sink

    events: list[Event] = []
    set_sink(events.append)

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": "chatcmpl-x",
            "model": "gpt-4o-mini-2024-07-18",
            "choices": [{"message": {"content": "hi"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                      "total_tokens": 15,
                      "prompt_tokens_details": {"cached_tokens": 2}},
        })

    pool = lancelot.llm.TransportPool(
        transport=httpx.MockTransport(handler), max_retries=0)

    async def main():
        await pool.request("POST", "https://api.openai.com/v1/chat/completions",
                           json={"model": "gpt-4o-mini-2024-07-18",
                                 "messages": [{"role": "user", "content": "hi"}]})
        await pool.aclose()

    try:
        asyncio.run(main())
        cost = [e for e in events if e.kind == "gen_ai.cost"]
        assert len(cost) == 1, events
        ev = cost[0]
        # OTel GenAI semconv attribute names
        assert ev.attrs["gen_ai.usage.input_tokens"] == 10
        assert ev.attrs["gen_ai.usage.output_tokens"] == 5
        assert ev.attrs["gen_ai.usage.cached_input_tokens"] == 2
        assert ev.attrs["gen_ai.response.model"] == "gpt-4o-mini-2024-07-18"
        # Cost calculated against the built-in price table
        assert ev.attrs["gen_ai.cost.usd"] > 0
    finally:
        set_sink(None)


def test_cost_event_zero_if_unknown_model():
    """Unknown model => emit the event with usd=None, never invent a
    price. (No-bullshit: don't fake numbers.)"""
    import httpx
    import lancelot
    from lancelot.telemetry import set_sink

    events = []
    set_sink(events.append)

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "model": "totally-made-up-model",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                      "total_tokens": 2}})

    pool = lancelot.llm.TransportPool(
        transport=httpx.MockTransport(handler), max_retries=0)

    async def main():
        await pool.request("POST", "https://api.x/v1/chat", json={})
        await pool.aclose()

    try:
        asyncio.run(main())
        cost = [e for e in events if e.kind == "gen_ai.cost"]
        assert cost[0].attrs.get("gen_ai.cost.usd") is None
    finally:
        set_sink(None)


def test_telemetry_sink_is_noop_when_unset():
    """The default sink is None → lancelot.llm calls do NOT emit
    anything; no perf penalty, no Phoenix/Langfuse dep needed."""
    import httpx
    import lancelot
    from lancelot.telemetry import set_sink

    set_sink(None)
    pool = lancelot.llm.TransportPool(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"usage": {}})),
        max_retries=0)
    # Should not raise; no events captured anywhere.
    asyncio.run(pool.request("POST", "https://api.x/v1/chat", json={}))
    asyncio.run(pool.aclose())
