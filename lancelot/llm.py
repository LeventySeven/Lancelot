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
import time as _time_mod

_IDEMPOTENT = frozenset({"GET", "HEAD", "PUT", "DELETE", "OPTIONS"})

# M17 P7 review: bound the SSE usage-scan carry-over buffer. A real usage frame
# is tiny; this caps memory for a track_usage upstream that never emits a frame
# boundary (max_stream_silence guards silence, not byte-volume).
_SSE_SCAN_CAP = 65536


class UpstreamIdleTimeout(Exception):
    """An upstream stream produced no bytes for the configured idle window —
    the connection stalled (no data, no close). Raised by
    TransportPool.idle_guarded_chunks so a zombie stream can't hold a
    connection open (and block graceful drain) forever."""

    def __init__(self, idle_secs: float):
        super().__init__(f"upstream stream idle for {idle_secs}s")
        self.idle_secs = idle_secs


async def _race_chunk_vs_cancel(it, cancel_event, idle):
    """M16 P8: pull the next upstream chunk (`it.__anext__()`), but if
    `cancel_event` fires first (the client disconnected), cancel the pull and
    raise asyncio.CancelledError so the relay/forward unwinds and the enclosing
    `async with pool.stream(...)` aclose()s the upstream. Honors the per-stream
    idle window (idle>0 → TimeoutError → UpstreamIdleTimeout upstream).

    Same FIRST_COMPLETED dual-task shape as `TransportPool._await_cancellable`
    (the unary cancel path) — re-used here for the streaming relay so a
    disconnect stops paying for unwatched tokens on the relay path too."""
    if cancel_event.is_set():
        raise asyncio.CancelledError("client disconnected; upstream cancelled")
    pull = asyncio.ensure_future(it.__anext__())
    waiter = asyncio.ensure_future(cancel_event.wait())
    timeout = idle if (idle and idle > 0) else None
    try:
        done, _pending = await asyncio.wait(
            {pull, waiter}, timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED)
        if not done:
            # Neither fired within the idle window → idle timeout.
            raise asyncio.TimeoutError
        if pull in done:
            waiter.cancel()
            return pull.result()       # chunk (or re-raises StopAsyncIteration)
        # Cancel event fired first → abort the pull and propagate cancellation.
        pull.cancel()
        try:
            await pull
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        raise asyncio.CancelledError("client disconnected; upstream cancelled")
    finally:
        if not pull.done():
            pull.cancel()
        if not waiter.done():
            waiter.cancel()


def _scan_last_data_frame(pending: bytes, chunk: bytes, last_frame):
    """M17 P7: feed `chunk` into the running SSE-frame scanner. SSE frames are
    `\\n\\n`-terminated; chunks need not align to frame boundaries, so we buffer
    incomplete trailing bytes in `pending`. For each COMPLETE frame we pull the
    `data:` line(s) payload; we retain (as `last_frame`) the most recent data
    payload whose raw bytes contain `b"usage"` — a cheap substring gate so we
    JSON-parse at MOST one frame at stream end (not every frame). Returns
    `(new_pending, new_last_frame)`. Provider-reported usage only: a stream with
    no `usage` anywhere leaves `last_frame` None and nothing is recorded.

    Handles the three provider shapes the streamed usage frame takes:
      * OpenAI `stream_options={"include_usage": true}`: a near-terminal
        `data: {...,"usage":{"prompt_tokens":..,"completion_tokens":..}}` frame
        (then `data: [DONE]`).
      * Anthropic `message_delta`: `data: {"usage":{"output_tokens":..}}`.
      * Google: `data: {...,"usageMetadata":{...}}`.
    """
    buf = pending + chunk
    # Process only complete frames; keep the (possibly partial) tail in pending.
    while True:
        idx = buf.find(b"\n\n")
        if idx < 0:
            break
        frame = buf[:idx]
        buf = buf[idx + 2:]
        # Concatenate the payloads of all `data:` lines in this frame (SSE allows
        # multi-line data; LLM providers use one, but be correct).
        payload_parts = []
        for line in frame.split(b"\n"):
            if line.startswith(b"data:"):
                payload_parts.append(line[5:].lstrip(b" "))
        if not payload_parts:
            continue
        payload = b"\n".join(payload_parts)
        # Cheap gate: only frames mentioning usage are candidates (avoids
        # parsing every content delta). The LAST such frame wins.
        if b"usage" in payload or b"usageMetadata" in payload:
            last_frame = payload
    # Bound carry-over: keep only the trailing window (where the next frame
    # boundary will land) so a no-"\n\n" stream can't accumulate unbounded.
    if len(buf) > _SSE_SCAN_CAP:
        buf = buf[-_SSE_SCAN_CAP:]
    return buf, last_frame


