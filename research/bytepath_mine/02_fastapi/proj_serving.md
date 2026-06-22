# ML/LLM Serving archetype — byte-path mechanisms for Lancelot
## Sources: LitServe · BentoML · Ray Serve · Modal

Teardown files: `researchfms/teardowns/LITSERVE.md`, `BENTOML.md`, `RAY_SERVE.md`, `MODAL.md`.
Code: `researchfms/fastapi/code/litserve/`, `bentoml/`, `ray_serve/deep/`, `modal/deep/`.
Date: 2026-06-22.

---

## Additive status legend

- **HAVE** — Lancelot already ships this; note is for completeness only.
- **IMPROVE** — Lancelot has a partial form; a specific upgrade is identified.
- **NEW** — not in Lancelot at all; real byte-path candidate.
- **SKIP** — out of scope per cardinal rules or Tier-D; reason given.

---

## 1. `@app.batch` — max_concurrent_batches pipelining  [IMPROVE]

**Source:** `ray_serve/deep/batching.py:254-363`, `batching.py:474-518`

Ray Serve's `@serve.batch` has a `max_concurrent_batches=N` semaphore
(`_process_sub_batches`, `:439-444`). The dispatcher loops calling
`wait_for_batch`, creates a task for the batch, and immediately starts
waiting for the *next* batch under the same `asyncio.Semaphore(N)`. This
lets batch `k+1` fill on the CPU while batch `k` runs on the GPU — a
second batch accumulates during the LLM forward pass latency.

The `NonBlockSema` in BentoML's `CorkDispatcher` (`dispatcher.py:253:266`)
does the same: `self._sema.is_locked() → sleep(TICKET_INTERVAL)`, with max
1 concurrent outbound call by default.

Lancelot `@app.batch` has no such pipeline. Each `_BatchGroup._run_batch`
is `await`ed sequentially — the next window doesn't start until the current
handler returns. For an async upstream (LLM call taking 200–800 ms) this
serializes windows unnecessarily. The fix is a `max_concurrent_batches: int`
kwarg on `@app.batch` that wraps `_run_batch` under an `asyncio.Semaphore`.

**Byte-path relevance:** throughput improvement for rate-limited LLM upstreams
(which is exactly the `@app.batch` use case). A max_concurrent_batches=2
doubles throughput whenever LLM latency > window_ms.

**Lancelot check:** `lancelot/batch.py:_run_batch` line ~159; no semaphore.

---

## 2. CorkDispatcher — learned adaptive batching (duration ≈ o_a·n + o_b)  [SKIP → future]

**Source:** `bentoml/deep/` → `teardowns/BENTOML.md §5, Appendix A`

`CorkDispatcher` (`dispatcher.py:38-101, 199-249`) online-fits batch latency as a linear model:
`duration ≈ o_a · n + o_b` (per-item slope + fixed overhead), using
`np.linalg.lstsq` over the last 50 samples (`N_KEPT_SAMPLE = 50`). It
holds for a bigger batch only while `(batch_size·o_a + o_b) + head_wait ≤
max_latency·0.95`, sheds to `ServiceUnavailable(503)` when overloaded.
Warmup: 3 phases at batch sizes 1→2→3 before steady state.

The constants: `o_a`, `o_b` (learned slopes), `max_latency` (SLA knob),
`TICKET_INTERVAL = 0.001` s (poll), `N_KEPT_SAMPLE = 50`, `N_SKIPPED_SAMPLE = 2`.

Lancelot `@app.batch` is a fixed-window timer (accumulate-or-timeout with
`batch_window_ms`). This is correct and simpler. The CorkDispatcher improvement
is real but requires numpy as a dep, adds complexity, and is only a
second-order improvement over a well-tuned fixed window. Per cardinal rule:
no new dep unless it changes the byte path materially. **Skip for now**;
revisit if operators demand SLA-aware batching.

---

## 3. `@serve.batch` — requests_available_event (avoid sleep polling)  [IMPROVE]

**Source:** `ray_serve/deep/batching.py:384-414`

Ray Serve's batch fill-loop waits on `asyncio.Event requests_available_event`
(`batching.py:389-390`):

```python
await asyncio.wait_for(self.requests_available_event.wait(), remaining_batch_time_s)
```

