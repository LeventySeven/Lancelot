# Serving Frontier — Byte-Path Mechanisms from SGLang / TGI / Ray Serve / Groq

**Sources:** `researchfms/teardowns/SGLANG.md`, `researchfms/teardowns/TGI.md`, `researchfms/teardowns/RAY_SERVE.md`, `researchfms/teardowns/GROQ.md`
**Date:** 2026-06-22
**Scope:** What serving-grade patterns translate to Lancelot byte-path features WITHOUT building a model server. Lancelot owns the HTTP transport layer; the user owns the model.

---

## Status legend

- **NEW** — not in Lancelot today; real byte-path or operator-exp win
- **HAVE** — already shipped in Lancelot M1–M19
- **REFUSE** — would cross into AI-logic/model-server territory; Tier-D

---

## 1. `stream_interval` (SSE throttle) — HAVE

**Mechanism:** SGLang `stream_interval` (`server_args.py:439`, default **1**) controls how many decode steps are batched before emitting one SSE chunk (`data: {json}\n\n`). At default=1 every token is a separate chunk; setting 2–4 reduces SSE frame overhead at the cost of chunkier output.

**Lancelot state:** HAVE — `stream_interval` kwarg on `@app.stream` routes, `asgi.py:1054`. Already mirrors this exactly.

**Byte-path relevance:** none-new; confirming Lancelot already matches the production pattern.

---

## 2. TTFT metric (time-to-first-chunk) — HAVE

**Mechanism:** SGLang, TGI, and every serving stack universally agree that **TTFT + P99 are THE user-facing streaming metrics**, not total throughput. SGLang emits tokens the same step they're sampled (no accumulation). TGI's `PrefillResponse.total_ns` spans include the first-token timing.

**Lancelot state:** HAVE — `lancelot.http.stream.ttft` histogram (`metrics.py:67`), `_ttft_send_wrapper` + `_record_ttft_now` (`asgi.py:525–568`), `lancelot.upstream.ttft` on the outbound pool side (`upstream_metrics.py:215`). Both inbound stream TTFT and outbound upstream TTFT are tracked.

---

## 3. `waiting_served_ratio` backpressure heuristic — NEW (as `@app.batch` admission parameter)