class _CancellableStreamCtx:
    """M16 P8: async-CM wrapper for `client.stream(...)` that (a) tags the
    entered httpx response with the request's `_cancel_event` so
    `idle_guarded_chunks`/`relay` race each chunk against a client disconnect,
    and (b) races the connection-OPEN itself against the event (so a disconnect
    during connect cancels too) and aclose()s the upstream on exit. Built when
    `pool.stream(..., request=req)` carries a live cancel event OR when
    `track_usage=True` (M17 P7) needs the response tagged for cost accounting —
    the plain no-request/no-track path returns the bare httpx CM unchanged
    (zero overhead).

    M17 P7: `track_url` (the upstream URL) is tagged onto the response as
    `_lancelot_track_usage` so `idle_guarded_chunks` records streamed
    cost/tokens/TTFT for it. When `event` is None (track_usage without a bound
    request) there is no disconnect to race — the open is awaited directly."""

    __slots__ = ("_pool", "_cm", "_event", "_resp", "_track_url")

    def __init__(self, pool, cm, event, track_url=None):
        self._pool = pool
        self._cm = cm
        self._event = event
        self._resp = None
        self._track_url = track_url

    async def __aenter__(self):
        if self._event is None:
            # No client-disconnect event to race (track_usage-only path):
            # await the open directly, no extra task allocations.
            self._resp = await self._cm.__aenter__()
        else:
            if self._event.is_set():
                raise asyncio.CancelledError(
                    "client disconnected; upstream cancelled")
            # Race the connection-open against the disconnect (a slow connect to
            # a dead-client request shouldn't block).
            open_task = asyncio.ensure_future(self._cm.__aenter__())
            waiter = asyncio.ensure_future(self._event.wait())
            try:
                done, _ = await asyncio.wait(
                    {open_task, waiter}, return_when=asyncio.FIRST_COMPLETED)
                if open_task in done:
                    waiter.cancel()
                    self._resp = open_task.result()
                else:
                    # Disconnected before connect finished — unwind the open.
                    open_task.cancel()
                    try:
                        await open_task
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
                    # Best-effort close of the underlying CM if it half-opened.
                    try:
                        await self._cm.__aexit__(None, None, None)
                    except Exception:  # noqa: BLE001
                        pass
                    raise asyncio.CancelledError(
                        "client disconnected; upstream cancelled")
            finally:
                if not waiter.done():
                    waiter.cancel()
            # Tag the response so the chunk-iterators find the event.
            try:
                self._resp._lancelot_cancel_event = self._event
            except Exception:  # noqa: BLE001
                pass
        # M17 P7: tag the upstream URL so idle_guarded_chunks accounts usage.
        if self._track_url is not None:
            try:
                self._resp._lancelot_track_usage = self._track_url
            except Exception:  # noqa: BLE001
                pass
        return self._resp

    async def __aexit__(self, exc_type, exc, tb):
        return await self._cm.__aexit__(exc_type, exc, tb)


class _TokenBucket:
    """Minimal asyncio token bucket (calls/second). asyncio is single-threaded
    cooperative — state only mutates synchronously between awaits, so no lock.
    Starts full (one burst of `rate`), refills at `rate`/s capped at `rate`."""

    __slots__ = ("_rate", "_tokens", "_last")

    def __init__(self, rate: float):
        if rate <= 0:
            raise ValueError("rate_limit must be > 0")
        self._rate = float(rate)
        self._tokens = float(rate)
        self._last = _time_mod.monotonic()

    async def acquire(self) -> None:
        while True:
            now = _time_mod.monotonic()
            self._tokens = min(self._rate,
                               self._tokens + (now - self._last) * self._rate)
            self._last = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            await asyncio.sleep((1.0 - self._tokens) / self._rate)


