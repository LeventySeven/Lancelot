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
                 timeout: float = 30.0,
                 retry_budget_ms: int | None = None,
                 max_connections: int | None = None,
                 max_keepalive_connections: int | None = None,
                 keepalive_expiry: float | None = None,
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
        watching.

        M7 T1.3 + T2.2: per-call upstream metrics emitted (LLM-aware
        attrs: provider/model/class/tokens/cost). Also accumulates cost
        into the request's `cost_usd` ContextVar — so the parent request
        sees the sub-LLM-call's cost automatically (Sim.ai pattern)."""
        method = method.upper()
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
        kw = self._inject_traceparent(kw)
        kw.setdefault("timeout", self._stream_timeout)  # no read deadline; caller can override
        return self._client.stream(method, url, **kw)

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
            t0 = asyncio.get_event_loop().time() if deadline else None
            while pending and successes < target_n:
                timeout = None
                if deadline is not None and t0 is not None:
                    remaining = deadline - (asyncio.get_event_loop().time() - t0)
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

    async def stream(self, method: str, url: str, **kw):
        kw = self._merge_headers(kw)
        return await self._pool.stream(method, url, **kw)