**Mechanism:** TGI `backend.rs:186` — when a model does NOT support chunked prefill, the batching loop only admits a new request batch if either (a) the queue has at least `waiting_served_ratio * current_batch_size` requests waiting (default **0.3**) OR (b) the server has gone `max_waiting_tokens` (default **20**) decode steps without admitting anyone (anti-starvation). The ratio prevents micro-bursts of tiny admissions from disrupting the decode cadence; the step cap prevents head-of-line starvation. When chunked prefill is active this entire heuristic is *discarded* (the system runs compute-bound and doesn't need pacing).

**Cite:** `backends/v3/src/backend.rs:186`; `launcher/src/main.rs:707,754` (defaults).

**Byte-path relevance for Lancelot:** `@app.batch` currently uses a fixed `batch_window_ms` timer. When the upstream LLM is rate-limited (the `@app.batch` premise), a `waiting_served_ratio` analog would let the batch window *extend automatically* while the upstream is busy and shrink when it's free — more requests per upstream call without increasing worst-case latency. The anti-starvation step cap maps to a maximum wait time before flushing a partial batch regardless of size.

**Lancelot check hint:** `lancelot/batch.py` — `_BatchState._flush_task` timer logic; compare to Ray Serve's `batch_wait_timeout_s` at `batching.py:387` (`requests_available_event.wait(remaining_batch_time_s)`).

**Honest assessment:** Real win only when the upstream is the bottleneck (the `@app.batch` use case). For non-rate-limited upstreams a fixed window is sufficient. Worth adding as `@app.batch(adaptive_window=True)` — the batch flushes when full OR on the starvation timeout.

---

## 4. In-batch prefix dedup (throwaway radix tree over waiting queue) — NEW

**Mechanism:** SGLang `schedule_policy.py:160` — when N cold requests in the waiting queue all share a prefix that is NOT yet in the KV cache (e.g. 100 requests all starting with the same 2k-token system prompt at cold start), naively admitting all 100 causes 100× recompute of the same prefix. SGLang builds a **throwaway simulated RadixCache over the waiting queue** (`schedule_policy.py:160`, `RadixCache.create_simulated()`). Any request whose real-cache prefix is short (`< IN_BATCH_PREFIX_CACHING_CHECK_THRESHOLD = 32`, `:70`) is checked against the throwaway tree; if it shares `>= IN_BATCH_PREFIX_CACHING_DEPRIORITIZE_THRESHOLD = 32` tokens (`:77`) with an already-seen request, it is **deprioritized to the back of the queue** (sort key `float("inf")`, `:283`) so one request runs first, populates the real tree, and the rest hit the cache.

**Cite:** `schedule_policy.py:70,77,160,204,275,283`.

**Byte-path relevance for Lancelot:** Lancelot's `@app.batch` coalesces requests by key prefix. It does NOT currently deduplicate requests that have the same LLM prompt prefix. An operator batching `/chat` requests where all 100 simultaneous users start with the same system prompt would benefit from a "first one wins, rest hit cache" ordering. This is a pure scheduling reordering — zero extra memory, no model-server logic. The mechanism: on batch flush, sort by the presence of a `cache_key` (Lancelot already has `key=` on `@app.batch`) and emit the canonical representative first, then the rest.

**Lancelot check hint:** `lancelot/batch.py` — `_BatchState._entries` sorting on flush. The `key=` lambda already extracts a routing key; sort so identical keys are grouped and only the first is sent immediately. The rest wait for the first's response, then receive the same result (cache coalescence, not just batching).

**Honest assessment:** This is the `@app.batch` dedup upgrade. Currently `@app.batch` groups by key and sends all together; the in-batch-dedup optimization would let the batch send ONE and fan the result to all identical-key waiters. This is strictly better than the current N-requests-to-upstream pattern when N >> 1 for the same prompt. High-value for the AI serving use case Lancelot targets.

---

## 5. Adaptive admission budget feedback controller (`new_token_ratio` analog) — NEW

**Mechanism:** SGLang's `PrefillAdder` (`schedule_policy.py:487`) reserves `min(max_new_tokens - output_len, CLIP_MAX_NEW_TOKENS=4096) × new_token_ratio` KV slots per running request for future decodes. `new_token_ratio` starts at 1.0 (fully conservative). After a retract-preemption event (`schedule_batch.py:2300`), it **jumps up** (more conservative — admit fewer, prevent OOM thrashing); when no memory pressure occurs, it **decays back** toward optimistic (`decay_step`, `scheduler.py:2879`). This is a feedback controller that self-tunes admission aggressiveness based on observed pressure.

**Cite:** `schedule_policy.py:62,487`; `scheduler.py:2820,2879`; `schedule_batch.py:2300,2312`.

**Byte-path relevance for Lancelot:** In a heavy-traffic `@app.batch` scenario where the upstream LLM backend has a quota and Lancelot's in-flight window fills up, an adaptive `bytes_in_flight_cap` (or request-in-flight cap) that backs off when upstream errors spike and relaxes when they clear would be the direct analog. The mechanism is: track upstream 429/503 rate; if above threshold, reduce `max_batch` temporarily; decay when clear.

**Lancelot check hint:** `lancelot/llm.py` — `TransportPool` retry/error handling; `lancelot/upstream_metrics.py` — error taxonomy (`quota`/`transient`/`upstream_5xx`). The `quota` error count is already tracked; a feedback loop from error_rate → batch_size adjustment is the new piece.

**Honest assessment:** This is a real operator-experience win for the AI-serving case. A batch that keeps hammering a rate-limited LLM at full `max_batch` wastes retries. An adaptive cap avoids the thundering-herd → retry cascade. Moderate complexity; directly liftable from SGLang's decay+jump pattern.

---

## 6. Three-state health (`ok` / `starting_up` / `draining`) — NEW

**Mechanism:** SGLang `/health_generate` endpoint (`http_server.py:656`) fires an actual **generate request** through the engine to verify it's not just alive but can produce tokens. TGI's `Warmup` gRPC call (`flash_causal_lm.py:1369`) runs the max-batch generate to probe VRAM capacity before accepting any traffic. The SGLang watchdog (`server_args.py:447`, `watchdog_timeout = 300 s`) detects a hung scheduler (no progress for 300 s) and kills the process.

Lancelot currently has **two health states**: `/livez` (process alive) and `/readyz` (500 when draining). A third state is missing: **starting up** — `503` with `Retry-After` while the app is initializing (before `startup` lifespan events complete).

**Cite:** SGLang `http_server.py:656` (`/health_generate`); `server_args.py:447` (`watchdog_timeout`); TGI `flash_causal_lm.py:1369`.

**Byte-path relevance for Lancelot:** `/readyz` already returns 503 during drain. The missing case is the startup window: between `run()` binding the port and the `startup` lifespan completing, `/readyz` currently returns 200 (or 404 before the route is registered). LB/orchestrators will route traffic to a partially-initialized worker. Fix: flip `/readyz` to 503 + `Retry-After: 1` until `startup` lifespan is confirmed complete.

**Lancelot check hint:** `lancelot/app.py:307` (`_readyz` handler); `lancelot/asgi.py` lifespan startup sequence. The `_shutting_down` flag is set on shutdown; a parallel `_starting_up` flag (set True at `__init__`, cleared after `startup` lifespan) would complete the three-state machine.

**Honest assessment:** Small change, significant correctness win. Every LB that probes `/readyz` before the first request will currently get a false-positive 200 during the startup window. This matters for deploy-and-roll: a new replica can receive traffic before its DB connection pool or model client is ready.

---

## 7. `health_generate` deep probe (generate-through ping) — NEW

**Mechanism:** SGLang `GET /health_generate` (`http_server.py:656`) sends a 1-token generate request through the full scheduling + sampling stack and returns 200 only if a token comes back. TGI's `Health` gRPC (`generate.proto:8`) similarly does a lightweight forward. This is the "can I actually produce output?" probe, not just "is the process alive?"

**Cite:** SGLang `entrypoints/http_server.py:656`; TGI `proto/v3/generate.proto:8`.

**Byte-path relevance for Lancelot:** Lancelot's `/livez` is a prebaked-bytes const-tier response (zero cost). A `/health/generate` route analog for AI serving would be `@app.get("/health/generate", const=False)` that calls a user-provided "probe handler" (e.g. a 1-token LLM call). This is an **operator hook**, not a built-in. The framework provides the route contract; the user provides the probe logic. The const-tier `/livez` stays as the cheap process-alive check; `/health/generate` is the deep-probe tier.

**Lancelot check hint:** `lancelot/app.py` health route registration. Current: `_livez` and `_readyz` are framework-provided. The new piece is a `@app.health_generate` decorator (or `App(health_generate_fn=...)`) that registers a user hook under `/health/generate` and wires it to the same 200/503 contract as `/readyz`.

**Honest assessment:** Operator-experience win, not a byte-path win. Worth shipping as a one-liner: `App(health_generate_fn=probe_fn)` registers the route and calls `probe_fn` on every `/health/generate` GET. Useful for AI serving operators who want LB routing to only healthy-LLM replicas.

---

## 8. `pause_generation` / `abort_request` API (generation control endpoints) — NEW (as operator surface)

**Mechanism:** SGLang `POST /pause_generation` (`:1467`) and `POST /continue_generation` (`:1478`) suspend and resume the scheduler. `POST /abort_request` (`:1412`) cancels a specific in-flight request by ID.

**Cite:** `entrypoints/http_server.py:1412,1467,1478`.

**Byte-path relevance for Lancelot:** Lancelot's `cancel_on_disconnect` aborts the upstream iteration when the client disconnects. The missing operator primitive is **abort by request-id** — a control-plane endpoint an operator can call to kill a stuck in-flight stream without disconnecting their client. Lancelot has `x-request-id` propagation (`context.request_id`) and W3C `traceparent`; the abort endpoint would look up the in-flight request by its `x-request-id` and cancel its `asyncio.Task`.

**Lancelot check hint:** `lancelot/asgi.py` — request task lifecycle; `lancelot/context.py` — `request_id` ContextVar. A registry of `{request_id: asyncio.Task}` with an `@app.delete("/requests/{request_id}")` handler would complete this.

**Honest assessment:** Useful for AI serving operators but not a throughput or latency win. Medium complexity (requires thread-safe request registry). Ship only if operators ask for it — the cancel-on-disconnect path covers the high-frequency case.

---

## 9. `bytes_in_flight` streaming cap (back-pressure on the egress path) — NEW

**Mechanism:** SGLang's `PrefillAdder.rem_total_tokens` (`schedule_policy.py:496`) counts `evictable_cache_size` as available memory — KV memory reclaim is automatic on admission. The deeper pattern: **the scheduler never emits more data than the KV pool can sustain**. In the HTTP layer, TGI's `max_batch_total_tokens` (`backend.rs:127`) is the per-step token budget. The analogous HTTP primitive is **bytes in flight on the egress side**: if the server has emitted N bytes of streaming SSE but the client hasn't consumed them (TCP buffer full, slow client), keep emitting and you OOM the process.

**Lancelot current:** `LANCELOT_MAX_BODY` caps the inbound body (16 MiB). The outbound streaming path has no global bytes-in-flight cap.

**Cite:** TGI `backend.rs:127`; SGLang `schedule_policy.py:496,847`.

**Byte-path relevance for Lancelot:** An `LANCELOT_STREAM_BYTES_IN_FLIGHT_MAX` env (or `App(stream_bytes_in_flight=N)`) would bound the total un-ACKed streamed bytes across all concurrent `@app.stream` routes. When the cap is hit, new stream chunks block until TCP acks drain them. This is the missing backpressure primitive for a streaming AI server under slow/stalled clients.

**Lancelot check hint:** `lancelot/asgi.py` streaming pump — the `_drain` coroutine in `src/stream.rs` + the bounded mpsc channel (`LANCELOT_STREAM_BUFFER`). The Rust-side already has a bounded channel; the Python-side `@app.stream` needs the same.

**Honest assessment:** This is a real DoS-resilience win. A slow client that stops reading SSE will stall the asyncio event loop if the Python side buffers without bound. The LANCELOT_STREAM_BUFFER already bounds the Rust-side mpsc; adding a per-route `max_buffered_bytes` on the Python SSE path completes the picture. Medium complexity.

---

## 10. Per-request `cost_cap` / streaming budget enforcement — REFUSE (Tier-D, AI-logic)

**Mechanism:** SGLang's `CLIP_MAX_NEW_TOKENS = 4096` (`schedule_policy.py:62`) clips the reserved-future-token budget per request so one `max_new_tokens=1000000` request doesn't starve everyone else. The HTTP analog is a per-request output budget (`max_response_bytes`).

**Byte-path relevance:** Lancelot's `@app.mcp` already has `max_response_bytes=4096`. For streaming, a `max_stream_bytes` per route (abort after N bytes emitted) would give operators a hard cost cap.

**Lancelot check hint:** `lancelot/asgi.py` streaming pump — `_max_lifetime` already bounds stream DURATION. A `max_stream_bytes` kwarg on `@app.stream` would bound stream SIZE.

**Honest assessment:** `max_stream_lifetime` (already shipped) plus `max_stream_silence` covers most abuse cases. A bytes-based cap adds precision. Small; worth shipping as `@app.stream(max_stream_bytes=N)`. NOT Tier-D — this is pure transport cap, same shape as body cap, no AI logic.

**Revised verdict:** NEW (not REFUSE) — it's an output body cap, not model-server logic.

---

## 11. Request multiplexing with LRU model cache + controller push-back — PARTIAL HAVE

**Mechanism:** Ray Serve `@serve.multiplex` (`multiplex.py:167`) — per-replica LRU `OrderedDict` of up to N loaded models, evicting LRU with eager `__del__` GPU-memory free, and **pushing loaded+loading model IDs to the controller every 1 s** so the router sends requests for model X only to replicas that already have X loaded. The router uses three-tier fallback: (0) replicas with X loaded, (1) replicas with fewest models loaded (best eviction target), (2) all replicas. Matching timeout is `uniform(T, 2T)` to avoid thundering herd.

**Cite:** `multiplex.py:118,135,167,499`; `request_router.py:278,592`; `pow_2_router.py:52`.

**Lancelot state:** PARTIAL HAVE — `Response.set_session_affinity(key)` sets a sticky-routing hint (`responses.py:74`). `TransportPool.spawn_child(parent_request, session_affinity=...)` propagates affinity downstream (`llm.py:937,961`). This is the **client-affinity** half. The missing half is **server-side LRU model cache + controller notification** so the LB can route to warm replicas.

**Byte-path relevance:** The session-affinity header Lancelot sends downstream is exactly what a smart LB (nginx, HAProxy, a custom Lancelot router) needs to implement warm-replica routing. Lancelot can't own the LB. What it CAN do: document the affinity key contract and provide an in-process LRU cache primitive that emits the loaded-model set to a shared registry (e.g. Redis).

**Lancelot check hint:** `lancelot/responses.py:74`; `lancelot/llm.py:961`. The LRU cache + push is the missing piece; see Ray Serve `multiplex.py:135` as the reference pattern.

**Honest assessment:** The affinity header is the right abstraction for Lancelot. The full multiplex cache is operator-side logic. Document the pattern, maybe provide a `lancelot.multiplex.ModelCache` helper (LRU dict + optional callback). Don't own the LB routing.

---

## 12. Power-of-Two-Choices on live queue length (P2C routing) — REFUSE

**Mechanism:** Ray Serve `pow_2_router.py:52` — pick 2 random replicas, send to the shorter queue. Optimized k=2 sampler (`random.randrange(n); j+=1 if j>=i`), 1.9× faster than `random.sample`.

**Byte-path relevance:** This is load-balancer logic. Lancelot is not a load balancer. The operator's LB (nginx, Fly.io, AWS ALB) does this.

**Verdict:** REFUSE — not Lancelot's job.

---

## 13. ASGI lifespan inside a worker without a server (via `uvicorn.LifespanOn`) — HAVE

**Mechanism:** Ray Serve `http_util.py:569-601` — borrows uvicorn's `LifespanOn(Config(app, lifespan="on"))` to run the FastAPI app's `startup`/`shutdown` events inside a Ray actor that has no HTTP server. The replica calls `await self._serve_asgi_lifespan.startup()` at actor init and `shutdown()` at teardown.

**Lancelot state:** HAVE — `App.__init__` registers `@app.on_event("startup"/"shutdown")` (deprecated) and the `lifespan=` context manager; `serve()` / `App.run()` manages the ASGI lifespan protocol natively. The uvicorn path is standard. Lancelot's `run_until_complete` wraps the lifespan correctly.

---

## 14. Disconnect propagation across hops (ASGI receive bridge pattern) — HAVE (partial)

**Mechanism:** Ray Serve `http_util.py:391-398` — when the proxy's `asgi_receive_queue` raises `KeyError` (client gone), synthesize `{"type":"http.disconnect"}` and push it to the replica's `ASGIReceiveProxy` queue. `receive()` then keeps returning the disconnect message forever (per ASGI spec). The replica's FastAPI `await request.is_disconnected()` works correctly across the actor hop.

Ray Serve direct-ingress `direct_ingress_http_util.py:97-98` — same pattern but across asyncio loops: lazy queue creation (binds to the right loop), `call_soon_threadsafe` for cross-loop enqueue, `_disconnect_message` short-circuit so `receive()` keeps returning disconnect.

**Lancelot state:** HAVE — `cancel_on_disconnect` (`asgi.py:1292`) cancels the stream task when the client disconnects. The Rust side detects TCP close and cancels via the mpsc channel closure. The `ASGIDIReceiveProxy` lazy-queue + `call_soon_threadsafe` pattern is the reference implementation for the cross-event-loop case Lancelot doesn't yet have (Lancelot runs one loop).

---

## 15. Request batching with `batch_size_fn` (cost-aware sizing) — NEW

**Mechanism:** Ray Serve `batching.py:377-403` — `batch_size_fn(items) -> int` lets the batch handler use **token count** instead of `len(items)` as the batch-size accounting unit. A single request with 8k tokens would count as 8 units against a `max_batch_size=16384` (tokens), preventing OOM. The over-budget item is pushed to the queue tail, not discarded. Additionally: `max_concurrent_batches` semaphore (`batching.py:439`) — pipeline N batches in flight so batch k+1 fills while batch k processes on the GPU.

**Cite:** `batching.py:254,377,439,474`.

**Byte-path relevance for Lancelot:** Lancelot's `@app.batch(max_batch=N)` counts by request, not by cost. An operator batching LLM calls where each request has `n_tokens` in the payload would benefit from `@app.batch(max_batch=16384, batch_size_fn=lambda req: req["n_tokens"])`. The `max_concurrent_batches` analog maps to pipelining upstream LLM calls while the current batch waits.

**Lancelot check hint:** `lancelot/batch.py` — `_BatchState._entries` accumulation. Add `batch_size_fn` kwarg to `@app.batch`; keep a running `current_cost` alongside `len(entries)`.

**Honest assessment:** Real win for AI serving operators. Token-counted batch sizing prevents over-admission to the LLM backend. `max_concurrent_batches` is the pipeline-N win (~10% throughput gain per extra in-flight batch for GPU-bound backends). Medium complexity for `batch_size_fn`; small for `max_concurrent_batches`.

---

## 16. Per-step chunked prefill with cache-miss-only budget accounting — REFUSE (model-server logic)

**Mechanism:** TGI `queue.rs:337` — `postfix_len = input_length - radix_prefix_len`. Only the cache-miss suffix counts against `prefill_token_budget`. The portion already in the KV tree is free. SGLang `schedule_policy.py:328-336` — chunked prefill splits a long prompt into `chunked_prefill_size = 2048` token chunks, carrying the remainder to the next scheduler step.

**Verdict:** REFUSE — this is model-server KV accounting. Lancelot doesn't own the KV cache.

---

## 17. `get_load` endpoint (queue depth for LB shedding) — NEW (light)

**Mechanism:** SGLang `GET /get_load` (`http_server.py:656`) returns `{"num_requests_waiting": N, "num_requests_running": M}` so a frontend load balancer can send new requests to the less-loaded replica.

**Cite:** `entrypoints/http_server.py:656`.

**Byte-path relevance for Lancelot:** `/fleet/status` (shipped in M7) returns `{in_flight, uptime_s, status}`. Adding `queue_depth` (how many requests are waiting behind the concurrency semaphore, from `TenantConcurrencyLimiter`) to the `/fleet/status` response gives LBs the same signal. Zero new code beyond exposing an existing counter.

**Lancelot check hint:** `lancelot/fleet.py:85` — `fleet_info` dict; `lancelot/tenant.py` — `TenantConcurrencyLimiter._semaphores`. Add `queue_depth` from the semaphore's `_value` delta.

**Honest assessment:** One-liner addition to `/fleet/status`. Ships with fleet observability pass.

---

## 18. Structured output via FSM token masking (grammar backends) — REFUSE (AI-logic)

**Mechanism:** SGLang/TGI compile `json_schema`/`regex` → FSM; each decode step masks logits to the FSM-allowed token subset. SGLang's jump-forward (`constrained/outlines_jump_forward.py:62,146`) emits deterministic-FSM spans without model calls.

**Verdict:** REFUSE — this is sampler / LLM-side logic. Lancelot already has the `@app.tool` output_schema mechanism for response validation; token-level FSM masking is the model server's job.

---

## 19. `service_tier` as a per-request priority header — NEW (operator surface)

**Mechanism:** Groq's `service_tier: Literal["auto","on_demand","flex","performance"]` (`completion_create_params.py:214`). `flex` = fast-fail if no capacity. `performance` = head-of-queue priority. This is queue preemption at the gateway, not mid-flight preemption (the LPU can't preempt mid-decode without breaking determinism).

