# Lancelot byte-path state inventory
# Source: lancelot/ Python modules + src/ Rust core — read in full 2026-06-22

Precise HAS / LACKS inventory of every byte-path and AI-transport primitive, cited to file:line.

---

## 1. R3 Streaming pump (Rust + Python)

**HAS — fully built, mechanism-level understood.**

Architecture (`src/stream.rs`):
- `stream_buffer()` fn (line 274): `LANCELOT_STREAM_BUFFER` env → OnceLock<usize>, default 256 prod / 1 under `cfg(test)` (T4.1 backpressure guarantee). Clamped [1, 4096].
- `ChunkSink` pyclass (line 202): `tx: Option<Sender<...>>`, `locals: TaskLocals`. Methods: `try_push` (same-thread bounded try_send), `await_capacity` (GIL-free `reserve().await` Rust future → Python awaitable), `fail`, `close`.
- `drainer()` fn (line 295): compiles the `_drain(gen, sink)` Python coroutine once via `OnceLock<Py<PyAny>>`. The coroutine runs ON the asyncio loop (`async for chunk in gen: while not sink.try_push(chunk): if not await sink.await_capacity(): return`). ONE bridge crossing per stream via `into_future_with_locals`.
- `stream_body()` fn (line 334): `tokio::sync::mpsc::channel(stream_buffer())` → `ChunkSink` + `_drain` coro bridged once → `tokio::spawn` drive task with `select! { biased; shutdown.notified() => {} ... }` → `StreamBody<ReceiverStream<...>>`.
- `chunk_to_bytes()` fn (line 80): PyBytes pass-through (refcount bump only), PyString → utf-8, hasattr `render` (interned, line 92) → call render, else pydantic-core JSON + newline.
- `as_async_iter()` fn (line 116): checks `__anext__`/`__aiter__` (both interned via `pyo3::intern!`).
- `stream_media_type()` fn (line 142): reads `media_type` attr (interned), strips CRLF/ctrl, defaults `"application/jsonl"`.
- `next_chunk()` async fn (line 163): retained for in-process `_drive_stream` test only — NOT used in the prod pump.

Python side (`lancelot/stream.py`):
- `Event` dataclass (line 23): `name`, `data`, `id` → `render()` bytes (CRLF-stripped via `_clean_field` line 15).
- `sse_adapter(gen, done_sentinel="[DONE]")` (line 76): wraps user gen so bare models/dicts become `data: <pydantic-core json>\n\n` frames. Yields `bytes`/`str`/`Event` unchanged. Emits terminal `data: [DONE]\n\n` only on clean exhaustion. Returns `_SSEStream` proxy.
- `_SSEStream` (line 121): proxy carries `media_type='text/event-stream'` for native R3 path's content-type detection. Forwards `__aiter__`/`__anext__`/`aclose()` with no per-chunk overhead.

App surface (`lancelot/app.py` line 541):
- `@app.stream(sse=True, done_sentinel="[DONE]", defer_status=False, max_stream_lifetime=None, cancel_on_disconnect=False)`.
- `max_stream_lifetime` (line 588): absolute wall-clock cap; seam sends `event: reconnect\ndata: {"reason":"max_stream_lifetime"}\n\n` + closes.
- `defer_status` (line 569): ASGI seam only — pulls first chunk before committing 200. NOT mirrored in native path (line 66-79 src/stream.rs — intentional, native path has no per-route config).
- `cancel_on_disconnect` (line 65, app.py): sets `request._cancel_event` on client disconnect; ASGI seam: `asgi.py` line 1292.

Metrics: `lancelot/metrics.py` line 67 — `lancelot.http.stream.ttft` histogram; `record_stream_ttft()` line 182 called on first streamed chunk by ASGI seam.

**LACKS:** `defer_status` is ASGI-seam-only (native server commits 200 before any chunk). `Last-Event-Id` replay is explicitly excluded (Tier-D). Per-chunk gzip is Tier-D. Stream-level timeout (distinct from per-route timeout) is not built as a first-class knob (max_stream_lifetime is the closest).

---

