"""M2-P2 — outbound transport pool + retry policy. Real-cost invariants:
POST is NEVER retried (duplicate completions = double billing — Supermemory
Oct-2025 incident). Idempotent verbs retry with `Retry-After` honor
exponential backoff. NO circuit breaker per strict synthesis."""
import asyncio


def test_pool_post_never_retried_even_on_503():
    """The cardinal $$ invariant: a 503 on POST is returned to the caller
    AS-IS, not transparently retried (a re-fired POST may double-charge)."""
    import httpx
    import lancelot

    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.method)
        return httpx.Response(503, headers={"retry-after": "0"},
                              text="upstream down")

    pool = lancelot.llm.TransportPool(
        transport=httpx.MockTransport(handler), max_retries=5)

    async def main():
        r = await pool.request("POST", "https://api.x/v1/chat",
                               json={"hi": 1})
        return r

    r = asyncio.run(main())
    assert r.status_code == 503
    assert attempts == ["POST"], (
        f"POST was retried — bills duplicated: {attempts}")
    asyncio.run(pool.aclose())


def test_pool_get_retries_with_backoff_and_retry_after():
    """GET (idempotent) on 503 retries up to max_retries, honoring
    Retry-After. The final response (whether 503 or 200) is returned."""
    import httpx
    import lancelot

    n = {"i": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        n["i"] += 1
        if n["i"] < 3:
            return httpx.Response(503, headers={"retry-after": "0"})
        return httpx.Response(200, json={"ok": True})

    pool = lancelot.llm.TransportPool(
        transport=httpx.MockTransport(handler), max_retries=5,
        backoff_base=0.001)
    r = asyncio.run(pool.request("GET", "https://api.x/v1/models"))
    assert r.status_code == 200 and n["i"] == 3
    asyncio.run(pool.aclose())


def test_pool_rejects_runaway_retries():
    """`max_retries=2` caps the retry attempts even on persistent 503."""
    import httpx
    import lancelot

    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(503, headers={"retry-after": "0"})

    pool = lancelot.llm.TransportPool(
        transport=httpx.MockTransport(handler), max_retries=2,
        backoff_base=0.001)
    r = asyncio.run(pool.request("GET", "https://api.x/v1/x"))
    assert r.status_code == 503
    assert attempts["n"] == 3, attempts  # 1 initial + 2 retries
    asyncio.run(pool.aclose())