**Byte-path relevance for Lancelot:** Lancelot's `TenantConcurrencyLimiter` (`lancelot/tenant.py`) does per-tenant concurrency caps. A `X-Priority: high/normal/low` request header that re-orders the semaphore waiters would let operators implement Groq-style priority tiers at the HTTP layer. The mechanism: replace `asyncio.Semaphore` with a priority-queue semaphore where `high` priority tasks are scheduled before `normal`.

**Lancelot check hint:** `lancelot/tenant.py` — `TenantConcurrencyLimiter.acquire()`; currently FIFO `asyncio.Semaphore`. A priority-aware queue (e.g. `asyncio.PriorityQueue` + per-waiter `asyncio.Event`) would implement the ordering.

**Honest assessment:** Useful operator-experience feature for multi-tier AI APIs. Medium complexity. Worth shipping as `LANCELOT_PRIORITY_HEADER=X-Priority` env + priority-queue semaphore in `TenantConcurrencyLimiter`.

---

## 20. `max_stream_lifetime` reconnect sentinel — HAVE

**Mechanism:** SGLang `stream_interval` + streaming `data: [DONE]\n\n` terminator. Ray Serve `MessageQueue.fetch_messages_from_queue` (`http_util.py:283-325`) — the drain loop that races user-app future against "new message" waiter, batches messages per round-trip, re-raises app exceptions.

