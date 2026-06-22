# FastAPI Production Patterns — byte-path mine (cross-project corpus)

**Source cluster:**
- `researchfms/core/FASTAPI_PRODUCTION_PATTERNS.md` — 19-repo cross-project synthesis
- `researchfms/fastapi/patterns/VALIDATION_FINDINGS.md` — grep-validated guide verdict table
- `researchfms/fastapi/patterns/_patterns_draft.md` — guide-grounded scaffold
- `researchfms/teardowns/FASTAPI.md` §11 (SSE), §10 (threadpool), §3 (lifecycle), §7 (serialization)

**Method:** read each source in full; cross-checked against `lancelot/` source +
`CLAUDE.md` M1-M19 history; every mechanism is tagged HAVE / GAP / IMPROVE.
**Date:** 2026-06-22. **Additive mandate:** items already in `core.md`, `di_params.md`,
`openapi_security.md` are not re-documented here.

---

## 0. What this note adds

The three prior `02_fastapi/` notes cover FastAPI *internals* (DI resolver, params, OpenAPI,
SSE pump architecture). This note adds what those miss: **real-world production patterns
and anti-patterns extracted from 19 real codebases** — the byte-path implications of how
inference servers, agent frameworks, and web apps actually use FastAPI. The primary source
is `FASTAPI_PRODUCTION_PATTERNS.md` (cross-project synthesis, 2026-05-30).

---

## 1. Disconnect cancellation — ≥4 distinct patterns; one Lancelot already has the RIGHT shape

**Source:** `FASTAPI_PRODUCTION_PATTERNS.md §3.4`; `VALIDATION_FINDINGS.md §"Disconnect
cancellation"`.

**Finding:** FastAPI provides no built-in client-disconnect cancellation. Every streaming
system in the corpus rolls its own:

| Project | Mechanism | File reference |
|---|---|---|
| vLLM | `@with_cancellation` **dual-task race** — NOT `request.is_disconnected()` (explicitly documented as "broken under middleware"); `asyncio.wait({handler_task, disconnect_task}, FIRST_COMPLETED)` | `vllm/utils.py:57-99` |
| Letta | `asyncio.shield` to let the agent step finish + cancel by `.athrow()` into the generator — the agent step ALWAYS completes but post-step cleanup is cancelled | `letta/streaming_service.py` |
| Ray Serve | Socket close → `KeyError` → synthetic `http.disconnect` injected into the ASGI `receive` queue | `ray/serve/http_util.py:391-398` |
| Langflow | `DisconnectHandlerStreamingResponse` overrides `listen_for_disconnect` (Starlette hook) | `langflow/api/v1/utils.py` |
| Ludwig | `asyncio.wait_for(run_in_executor(...), timeout)` → **504** on timeout | `ludwig/serve.py` |