The event is set when a new item is enqueued and cleared **only when the
queue empties** (`:347`), so a queue that still has items when the timer
expires immediately starts the next batch without sleeping at all.

Lancelot `@app.batch` uses `asyncio.sleep(window_s)` in `_wait_then_flush`.
Each timer fires once and exits. The submit path is event-loop-friendly
(the future awaits), but between windows there is no "batch started
immediately because items already waited". This is a micro-latency issue
for fast-arriving streams; the fix is cheap (`asyncio.Event` on the group,
set in `submit`, cleared after flush).

**Byte-path relevance:** low overhead fix for the case where items queue
faster than the window; avoids spurious sleep at window boundaries.

**Lancelot check:** `lancelot/batch.py:_BatchGroup._wait_then_flush:131-136`.

---

## 4. `batch_size_fn` / token-budget deferral  [HAVE — already shipped]

**Source:** `ray_serve/deep/batching.py:393-403`

Ray Serve's batcher has `batch_size_fn` — a callable that returns a
cost/token count for a candidate batch; if the next item would push cost
over the budget it's deferred to the next batch (`:397-403`).

Lancelot ships this as `size_fn` / `max_size` in `@app.batch` (M15 T9),
with the same defer-to-tail semantics (`batch.py:80-117`). **HAVE.**

---

## 5. Streaming batch demux — chained future-deque for per-caller generators  [NEW → SKIP]

**Source:** `ray_serve/deep/batching.py:375-424`, `_consume_func_generator`

When the batch function is an `async def ... -> AsyncGenerator` (streaming
output), Ray Serve uses a rotating `deque` of futures — each call site
starts with `first_future`; the batch generator advances per step and for
each step sets each caller's current future to `_GeneratorResult(value, next_future)`;
callers hop via `batch_handler_generator` which `await`s the next future.
This lets N concurrent callers each receive an independent token stream from
one batched generator call.

Lancelot `@app.batch` does NOT support streaming batch handlers today.
However: the Lancelot streaming path is `@app.stream` (per-request async
generator over the R3 pump). A batched streaming handler would require
coordinating the R3 pump with the batch demux — a non-trivial seam.
More critically: LLM streaming is upstream-rate-limited, not throughput-
limited at the framework; the real gain of batching streaming is fewer
upstream calls (which `@app.batch` + non-streaming batched request already
provides). This crosses into AI-logic territory. **SKIP** per Tier-D.

---

## 6. LitServe broker architecture — uid/Event correlation via mp queue  [SKIP]

**Source:** `teardowns/LITSERVE.md §3, §4, §6`; `server.py:306-321, 137-203`

LitServe decouples the web process from model workers via OS-process
boundaries and a mp.Manager().Queue. Request handler enqueues `(response_queue_id, uid, monotonic(), payload)`; a background asyncio pump receives replies and sets per-uid `asyncio.Event`s; model processes run separate from uvicorn.

This architecture solves: a single GPU worker serving N uvicorn processes.
Lancelot's positioning is HTTP transport/byte-path, not GPU worker management.
Users bring their own ML frameworks (vLLM, Triton, Hugging Face). Building
a cross-process model-worker broker would replicate LitServe — directly
against the "AI logic stays user-side" cardinal rule. **SKIP.**

---

## 7. LitServe ZMQ "fast_queue" — pub/sub topic-as-routing-key  [SKIP same]

**Source:** `teardowns/LITSERVE.md §R2.1`; `transport/zmq_queue.py:31-185`

The ZMQ transport uses XPUB/XSUB proxy (`Broker`), `zmq.PUB` per worker,
`zmq.SUB` per web-process subscribed to `str(consumer_id)` as the topic,
and `f"{consumer_id}|".encode() + pickle(item)` framing — C-level demux by
prefix. Faster than mp.Manager proxy queue.

Same reason as #6: the broker architecture is not Lancelot's job. **SKIP.**

---

## 8. BentoML CorkDispatcher — load shedding to 503  [IMPROVE → batch]

**Source:** `teardowns/BENTOML.md §5`; `dispatcher.py:121-122`

When the `NonBlockSema` is held and a request hits the concurrency cap,
BentoML raises `ServiceUnavailable("process is overloaded")` (503).