**Lancelot state:** HAVE — `max_stream_lifetime` on `@app.stream` routes sends `event: reconnect\ndata: {"reason":"max_stream_lifetime"}\n\n` (`asgi.py:1360`). Already ships.

---

## 21. Watchdog timeout (hung-scheduler detection) — HAVE (partial)

**Mechanism:** SGLang `watchdog_timeout = 300 s` (`server_args.py:447`) — a background task that kills the scheduler process if no batch has been processed in 300 seconds.

**Lancelot state:** HAVE (partial) — `max_stream_silence` (`routing.py:63`) aborts a stream if no data chunk arrives in N seconds. This covers the streaming silence case. The global "no requests processed in N seconds" watchdog (which would restart the whole server) is not implemented in Lancelot and is an infrastructure/orchestrator concern (Kubernetes liveness probe). Not worth adding.

---

## 22. Weight hot-swap endpoints (RLHF in-place update) — REFUSE (model-server logic)

**Mechanism:** SGLang `POST /update_weights_from_disk` (`:1048`), `/update_weights_from_tensor` (`:1176`), `/update_weights_from_distributed` (`:1198`) — atomic in-place weight refresh for online RLHF without restarting the server.

**Verdict:** REFUSE — pure model-server functionality. Not Lancelot's layer.

