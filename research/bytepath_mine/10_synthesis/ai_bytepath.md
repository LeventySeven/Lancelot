# Domain synthesis — AI byte-path (TransportPool · @app.batch · serving-grade batching · MCP · prompt-cache serialization)

**Synthesizer:** ai_bytepath. **Date:** 2026-06-22.
**Mission lens:** own the byte path for AI/cloud/web/startups; the user owns the model. "Just build, brutal" — every win must change the byte path or the operator experience, or it doesn't ship.
**Sources diffed:** `04_serving/serving_frontier.md` (SGLang/TGI/Ray Serve/Groq), `04_serving/gateways_deploy.md` (LiteLLM/Portkey/Kong/Railway/Render), `02_fastapi/proj_inference.md` (vLLM), `02_fastapi/proj_serving.md` (LitServe/BentoML/Ray Serve/Modal), `02_fastapi/proj_auth_obs_ai.md` (LangServe/R2R/Hayhooks), `05_lancelot_state/{bytepath,research_plans_ledger}.md`.
**Baseline verified in source:** `lancelot/batch.py:159-186` (fan semantics), `lancelot/llm.py:376-490` (retry/backoff/jitter), `lancelot/app.py:313-321` (`/readyz`), `src/service.rs:505-523` (header timeout, no body timeout).

---

## The positioning lock for this domain (do not drift)

The AI byte-path moat is **the one Rust-owned, backpressured, cancellation-correct system that spans the inbound HTTP stream AND the outbound LLM call**, behind FastAPI's exact ergonomics. Lancelot already ships the marquee primitives: `TransportPool` (failover/cost/cache/cancel), `@app.batch` (prefix coalescing), `lancelot.mcp.mount`, `canonical.dumps_canonical` (prompt-cache stability), `Response.set_session_affinity` (KV-cache routing hint), `lancelot.upstream_metrics` (cost/token/TTFT taxonomy). The serving-infra mine confirms these are the *right* primitives — every model server (SGLang/TGI/vLLM) and gateway (LiteLLM/Portkey) converges on the same shapes. **The additive wins are completions of the batch primitive and one cancellation gap, not new surface.** Everything that hosts the model, routes between providers, or masks logits is REFUSED (user-side).

**Hard boundary that kills half the candidates:** Lancelot is single-provider per `TransportPool` instance and is NOT a load balancer. Every multi-deployment / multi-key / P2C-routing / model-cache mechanism from the gateways and Ray Serve is `app.llm`-router-layer or the operator's LB — out of scope for the transport pool itself.

---

## MUST-BUILD (additive, in-mission, cited)

### 1. `@app.batch` in-batch dedup — send ONE representative per identical key, fan the result to all waiters  [missing] [HIGH]

**Mechanism (source):** SGLang `schedule_policy.py:160,283` builds a throwaway simulated RadixCache over the waiting queue; requests sharing `>= 32` tokens of prefix with an already-seen request are deprioritized (sort key `float("inf")`) so ONE runs first, populates the cache, and the rest hit it. The HTTP analog: when N concurrent requests coalesce under the *same* `@app.batch(key=...)` value, only the canonical representative needs to go upstream; identical-key waiters receive the same result.

**Lancelot state (verified):** `lancelot/batch.py:159-162` — `_run_batch` does `payloads = [p for p, _ in batch]; results = await self.handler(payloads)` and fans `results[i]` back per-waiter. It sends **all N payloads** to the handler. If 100 users share one system prompt + identical body, the handler gets a 100-element list and (if it 1:1-maps to upstream) makes 100 upstream calls. There is no "first wins, rest reuse" path. This is the single biggest AI-serving win still on the table: for the repeated-prompt case (RAG with a shared system prompt, retry storms, identical health-style polls) it turns the current ~16× coalescing into N× dedup.