class TransportPool:
    """Bounded per-host transport. Pass `transport=httpx.MockTransport(...)`
    in tests to avoid real I/O. POST/PATCH (non-idempotent) are NEVER
    retried — the response is returned as-is and the caller decides."""

    def __init__(self, *, transport=None, max_retries: int = 3,
                 backoff_base: float = 0.2, backoff_max: float = 8.0,
                 timeout: float = 30.0,
                 retry_budget_ms: int | None = None,
                 max_connections: int | None = None,
                 max_keepalive_connections: int | None = None,
                 keepalive_expiry: float | None = None,
                 block_private_ips: bool = False,
                 rate_limit: float | None = None,
                 mtls_cert: str | None = None,
                 mtls_key: str | None = None,
                 mtls_ca: str | None = None):
        """M7 T3.4 mTLS args (Infisical PKI pattern):
            mtls_cert: client certificate path (PEM)
            mtls_key:  client key path (PEM)
            mtls_ca:   optional custom CA bundle (PEM) for verifying
                       the upstream's cert. None = system trust store.
        All three default off. Cert + key together are required for
        mTLS (one without the other raises ValueError). Pass an
        already-built httpx.AsyncClient via `transport=` if you need
        an exotic auth method that we don't model.
        """
        import httpx  # lazy: lancelot core stays pydantic-only

        # mTLS argument validation: cert + key are paired.
        if (mtls_cert is None) != (mtls_key is None):
            raise ValueError(
                "TransportPool: mtls_cert and mtls_key must be set "
                "together (one without the other is meaningless)")

        # TP1: connection-pool knobs (Turbopuffer/Bifrost pattern). httpx's
        # silent defaults (100/20/5s) cap fanout + multi-model routing under
        # load; surface them as kwargs + env. The httpx connection pool IS the
        # outbound transport path, so these are byte-path knobs.
        import os as _os

        def _env_int(name, default):
            try:
                return int(_os.environ[name])
            except (KeyError, ValueError):
                return default

        def _env_float(name, default):
            try:
                return float(_os.environ[name])
            except (KeyError, ValueError):
                return default

        mc = max_connections if max_connections is not None else \
            _env_int("LANCELOT_UPSTREAM_MAX_CONNECTIONS", 100)
        mk = max_keepalive_connections if max_keepalive_connections is not None \
            else _env_int("LANCELOT_UPSTREAM_MAX_KEEPALIVE", 20)
        ke = keepalive_expiry if keepalive_expiry is not None else \
            _env_float("LANCELOT_UPSTREAM_KEEPALIVE_EXPIRY", 5.0)
        self.limits = httpx.Limits(max_connections=mc,
                                   max_keepalive_connections=mk,
                                   keepalive_expiry=ke)

        client_kw = {"transport": transport, "timeout": timeout,
                     "http2": False, "limits": self.limits}
        if mtls_cert and mtls_key:
            # httpx accepts a (cert, key) tuple as the cert= kwarg.
            client_kw["cert"] = (mtls_cert, mtls_key)
        if mtls_ca:
            client_kw["verify"] = mtls_ca

        self._httpx = httpx
        self._client = httpx.AsyncClient(**client_kw)
        self._mtls_cert = mtls_cert
        self._mtls_key = mtls_key
        self._mtls_ca = mtls_ca
        self._client_kw_template = {
            k: v for k, v in client_kw.items() if k != "transport"
        }
        self._transport = transport
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        # T1 (Portkey MAX_RETRY_LIMIT_MS): wall-clock cap on the whole retry
        # chain so a runaway Retry-After (e.g. 3×30s) can't hold an outbound
        # call open far longer than the operator expects — bounds the call's
        # contribution to graceful-drain + per-route-timeout budgets. Default
        # 60 s; env LANCELOT_UPSTREAM_RETRY_BUDGET_MS; None disables the cap.
        if retry_budget_ms is None:
            try:
                retry_budget_ms = int(
                    _os.environ.get("LANCELOT_UPSTREAM_RETRY_BUDGET_MS", 60000))
            except ValueError:
                retry_budget_ms = 60000
        self.retry_budget_ms = retry_budget_ms
        # Streaming LLM responses legitimately outlive the unary `timeout` — a
        # steady token feed must NOT be severed by a total/read deadline (a 30s
        # unary read timeout kills any stream with a >30s generation). Keep
        # connect/write/pool bounded but DISABLE the read deadline for streams;
        # runaway streams are bounded instead by client-disconnect cancellation
        # (C2). Stolen from Bifrost's separate streaming-client timeout policy.
        self._stream_timeout = httpx.Timeout(timeout, read=None)
        # SSRF guard (opt-in): validate user-supplied outbound URLs before any
        # I/O. Default OFF so fixed-provider LLM calls aren't resolved/blocked.
        self._block_private_ips = block_private_ips
        # Per-stream inter-chunk idle timeout (Bifrost NewIdleTimeoutReader):
        # close a stalled upstream SSE. 0 = off (default; don't silently break
        # existing long streams). LANCELOT_UPSTREAM_STREAM_IDLE_SECS overrides.
        try:
            self._stream_idle_secs = float(
                _os.environ.get("LANCELOT_UPSTREAM_STREAM_IDLE_SECS", "0"))
        except ValueError:
            self._stream_idle_secs = 0.0
        # Outbound rate cap (calls/s token bucket) so bursty fanout doesn't
        # explode a shared API key (Hatchet pattern). None = unlimited (default).
        if rate_limit is None:
            try:
                _rl = float(_os.environ.get("LANCELOT_UPSTREAM_RATE_LIMIT", "0"))
                rate_limit = _rl if _rl > 0 else None
            except ValueError:
                rate_limit = None
        self._bucket = _TokenBucket(rate_limit) if rate_limit else None

    async def aclose(self):
        await self._client.aclose()

    async def reload_mtls(self) -> None:
        """M7 T3.4: re-open the underlying client with the SAME cert
        paths so a SIGHUP-triggered cert rotation (the operator
        replaced the cert/key files on disk) takes effect on the
        next outbound call.

        In-flight calls on the existing client KEEP USING the old
        cert (no mid-stream cert swap — httpx loads on connect, the
        old TLS state lives on the existing connection). When the
        old client closes, new connections pick up the rotated cert.
        Bounded clean-up: we close the OLD client after rebuilding.
        """
        if not (self._mtls_cert and self._mtls_key):
            return  # no mTLS configured; nothing to reload
        old = self._client
        client_kw = {
            "transport": self._transport,
            **self._client_kw_template,
        }
        self._client = self._httpx.AsyncClient(**client_kw)
        try:
            await old.aclose()
        except Exception:  # noqa: BLE001
            pass

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
        # T3.3: Azure OpenAI (a top-3 backend) sends its own header; without
        # this it silently fell back to exponential backoff and over-waited.
        if "x-ms-retry-after-ms" in h:
            try:
                return float(h["x-ms-retry-after-ms"]) / 1000.0
            except ValueError:
                return None
        return None

    @staticmethod
    def _is_retryable(resp) -> bool:
        """T3.5: honor the provider's `x-should-retry` hint (Anthropic/OpenAI
        send it to override the status heuristic) — `false` means do NOT retry a
        429/5xx, `true` means retry an otherwise-final status. Absent = the
        default status-based decision, so behavior is unchanged with no header."""
        hint = resp.headers.get("x-should-retry")
        if hint is not None:
            return hint.strip().lower() == "true"
        return resp.status_code in (429, 500, 502, 503, 504)

    def _backoff_wait(self, attempt: int) -> float:
        """T1.9: equal-jitter backoff. The old `base*2**n + base*rand` term was
        dominated by the exponential floor, so a @app.batch fanout of N retriers
        re-fired near-lockstep at a rate-limited LLM (a thundering herd). Spread
        them uniformly across [cap/2, cap] instead — decorrelated, with a floor,
        capped at backoff_max."""
        cap = min(self.backoff_max, self.backoff_base * (2 ** attempt))
        return cap / 2.0 + random.uniform(0.0, cap / 2.0)

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
        watching.

        M7 T1.3 + T2.2: per-call upstream metrics emitted (LLM-aware
        attrs: provider/model/class/tokens/cost). Also accumulates cost
        into the request's `cost_usd` ContextVar — so the parent request
        sees the sub-LLM-call's cost automatically (Sim.ai pattern)."""
        method = method.upper()
        if self._block_private_ips:
            from lancelot.net import assert_safe_url
            assert_safe_url(url)  # raises SSRFError before any network I/O
        if self._bucket is not None:
            await self._bucket.acquire()  # outbound rate cap (calls/s)
        kw = self._inject_traceparent(kw)
        idempotent = method in _IDEMPOTENT
        # TP2: stamp a stable Idempotency-Key on retry-eligible (idempotent)
        # calls so the upstream can dedupe a retry against the original when a
        # response was lost in transit (Turbopuffer/Stainless pattern). Set
        # ONCE here so every retry attempt below reuses the SAME key; a
        # caller-supplied key always wins. POST/PATCH never retry, so no key.
        if idempotent:
            kw = self._inject_idempotency(kw)
        attempt = 0
        import time as _time
        _budget_start = _time.monotonic()
        while True:
            t0 = _time.perf_counter()
            try:
                resp = await self._await_cancellable(
                    self._client.request(method, url, **kw), request)
            except self._httpx.TimeoutException:
                # T2: classify transport-level timeouts (Read/Connect/Pool/
                # Write) into the upstream metric taxonomy (408 → "timeout")
                # so they're visible in lancelot.upstream.* alongside HTTP
                # 408/504. Behavior preserved: we do NOT auto-retry a timeout
                # — propagate to the caller as before.
                try:
                    from lancelot.upstream_metrics import record_upstream
                    record_upstream(url, 408, _time.perf_counter() - t0,
                                    body=None)
                except Exception:  # noqa: BLE001
                    pass
                raise
            elapsed = _time.perf_counter() - t0
            # M7 T1.3 + T2.2: emit per-call upstream metric + accumulate
            # cost into parent's cost_usd ContextVar.
            self._emit_upstream(url, resp, elapsed)
            # POST/PATCH/CONNECT etc. are NEVER retried — the cardinal
            # rule. Return as-is so the caller (handler) decides.
            if not idempotent:
                self._maybe_emit_cost(resp)
                return resp
            # Idempotent verbs retry on 5xx + 429 (or per x-should-retry — T3.5).
            if not self._is_retryable(resp):
                self._maybe_emit_cost(resp)
                return resp
            if attempt >= self.max_retries:
                self._maybe_emit_cost(resp)
                return resp
            wait = self._retry_after_seconds(resp)
            if wait is None:
                wait = self._backoff_wait(attempt)  # T1.9 equal-jitter
            # T1: bail if this sleep would push the total retry wall-clock past
            # the budget — return the last response instead of holding the call
            # open (e.g. a 30s Retry-After under a 1s budget never sleeps).
            if self.retry_budget_ms is not None:
                spent_ms = (_time.monotonic() - _budget_start) * 1000.0
                if spent_ms + wait * 1000.0 > self.retry_budget_ms:
                    self._maybe_emit_cost(resp)
                    return resp
            await asyncio.sleep(wait)
            attempt += 1

    @staticmethod
    def _emit_upstream(url, resp, duration_s):
        """M7 T1.3 + T2.2 metric emit. Best-effort: never raises into
        the user's call path."""
        try:
            body = None
            ct = resp.headers.get("content-type", "")
            if "json" in ct:
                try:
                    body = resp.json()
                except Exception:  # noqa: BLE001
                    body = None
            from lancelot.upstream_metrics import record_upstream
            info = record_upstream(url, resp.status_code, duration_s,
                                    body=body)
            # T2.1 cost ContextVar: accumulate per-call cost into parent
            # request so the X-Lancelot-Cost-USD trailer covers sub-LLM
            # calls automatically.
            if info.get("cost_usd", 0.0) > 0:
                try:
                    from lancelot.context import cost_usd as _cv
                    _cv.set(_cv.get() + info["cost_usd"])
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _record_stream_usage(url, last_frame, ttft_s):
        """M17 P7: streamed twin of `_emit_upstream`. `last_frame` is the raw
        bytes of the terminal SSE `data:` payload that carried usage (or None if
        the stream never reported any). Parse it, run the existing
        `extract_tokens` (provider-reported usage only — NO tiktoken), record the
        `upstream.*` token/cost metrics (status 200 = a completed stream) with
        the upstream-TTFT, and accumulate the per-call cost into the parent's
        `cost_usd` ContextVar so `X-Lancelot-Cost-USD` covers streamed calls.

        Best-effort: never raises into the user's stream. No usage frame (None or
        unparseable / no usage block) -> record nothing (don't guess)."""
        if last_frame is None:
            return
        try:
            import json as _json
            try:
                body = _json.loads(last_frame)
            except Exception:  # noqa: BLE001
                return
            if not isinstance(body, dict):
                return
            from lancelot.upstream_metrics import extract_tokens, record_upstream
            tin, tout = extract_tokens(body)
            if tin <= 0 and tout <= 0:
                return  # provider-reported usage only; nothing to record
            info = record_upstream(url, 200, 0.0, body=body, ttft_s=ttft_s)
            if info.get("cost_usd", 0.0) > 0:
                try:
                    from lancelot.context import cost_usd as _cv
                    _cv.set(_cv.get() + info["cost_usd"])
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass

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

    async def stream(self, method: str, url: str, *, request=None,
                     track_usage: bool = False, **kw):
        """Streaming outbound — POST/PATCH never retried; idempotent
        verbs may retry once before opening the stream. Returns an httpx
        Response context (use `async with pool.stream(...) as r:`).

        M16 P8: optional `request=` binds the stream to a Lancelot Request, the
        streaming twin of the unary `request()` cancel-on-disconnect (C2). When
        the client disconnects mid-stream (`request._cancel_event` fires), the
        upstream chunk iteration is cancelled — so the user stops paying for LLM
        tokens nobody is watching (the exact thing the unary path stops, on the
        relay path). The cancel race is applied at chunk iteration by
        `idle_guarded_chunks`/`relay` (which find the event tagged on the
        response) AND at connection-open here. When `request=None` (default),
        this is a zero-overhead pass-through — byte-identical to before.
        Pure Python: no Rust pump variant (Tier-D — the per-chunk path is
        upstream-rate-limited).

        M17 P7 (opt-in `track_usage=True`): the streamed twin of the unary
        `_emit_upstream` cost/token accounting. `pool.request` records
        `upstream.*` metrics + accumulates the per-call `cost_usd` ContextVar so
        the `X-Lancelot-Cost-USD` trailer covers unary sub-LLM calls; the
        streamed path recorded NOTHING (cost/tokens/upstream-TTFT silently 0).
        With `track_usage=True`, `idle_guarded_chunks` retains the LAST SSE
        `data:` frame as chunks flow, records upstream-TTFT on the first chunk,
        and on clean end parses the retained frame with the existing
        `extract_tokens` (provider-reported usage only — NO tiktoken/new dep),
        computes cost via the SAME rate path `_emit_upstream` uses, records the
        `upstream.*` token/cost metrics, and accumulates into `cost_usd`. The
        flag is carried on the entered response (`_lancelot_track_usage`) so the
        single accounting site is `idle_guarded_chunks` (which both `pool.stream`
        and `pool.relay` iterate). Default off -> NO frame retention/scan
        (zero overhead, byte-identical)."""
        method = method.upper()
        if self._block_private_ips:
            from lancelot.net import assert_safe_url
            assert_safe_url(url)
        kw = self._inject_traceparent(kw)
        kw.setdefault("timeout", self._stream_timeout)  # no read deadline; caller can override
        cm = self._client.stream(method, url, **kw)
        ev = getattr(request, "_cancel_event", None) if request else None
        if ev is None and not track_usage:
            return cm  # zero-overhead pass-through (unchanged)
        return _CancellableStreamCtx(self, cm, ev,
                                     track_url=(url if track_usage else None))

    async def idle_guarded_chunks(self, response, idle_secs: float | None = None):
        """Yield `response.aiter_bytes()` chunks, but raise UpstreamIdleTimeout
        if no chunk arrives within the idle window (defaults to the pool's
        LANCELOT_UPSTREAM_STREAM_IDLE_SECS). idle<=0 → plain pass-through.
        Use as: `async with pool.stream(...) as r:
                     async for chunk in pool.idle_guarded_chunks(r): ...`

        M16 P8: if the stream was opened with `pool.stream(..., request=req)`,
        the response carries a `_cancel_event`; each chunk pull then races that
        event so a client disconnect aborts the upstream iteration (the iterator
        is left suspended ≤1 chunk; the enclosing `async with pool.stream`
        aclose()s the upstream). No-op overhead when no event is bound.

        M17 P7: if the stream was opened with `pool.stream(..., track_usage=True)`
        the response carries `_lancelot_track_usage` (the upstream URL); this
        loop then retains the LAST SSE `data:` frame as chunks flow, records
        upstream-TTFT on the first chunk, and on CLEAN end parses the retained
        frame + records cost/tokens (provider-reported usage only). No frame
        retention/scan when the tag is absent (zero overhead)."""
        idle = self._stream_idle_secs if idle_secs is None else idle_secs
        ev = getattr(response, "_lancelot_cancel_event", None)
        track_url = getattr(response, "_lancelot_track_usage", None)
        it = response.aiter_bytes().__aiter__()
        # M17 P7 usage-tracking state (only touched when track_url is set).
        _u_t0 = None
        _u_ttft = None
        _u_last_frame = None
        _u_pending = b""
        if track_url is not None:
            _u_t0 = _time_mod.perf_counter()
        while True:
            try:
                if ev is not None:
                    chunk = await _race_chunk_vs_cancel(it, ev, idle)
                elif idle > 0:
                    chunk = await asyncio.wait_for(it.__anext__(), timeout=idle)
                else:
                    chunk = await it.__anext__()
            except asyncio.TimeoutError:
                raise UpstreamIdleTimeout(idle) from None
            except StopAsyncIteration:
                # M17 P7: clean end -> account the retained terminal frame.
                if track_url is not None:
                    self._record_stream_usage(track_url, _u_last_frame, _u_ttft)
                return
            if track_url is not None:
                if _u_ttft is None:
                    _u_ttft = _time_mod.perf_counter() - _u_t0
                _u_pending, _u_last_frame = _scan_last_data_frame(
                    _u_pending, chunk, _u_last_frame)
            yield chunk

    def relay(self, method: str, url: str, *, request=None,
              track_usage: bool = False, **kw):
        """M16 P8: upstream->client SSE relay primitive — open the upstream
        stream, forward its chunks (via `idle_guarded_chunks`), cancel on
        client disconnect, and `aclose()` the upstream in `finally`. The spine
        of the inference reverse-proxy archetype (BentoML/r2r/langserve all
        hand-roll open+forward+cleanup).

        An async-context-manager yielding an async-iterator of the upstream's
        chunks; it `aclose()`s the upstream when the `async with` block exits.
        Use it INSIDE an `@app.stream` async-gen handler so iteration happens
        within the block:

            @app.stream("/proxy")
            async def proxy(request):
                async with pool.relay("POST", url, request=request) as upstream:
                    async for chunk in upstream:
                        yield chunk

        Do NOT `return StreamingResponse(upstream)` from inside the `async with`:
        the block exits on `return` and acloses the upstream BEFORE the response
        iterates. For a StreamingResponse, drive `pool.stream(..., request=request)`
        from within your own async-gen instead.

        Pass `request=request` so a client disconnect cancels the upstream (the
        whole point). Pure Python — NO Rust pump variant (Tier-D).

        NOTE: do NOT put `sse=True` on a relay handler — a relay forwards the
        upstream's ALREADY-framed SSE bytes (incl. its terminal `data: [DONE]`),
        so `sse=True` would append a SECOND `[DONE]`. Use a plain `@app.stream`
        (no `sse=`) and `yield` the relay chunks as-is. Set `track_usage=True`
        to record streamed cost/tokens/TTFT (M17 P7).

        M17 P7 (opt-in `track_usage=True`): account the streamed call's
        cost/tokens/upstream-TTFT — the relayed twin of the unary
        `_emit_upstream` accounting. See `pool.stream`'s docstring; the flag is
        forwarded to the underlying stream and the accounting happens in
        `idle_guarded_chunks`. Default off -> byte-identical relay (no scan)."""
        import contextlib as _cl

        @_cl.asynccontextmanager
        async def _cm():
            forward = self._relay_forward(method, url, request=request,
                                          track_usage=track_usage, **kw)
            try:
                yield forward
            finally:
                # Ensure the upstream is torn down even if the consumer broke
                # out early / returned a StreamingResponse without iterating to
                # exhaustion (aclose drives the inner `async with` __aexit__).
                await forward.aclose()

        return _cm()

    async def _relay_forward(self, method, url, *, request=None,
                             track_usage: bool = False, **kw):
        """The relay's forwarding async-generator: open the upstream stream,
        yield each chunk, aclose() the upstream in `finally`. Cancellation
        (client disconnect via `request=`) propagates through `aiter_bytes`.
        M17 P7: `track_usage` is forwarded to `stream` so the streamed
        cost/token/TTFT accounting fires in `idle_guarded_chunks`."""
        async with await self.stream(method, url, request=request,
                                     track_usage=track_usage, **kw) as r:
            async for chunk in self.idle_guarded_chunks(r):
                yield chunk

    @staticmethod
    def _inject_idempotency(kw):
        """Add a stable `Idempotency-Key` unless the caller already set one
        (case-insensitive). Generated once per logical request and reused
        across retries by the caller (kw persists across the retry loop)."""
        headers = dict(kw.get("headers") or {})
        if any(k.lower() == "idempotency-key" for k in headers):
            return kw  # caller's key wins
        import uuid
        headers["idempotency-key"] = f"lancelot-{uuid.uuid4()}"
        return {**kw, "headers": headers}

    @staticmethod
    def _inject_traceparent(kw):
        """Forward the active request's W3C `traceparent` onto the outbound call
        so the upstream LLM/gateway joins the SAME distributed trace (Tier-B
        cloud observability). No-op when there's no trace context (the common
        case) or the caller set `traceparent` explicitly (their value wins).
        Lightweight context propagation — full span export is the user's OTel.

        M7 T1.1 also injects `x-request-id` and `x-agent-id` (if set) so the
        upstream LLM call inherits the parent request's identifiers; the
        downstream service can join the same RED/trace dashboards via these
        same headers. Backwards-compatible — only adds headers; never
        overwrites a caller-set value (.setdefault)."""
        from lancelot.context import trace_parent, request_id, agent_id

        headers = dict(kw.get("headers") or {})
        tp = trace_parent.get()
        if tp:
            headers.setdefault("traceparent", tp)
        try:
            rid = request_id.get()
            if rid and rid != "-":
                headers.setdefault("x-request-id", rid)
        except LookupError:
            pass
        try:
            aid = agent_id.get()
            if aid and aid != "-":
                headers.setdefault("x-agent-id", aid)
        except LookupError:
            pass
        if not headers:
            return kw
        return {**kw, "headers": headers}

    # ----- M7 T1.2 parallel-probe fanout ----------------------------------
    async def fanout(self, requests, *,
                     first_n: int | None = None,
                     deadline: float | None = None,
                     cancel_laggards: bool = True,
                     concurrency: int | None = None):
        """M7 T1.2 (Chroma/Huber pattern): fire N outbound calls in
        parallel; resolve as soon as the first `first_n` complete; cancel
        the laggards. Bounded concurrency, deadline, and partial-failure
        semantics built in.

        Args:
            requests: iterable of `(method, url, kwargs)` tuples OR
                pre-built coroutines (anything awaitable). Pre-built
                coros let callers reuse spawn_child handles.
            first_n: resolve after this many successes. Default: all.
            deadline: wall-clock cap in seconds. None = no cap.
            cancel_laggards: cancel remaining tasks once `first_n`
                successes land. True is the Chroma default.
            concurrency: max in-flight at once. None = unbounded
                (typical for fanout; the *upstream* rate-limits us).

        Returns:
            List of results in COMPLETION ORDER (not input order).
            Successes are httpx.Response; failures (timeout, error,
            cancellation) are the exception instances. Caller sees
            the partial-failure shape just like asyncio.gather(
            return_exceptions=True) — typical pattern is
            `[r for r in results if not isinstance(r, Exception)]`.

        Why completion order (not input order): the whole point of
        fanout is "first-N wins" — input-order positions of cancelled
        laggards aren't useful to the caller. asyncio.gather(*,
        return_exceptions=True) returns input order but blocks on the
        slowest; we deliberately don't do that here.

        Example:
            child = pool.spawn_child(req)
            payloads = [{"prompt": p} for p in prompts]
            coros = [child.request("POST", url, json=p) for p in payloads]
            results = await pool.fanout(coros, first_n=3, deadline=2.0)
            successes = [r for r in results if not isinstance(r, Exception)]
        """
        # Materialize input into a list of coros.
        coros = []
        for r in requests:
            if isinstance(r, tuple):
                m, u, kw = r if len(r) == 3 else (r[0], r[1], {})
                coros.append(self.request(m, u, **kw))
            else:
                coros.append(r)
        if not coros:
            return []
        target_n = first_n if first_n is not None else len(coros)
        target_n = max(1, min(target_n, len(coros)))

        sem = asyncio.Semaphore(concurrency) if concurrency else None

        async def _run(coro):
            # Rate gate BEFORE the concurrency slot: rate caps sustained calls/s,
            # concurrency caps burst width — orthogonal (Hatchet pattern).
            if self._bucket is not None:
                await self._bucket.acquire()
            if sem is None:
                return await coro
            async with sem:
                return await coro

        tasks = [asyncio.ensure_future(_run(c)) for c in coros]
        results: list = []
        successes = 0
        pending = set(tasks)
        try:
            # Loop until we have `target_n` successes or all tasks done.
            t0 = asyncio.get_running_loop().time() if deadline else None
            while pending and successes < target_n:
                timeout = None
                if deadline is not None and t0 is not None:
                    remaining = deadline - (asyncio.get_running_loop().time() - t0)
                    if remaining <= 0:
                        break
                    timeout = remaining
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED,
                    timeout=timeout)
                if not done:
                    # Timed out with no completions → deadline exceeded.
                    break
                for d in done:
                    try:
                        results.append(d.result())
                        successes += 1
                    except Exception as e:  # noqa: BLE001
                        results.append(e)
        finally:
            if cancel_laggards:
                for t in pending:
                    t.cancel()
                # Drain cancellations so they don't leak as warnings.
                if pending:
                    try:
                        await asyncio.gather(*pending, return_exceptions=True)
                    except Exception:  # noqa: BLE001
                        pass
        return results

    # ----- M7 T1.1 sub-agent / nestable pool ------------------------------
    def spawn_child(self, parent_request=None, *,
                    session_affinity: str | None = None):
        """M7 T1.1 (Giga / Parahelp pattern): create a child handle that
        propagates the *current* parent request's identifiers (traceparent,
        request_id, agent_id, session_affinity) into every outbound call,
        even if the parent ContextVars get reset before this child's call
        completes.

        Use this when a handler spawns asyncio.Tasks that may outlive the
        parent's ContextVar scope (e.g. fire-and-forget audit emit; long
        background LLM call). For the common in-line pattern
        (`async def handler(req): r = await pool.post(...)`), no spawn_child
        is needed — ContextVar copy-on-await semantics already propagate.

        Args:
            parent_request: optional Lancelot Request for cancellation
                propagation. When the parent's `_cancel_event` fires
                (client disconnected), the child's in-flight calls are
                cancelled too — same C2 semantics as the unary path.
            session_affinity: optional explicit KV-cache routing key,
                forwarded as `x-lancelot-affinity` header so the
                upstream LB pins this child to the same cache shard as
                the parent. If None, the framework will not add one
                (the parent middleware/router is responsible).

        Returns:
            A `_ChildHandle` that exposes `.request()` / `.stream()` with
            the captured snapshot pinned in. Same shape as the parent
            pool so call sites are interchangeable.

        Example:
            child = pool.spawn_child(req, session_affinity=user_id)
            # Schedule N parallel sub-agent calls; each inherits parent's
            # traceparent + request_id + agent_id + affinity.
            results = await asyncio.gather(*[child.request("POST", url, json=p)
                                             for p in payloads])
        """
        from lancelot.context import trace_parent, request_id, agent_id

        snap = {}
        tp = trace_parent.get()
        if tp:
            snap["traceparent"] = tp
        try:
            rid = request_id.get()
            if rid and rid != "-":
                snap["x-request-id"] = rid
        except LookupError:
            pass
        try:
            aid = agent_id.get()
            if aid and aid != "-":
                snap["x-agent-id"] = aid
        except LookupError:
            pass
        if session_affinity:
            snap["x-lancelot-affinity"] = session_affinity
        return _ChildHandle(self, parent_request, snap)