---

## 23. Jittered backoff on multiplex routing timeout — NEW (pattern)

**Mechanism:** Ray Serve `request_router.py:592-596` — the multiplex matching timeout is `random.uniform(T, 2T)` to prevent a thundering herd of all queued requests abandoning model affinity at the same instant and flooding the fallback set.

**Byte-path relevance for Lancelot:** `TransportPool` retry/backoff uses `exponential_backoff` (M14). Adding `uniform(T, 2T)` jitter to retry delays prevents synchronized retry spikes when many concurrent requests hit a rate limit simultaneously. This is the "retry jitter" pattern; Lancelot's backoff may already do this.

**Lancelot check hint:** `lancelot/llm.py` — `TransportPool._backoff` or equivalent; check if `random.uniform` jitter is applied to retry delays.

**Honest assessment:** One-liner fix if not already present. Check first.

---

## 24. `DONE` sentinel termination (`data: [DONE]\n\n`) — HAVE

**Mechanism:** SGLang `http_server.py:728` — SSE stream terminates with `data: [DONE]\n\n` (OpenAI-compatible). TGI same.

**Lancelot state:** HAVE — Lancelot's `@app.stream` sends the `data: [DONE]\n\n` sentinel on clean completion. Standard SSE terminator.

---

## Summary — Actionable Build List (byte-path / operator-experience, additive)