## 2. @app.batch — prefix-aware request coalescing

**HAS — fully built, benchmarked, bug-fixed.**

(`lancelot/batch.py`):
- `_BatchGroup` dataclass (line 46): `handler`, `window_s` (snapshotted at construction — live retune can't corrupt a half-built batch), `max_batch`, `size_fn`/`max_size` (byte/token budget, M15 T9), `pending: list[(payload, Future)]`, `timer_task`, `lock: asyncio.Lock`.
- `_BatchGroup.submit()` (line 95): defer-to-tail on budget overflow (atomic list-swap under lock, line 109-116), timer arm via `ensure_future(_wait_then_flush())`, flush on `len >= max_batch` OR `_budget_full()`.
- `_BatchGroup._flush()` (line 138): timer self-cancel guard: `timer is not asyncio.current_task()` check (line 153) — the critical bug fix preventing CancelledError on awaiting handlers.
- `_BatchGroup._run_batch()` (line 159): `BaseException` catch for whole-batch failure (line 163) — fanout, correct semantics, CancelledError doesn't strand waiters.
- `_BatchRegistry` (line 188): per-route container, `max_keys=1024` cap → `BatchOverloadError` (line 224) → ASGI maps to 503 + Retry-After. Forwards `size_fn`/`max_size` to groups.
- `BatchOverloadError` (line 34): `retry_after` field → 503 Retry-After header.

Surface: `@app.batch("/chat", batch_window_ms=50, max_batch=10, key=..., max_keys=1024, size_fn=..., max_size=0)`.

Validated benchmark: ~12x throughput / ~16x fewer upstream calls vs rate-limited LLM backend.

---

## 3. Canonical JSON serialization

**HAS — minimal, stdlib, no deps.**

(`lancelot/canonical.py`):
- `dumps_canonical(obj)` (line 19): `json.dumps(sort_keys=True, separators=(",",":"), ensure_ascii=False, allow_nan=False).encode("utf-8")`. Deterministic bytes for KV-cache stability (Anthropic prompt caching, OpenAI completions cache, vLLM prefix cache).
- `loads(raw)` (line 38): stdlib json.loads passthrough.

**LACKS:** No binary canonical form (CBOR/msgpack). No normalization of float representations (NaN excluded but ±Infinity not tested). This is purely stdlib; pydantic-core `to_json` is a separate faster path for serialization (not used here — canonical needs sort_keys which pydantic-core doesn't natively expose sorted-key stable).

---

## 4. GZip compression (GIL-releasing, ASGI-seam only)

**HAS — opt-in, ASGI path only, GIL-releasing via run_in_executor.**

(`lancelot/compress.py`):
- `GZipMiddleware` (line 62): pure-ASGI (not BaseHTTPMiddleware), `minimum_size=1000`, `compresslevel=6` (libdeflate level 6 beats zlib level 9 on ratio), `exclude_content_types=("text/event-stream",)`.
- `_GZipResponder` (line 107): buffers `http.response.start`; on first body: streaming (`more_body=True`) → identity passthrough from here on (Tier-D ban on per-chunk compression); already-encoded or excluded CT → passthrough; one-shot body < `minimum_size` → passthrough.
- **The crux** (line 188-191): `await loop.run_in_executor(None, _native.gzip_encode, body, compresslevel)` — Rust `libdeflater` encode runs off-loop in a thread-pool thread, GIL released, event loop free to serve concurrent requests during the encode. `run_in_executor` is load-bearing (inline would release GIL to nobody).
- Header rewrite (lines 196-210): drops old `content-length`, sets new one + `content-encoding: gzip`, merges `Vary: Accept-Encoding`.

**Narrow envelope (honest):** ASGI seam only — native `app.run()` cannot host this. Win is structural concurrency (single-worker, no compressing proxy). At low concurrency (c=1), slower than Starlette (run_in_executor overhead). Shrinks behind multi-worker uvicorn (separate GILs) or a TLS proxy. Benchmarked: ~2.2x RPS, ~12x p99 improvement, co-tenant route starved by Starlette GZip (1.43 RPS) vs Lancelot (230 RPS, ~161x). Brotli NOT built (Tier-D per current state).

---

## 5. Bounded concurrency gather

**HAS — minimal, stdlib-only.**

(`lancelot/concurrency.py`):
- `gather(*coros, concurrency=None, return_exceptions=True)` (line 14): `asyncio.Semaphore(concurrency)` gate + `asyncio.gather(return_exceptions=True)`. `concurrency=None` → plain asyncio.gather (zero overhead on the common path).
- `return_exceptions=True` default: partial failure is normal in LLM/scrape workloads; a single 429 should not cancel the batch.

**LACKS:** No timeout on individual coros within gather (use `asyncio.wait_for` per coro). No result ordering beyond input order (unlike `TransportPool.fanout` which returns completion order).

---

## 6. BlockingPool — sync handler offload

**HAS — stdlib ThreadPoolExecutor + inflight gate.**

(`lancelot/pool.py`):
- `BlockingPool(max_workers=40, max_inflight=10_000)` (line 24): ThreadPoolExecutor + threading.Lock + `_inflight` counter.
- `offload(fn, /, *args, **kwargs)` (line 35): inflight gate (raises `PoolFull` at max_inflight → ASGI maps to 503 Retry-After). `contextvars.copy_context().run(fn, ...)` propagates per-request ContextVars (request_id, trace_id, tenant_id, etc.) into the thread.
- `run_backpressured(fn, /, *args, **kwargs)` (line 56): no inflight gate — for post-response BackgroundTasks path (backpressure not rejection).
- `PoolFull` exception (line 18).
- Module-level singleton via `get_pool()` + `configure(max_workers, max_inflight)` (line 89).

**LACKS:** No per-tenant thread cap. No priority lanes (fast vs slow sync handlers). `shutdown(wait=False, cancel_futures=True)` — futures cancelled at shutdown, not drained.

---

## 7. BackgroundTasks + BackgroundPool

**HAS — both FastAPI-parity BackgroundTasks and detached BackgroundPool.**

(`lancelot/background.py`):
- `BackgroundTasks` (line 12): list of `(func, args, kwargs)` tasks; `__call__()` runs async tasks awaited, sync tasks via `run_backpressured` (no inflight gate — accepted work is accepted).
- `BackgroundPool(max_workers=8, max_queue=10_000)` (line 38): separate ThreadPoolExecutor from request workers. `spawn(fn, /, *args, **kwargs)` returns immediately, job outlives response. `depth` counter, ceiling = `max_workers + max_queue` → `QueueFull`. `contextvars.copy_context()` called at spawn time (line 66) so detached job sees parent's request_id/trace_id/tenant_id.
- Module-level `spawn(fn, /, *args, **kwargs)` function (line 99).

---

## 8. TransportPool — outbound LLM transport

**HAS — full-featured, most complex module.**

(`lancelot/llm.py`):

**Connection pool knobs (line 277-285):** `LANCELOT_UPSTREAM_MAX_CONNECTIONS` (default 100), `LANCELOT_UPSTREAM_MAX_KEEPALIVE` (default 20), `LANCELOT_UPSTREAM_KEEPALIVE_EXPIRY` (default 5.0s) → `httpx.Limits`. Surfaced as constructor kwargs + env overrides.

**Retry policy (line 443-490):**
- POST/PATCH: NEVER retried (cardinal rule — line 466-469).
- Idempotent verbs (GET/HEAD/PUT/DELETE/OPTIONS): retry on 429/500/502/503/504, up to `max_retries=3`.
- `Retry-After` / `retry-after-ms` header honored (line 376-390).
- Exponential backoff (line 477-479): `min(backoff_max, backoff_base * 2^attempt + random * backoff_base)`, base=0.2, max=8.0.
- `retry_budget_ms` (line 308-317): wall-clock cap on whole retry chain. `LANCELOT_UPSTREAM_RETRY_BUDGET_MS` (default 60000). Prevents runaway Retry-After (e.g. 3×30s under 1s budget).

**Idempotency-Key injection (line 752-761):** stable `lancelot-{uuid4}` key set once, reused across retries. Caller-set key wins. Only on idempotent verbs.

**W3C traceparent propagation (line 764-797, `_inject_traceparent`):** reads `lancelot.context.trace_parent` + `request_id` + `agent_id` ContextVars, injects into outbound headers (setdefault — never overwrites caller-set values).

**Cancel-on-disconnect (C2) — unary (line 556-586, `_await_cancellable`):** races `client.request(...)` coro vs `request._cancel_event.wait()` via `asyncio.wait(FIRST_COMPLETED)`. Zero overhead when `request=None`.

**Streaming (line 588-632, `stream()`):** returns httpx CM or `_CancellableStreamCtx`. `_stream_timeout = httpx.Timeout(timeout, read=None)` — no read deadline for streams (bounded instead by C2 client-disconnect cancellation). `LANCELOT_UPSTREAM_STREAM_IDLE_SECS` (default 0) for `idle_guarded_chunks`.

**Cancel-on-disconnect — streaming (line 36-72, `_race_chunk_vs_cancel`):** races `it.__anext__()` vs `cancel_event.wait()` + optional idle timeout via `asyncio.wait(FIRST_COMPLETED, timeout=idle)`. Connected to `_CancellableStreamCtx` which tags `_lancelot_cancel_event` on the response.

**idle_guarded_chunks (line 634-684):** yields `response.aiter_bytes()` chunks. Races vs cancel event (disconnect) and vs idle timeout. M17 P7 `track_usage=True`: retains last SSE `data:` frame containing `"usage"` or `"usageMetadata"` (cheap substring gate, line 111), records upstream TTFT on first chunk, records cost/tokens on clean StopAsyncIteration. `_SSE_SCAN_CAP = 65536` bounds carry-over buffer (line 18).

**relay (line 686-749):** async-CM yielding async-iterator for upstream→client SSE relay. Drives `_relay_forward` → `idle_guarded_chunks` → aclose on exit.

**fanout (line 800-903):** fires N outbound calls in parallel, resolves on first `first_n` successes, cancels laggards. Completion order (not input order). Deadline (wall-clock). Bounded concurrency via `asyncio.Semaphore`. Rate cap via `_TokenBucket.acquire()` before semaphore.

**spawn_child (line 906-963):** snapshots current ContextVars (traceparent, request_id, agent_id) + optional `session_affinity` → `x-lancelot-affinity` header. Returns `_ChildHandle` with same `.request()/.stream()` shape. Snapshot captured at construction so ContextVar reset (request finishes) doesn't lose identifiers.

**_TokenBucket (line 198-221):** asyncio cooperative token bucket. `LANCELOT_UPSTREAM_RATE_LIMIT` (default 0=disabled). Starts full (burst of `rate`), refills at rate/s. No lock needed (asyncio single-threaded cooperative).

**mTLS (line 289-297):** `TransportPool(mtls_cert=, mtls_key=, mtls_ca=)`. `reload_mtls()` (line 350) for SIGHUP cert rotation — builds new client with rotated cert files, closes old client. In-flight calls keep old cert (correct: no mid-stream cert swap).

**SSRF guard (line 326-328):** `block_private_ips=True` → `lancelot.net.assert_safe_url(url)` before any I/O.

**Upstream metrics emit (line 494-518, `_emit_upstream`):** calls `lancelot.upstream_metrics.record_upstream`, accumulates `cost_usd` ContextVar from info dict.

**LACKS:** No circuit breaker (explicit Tier-D / strict synthesis). No explicit HTTP/2 (disabled: `http2=False` line 288). `stream()` returns bare httpx CM when no cancel event and no track_usage — zero overhead, but caller must know to pass `request=` to get cancellation.

---

## 9. upstream_metrics — LLM-aware observability

**HAS — full provider/model/token/cost/taxonomy.**

(`lancelot/upstream_metrics.py`):

**Provider detection (line 74-96, `detect_provider`):** URL regex match for openai, anthropic, google, groq, deepseek, mistral, together, fireworks. Default "unknown" (still useful tag).

**Token extraction (line 108-129, `extract_tokens`):** handles 3 provider shapes: OpenAI (`usage.prompt_tokens`/`completion_tokens`), Anthropic (`usage.input_tokens`/`output_tokens`), Google (`usageMetadata.promptTokenCount`/`candidatesTokenCount`). Returns (0,0) on missing/malformed.

**Cost calc (line 132-151, `calc_cost_usd`):** user-supplied rate table only — Lancelot ships no rates. `set_rate_table(table)` / `LANCELOT_LLM_RATES` env JSON. "default"/"*" fallback key per provider.

**Error taxonomy (line 156-185, `classify_status`):** ok / quota (429) / timeout (408,504) / permanent (400/401/403/404/422) / transient (502/503/521) / upstream_5xx (other 5xx).

**OTel metrics (line 194-291, `record_upstream`):** lazy bind to `opentelemetry.metrics` (early-return if absent). 6 instruments under `lancelot.upstream` meter: `requests` counter, `duration` histogram (s), `ttft` histogram (s), `tokens.input` counter, `tokens.output` counter, `cost.usd` histogram. Attrs: `llm.provider`, `llm.model`, `llm.class`, `http.status_code`.

**SSE scan (line 75-117, `_scan_last_data_frame` in llm.py):** `\n\n`-terminated frame scanning for `"usage"` substring (cheap gate before JSON parse). Handles multi-line data payloads. `_SSE_SCAN_CAP = 65536` bounds carry-over.

---

## 10. context ContextVars

**HAS — all per-request propagation scaffolding.**

(`lancelot/context.py`):
- `request_id: ContextVar[str]` (line 20), default "-". Set from `X-Request-ID`/`X-Correlation-ID` header or `new_request_id()` (UUIDv4 hex). CRLF-sanitized, 128-char bounded (in ASGI seam).
- `principal: ContextVar[object]` (line 25), default None.
- `trace_id: ContextVar[str]` (line 45), default "-". Parsed from W3C `traceparent` header.
- `trace_parent: ContextVar` (line 48), default None. Raw traceparent header value forwarded by TransportPool to outbound LLM calls.
- `cost_usd: ContextVar[float]` (line 65), default 0.0. Accumulated by `TransportPool._emit_upstream` per sub-call. ASGI seam emits as `X-Lancelot-Cost-USD` + `lancelot.http.cost.usd` metric in `finally`.
- `tokens_in: ContextVar[int]` (line 71), default 0.
- `tokens_out: ContextVar[int]` (line 74), default 0.
- `agent_id: ContextVar[str]` (line 84), default "-". From `X-Agent-Id` header.
- `tenant_id: ContextVar[str]` (line 92), default "-". `LANCELOT_TENANT_LABEL_MAX` cardinality cap in metrics emit path.
- `queue_wait_ms: ContextVar[float]` (line 103), default 0.0. Set by `TenantConcurrencyLimiter` after semaphore acquire. Emitted as `lancelot.http.queue_wait_ms` histogram.
- `parse_traceparent(value)` (line 108): validates W3C shape (`^[0-9a-f]{2}-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$`), rejects all-zero trace/span ids.

---

## 11. Rust server core — service.rs constants and knobs

(`src/service.rs`):
- `max_body()` (line 37): `LANCELOT_MAX_BODY` env, default 16 MiB, clamped [1 KiB, 1 GiB]. `Limited` wrapper on hyper body — enforced BEFORE body buffering.
- `max_connections()` (line 57): `LANCELOT_MAX_CONNECTIONS` env, default 1024, clamped [1, 1<<20]. `tokio::sync::Semaphore` acquired before accept.
- `max_inflight_bytes()` (line 79): `LANCELOT_MAX_INFLIGHT_BYTES` env, default 0 (OFF). AtomicU64 `INFLIGHT_BYTES` + RAII `InflightGuard`. Two-budget: count cap × per-body = 16 GiB product; this is the missing second axis. Load-shedding (503), NOT queue-and-wait.
- `drain_timeout()` (line 107): `LANCELOT_DRAIN_SECS` env, default 10s, clamped [0,300]. Graceful drain: `acquire_many_owned(max_conns)` holds until all in-flight permit release.
- `header_timeout_ms()` (line 124): `LANCELOT_HEADER_TIMEOUT_MS` env, default 15000ms, clamped [100ms, 600s]. Slow-loris guard.
- `Prebaked` struct (line 142): `status: StatusCode`, `content_type: HeaderValue` (validated at registration), `body: Bytes`. `response()` = refcount `body.clone()` + infallible builder. Zero GIL, zero Request alloc, zero handler call.
- `App.const_bodies: Vec<Option<Prebaked>>` (line 175): parallel to handlers slot array; const slot check in `handle()` (line 264).
- Shutdown: `App.shutdown: Arc<Notify>` (line 178) notified on SIGTERM → R3 pump drive tasks stop via `select! { biased; shutdown.notified() => {} ... }`.
- Lifespan: `App.on_startup`/`App.on_shutdown` Python callables (line 187). on_startup before bind; on_shutdown after drain, bounded by drain_timeout.

---

## Summary: HAS / LACKS table

| Domain | HAS | LACKS / GAP |
|---|---|---|
| Streaming pump | R3 on-loop drain, bounded-mpsc, GIL-released per chunk, TTFT metric, SSE framing, done_sentinel, max_stream_lifetime, cancel_on_disconnect, defer_status (ASGI) | defer_status on native server; no stream-level idle timeout knob at the stream() decorator level (only via TransportPool) |
| Batch coalescing | @app.batch, window/count/budget flush, key fn, max_keys cap, defer-to-tail, timer self-cancel fix, BatchOverloadError→503 | No streaming batch (response streaming + batch coalescing at same time) |
| Canonical JSON | dumps_canonical (stdlib, sort_keys, no-NaN, utf-8) | No pydantic-core canonical path (sort_keys not natively exposed); no CBOR/msgpack |
| Compression | GZipMiddleware (libdeflater off-loop via run_in_executor, ASGI-only, one-shot bodies) | Brotli; native server compression; streaming compression (Tier-D) |
| Concurrency | gather(concurrency=N) Semaphore gate | No timeout-per-coro in gather |
| Blocking offload | BlockingPool(max_workers=40, max_inflight=10_000, ContextVar propagation) | No per-tenant thread cap; no priority lanes |
| Background tasks | BackgroundTasks (FastAPI parity) + BackgroundPool (detached, bounded queue, ContextVar snapshot at spawn time) | No async background pool (all threads) |
| Outbound transport | TransportPool: retry (POST never), backoff, retry_budget_ms, Idempotency-Key, traceparent/request-id/agent-id propagation, cancel-on-disconnect (unary+streaming), idle_guarded_chunks, relay, fanout, spawn_child, mTLS, SSRF guard, rate limit token bucket | No circuit breaker (explicit Tier-D); HTTP/2 disabled |
| Upstream metrics | Provider/model detection (8 providers), token extraction (3 shapes), cost calc (user rate table), error taxonomy (5 classes), 6 OTel histograms/counters, upstream TTFT on first chunk, cost_usd ContextVar accumulation, X-Lancelot-Cost-USD trailer | No tiktoken/local estimation (by design — provider-reported only) |
| ContextVars | request_id, trace_id, trace_parent, cost_usd, tokens_in, tokens_out, agent_id, tenant_id, queue_wait_ms, principal | No user-extensible ContextVar registry (users must import and set directly) |
| Memory budget | MemoryPool(total_bytes): cooperative reserve/release, named consumers, ResourcesExhausted names top consumer | Not wired to any automatic enforcement — purely cooperative opt-in |
| Rust server knobs | LANCELOT_MAX_BODY (16MiB), MAX_CONNECTIONS (1024), DRAIN_SECS (10), HEADER_TIMEOUT_MS (15000), MAX_INFLIGHT_BYTES (0=off), STREAM_BUFFER (256), WORKERS, LOOP, CONST_STRICT | LANCELOT_MAX_INFLIGHT_BYTES default=0 (off by default — gap for tight deployments) |
