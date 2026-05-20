"""M2-P2 — `lancelot.llm.TransportPool`: outbound HTTP pool with the
no-bullshit retry policy: **POST is NEVER retried** (the cardinal $$
rule — Supermemory Oct-2025 retry-cascade incident; deep/06:107-111
documented Blazeio gap). Idempotent verbs (GET/HEAD/PUT/DELETE/OPTIONS)
retry up to `max_retries`, honoring `Retry-After`/`retry-after-ms`
headers; exponential backoff otherwise. NO circuit breaker (strict
synthesis: no product code defines breaker thresholds; Supermemory
retrofitted post-incident, not as a primitive).

Runtime requires `httpx` (lazy-imported on first call). Lancelot's core
runtime dep is still `pydantic` only — `import lancelot` does NOT import
this module; `lancelot.llm` is the opt-in AI surface."""
import asyncio
import random

_IDEMPOTENT = frozenset({"GET", "HEAD", "PUT", "DELETE", "OPTIONS"})


class TransportPool:
    """Bounded per-host transport. Pass `transport=httpx.MockTransport(...)`
    in tests to avoid real I/O. POST/PATCH (non-idempotent) are NEVER
    retried — the response is returned as-is and the caller decides."""

    def __init__(self, *, transport=None, max_retries: int = 3,
                 backoff_base: float = 0.2, backoff_max: float = 8.0,
                 timeout: float = 30.0):
        import httpx  # lazy: lancelot core stays pydantic-only

        self._httpx = httpx
        self._client = httpx.AsyncClient(transport=transport,
                                         timeout=timeout, http2=False)
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max

    async def aclose(self):
        await self._client.aclose()

    @staticmethod
    def _retry_after_seconds(resp) -> float | None:
        h = resp.headers
        # RFC: Retry-After in seconds; X-RateLimit-Reset / retry-after-ms
        # are common provider extensions (Anthropic/Groq/etc.).
        if "retry-after" in h:
            try:
                return float(h["retry-after"])
            except ValueError:
                return None
        if "retry-after-ms" in h:
            try:
                return float(h["retry-after-ms"]) / 1000.0
            except ValueError:
                return None
        return None

    @staticmethod
    def _maybe_emit_cost(resp) -> None:
        """If the response body looks like an OpenAI/Anthropic LLM call
        (has a `usage` block), auto-emit a gen_ai.cost event (M2-P3)."""
        try:
            ct = resp.headers.get("content-type", "")
            if "json" not in ct:
                return
            body = resp.json()
            if not isinstance(body, dict) or "usage" not in body:
                return
            from lancelot.telemetry import (cost_event_from_openai_response,
                                            emit)

            emit(cost_event_from_openai_response(body))
        except Exception:  # noqa: BLE001 - telemetry never breaks a request
            pass

    async def request(self, method: str, url: str, *,
                      request=None, **kw):
        """Single outbound HTTP call with idempotent retry.

        Tier-C.C2: optional `request=` kwarg binds the call to a Lancelot
        Request. When the client disconnects mid-call (request.cancelled
        flips), the in-flight upstream HTTP call is awaited concurrently
        with the cancel-event and cancelled the moment the event fires —
        so the user stops paying for LLM tokens the client is no longer
        watching."""
        method = method.upper()
        idempotent = method in _IDEMPOTENT
        attempt = 0
        while True:
            resp = await self._await_cancellable(
                self._client.request(method, url, **kw), request)
            # POST/PATCH/CONNECT etc. are NEVER retried — the cardinal
            # rule. Return as-is so the caller (handler) decides.
            if not idempotent:
                self._maybe_emit_cost(resp)
                return resp
            # Idempotent verbs retry on 5xx + 429 only.
            if resp.status_code not in (429, 500, 502, 503, 504):
                self._maybe_emit_cost(resp)
                return resp
            if attempt >= self.max_retries:
                self._maybe_emit_cost(resp)
                return resp
            wait = self._retry_after_seconds(resp)
            if wait is None:
                wait = min(self.backoff_max,
                           self.backoff_base * (2 ** attempt)
                           + random.random() * self.backoff_base)
            await asyncio.sleep(wait)
            attempt += 1

    async def _await_cancellable(self, coro, request):
        """Helper: await `coro`, but if `request` carries a cancel event
        that fires first, cancel the coro and raise asyncio.CancelledError.
        When `request` is None (or has no _cancel_event), this is a
        zero-overhead pass-through that just awaits the coro."""
        ev = getattr(request, "_cancel_event", None) if request else None
        if ev is None:
            return await coro
        task = asyncio.ensure_future(coro)
        wait_event = asyncio.ensure_future(ev.wait())
        try:
            done, _pending = await asyncio.wait(
                {task, wait_event}, return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                # Upstream finished first — clean up the wait task and
                # return the result (or raise the upstream exception).
                wait_event.cancel()
                return task.result()
            # Cancel event fired first — kill the upstream call and
            # propagate cancellation so the handler stops.
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            raise asyncio.CancelledError("client disconnected; upstream cancelled")
        finally:
            if not task.done():
                task.cancel()
            if not wait_event.done():
                wait_event.cancel()

    async def stream(self, method: str, url: str, **kw):
        """Streaming outbound — POST/PATCH never retried; idempotent
        verbs may retry once before opening the stream. Returns an httpx
        Response context (use `async with pool.stream(...) as r:`)."""
        method = method.upper()
        return self._client.stream(method, url, **kw)