| # | Mechanism | Effort | Value | Priority |
|---|-----------|--------|-------|----------|
| 3 | `@app.batch` adaptive window (`waiting_served_ratio` + anti-starvation step cap) | S | M | HIGH (completes batch primitive) |
| 4 | `@app.batch` in-batch dedup (fan identical-key results to all waiters) | M | H | HIGH (10× → 100× for repeated-prompt case) |
| 15 | `@app.batch(batch_size_fn=)` token-cost sizing + `max_concurrent_batches` | M | H | HIGH (prevents LLM OOM) |
| 6 | Three-state health: `starting_up` → `/readyz` 503 until lifespan complete | S | H | HIGH (correctness) |
| 10 | `@app.stream(max_stream_bytes=N)` output bytes cap | S | M | MEDIUM |
| 9 | `LANCELOT_STREAM_BYTES_IN_FLIGHT_MAX` egress backpressure | M | M | MEDIUM |
| 7 | `App(health_generate_fn=probe)` deep-probe `/health/generate` hook | S | M | MEDIUM |
| 17 | `/fleet/status` queue_depth field (from TenantConcurrencyLimiter) | S | L | LOW |
| 19 | Priority semaphore in TenantConcurrencyLimiter + `X-Priority` header | L | M | LOW |
| 5 | Adaptive batch backoff from upstream error rate | L | M | LOW (complex feedback loop) |