**Recommended approach:** opt-in `@app.batch(dedup_key=lambda item: ...)` (distinct from the routing `key=` that buckets into groups). On flush, within a group, partition `pending` by `dedup_key`; for each distinct dedup-value, send ONE representative payload to the handler, then `set_result` the same object to every waiter in that partition. Handler contract unchanged (still receives a `list`, just deduplicated length). Identity/None dedup_key → today's behavior (zero overhead, zero risk). **Validate-under-load:** real-server test with 200 identical-key requests → assert exactly 1 upstream call observed and 200 correct responses (the M4 lesson: the awaiting-handler path is where batch bugs live).

**Buildability:** M. **Gate risk:** perf — the partition is O(n) per flush on an already-small batch (bounded by `max_batch`), negligible; conformance untouched (`@app.batch` is Lancelot-only, no vendored tests); clippy untouched (pure Python). Risk is the fanout-correctness edge (a waiter that disconnected mid-window) — covered by the existing `if not f.done()` guard.

---

### 2. `@app.batch(max_concurrent_batches=N)` — pipeline the next window while the current upstream call is in flight  [missing] [HIGH]

**Mechanism (source):** Ray Serve `batching.py:439-444` wraps `_process_sub_batches` in `asyncio.Semaphore(max_concurrent_batches)` and immediately starts filling batch k+1 while batch k runs on the GPU. BentoML's `CorkDispatcher` `NonBlockSema` (`dispatcher.py:253-266`) is the same idea (default 1 concurrent outbound).

**Lancelot state (verified):** `lancelot/batch.py` — `_run_batch` is `await`ed inside `_flush`; the next window does not start accumulating until the current handler returns. For an LLM upstream taking 200-800 ms, windows serialize: throughput is capped at `batch_size / handler_latency`, leaving the accumulation window idle during the upstream call.

**Recommended approach:** add `max_concurrent_batches: int = 1` kwarg; wrap `_run_batch` in an `asyncio.Semaphore(max_concurrent_batches)` held on the `_BatchRegistry` (shared across groups so the cap is global per route, not per key). Default 1 = exactly today's behavior. `max_concurrent_batches=2` doubles throughput whenever upstream latency > window_ms. Pairs naturally with #3 (the semaphore is also the backpressure point for the shed).

**Buildability:** S (one semaphore + kwarg). **Gate risk:** perf — pure win for rate-limited upstreams (the `@app.batch` premise); conformance/clippy untouched. The only correctness note: the semaphore must not be held across the accumulation wait, only across `_run_batch`, or it serializes anyway.

---

### 3. `@app.batch` per-item request timeout → cancel stale waiters before the handler runs  [missing] [HIGH]

**Mechanism (source):** BentoML `dispatcher.py:227-230` cancels a queued request's future once it has waited longer than `max_latency` ("a request that ALREADY timed out wastes GPU cycles"). LitServe `loops/base.py:111-130` checks the enqueue `time.monotonic()` and returns 504 for over-aged requests instead of forwarding them.