**Lancelot state:** HAVE — `asgi.py:599-635` implements `_race_handler_vs_disconnect` using
`asyncio.wait({task, wait_event}, FIRST_COMPLETED)` — the same dual-task shape as vLLM.
The comment on the function explicitly notes this equivalence ("same FIRST_COMPLETED
dual-task shape as TransportPool._await_cancellable"). vLLM's explicit rejection of
`request.is_disconnected()` ("broken under middleware") validates Lancelot's choice.

**Byte-path relevance / operator experience:** This is a marquee correctness feature —
without it, a disconnected client keeps the upstream LLM call alive + billing the operator.
Lancelot has the best-in-class approach (same as vLLM). **No action needed; verify the
`cancel_on_disconnect` route-level flag propagation under load (`asgi.py:1461-1479`).**

**Key anti-pattern to stay clear of:** `request.is_disconnected()` polling loop — broken
under any non-trivial ASGI middleware stack (Starlette BaseHTTPMiddleware wraps receive,
so the disconnect event never appears). vLLM confirmed this explicitly.

---

## 2. Streaming status code AFTER streaming starts — the post-200 limitation

**Source:** `FASTAPI_PRODUCTION_PATTERNS.md §3.3`.

**Finding:** Once SSE headers (200 + `text/event-stream`) are sent, an HTTP status change
is impossible — errors must be emitted AS an SSE chunk. The corpus approaches:
- **vLLM:** emits error as an SSE chunk with `"error"` key; additionally triggers a 5-second
  `watchdog_loop` that calls `self.should_exit = True` to force-restart the process on
  repeated stream errors (`launcher.py:144-167`).
- **Letta:** `StreamingResponseWithStatusCode` (`streaming_response.py:232-306`) peeks the
  first chunk for a `(content, status_code)` 2-tuple — if the first yield returns a tuple,
  use its status_code for the HTTP response headers before sending any body.

**Lancelot state:**
- Post-200 SSE error emitted as SSE chunk: HAVE — `asgi.py:1156-1168` emits
  `ProblemDetail` as a scrubbed RFC-9457 SSE chunk (not silently swallowed).
- `StreamingResponseWithStatusCode` pattern (peek first chunk for status): NOT implemented.
  Lancelot streams always start with 200 (or whatever the route status_code is).

**Byte-path relevance:** The `StreamingResponseWithStatusCode` pattern matters when a
handler wants to conditionally return 400 or 503 based on a condition checked at
stream-start time (e.g. invalid parameters that can only be checked after parsing the
request body). Without it, the handler must either validate before the generator or emit
the error as an SSE chunk. Medium priority DX gap. Not a throughput item.

**Lancelot check hint:** `lancelot/asgi.py:1093+` — the `@app.stream` path. Check if the
handler is allowed to yield a `(content, status_int)` tuple as its FIRST yield to set the
response status retroactively. This is what `lancelot/stream.py:6` notes as deferred with
"zero product citations" — the corpus now provides 1 citation (Letta).

---

## 3. Typed SSE event taxonomy — multi-field `event:` framing beyond OpenAI's `data:` only

**Source:** `FASTAPI_PRODUCTION_PATTERNS.md §3.3`.

**Finding:** Production agent SSE streams use richer event types than the bare
`data: {json}\n\n` + `data: [DONE]\n\n` wire format:

- **Letta:** `event: message` / `event: reasoning` / `event: tool_call` / `event: tool_return` /
  `event: assistant` + `event: error` + `LettaPing` keepalive (SSE comment) carrying
  `last_seq_id`. Resumable via Redis Streams (`XADD MAXLEN ~ <stream> seq_id <id> data <json>`
  + cursor replay from `seq_id`).
- **R2R:** `yield_sse_event` produces types `search_results` / `thinking` / `tool_call` /
  `citation` (first-occurrence-only dedup) / `final_answer` / `done`.
- **LangServe:** `/stream_log` endpoint streams **RFC-6902 JSONPatch** ops (`{"ops":[...]}`)
  via `astream_log(diff=True)` — each chunk is a diff, not a full state snapshot.

**Lancelot state:**
- `ServerSentEvent` model in `lancelot/sse.py` (via FastAPI's `sse.py` conformance path):
  supports `event:`, `id:`, `retry:`, `data:`, `comment:` fields. The user can set `event=`
  to any string. The typed taxonomy above is user-side (the user sets `event="tool_call"` on
  their `ServerSentEvent`). **HAVE** at the infrastructure layer.
- Redis-backed resumable streams: NOT built (correctly deferred per `stream.py:6` comment;
  "zero product citations" was accurate at time of writing — Letta is the one production
  citation, but this is stateful storage = Tier-D / user-side).
- JSONPatch SSE (LangServe): user-side protocol; Lancelot's SSE framing is agnostic.
  **HAVE** at the infrastructure layer.

**Byte-path relevance:** Infrastructure-level HAVE. The user-side typed taxonomy is not
Lancelot's concern. The one NEW finding is Letta's `LettaPing` SSE keepalive includes
`seq_id` data — Lancelot's keepalive is `b": ping\n\n"` (comment, no data). This is
a documentation/DX item: operators implementing resumable streams can use `id:` + `retry:`
fields Lancelot already supports, not a missing feature.

---

## 4. Blocking sync in async routes — the one real anti-pattern (rare but real)

**Source:** `VALIDATION_FINDINGS.md §8`; `FASTAPI_PRODUCTION_PATTERNS.md §5`.

**Finding:** Raw grep showed 792 `requests` + 815 `time.sleep` calls across the 19 repos,
but AST-level scan (tests excluded) found only **9 genuine in-async-route violations** in
5 repos:
- **r2r `api/v3/users_router.py:1608/1676/1696`** — `requests.post/get` (sync HTTP) inside
  `async def` OAuth handlers. Each call blocks the event loop for the entire TCP handshake +
  HTTP round-trip (≥50-200ms in LAN, worse across the internet).
- **letta `builtin_tool_executor.py:387`** — sync blocking call inside async tool executor.

**Lancelot state:** Lancelot's `_race_handler_vs_disconnect` and `pool.py:offload` offload
sync handlers to a bounded threadpool. **The per-request check in `asgi.py:1472`:
`_isp_local.isasyncgenfunction(route.handler)` detects streaming handlers.** The analogous
`is_coroutine` check (from `routing.py` DI plan compilation) ensures non-coroutine handlers
go to the pool. **HAVE** — the framework-level protection is correct.

**Byte-path relevance:** This is a user-code anti-pattern Lancelot is already protected
against at the framework level (sync handlers automatically offloaded). The NEW finding
is quantitative: only 9/792 = ~1% of `requests` calls are genuine in-async violations. The
guide's warning is valid but 90× overstated. No framework action needed.

---

## 5. `run_in_executor` vs `run_in_threadpool` — production norm

**Source:** `VALIDATION_FINDINGS.md §7`; `FASTAPI_PRODUCTION_PATTERNS.md §2`.

**Finding:** Real production code overwhelmingly prefers `asyncio.run_in_executor` (67 hits,
12 repos) over Starlette's `run_in_threadpool` (16 hits, 3 repos). The guide's specific
recommendation of `run_in_threadpool` is the minority in practice.

**Lancelot state:** `lancelot/pool.py:50-69` implements `OffloadPool.offload()` which calls
`loop.run_in_executor(None, ...)` (with a bounded inflight gate). This is the dominant
production idiom. **HAVE** — and better than FastAPI's unbounded threadpool (Lancelot's
pool provides backpressure).

**Byte-path relevance:** The bounded gate prevents threadpool exhaustion under high
concurrency (Starlette/FastAPI uses the default asyncio executor = 40-slot unbounded
at the framework level). Lancelot's bounded pool prevents the 41st concurrent sync
handler from stalling indefinitely. **Already Lancelot's superiority claim.**

---

## 6. `app.state` injection for inference engines — the inference-server DI pattern

**Source:** `FASTAPI_PRODUCTION_PATTERNS.md §3.2`; `VALIDATION_FINDINGS.md §"Archetype 2"`.

**Finding:** vLLM's canonical injection pattern (`api_server.py:108-154`) uses a spanning
`@asynccontextmanager` that creates the engine and writes it to `app.state`:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    engine_client = AsyncEngineClient(...)
    async with engine_client:
        app.state.engine_client = engine_client  # ← single object for the process
        yield
```

Then routes access it via `request.app.state.engine_client`. NOT via `Depends` (the
engine is one object for the whole process lifetime, not per-request).

**Lancelot state:** `lancelot/context.py` has per-request ContextVars. The `app.state`
(Starlette `State`) is accessible via `request.app.state`. Lancelot's lifespan
mechanism (`@app.on_event("startup")` / `lifespan=` ASGI CM) allows the same pattern.
**HAVE** — the mechanism is present; users can do `app.state.engine = MyEngine()` in
lifespan. No new code needed.

**Lancelot check hint:** `lancelot/asgi.py` — verify `request.app.state` is accessible
on `Request` objects (it's Starlette State, passed through `scope["app"]`). Confirm
`scope["app"] = self` is set at `asgi.py:644+`.

**Byte-path relevance:** Operator experience only. The engine-in-`app.state` pattern
is the canonical Archetype 2 (inference server) idiom — Lancelot needs to document
this clearly for AI serving use cases. No code gap.

---

## 7. Dynamic route registration on a LIVE app — Hayhooks pattern

**Source:** `FASTAPI_PRODUCTION_PATTERNS.md §3.5`.

**Finding:** Hayhooks (`deploy_utils.py:519-554`) mutates the live app:
1. `app.routes.remove(old_route)` (semi-private `app.routes` list)
2. `app.add_api_route(path, handler, ...)` with new handler
3. `app.openapi_schema = None; app.setup()` — forces OpenAPI regeneration

The serialization is: ThreadPoolExecutor for parallel preprocessing (copy_context() for
ContextVar propagation), then serial `app.routes` mutation, then one OpenAPI rebuild.

**Lancelot state:** `lancelot/app.py` — Lancelot has `lifetime_task` for background
tasks, but NOT a `add_api_route` / `app.routes.remove` live-mutation path. Adding a
route to a running app would require re-running the matchit router registration in Rust
(`_native.run` receives the route list at startup). The Rust router is NOT designed
for runtime mutation.

**Byte-path relevance:** NOT a byte-path item for Lancelot's positioning (cleartext
behind a TLS proxy, not Hayhooks-style dynamic route serving). Also an anti-pattern
(`app.routes.remove` + `app.setup()` couples to FastAPI internals, cited explicitly
as an anti-pattern in §5). Mark as Tier-D for Lancelot.

---

## 8. Serving-broker family — patterns to validate `@app.batch` completeness

**Source:** `FASTAPI_PRODUCTION_PATTERNS.md §3.7`; `VALIDATION_FINDINGS.md §"Archetype 2"`.

**Finding:** The three broker patterns in production:

**(a) LitServe** (`loops/base.py:99-166`): mp.Queue (one per worker process) + per-request-uid
`asyncio.Event` for response demux + dynamic batching loop (collects items until `max_batch`
or `timeout`, processes as a batch, fans back out by uid). Responses are delivered via
a ZMQ pubsub pattern (one pub socket, N subscriber sockets keyed by uid). The batching
window is strictly bounded by `timeout`; no adaptive/learned sizing.

**(b) BentoML's `CorkDispatcher`**: learns the handler's latency model `≈ o_a·n + o_b`
via `numpy.linalg.lstsq` (periodic rolling fit). Fires when `latency_budget × 0.95`
is exhausted or batch size reached. Sheds to 503 when queue depth exceeds cap. The
adaptive approach means it self-tunes to the actual model without needing the user to
calibrate `batch_window_ms`.

**(c) vLLM engine**: in-process async engine queue; the engine IS the rate-limiter;
requests are added to the engine's internal scheduler; no external queue.

**Lancelot state:** `@app.batch` (`lancelot/batch.py`) is the Lancelot primitive. It
uses a fixed `batch_window_ms` + `max_batch` window (LitServe-style, not CorkDispatcher
adaptive). The fix in M3 (skip `timer.cancel()` when running inside the timer task) is
the equivalent of the LitServe bug surface. **HAVE** — and validated under real load
(~12×/~16× win). **Gap:** no adaptive/learned latency sizing (CorkDispatcher).

**Byte-path relevance:**
- The CorkDispatcher learned-latency approach (`np.linalg.lstsq`) is a non-trivial
  numerical approach that adds `numpy` as a dependency (violates cardinal rule #3:
  runtime dep = pydantic only). This is explicitly Tier-D for Lancelot.
- The ZMQ demux (LitServe) is similarly out of scope (separate dep, inference-specific).
- Lancelot's fixed-window `@app.batch` is the right approach for the byte-path mission.
  **No action needed; the CorkDispatcher-style learned sizing is Tier-D.**

---

## 9. Sentry's 2-phase route-template transaction naming — cardinality guard

**Source:** `FASTAPI_PRODUCTION_PATTERNS.md §4` ("Two-phase transaction naming");
`VALIDATION_FINDINGS.md §13`.

**Exact mechanism:** Sentry's FastAPI instrumentation (`sentry_sdk/integrations/fastapi.py:52-75`)
renames the transaction from the RAW URL to the ROUTE TEMPLATE only after Starlette's
router populates `request.scope["route"].path`. Before routing, the name would be
`/users/abc123` (per-user, high cardinality). After routing, it becomes `/users/{user_id}`
(low cardinality). The two-phase rename is necessary because the `scope["route"]` key
isn't populated until Starlette's match loop completes.

**Lancelot state:** `lancelot/metrics.py:138-161` — `record_request(route_path, method, ...)`.
The `route_path` is passed in from the ASGI dispatch seam at `asgi.py`. **Check:**
is `route_path` the template (`/users/{user_id}`) or the resolved path (`/users/abc123`)?

**Lancelot check hint:** `lancelot/asgi.py:638+` — the `_http_dispatch` function. Find
where `route_path` for `record_request` is set. If it's `scope["path"]` (the raw URL),
that's the high-cardinality bug. It should be `route.path` (the template,
e.g. `/users/{user_id}`) from the matched route object. From CLAUDE.md M7: the
"metric cardinality" fix was T2.4 + T2.5, shipped. Verify this is template-based,
not raw-URL-based.

**Byte-path relevance:** Metric cardinality is a real operator pain — with raw URLs in
`http.route`, a 404 flood or high-user-count app explodes Prometheus/OTel cardinality,
causing metric store OOM. Lancelot likely already has this correct (M7 T2.4 is the
cardinality fix), but the Sentry pattern gives the exact mechanism to verify against.

---

## 10. `enqueue_only_on_commit` — Polar's outbox semantics

**Source:** `FASTAPI_PRODUCTION_PATTERNS.md §4`.

**Exact mechanism:** Polar's `_enqueue.py` + `middlewares.py:47-57`: task enqueue is
buffered into a request-scoped list during the handler; the buffer is flushed to
Dramatiq (the task queue) ONLY on clean route exit (no exception). If the handler raises,
the buffer is discarded — so a task is never enqueued for a transaction that rolled back.
This is the "outbox pattern without an outbox table" — atomicity between DB commit and
task queue via the request lifecycle.

**Lancelot state:** `lancelot/background.py` — `BackgroundTasks` adds tasks to a list
and runs them after response send. Tasks are NOT discarded on handler exception (they
run regardless in Starlette's `BackgroundTasks`). For "run-only-on-success" semantics,
the user must add the task inside a try/finally (or only add it after the main work
succeeds).

**Byte-path relevance:** This is a correctness pattern for transactional correctness,
NOT a byte-path win. Lancelot's `BackgroundTasks` behavior matches FastAPI (run after
response, regardless of handler outcome). The Polar pattern is an application-level
convention on top. **No Lancelot action needed.** Document in operator guide if needed.

---

## 11. Timing-attack-safe auth — always hash even on user miss

**Source:** `FASTAPI_PRODUCTION_PATTERNS.md §4` ("Timing-attack-safe auth").

**Exact mechanism:** `full-stack-fastapi-template/crud.py:40-60`: `authenticate()` always
calls `pwd_context.verify(DUMMY_HASH)` even when the user is not found (before returning
`None`). Without this, the timing difference between "user not found" (returns immediately)
and "user found but wrong password" (hashes + compares) leaks whether the user exists.

**Lancelot state:** Lancelot does not provide auth utilities (by design). The
security primitives (`HTTPBearer`, `APIKeyHeader`, etc.) extract and forward credentials
but do NOT verify passwords. Password hashing is user-side. **Correctly NOT in Lancelot.**

**Byte-path relevance:** User-side, not framework-side. Not a Lancelot action item.
Document as an operator guide pattern.

---

## 12. IoC container over `Depends` for large apps — dishka / Langflow ServiceManager

**Source:** `FASTAPI_PRODUCTION_PATTERNS.md §3.2`.

**Finding:** Two production systems outgrow `Depends` and build IoC containers:
- **Bitcart's dishka** (`ioc/starlette.py:13-31`): `DishkaRoute` + `FromDishka[Service]` —
  per-request `DIScope.REQUEST` container; factory defined once, resolved everywhere
  (including from lifespan AND sync threads that `Depends` cannot reach).
- **Langflow's `ServiceManager`** (`services/manager.py`): `get_service(ServiceType, factory)`
  over a global registry, lazy + `KeyedMemoryLock`-guarded + recursive (deps inferred from
  factory's `create()` type hints).

**Lancelot state:** `Depends` is the mechanism. For large apps, Lancelot users can use
dishka or any IoC container via `route_class=DishkaRoute` (since M2 conformance push 4,
custom `APIRoute.get_route_handler()` is shipped). **HAVE** at the infrastructure level
(custom route class support enables dishka integration).

**Byte-path relevance:** DX / architecture pattern for large apps. Not a byte-path item.
The one load-bearing Lancelot note: the `KeyedMemoryLock` in Langflow's ServiceManager
(a per-type async lock ensuring a service is initialized exactly once under concurrent
startup) is the same problem Lancelot solved with `PyOnceLock` for pydantic_core
memoization (the GIL × OnceLock deadlock lesson). **Both are the same pattern.**
Not a new action item.

---

## 13. OpenAI-compatible Pydantic request model patterns

**Source:** `FASTAPI_PRODUCTION_PATTERNS.md §3.7`; vLLM, LitServe, Hayhooks entries.

**Finding:** All inference servers in Archetype 2 use a common OpenAI-compatible wire format:
```
POST /v1/chat/completions   → {"model":..., "messages":[...], "stream":bool, ...}
GET  /v1/models             → {"data": [{"id": model_id, ...}]}
```
The models are defined as Pydantic subclasses of OpenAI's request/response schemas.
vLLM serves 3 vendor dialects (OpenAI Chat/Responses + Anthropic Messages) over one engine.

**Lancelot state:** Lancelot does NOT provide OpenAI-compatible Pydantic models (by
design — AI logic stays user-side). The framework serves any handler, including one
that accepts `ChatCompletionRequest`. **Correctly NOT in Lancelot.**

**Byte-path relevance:** Lancelot's `@app.batch` is the natural companion to these
models — a handler that accepts `ChatCompletionRequest` and is decorated with `@app.batch`
gets coalescing for free. The documentation could show this pattern explicitly.

---

## 14. Custom `APIRoute` subclass — the production power-tool for cross-cutting behavior

**Source:** `FASTAPI_PRODUCTION_PATTERNS.md §3.1`.

**Finding:** Five production repos subclass `APIRoute` for cross-cutting behavior:
- **Polar `AutoCommitAPIRoute`** (`kit/routing.py:15-44`): after handler returns,
  find the `AsyncSession` dep and call `await session.commit()`. Endpoints return ORM
  objects; commit is automatic.
- **prefect `PrefectAPIRoute`** (`server/utilities/server.py:30-87`): response-scoped
  `AsyncExitStack` so yield-deps close BEFORE body serialization (tighter than default
  request scope).
- **bitcart `DishkaRoute`** + **letta 18× `route_class`** — IoC and streaming-aware
  handling.

**Lancelot state:** M2 conformance push 4 shipped `APIRoute.get_route_handler()` opt-in
and `Router.route_class` / `APIRouter(route_class=...)`. **HAVE** — the extension point
exists. The `AutoCommitAPIRoute` pattern works on Lancelot.

**Byte-path relevance / operator experience:** The key message: the `route_class=` pattern
is a first-class power tool that the guide never mentions. Lancelot has it (99.6%
conformance). Document this in the operator guide as an advanced extension pattern.

---

## 15. `request.is_disconnected()` — documented as broken under middleware

**Source:** `FASTAPI_PRODUCTION_PATTERNS.md §3.4`; vLLM `utils.py:57-99`.

**Exact mechanism:** vLLM's `@with_cancellation` decorator comment reads:
> "We cannot use `request.is_disconnected()` because it is broken when middleware is
> involved (e.g. add_middleware)."

The root cause: `BaseHTTPMiddleware` (and any middleware using `call_next`) wraps the
`receive()` ASGI callable. When the client disconnects, the disconnect event arrives in
the wrapped `receive()`, but `request.is_disconnected()` (Starlette's polling helper)
may not see it because the middleware's wrapped `receive` has already consumed the event
or is buffered differently.

**Lancelot state:** Lancelot's disconnect mechanism does NOT use `is_disconnected()`.
`_watch_disconnect` (`asgi.py:574`) reads from `receive` directly (not via the Starlette
`Request.is_disconnected()` helper). **HAVE** — and superior to polling-based approaches.

**Byte-path relevance:** This confirms Lancelot's architectural correctness. The direct
`receive` approach in `_watch_disconnect` is the right design. Note for documentation:
if users bring their own Starlette `BaseHTTPMiddleware`, the `receive` wrapping could
in theory affect the disconnect event path — worth noting in the middleware compatibility
section.

---

## 16. Unbounded queues + insufficient backpressure — the production anti-pattern

**Source:** `FASTAPI_PRODUCTION_PATTERNS.md §5`.

**Finding (from real code):**
- **LitServe**: `manager.Queue()` (Python's `queue.Queue`, unbounded) — the only guard is
  a per-request timeout. Under sustained overload, the queue grows without bound → OOM.
- **Ludwig**: `asyncio.wait_for(run_in_executor(...), timeout)` — cancels the `await` on
  timeout but does NOT cancel the underlying thread. The thread keeps running (consuming
  CPU/memory) even after the client got a 504.

**Lancelot state:**
- `@app.batch`: `max_batch` + `batch_window_ms` bounds the queue. **HAVE.**
- `lancelot.memory_pool.MemoryPool`: named-consumer memory reservation with
  `ResourcesExhausted` on overuse. **HAVE.**
- `TenantConcurrencyLimiter`: per-tenant `asyncio.Semaphore` prevents one tenant from
  monopolizing slots. **HAVE.**
- `LANCELOT_MAX_CONNECTIONS`: connection-cap semaphore in Rust. **HAVE.**

**Byte-path relevance:** Confirms Lancelot's existing resilience posture covers the
anti-pattern. The one check: does `@app.batch` have a MAX QUEUE DEPTH guard? If 10,000
concurrent requests arrive simultaneously, do they all queue up in the batch window
(`_pending: list`) without bound? **Check `lancelot/batch.py:69`** — `pending` is a
plain list; there is no cap on how many requests can accumulate between flushes.
If `max_batch=10` and the batch window is 50ms with 10,000 concurrent requests, 999
batches queue up. This is bounded by `LANCELOT_MAX_CONNECTIONS` at the Rust level (1024
default), so the effective max queue depth is 1024. OK as-is but worth documenting.

---

## 17. Schema-switch multi-tenancy pattern

**Source:** `FASTAPI_PRODUCTION_PATTERNS.md §3.6`; `teardowns/DISPATCH.md`.

**Exact mechanism:** Netflix Dispatch's `db_session_middleware` (`main.py:138-142`) sets
`execution_options(schema_translate_map={"dispatch_core": slug})` on the SQLAlchemy
session based on the org slug in the URL path. This switches every query to the
per-tenant Postgres schema transparently. Per R2R: `ProjectSchemaMiddleware` uses a
regex-validated `x-project-name` header + `set_schema_path()` contextvar.

**Lancelot state:** Lancelot has `TenantConcurrencyLimiter` (for rate-limiting) and
`LANCELOT_TENANT_LABEL_MAX` (for metric cardinality). Schema-level tenancy is
application-level (Lancelot doesn't own the DB layer). **Correctly NOT in Lancelot.**

**Byte-path relevance:** Not relevant to the byte path. Document as an operator guide
pattern for the "DB-backed web app archetype".

---

## Summary — net new findings vs existing `02_fastapi/` notes

| # | Mechanism | Lancelot status | Action |
|---|-----------|-----------------|--------|
| 1 | Disconnect cancel (dual-task race, NOT `is_disconnected`) | HAVE (asgi.py:599-635) | Verify under middleware stack; document in operator guide |
| 2 | Post-200 SSE error as SSE chunk | HAVE (ProblemDetail emission) | GAP: `StreamingResponseWithStatusCode` first-chunk status peek — medium DX value |
| 3 | Typed SSE event taxonomy (event: field) | HAVE (infrastructure level) | Document usage in operator guide |
| 4 | Blocking-in-async (rare ~1%, framework protected) | HAVE (pool.py offload) | No action; lancelot already better than FastAPI |
| 5 | `run_in_executor` vs `run_in_threadpool` | HAVE + better (bounded pool) | No action |
| 6 | `app.state` engine injection (inference-server DI) | HAVE (Starlette State passthrough) | Verify `scope["app"]` set; add to operator guide |
| 7 | Live route add/remove (Hayhooks) | Tier-D (Rust router not designed for mutation) | Explicitly refuse; document as anti-pattern |
| 8 | CorkDispatcher adaptive learned batching | Tier-D (numpy dep + inference-specific) | Refuse; `@app.batch` fixed window is correct for byte-path |
| 9 | Route-template metric naming (2-phase, Sentry) | Likely HAVE (M7 T2.4) | Verify `asgi.py` uses `route.path` not `scope["path"]` for RED metrics |
| 10 | Outbox-on-commit pattern (Polar) | Application pattern, not framework | Document in operator guide |
| 11 | Timing-attack-safe auth (DUMMY_HASH) | User-side; not Lancelot's | Document in operator guide |
| 12 | IoC container (dishka/ServiceManager) | HAVE via `route_class=` | Document integration pattern |
| 13 | OpenAI-compat Pydantic request models | User-side; not Lancelot's | Show `@app.batch` + OpenAI model example in docs |
| 14 | Custom `APIRoute` subclass | HAVE (M2 push 4) | Document in operator guide as power-tool |
| 15 | `request.is_disconnected()` broken under middleware | HAVE (use direct receive) | Document in middleware compat note |
| 16 | Unbounded queue anti-pattern | HAVE (connection-cap + batch bounds) | Verify `@app.batch` pending list bounded by connection cap |
| 17 | Schema-switch multi-tenancy | Application-level; not Lancelot's | Document in DB-app operator guide |

### Highest-value actionable findings from this pass

1. **StreamingResponseWithStatusCode first-chunk status peek (§2):** Medium DX gap. One
   citation (Letta). Allows conditionally returning 4xx based on a condition only known
   at stream-start time. Implementation: in the `@app.stream` path, if the handler's first
   yield is a `(content, int)` tuple, use the int as the response status code and content
   as the first chunk. `lancelot/asgi.py:1093+`.

2. **Verify route-template metric naming (§9):** Low risk but worth a 5-minute grep.
   If `record_request` is called with `scope["path"]` instead of `route.path`, Prometheus
   will explode under user-ID-in-URL patterns. Grep `asgi.py` for the `record_request`
   call site and confirm the route_path arg is the template.

3. **Document `app.state` engine injection (§6):** Add a one-page operator guide entry
   showing the canonical Archetype 2 (inference server) Lancelot setup. No code change.

4. **Document `route_class=` custom APIRoute as a power-tool (§14):** Zero-code-cost
   operator DX win. The `AutoCommitAPIRoute` pattern works on Lancelot today.

---

*Source: `researchfms/core/FASTAPI_PRODUCTION_PATTERNS.md` + `VALIDATION_FINDINGS.md` + `_patterns_draft.md` + `teardowns/FASTAPI.md §11,10,3,7`. Mined 2026-06-22 by subagent (claude-sonnet-4-6).*