**REFUSE list (confirmed Tier-D for this pass):** #2 (chunked prefill accounting), #12 (P2C LB), #16 (prefill budgeting), #18 (FSM token masking), #22 (weight hot-swap).

---

## Constants reference (cross-source, for implementation)

| Constant | Value | Source |
|---|---|---|
| SGLang `stream_interval` default | 1 | `server_args.py:439` |
| SGLang `watchdog_timeout` | 300 s | `server_args.py:447` |
| SGLang `IN_BATCH_PREFIX_CACHING_CHECK_THRESHOLD` | 32 tokens | `schedule_policy.py:70` |
| SGLang `IN_BATCH_PREFIX_CACHING_DEPRIORITIZE_THRESHOLD` | 32 tokens | `schedule_policy.py:77` |
| SGLang LPM→FCFS queue cutoff | 128 requests | `schedule_policy.py:204` |
| SGLang `CLIP_MAX_NEW_TOKENS` reservation clip | 4096 | `schedule_policy.py:62` |
| SGLang `chunked_prefill_size` default | 2048 tokens | `server_args.py:1445` |
| SGLang `max_prefill_tokens` per-batch | 16384 tokens | `server_args.py:411` |
| TGI `waiting_served_ratio` | 0.3 | `launcher/main.rs:707` |
| TGI `max_waiting_tokens` | 20 decode steps | `launcher/main.rs:754` |
| TGI `max_concurrent_requests` default | 128 | `launcher/main.rs:648` |
| TGI `BLOCK_SIZE` (flashinfer, token-granular) | 1 | `models/globals.py:43` |
| TGI `TGI_WIGGLE_ROOM` (VRAM fraction) | 0.90 | `models/globals.py:32` |
| TGI reserved KV block for health checks | block 0 | `radix.rs:45` |
| TGI `CUDA_GRAPHS` default batch sizes | 1,2,4,8,16,32 (descending) | `launcher/main.rs:764`; `globals.py:61` |
| Ray Serve `batch_wait_timeout_s` default | (per-decorator) | `batching.py:387` |
| Ray Serve multiplex push cadence | 1 s | `multiplex.py:118` |
| Ray Serve multiplex matching timeout | `uniform(T, 2T)` | `request_router.py:592` |
| Groq `service_tier` values | auto/on_demand/flex/performance | `completion_create_params.py:214` |
| Groq `stream_interval` equivalent | implicit (1 token/chunk) | SDK SSE format |