class _ChildHandle:
    """M7 T1.1: child handle that pins the parent's identifier snapshot
    onto every outbound call. Returned by `TransportPool.spawn_child(...)`.
    Same shape as the pool (request / stream) so call sites are uniform.

    The snapshot is captured at construction time, so even if the parent
    ContextVars get reset (the request finishes before the child's call
    completes), the child still carries the correct identifiers."""

    __slots__ = ("_pool", "_parent_request", "_snap")

    def __init__(self, pool, parent_request, snap):
        self._pool = pool
        self._parent_request = parent_request
        self._snap = snap

    def _merge_headers(self, kw):
        if not self._snap:
            return kw
        headers = dict(kw.get("headers") or {})
        for k, v in self._snap.items():
            headers.setdefault(k, v)
        return {**kw, "headers": headers}

    async def request(self, method: str, url: str, *, request=None, **kw):
        kw = self._merge_headers(kw)
        # Forward parent cancellation if no explicit request= override.
        if request is None:
            request = self._parent_request
        return await self._pool.request(method, url, request=request, **kw)

    async def stream(self, method: str, url: str, *, request=None, **kw):
        kw = self._merge_headers(kw)
        # M16 P8: forward parent cancellation if no explicit request= override
        # (same as _ChildHandle.request) so a sub-agent's relayed stream is
        # cancelled when the parent client disconnects.
        if request is None:
            request = self._parent_request
        return await self._pool.stream(method, url, request=request, **kw)