Lancelot `@app.batch` today has `BatchOverloadError` → 503 only on the
registry key-cap (`max_keys` exceeded, `batch.py:222-229`). There is NO
shed path for actual CPU overload (e.g. when `_run_batch` takes longer
than `batch_window_ms` and the next batch is already full). The
`max_concurrent_batches` semaphore from mechanism #1 provides the correct
backpressure point: when `semaphore.locked()` AND the queue depth exceeds
a threshold, a 503 (with `Retry-After: 1`) is the right response.

**Byte-path relevance:** prevents queue runaway under overload without
orphaning waiting requests.

**Lancelot check:** `lancelot/batch.py:_run_batch` — add overflow check
after acquiring the semaphore (#1).

---

## 9. BentoML `CorkDispatcher` — request timeout + head-of-line drop  [IMPROVE]

**Source:** `teardowns/BENTOML.md Appendix A.2:346-351`; `dispatcher.py:227-230`

```python
if n > num_required_reqs - req_count and w0 >= self.max_latency:
    self._queue.popleft().future.cancel()   # drop a request that ALREADY timed out
    continue
```

A request that has waited longer than `max_latency` is **cancelled** (its
future set to cancelled), not run. This is the correct behaviour: running
a stale request wastes GPU cycles for a client that already timed out.

LitServe does the same in `collate_requests` (`loops/base.py:111-130`):
enqueue timestamp `time.monotonic()` is checked; over-aged requests get a
504 response instead of being forwarded to the model.

Lancelot `@app.batch` has no per-item timeout. An item enqueued at t=0
with window_ms=50 but where the handler takes 2 s is still served.
A `request_timeout_ms` kwarg + timestamp in the `(payload, future)` tuple
would let `_flush` cancel stale futures before calling the handler.

**Byte-path relevance:** prevents pileup under overload (same class of bug
that made `@app.batch` break under adversarial load in M4).

**Lancelot check:** `lancelot/batch.py:_BatchGroup.submit:95` — no timestamp stored.

---

## 10. BentoML eager-first-chunk streaming — surface pre-stream errors as real 4xx  [HAVE]

**Source:** `teardowns/BENTOML.md Appendix B`; `io_models.py:225-257`

```python
first_chunk = await obj.__anext__()   # consume FIRST yield eagerly
# → StopAsyncIteration → pre_chunks = [] (empty stream is fine)
# → Exception → propagates as a real error response, not a 200+broken body
```

Lancelot `@app.stream` drives the generator through the R3 pump
(`src/stream.rs`) where the first chunk flows through the same
`try_push` path as any other chunk. A raising generator propagates via
`catch_unwind` and surfaces as a 500. This is functionally equivalent for
the happy path; however Lancelot does NOT guarantee that a generator that
raises before the first yield produces a distinct HTTP error response
(vs a 200 with a broken body) — the Rust pump may have committed the
response header before Python raises. **Worth verifying under adversarial
load** (a test for "generator raises on first yield → client sees 500
not truncated-200"). Not a new build, but a validation gap.

**Lancelot check:** `src/stream.rs` — response header commitment point
relative to first `__anext__` call.

---

## 11. BentoML `ContextMiddleware` + `in_request` ContextVar scoping  [HAVE / partial]

**Source:** `teardowns/BENTOML.md §R2.2`; `context.py:22-128`, `app.py:56-71`

BentoML sets `_request_var` / `_response_var` (module-level `ContextVar`s)
via an ASGI middleware that calls `context.in_request(req)`, using
`_request_var.set(req)` → token, and `_request_var.reset(token)` in
`finally`. The per-request temp-dir pool is released in the same `finally`.

Lancelot has `lancelot.context` (`context.py`) with `request_id`,
`cost_usd`, `tokens_in/out`, `agent_id`, `tenant_id`, `queue_wait_ms`
ContextVars — all set/reset by the ASGI seam (`asgi.py`). The
set-by-token-reset pattern is already correct. **HAVE.**

---

## 12. BentoML `make_safe_connect` — DNS-rebinding-resistant SSRF guard  [HAVE — different approach]

**Source:** `teardowns/BENTOML.md §R2.3`; `uri.py:59-107`

BentoML patches `uvloop.Loop.create_connection` to reject private/loopback
IPs at connect time (after DNS resolution), not at URL-string parse time.
This is DNS-rebinding-resistant: the host is blocked at the moment of
connecting, when the IP is known for real.

Lancelot ships `lancelot.net.guard_url` (`net.py:81-103`) which does
`asyncio.get_event_loop().run_in_executor(None, socket.gethostbyname, hostname)`
and then `ipaddress.ip_address(resolved)` — resolves at check time, then
guards the connection. This is functionally equivalent for most cases but
has the classic TOCTOU gap: a DNS rebinding attack can return a public IP
at check time and a private IP at connect time. BentoML's monkeypatch of
`create_connection` closes this gap.

**Byte-path relevance:** security hardening for webhook / URL-fetch endpoints.
The monkeypatch approach is more robust. Worth noting as a future improvement
to `lancelot.net`, but not a day-1 build — the current guard is correct
for the non-adversarial case.

**Lancelot check:** `lancelot/net.py:81` — uses DNS-at-check-time, not
at-connect-time.

---

## 13. BentoML `@task` — REST async-job API on long-running inference  [NEW — real candidate]

**Source:** `teardowns/BENTOML.md §E.1`; `app.py:582-616`, `tasks.py`

A `@bentoml.task` method gets a 5-route REST job API: `POST /submit`
returns `{"task_id": ..., "status": "in-progress"}` immediately (the
inference runs as a `starlette.BackgroundTask` after the response is sent),
plus `GET /status?task_id=`, `GET /get?task_id=`, `POST /retry`,
`PUT /cancel`. Results are persisted to `Sqlite3Store` so they survive
worker restarts. The same `api_endpoint_wrapper` code path handles both
sync and async task execution.

Lancelot has `BackgroundTasks` injection (conformance-level) but no
built-in `@app.job` or async-task-poll pattern. For AI workloads with
minutes-long inference (image generation, fine-tuning, long-context
summarization), holding an HTTP connection is untenable. This is a real
operator-experience gap.

**However:** SQLite persistence is a dep (stdlib but still state).
The Lancelot version would be an opt-in `@app.job` decorator + in-memory
task store first (dict + asyncio.Task, no SQLite dep on import) + optional
SQLite backend. The `/submit` → 202 + `Location: /job/{id}` pattern is
standard REST.

**Byte-path relevance:** changes the operator experience for long-running
AI endpoints (the exact target user). No analogue in FastAPI/Robyn.

**Lancelot check:** no `@app.job` or task-store anywhere in `lancelot/`.

---

## 14. Ray Serve ASGI-over-actors bridge (MessageQueue + ASGIReceiveProxy)  [SKIP]

**Source:** `teardowns/RAY_SERVE.md §4-§6`; `http_util.py:182-417`, `replica.py:3630-3744`

Ray Serve bridges ASGI `scope`/`receive`/`send` across a Ray actor boundary:
`MessageQueue` (asyncio queue-backed send), `ASGIReceiveProxy` (callback into
the proxy actor for receive), `fetch_messages_from_queue` drain loop that
races `asyncio.wait([call_fut, wait_for_msg_task])`.

This solves "run an unmodified FastAPI app on a remote Ray actor." Lancelot
is a single-process ASGI framework; it IS the server. The multi-actor
dispatch is Ray's job. **SKIP** — out of scope.

---

## 15. Ray Serve `uvicorn.LifespanOn` — run ASGI lifespan without a server  [HAVE]

**Source:** `teardowns/RAY_SERVE.md §3`; `http_util.py:569-601`

Ray Serve borrows uvicorn's `LifespanOn(Config(app, lifespan="on"))` to
fire FastAPI startup/shutdown events inside a replica actor that has no
actual HTTP server.

Lancelot drives the ASGI lifespan itself via `_sub_lifespan_cm` in
`app.py:1026-1166` — a proper asyncio-queue channel that runs startup/
shutdown without uvicorn. **HAVE** (and arguably a cleaner design than
borrowing a private uvicorn class).

---

## 16. Ray Serve `@serve.multiplex` — per-replica LRU model cache + controller push-back  [NEW → SKIP]

**Source:** `ray_serve/deep/multiplex.py:118-239`

`OrderedDict`-based LRU for up to `max_num_models_per_replica` loaded
models; eviction calls `model.__del__()` eagerly (to free VRAM without
waiting for GC, neutralized after call to avoid double-free); cache
state is pushed to the Serve controller every 1 s so the router sends
`model_id=foo` requests to replicas that already have foo loaded.

This is GPU-model memory management. Lancelot doesn't manage model weights.
**SKIP** — AI logic is user-side.

---

## 17. Ray Serve Power-of-Two-Choices + multiplex-tier routing  [NEW → SKIP]

**Source:** `ray_serve/deep/pow_2_router.py:52-106`, `request_router.py:278-347`

Three-tier fallback for multiplexed requests: (0) replicas with the model
loaded, (1) replicas with fewest models loaded (best eviction candidate),
(2) all replicas. Randomized timeout `uniform(T, 2T)` to avoid thundering
herd. P2C selection with proven O(1) k=2 sampler.

Load balancing across replicas is the proxy/LB's job, not Lancelot's.
`SO_REUSEPORT` + multiprocess + upstream LB is the Lancelot deploy model.
**SKIP.**

---

## 18. Modal `LifespanManager` — portable hand-rolled ASGI lifespan driver  [HAVE / reference only]

**Source:** `teardowns/MODAL.md §4`; `_runtime/asgi.py:24-95`

Modal hand-rolls a lifespan state machine (`_lifespan_supported` graceful
degradation for apps that don't implement lifespan, `ensure_init` for
correct loop-binding on Python 3.9, `scope["state"]` injection into each
request scope).

Lancelot drives lifespan via `_sub_lifespan_cm` with its own asyncio queue.
The `_lifespan_supported` degradation pattern is worth checking — Lancelot's
`_sub_lifespan_cm` may not gracefully degrade if a mounted sub-app doesn't
implement lifespan and immediately errors. **Validation gap only.**

**Lancelot check:** `lancelot/asgi.py:_sub_lifespan_cm` — does it catch
the "no lifespan" case?

---

## 19. Modal `FIRST_MESSAGE_TIMEOUT_SECONDS = 5.0` → 408 on first-input delay  [NEW — real candidate]

**Source:** `teardowns/MODAL.md §2`; `_runtime/asgi.py:21, 121-138, 150-160`

Modal's container bridge imposes a 5-second deadline on receiving the
first ASGI message (`http.request`). If the edge sends no data within 5 s
it synthesizes `{"type":"http.response.start","status":408}` + a body
"Missing request, possibly due to expiry or cancellation" and disconnects.
The implementation uses `asyncio.shield(asyncio.wait_for(first_message_task, 5.0))`
followed by manual cancel of `first_message_task`.

Lancelot has `header_read_timeout` (the slow-loris guard, `LANCELOT_HEADER_TIMEOUT_MS`,
default 15 000 ms) at the Rust TCP layer — this fires if the HTTP header
never arrives. But once the header is parsed and the Rust-to-Python bridge
fires, Lancelot does NOT have a deadline on the first body chunk
arriving (a request with headers sent but body stalled forever would hold
a connection slot). Modal's pattern is ASGI-layer; Lancelot's gap is
the "headers parsed, body never arrives" shape.

**Byte-path relevance:** closes a slow-loris variant that bypasses the
Rust header timeout (send headers, hold body open). Measured Lancelot
resilience gap.

**Lancelot check:** `src/server.rs` — after hyper parses the request
headers and the ASGI bridge fires, is there a deadline on body reads?
Check `LANCELOT_BODY_TIMEOUT_MS` (not currently in CLAUDE.md knobs).

---

## 20. Modal proxy — `_proxy_lifespan_request` + shared ClientSession via ASGI state  [HAVE / reference]

**Source:** `teardowns/MODAL.md §R2.2`; `_runtime/asgi.py:463-528`

Modal creates one shared `aiohttp.ClientSession` on `lifespan.startup`,
injects into `scope["state"]["session"]`, closes on `lifespan.shutdown`.
Tuned: `TCPConnector(limit=1000, force_close=True)`, `auto_decompress=False`,
`DummyCookieJar`, 64 KB max_line_size/max_field_size.

Lancelot's `TransportPool` (`llm.py`) is the analogue — one shared httpx
session pool per LLM backend, created at startup, closed at shutdown.
`force_close=True` equivalent: Lancelot does NOT currently force-close
pooled connections in `TransportPool`. Worth checking if vLLM / anthropic
/ openai backends drop stale keep-alive sockets between requests, causing
`httpx.RemoteProtocolError` under low traffic. **Minor hardening candidate.**

**Lancelot check:** `lancelot/llm.py:TransportPool` — `httpx.AsyncClient`
connection pool settings; check for `httpx.Limits(max_keepalive_connections=...)`.

---

## 21. LitServe `RequestCountMiddleware` — shared-memory cross-process in-flight counter  [HAVE / different]

**Source:** `teardowns/LITSERVE.md §11`; `middlewares.py`

`mp.Value("i", 0, lock=True)` incremented/decremented by ASGI middleware,
summed across processes by `LitServer.active_requests`.

Lancelot counts in-flight requests in `context.py` (`in_flight` ContextVar,
read by `/fleet/status`). Each process has its own counter. For a
multiprocess deploy with `SO_REUSEPORT` the operator must sum across
processes via the fleet endpoint or OTel scrape. **HAVE** (design-level
equivalent; no cross-process shared memory — which Lancelot correctly
avoids, since the mp.Value approach is Linux-only and fragile). The fleet
endpoint at `/fleet/status` is the Lancelot answer.

---

## 22. LitServe `worker_id` attribution → fail-in-flight-requests on worker death  [SKIP]

**Source:** `teardowns/LITSERVE.md §E.2`; `server.py:1602-1680`

When a model worker dies, the monitor scans `response_buffer` for entries
with `resp.worker_id == dead_worker_id` and sets their event to ERROR,
unblocking those handlers. This is tight to the broker architecture (#6).
Not applicable to Lancelot's single-process or multiprocess (no cross-proc
response buffer) design. **SKIP.**

---

## 23. LitServe OpenAI-compatible SSE wire format  [HAVE / reference]

**Source:** `teardowns/LITSERVE.md §9`; `specs/openai.py:536-583`

`data: {chunk_json}\n\n` with `by_alias=True`, finish chunk with `finish_reason="stop"` + aggregate `usage`, then `data: [DONE]\n\n`. Multi-choice: `azip` of N per-choice generators so each SSE chunk contains all n deltas.

Lancelot's `@app.stream` emits raw `data: {payload}\n\n` SSE frames. No
built-in OpenAI `/v1/chat/completions` spec; that is user-side per cardinal
rule. **Reference only.**

---

## 24. BentoML `io_models.from_input` — signature → Pydantic model at decoration  [SKIP]

**Source:** `teardowns/BENTOML.md §2`; `io_models.py:335-417`

`create_model("Input", __base__=IODescriptor, **fields)` from
`inspect.signature(func)`. Gives OpenAPI schema + validation from the
method signature alone.

Lancelot routes are decorated with typed parameters; FastAPI-style DI
(`lancelot/di.py`) handles type extraction. The `create_model` approach is
more general (non-FastAPI targets, Starlette routes) but Lancelot's
conformance target is FastAPI semantics. **SKIP** — already correctly handled
by the existing DI pipeline.

---

## Summary table (actionable items only)

| # | Mechanism | Priority | Effort | File |
|---|---|---|---|---|
| 13 | `@app.job` async-task-poll API (202+poll) | HIGH | Medium | new `lancelot/job.py` |
| 19 | First-body-chunk deadline → 408 | HIGH | Small (Rust: `timeout` on body read) | `src/server.rs` |
| 1 | `max_concurrent_batches` on `@app.batch` | MEDIUM | Small (1 semaphore + kwarg) | `lancelot/batch.py` |
| 9 | Per-item request timeout in `@app.batch` | MEDIUM | Small (timestamp in tuple) | `lancelot/batch.py` |
| 8 | Overload shed 503 on batch semaphore full | MEDIUM | Follows from #1 | `lancelot/batch.py` |
| 3 | `asyncio.Event` instead of sleep in batch | LOW | Tiny | `lancelot/batch.py` |
| 12 | DNS-rebinding-resistant SSRF (at-connect) | LOW | Medium (monkeypatch uvloop) | `lancelot/net.py` |
| 10 | Validate first-yield-raises → 500 not 200 | VALIDATION | Test only | `tests/` |
| 18 | `_lifespan_supported` graceful degradation | VALIDATION | Code-read | `lancelot/asgi.py` |
| 20 | `TransportPool` stale-connection hardening | LOW | Tiny (httpx Limits) | `lancelot/llm.py` |