**Lancelot state (verified):** `lancelot/batch.py:95` — `submit` stores `(payload, Future)` with no timestamp. An item enqueued at t=0 with `window_ms=50` but stuck behind a 2 s handler (or behind a full `max_concurrent_batches` semaphore once #2 lands) is still served. This is the *same class of pileup bug* that made `@app.batch` break under adversarial load in M4 (timer-flush self-cancel). Under overload it lets the queue run away.

**Recommended approach:** `@app.batch(request_timeout_ms=N)`; store `monotonic()` in the tuple at `submit`; in `_flush`/`_run_batch`, before invoking the handler, drop any waiter whose age > `request_timeout_ms` with a `504`-mapped `BatchTimeoutError` (or `asyncio.TimeoutError`) on its future. Default `None` = today's behavior. **Validate-under-load** alongside #2 (the shed and the pipeline are one coherent backpressure story).

**Buildability:** S (timestamp in tuple + age check). **Gate risk:** perf negligible; conformance/clippy untouched. Correctness: must set the future exception, not just skip, or the waiter hangs forever — exactly the M4 failure mode, so it gets a real-server test.

---

### 4. `@app.batch` overload shed → 503 + Retry-After when the pipeline is saturated  [partial] [MEDIUM]

**Mechanism (source):** BentoML `dispatcher.py:121-122` raises `ServiceUnavailable(503)` when the `NonBlockSema` is held and a request hits the concurrency cap. This is the missing backpressure terminus that #2's semaphore enables.

**Lancelot state (verified):** `batch.py:188-229` — `BatchOverloadError` → 503 fires ONLY on the registry `max_keys` cap (too many distinct keys). There is NO shed for *compute* overload: when handlers run slower than the window and batches pile up. Once #2 adds `max_concurrent_batches`, the correct shed point is "semaphore locked AND queue depth over threshold → 503 + `Retry-After: 1`".

**Recommended approach:** follows directly from #2 — when the `max_concurrent_batches` semaphore is locked and the per-group `pending` exceeds `max_batch` (a second full batch already waiting with nowhere to go), `submit` raises `BatchOverloadError(retry_after=1)`. Reuses the existing 503-mapping ASGI path. Name the responsible knob in the shed body (ledger convention: "name the knob in the 503").

**Buildability:** S (follows from #2). **Gate risk:** none new (reuses `BatchOverloadError` → 503 path). Bundle PR with #2/#3.

---

### 5. First-body-chunk deadline → 408 (slow-loris variant the Rust header timeout misses)  [missing] [HIGH]

**Mechanism (source):** Modal `_runtime/asgi.py:21,121-160` — `FIRST_MESSAGE_TIMEOUT_SECONDS = 5.0`: a deadline on receiving the first ASGI `http.request` message; on expiry it synthesizes `{"type":"http.response.start","status":408}` + a body and disconnects. Closes the "headers sent, body stalled forever" hold.

**Lancelot state (verified):** `src/service.rs:523` — `header_read_timeout(header_timeout_ms())` (`LANCELOT_HEADER_TIMEOUT_MS`, default 15 000 ms) fires only while the HTTP *header* is incomplete. Grep confirms **no `body_timeout` / `BODY_TIMEOUT` / first-message timeout anywhere in `src/`.** Once hyper parses the headers and the Rust→Python bridge fires, a client that sent complete headers but stalls the body holds a connection slot indefinitely — a slow-loris variant that bypasses the header guard. This is a measured resilience gap for an AI server (a malicious or buggy client opening many header-complete-body-stalled connections exhausts `LANCELOT_MAX_CONNECTIONS`).

**Recommended approach:** `LANCELOT_BODY_TIMEOUT_MS` (default e.g. 30 000, 0 = disable for legitimately-slow large uploads) in `src/service.rs`: wrap the body-collection future (the `Limited` body read, already present for the 16 MiB cap) in a `tokio::time::timeout`; on elapse return `408 Request Timeout` before any handler dispatch. Clamp `[100ms, 600s]` like the other knobs. **Validate-under-load:** subprocess test that sends headers then holds the body socket open → assert 408 within the deadline and the connection slot is released (not a hang).

**Buildability:** M (Rust; the timeout wrap is small, but it touches the hyper body path that the body cap already guards, so it needs the cargo gate + a real-server adversarial test). **Gate risk:** clippy (Rust change → `cargo clippy --release --all-targets -- -D warnings` must stay clean); perf neutral (timeout only arms during body read); conformance untouched (no vendored test exercises a stalled body). Care: must not fire on legitimate streaming-request-body uploads — default conservative, document the 0-disable for upload-heavy routes.

---

### 6. Three-state health: `/readyz` → 503 + Retry-After during the startup window (readiness gate)  [missing] [HIGH]

**Mechanism (source):** SGLang `/health_generate` + watchdog model and TGI `Warmup` gRPC both refuse traffic until the engine can actually serve; the universal serving rule is "a new replica must report not-ready until startup completes, or the LB routes traffic to a half-initialized worker." Five projects in the corpus converge on a readiness-callback hook (ledger ROUND 2 ADAPT: "Readiness-callback hook → /readyz 503").

**Lancelot state (verified):** `lancelot/app.py:313-321` — `_readyz` returns 503 ONLY when `self._shutting_down`. Grep confirms **no `_starting_up` / `startupz` / lifespan-complete / readiness-callback flag.** During the window between `run()` binding the port and the `startup` lifespan completing (DB pool, LLM client, secrets warm), `/readyz` returns **200** — a false-positive that makes every LB probing `/readyz` route real traffic to a worker whose dependencies aren't up. This is a correctness bug for deploy-and-roll (the exact AI-serving deploy model).

**Recommended approach:** add a `_ready` flag (default False at `__init__`, flipped True after the `startup` lifespan completes in `asgi.py`/`app.run()`) AND an optional `App(readyz_check=callable)` hook (5-project convergence). `_readyz` returns 503 + `Retry-After: 1` with `{"status":"starting"}` while `not self._ready`, then `{"status":"ok"}`, then `{"status":"draining"}` on shutdown — the full three-state machine. The user hook lets AI operators gate on "LLM client reachable" without owning the route. Keep `/livez` as the const-tier zero-cost process-alive probe (unchanged).

**Buildability:** S (one flag set at lifespan-complete + optional callable). **Gate risk:** conformance — none (these are builtin non-schema routes, `include_in_schema=False`); must verify `/readyz` still works when the user overrides it (M19b: user routes may override builtins). No clippy (Python). Low risk, high correctness payoff.

---

### 7. Mid-stream upstream failure → terminal `finish_reason:"error"` SSE chunk before close  [partial] [MEDIUM]

**Mechanism (source):** vLLM `chat_completion/serving.py:989-996` and OpenRouter/LiteLLM all agree: once HTTP 200 + `text/event-stream` is sent, the status can't change, so a mid-stream upstream failure MUST be emitted as a terminal SSE data frame with `finish_reason:"error"` (same envelope as the HTTP error path) followed by `data: [DONE]\n\n`, so OpenAI-SDK-compatible clients parse both failure modes identically.

**Lancelot state (verified, from mine cross-check):** Lancelot ships `error_frame=True` on `@app.stream` (`asgi.py:1107,1155-1397`) which catches pump exceptions and emits a scrubbed `ProblemDetail` SSE frame + `[DONE]` — this is the *inbound stream* (Lancelot's own handler raising). The gap is the **relay/`TransportPool.stream` path**: when Lancelot is relaying an upstream LLM SSE stream (`llm.py:686-749` `relay` / `idle_guarded_chunks`) and the *upstream* fails after the first chunk, the cancellation propagates as a `CancelledError` and the stream terminates without a `finish_reason:"error"` frame in the OpenAI shape. A client SDK sees a truncated 200, not a parseable error.

**Recommended approach:** in the relay path (`relay` / `_relay_forward`), on an exception after the first forwarded chunk, emit one synthetic terminal frame `data: {"choices":[{"delta":{},"finish_reason":"error","index":0}],"error":{...scrubbed...}}\n\n` then `data: [DONE]\n\n` before closing. Opt-in `relay(..., error_frame=True)` to match the `@app.stream` ergonomics; scrub to a generic message (never leak upstream error text — the LangServe `api_handler.py:1213-1215` rule). ~10-15 lines.

**Buildability:** S. **Gate risk:** none (relay is Lancelot-only; no vendored test). Correctness: the frame is the OpenAI delta shape, so it's only correct for OpenAI-compatible relays — gate behind the opt-in flag and document that the envelope is OpenAI-shaped. **Validate-under-load:** test that kills the upstream mid-stream → client receives a parseable error frame + `[DONE]`, not a truncated body.

---

## IMPROVE (small, in-mission upgrades to shipped surface)

### I1. Decorrelated/full-jitter backoff in TransportPool — kill the thundering-herd retry on a 429'd LLM under fanout  [partial]

**Source:** Ray Serve `request_router.py:592-596` uses `uniform(T, 2T)` jitter to prevent synchronized retry spikes; ledger BUILD item (LanceDB decorrelated-jitter pattern). **Lancelot state (verified):** `llm.py:480-481` already adds jitter — `backoff_base * (2**attempt) + random.random() * backoff_base` — but this is *additive* jitter on top of the full exponential term, so competing retriers' delays are still highly correlated (the `2**attempt` floor dominates). Under `@app.batch` fanout where 16 retriers all hit one rate-limited provider, they re-fire in near-lockstep. **Fix:** switch to full jitter (`random.uniform(0, min(backoff_max, backoff_base * 2**attempt))`) or decorrelated (`min(backoff_max, random.uniform(backoff_base, prev_sleep*3))`). One-line change to the sleep computation; preserves `retry_budget_ms`. **Buildability:** S. **Gate risk:** none (no test asserts the exact sleep value).

### I2. Honor Azure `x-ms-retry-after-ms` in the retry-after parser  [partial]

**Source:** Portkey `retryHandler.ts` parses `x-ms-retry-after-ms` (Azure OpenAI-specific). **Lancelot state (verified):** `llm.py:380-387` parses `retry-after` and `retry-after-ms` only. Azure OpenAI is a top-3 LLM backend for the target user; its rate-limit header is silently ignored → Lancelot falls back to exponential backoff and over-waits. **Fix:** add the `x-ms-retry-after-ms` branch to `_retry_after_seconds`. One line. **Buildability:** S. **Gate risk:** none.

### I3. `/fleet/status` queue_depth field for LB shedding  [partial]

**Source:** SGLang `GET /get_load` returns `{num_requests_waiting, num_requests_running}` so a frontend LB sheds to the less-loaded replica. **Lancelot state:** `/fleet/status` (`fleet.py`) returns `in_flight`/`uptime_s`/`status` but not the count *waiting behind* the `TenantConcurrencyLimiter` semaphore. **Fix:** expose `queue_depth` derived from the semaphore's blocked-waiter count (the `queue_wait_ms` ContextVar machinery already tracks the wait). One-liner addition to `fleet_info`. **Buildability:** S. **Gate risk:** none.

### I4. Optional `lancelot.upstream.tpot_ms` histogram (time-per-output-token)  [partial]

**Source:** Kong `observability.lua:3` emits `llm_tpot_latency = e2e_latency / completion_tokens`. **Lancelot state:** `upstream_metrics.py` records TTFT + duration + token counts but not TPOT (a complementary capacity-planning metric — TTFT is UX, TPOT is throughput). All inputs already exist in `record_upstream`. **Fix:** one derived histogram. **Buildability:** S. **Gate risk:** none. Nice-to-have, not urgent — ship with an observability pass.

### I5. `gc.freeze()` after lifespan startup completes — cut GC pause jitter  [missing, but generic not AI-specific]

**Source:** vLLM `server_utils.py:467` calls `freeze_gc_heap()` (`gc.freeze()`) right before `yield` in lifespan so all startup allocations (route table, validators, OTel instruments, the user's warmed LLM client) move to the permanent generation and stop being scanned. **Lancelot state (verified, from mine):** not called anywhere. Impact is smaller than vLLM's (no model weights in-process) but real at scale (many routes + heavy middleware + the user's warmed clients). **Fix:** `if hasattr(gc, "freeze"): gc.freeze()` after startup hooks complete, with `LANCELOT_NO_GC_FREEZE=1` opt-out. **Buildability:** S. **Gate risk:** zero for correctness (GC still runs; frozen gen just skipped). Borderline-domain (it's a general server win, not AI-specific) — list it but it belongs equally to the resilience synthesizer.

---

## HAVE (already shipped — explicitly NOT to build; tagged so nothing is redundant)

- **TransportPool retry/backoff/budget** — POST never retried, `retry_budget_ms` (60s default), `Retry-After`/`retry-after-ms` honored, exponential backoff with jitter (`llm.py:443-490`). Matches LiteLLM/Portkey constants. (Improve only: I1 jitter shape, I2 Azure header.)
- **TransportPool Idempotency-Key injection** — stable key reused across retries, idempotent verbs only (`llm.py:752-761`). Modal/gateway parity.
- **TransportPool traceparent + request-id + agent-id propagation** — W3C, setdefault (`llm.py:764-797`).
- **TransportPool cancel-on-disconnect** — unary (`_await_cancellable`) + streaming (`_race_chunk_vs_cancel`), FIRST_COMPLETED dual-task race; client disconnect → upstream abort. The vLLM `@with_cancellation` mechanism, validated under M4 load.
- **TransportPool `fanout`** — first-N, deadline, cancel-laggards, bounded concurrency, rate cap; completion order. Chroma parallel-probe pattern.
- **TransportPool `spawn_child`** — ContextVar snapshot survives reset, `session_affinity` → `x-lancelot-affinity` header.
- **TransportPool `idle_guarded_chunks` + `relay`** — streamed cost/token/TTFT scan (last SSE `usage` frame), idle guard, SSE relay. (Improve only: I4 TPOT; build only: #7 relay error frame.)
- **TransportPool mTLS + SIGHUP reload + SSRF guard + rate-limit token bucket** — `reload_mtls`, `block_private_ips`, `_TokenBucket`.
- **upstream_metrics** — 8-provider detection, 3-shape token extraction, user-rate-table cost (Lancelot ships NO rates — correct vs LiteLLM's pricing JSON), 5-class error taxonomy, 6 OTel instruments, cost→ContextVar→`X-Lancelot-Cost-USD`.
- **`@app.batch` core** — window/count/`size_fn`-budget flush, `key=` bucketing, `max_keys` cap → 503, defer-to-tail, timer-self-cancel fix, BaseException fanout (`batch.py`). The `batch_size_fn`/token-budget win (Ray Serve `batching.py:393`) is already shipped as `size_fn`/`max_size` (M15 T9). (Build only: #1 dedup, #2 pipeline, #3 timeout, #4 shed.)
- **R3 streaming pump** — on-loop `_drain`, bounded-mpsc, GIL-dropped per chunk, backpressure via `await_capacity`, `LANCELOT_STREAM_BUFFER`, SSE framing, `done_sentinel` `[DONE]`, `max_stream_lifetime` reconnect sentinel, `cancel_on_disconnect`, `error_frame` (inbound), TTFT-on-any-body. (Build only: #7 relay-path error frame.)
- **`stream_interval`** — `@app.stream(stream_interval=N)`, first token immediate. SGLang/vLLM parity.
- **SSE keepalive as spec-valid comment** — `b": ping\n\n"`, 15s, excludes `text/event-stream` from gzip; CR/LF/NUL strip on id/name.
- **`canonical.dumps_canonical`** — sort_keys, no-NaN, utf-8, deterministic bytes for Anthropic/OpenAI/vLLM prompt-cache stability. The "prompt-cache-stable serialization" win.
- **`Response.set_session_affinity(key)`** — KV-cache-aware routing hint header (the client-affinity half; the server-side LRU model cache is REFUSED — user/LB-side).
- **`lancelot.mcp.mount`** — JSON-RPC server, `max_response_bytes=4096` minimal-return WARN (Dex Horthy handle-and-fetch), `call_tool` positional-only name. (Resumability/`Last-Event-ID` is REFUSED/deferred.)
- **`@app.tool` output_schema** — auto-derived from return annotation.
- **Per-request ContextVars** — `cost_usd`, `tokens_in/out`, `agent_id`, `tenant_id`, `queue_wait_ms`, `request_id`, `trace_id`, `trace_parent`; set/reset by-token by the ASGI seam (BentoML `ContextMiddleware` parity).
- **`TenantConcurrencyLimiter`** — per-tenant semaphore, `LANCELOT_TENANT_LABEL_MAX` cardinality cap, anon bucket. (`X-Priority` priority-queue variant is a LOW/deferred candidate, see refuse-adjacent below.)
- **`/livez` const-tier** — zero-GIL prebaked process-alive probe.
- **Constant-time bearer auth + path guard** — `StaticTokenGate` (SHA-256 pre-hash, `hmac.compare_digest`, OPTIONS-skip), const-tier auth guardrail (`LANCELOT_CONST_STRICT`). vLLM `AuthenticationMiddleware` parity.
- **`MemoryPool`** — named-consumer cooperative reserve/release, `ResourcesExhausted(top_consumer=)`. DataFusion TrackConsumersPool pattern.
- **`LANCELOT_MAX_INFLIGHT_BYTES`** — aggregate in-flight body budget → 503 (the egress bytes-in-flight cap from SGLang/TGI is the *inbound* analog already shipped; per-request bypass is a ledger BUILD).
- **Graceful drain / conn cap / header timeout / body cap** — `acquire_many_owned`, `LANCELOT_MAX_CONNECTIONS`, `LANCELOT_HEADER_TIMEOUT_MS`, `LANCELOT_MAX_BODY`. (Build only: #5 body-read timeout — the one missing slow-loris axis.)

---

## REFUSE (Tier-D / cardinal-rule / boundary violations — do not propose)

| Item | Source | Why refused |
|---|---|---|
| Provider request/response transform layer (OpenAI↔Anthropic, tool-name sanitize, `thinking` drop, `reasoning_effort`→budget) | LiteLLM `transformation.py`, Portkey, Kong drivers | AI-logic; "AI logic stays user-side". The 4-method Config abstraction is the user's / `app.llm`'s job. Document the Anthropic quirks in operator docs, build nothing. |
| Multi-deployment failure-RATE circuit breaker + cooldown (50%/5-req/single-deployment-valve) | LiteLLM `cooldown_handlers.py`, `constants.py:908` | `TransportPool` is single-provider per instance; circuit breaker is explicit Tier-D and an `app.llm`-router concern. Document the constants for that future layer. |
| Multi-key rotation within a provider on 429 (`keyProvider`) | Bifrost (gateways mine M6) | Single-key by design; `app.llm` concern. |
| TTFT-normalized latency routing (seconds-per-completion-token provider selection) | LiteLLM `lowest_latency.py` | Provider *selection* = `app.llm`/user; Lancelot *records* TTFT, doesn't route on it. Document the formula. |
| Cache-warm session→provider-key stickiness | Portkey/Bifrost (gateways M4) | Multi-provider routing = `app.llm`. The affinity *header* primitive is already HAVE. |
| Per-replica LRU model cache + controller push-back (`@serve.multiplex`) | Ray Serve `multiplex.py` | GPU/model-weight memory management = user-side. The affinity header is the right abstraction Lancelot can own. |
| Power-of-Two-Choices live-queue routing | Ray Serve `pow_2_router.py` | Load-balancer logic; the operator's LB (nginx/ALB/Fly) + `SO_REUSEPORT` multiprocess is the deploy model. |
| Chunked-prefill / cache-miss-only token budgeting / `new_token_ratio` admission controller | SGLang `schedule_policy.py`, TGI `queue.rs:337` | Model-server KV accounting; Lancelot doesn't own the KV cache. (The HTTP-side adaptive *batch* backoff from upstream error-rate is a real but LOW/complex candidate — see note.) |
| FSM/grammar token masking, jump-forward structured output | SGLang/TGI constrained backends | Sampler/LLM-side logic. `@app.tool` output_schema already covers response validation. |
| Weight hot-swap endpoints (`/update_weights_*`) | SGLang `:1048+` | Pure model-server functionality. |
| `pause_generation` / `continue_generation` scheduler control | SGLang `:1467` | Scheduler control = model server. (`abort_request`-by-id is a LOW operator candidate, not must-build — `cancel_on_disconnect` covers the high-frequency case.) |
| Cross-process model-worker broker (LitServe mp.Queue / ZMQ pub-sub) | LitServe `server.py`, `zmq_queue.py` | Hosts the model = positioning violation; explicit Tier-D (deferred-panel R3, 30-50h). |
| `@app.job` async-task-poll API with SQLite persistence | BentoML `@task` (`app.py:582`) | Tempting and AI-relevant, BUT durable/checkpointed execution + a state store = orchestration, which is explicit Tier-D ("Snapshot/restore / durable execution"). The 202+poll *shape* is fine; the persisted job store crosses into orchestration. If ever built, in-memory-only + opt-in, and only on demonstrated demand — NOT this pass. |
| Streaming batch demux (per-caller generator fan from one batched generator) | Ray Serve `batching.py:375` | LLM streaming is upstream-rate-limited; the throughput win is fewer upstream calls, already delivered by `@app.batch` + #1 dedup. The streaming+batch seam crosses into AI-logic. |
| CorkDispatcher learned-latency adaptive batching (`duration ≈ o_a·n + o_b`, numpy lstsq) | BentoML `dispatcher.py:38` | Needs numpy dep (cardinal-rule violation), second-order over a tuned fixed window; "adaptive SLA-aware batch window" is explicit Tier-D. The non-learned adaptive-window (#3 timeout + #2 pipeline) gives the real win without the dep. |
| Spend DB (Redis incr → Postgres flush), pricing JSON shipped in-package | LiteLLM `proxy_server.py:2099`, `model_prices.json` | Multi-tenant billing = gateway, not framework. `cost_usd` ContextVar + user rate table is the correct scope. |
| Brotli, per-chunk/streaming gzip, custom orjson serializer | gateways/serving compression | Explicit Tier-D (proxy's job / pydantic-core already Rust-fast). |

**Note on LOW/complex candidates deliberately NOT promoted:** (a) adaptive batch backoff from upstream-error-rate (SGLang `new_token_ratio` analog) — real operator win but a complex feedback loop, and "adaptive batch window" brushes Tier-D; revisit only if the simple #2/#3/#4 backpressure proves insufficient under load. (b) `X-Priority` priority-queue in `TenantConcurrencyLimiter` (Groq `service_tier`) — useful for multi-tier AI APIs but L-effort and not byte-path-critical; defer. (c) `abort_request`-by-id control endpoint — needs a thread-safe request registry; `cancel_on_disconnect` covers the common case. (d) DNS-rebinding-resistant SSRF (BentoML at-connect monkeypatch) — current `lancelot.net` check-time guard is correct for non-adversarial; a hardening candidate for the resilience synthesizer, not AI-byte-path.

---

## Recommended build order (one coherent batch-completion + cancellation PR set)

1. **Batch-completion bundle (#1 dedup + #2 pipeline + #3 timeout + #4 shed)** — one PR; these are the same primitive's missing axes and share the `_BatchRegistry` semaphore + per-item timestamp. Highest AI-serving value, all S/M, conformance-safe (Lancelot-only surface), and the M4 lesson means each gets a real-server awaiting-handler test. This is the headline of the pass.
2. **#5 body-read timeout (Rust)** — the one missing resilience axis; M-effort because it's Rust + adversarial test, but it closes a real slow-loris variant.
3. **#6 readyz readiness gate + #7 relay error frame** — correctness fixes, both S, deploy-and-roll + client-SDK compat.
4. **I1–I4 micro-improves** — fold into the above PRs where they touch the same file (I1/I2 with the relay/retry work; I3/I4 with an observability touch).

Gate every PR: `pytest tests/ -q` + `make m1-conformance` (floor passes=974) + `cargo clippy --release --all-targets -- -D warnings` + fmt. The batch and relay work is Python-only (no conformance/clippy exposure); #5 is the only one that moves the Rust gate.
