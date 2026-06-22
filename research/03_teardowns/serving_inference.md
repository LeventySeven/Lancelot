# Serving / Inference Engines — Mechanism-Level Distillation for Lancelot

**Sources read:** `researchfms/teardowns/VLLM.md`, `SGLANG.md`, `TGI.md`, `LITSERVE.md`, `RAY_SERVE.md`
**Tier:** 1A (source code read line by line in all five teardowns)
**Lens:** Lancelot IS the serving layer. What to adopt, adapt, or skip; every claim mapped to what Lancelot already has.

---

## 1. Mid-stream error as a finish-reason chunk (HTTP 200 already sent)

**The problem.** Once the serving layer has emitted `HTTP 200 + Content-Type: text/event-stream`, no exception can become an HTTP 4xx/5xx. A crash mid-token-stream is invisible to clients unless explicitly signalled.

**vLLM's exact protocol** (`chat_completion/serving.py:989-996`, VLLM.md §R.6, KNOWN verbatim):

```python
except GenerationError as e:
    yield f"data: {self._convert_generation_error_to_streaming_response(e)}\n\n"
except Exception as e:
    data = self.create_streaming_error_response(e)
    yield f"data: {data}\n\n"
# Always follows with the terminal sentinel even after an error:
yield "data: [DONE]\n\n"
```

The error chunk is a `ErrorResponse{error: ErrorInfo{message, type, param, code}}` Pydantic model dumped to JSON — the same envelope used for non-streaming errors. `create_streaming_error_response` is `json.dumps(ErrorResponse(...).model_dump())` (`engine/serving.py:182-197`). After the error chunk, `[DONE]` is still emitted so clients know the stream is closed.

**SGLang and TGI follow the same pattern**: their SSE streams emit `data: [DONE]\n\n` as the terminal sentinel and write error payloads into the stream body when the generator throws.

**The paired watchdog (vLLM `launcher.py:144-167`, VLLM.md §R.10):**

```python
async def watchdog_loop(server, engine):
    VLLM_WATCHDOG_TIME_S = 5.0
    while True:
        await asyncio.sleep(VLLM_WATCHDOG_TIME_S)
        terminate_if_errored(server, engine)

def terminate_if_errored(server, engine):
    engine_errored = engine.errored and not engine.is_running
    if not envs.VLLM_KEEP_ALIVE_ON_ENGINE_DEATH and engine_errored:
        server.should_exit = True
```

The swallowed exception design is a two-part contract: **generators never raise post-200** (they emit the error as an SSE chunk); an **out-of-band watchdog** (`engine.errored and not engine.is_running`) notices the dead engine and flips `server.should_exit`. Without the watchdog, a fatal engine crash during streaming would leave the process alive but broken.

**SGLang watchdog constant** (`server_args.py:447`, SGLANG.md §11): `watchdog_timeout = 300 s`. SGLang's watchdog checks whether the scheduler has made progress; if it hasn't advanced the forward counter within 300 s, it considers the process hung and exits.

**Lancelot implication.** Lancelot's R3 pump (`@app.stream`) already has graceful drain and disconnect→cancel. What it does NOT have: (a) error-chunk injection inside the streaming generator itself — an exception from the user's async-gen currently propagates as an unhandled task error, not as an SSE error frame; (b) a watchdog that detects a permanently-stuck handler and triggers server shutdown. Both halves are needed.

---

## 2. `@with_cancellation`: two-task disconnect race (the right way, not `is_disconnected`)

**The problem.** `request.is_disconnected()` is broken under ASGI middleware. vLLM documents this explicitly (`utils.py:61`): middleware wraps the ASGI `receive` callable; by the time a handler polls `is_disconnected`, the `http.disconnect` message has been consumed upstream and is gone.

**vLLM's exact fix** (`entrypoints/utils.py:57-99`, VLLM.md §R.6/§D2.1, KNOWN verbatim):

```python
async def listen_for_disconnect(request: Request) -> None:
    while True:
        message = await request.receive()
        if message["type"] == "http.disconnect":
            break

def with_cancellation(handler_func):
    @functools.wraps(handler_func)   # REQUIRED: preserves FastAPI signature introspection
    async def wrapper(*args, **kwargs):
        request = args[1] if len(args) > 1 else kwargs["raw_request"]
        handler_task = asyncio.create_task(handler_func(*args, **kwargs))
        cancellation_task = asyncio.create_task(listen_for_disconnect(request))
        done, pending = await asyncio.wait(
            [handler_task, cancellation_task], return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        if handler_task in done:
            return handler_task.result()
        return None
    return wrapper
```

**End-to-end cancel propagation** (`async_llm.py:524`, VLLM.md §D2.2): when the client disconnects mid-stream, Starlette throws `GeneratorExit`/`CancelledError` into `AsyncLLM.generate()`, which catches it and calls `await self.abort(request_id, internal=True)` — freeing the KV-cache blocks. The GPU memory is returned the instant a client hangs up.

**Subtleties** (docstring, `utils.py:57-77`):
1. `functools.wraps` is **required** or FastAPI loses the route's type annotations and the Pydantic body param disappears.
2. Safe only because FastAPI already parsed the body into a Pydantic model — the decorator consumes/discards further ASGI messages while watching for disconnect. Unsafe to apply to handlers that still need to read the request body.
3. For a `StreamingResponse` return: the handler task completes immediately (returns the response object), the disconnect-listener is cancelled, and Starlette's `StreamingResponse` takes over disconnect-watching for the body's duration. Two mechanisms cover the full lifecycle.

**SGLang parallel** (`scheduler.py:1439`, SGLANG.md §4.3): `event_loop_overlap` also propagates client aborts — the scheduler's `process_batch_result` calls `abort_requests` when it detects a finished/aborted uid. The `abort` path frees KV blocks and removes the sequence from the running batch within one forward step.

**Lancelot implication.** Lancelot's `C2 cancellation` already does client-disconnect→upstream cancel via `TransportPool`. The specific gap is at the **handler level**: the `@app.stream` decorator (and any regular handler) does NOT have the two-task race. If Lancelot adds middleware (CORS, auth, etc.), `request.is_disconnected()` will silently fail. The `with_cancellation` two-task race should be added as a Lancelot internal for any handler type. The exact `functools.wraps` + `asyncio.wait(FIRST_COMPLETED)` shape is the mechanism to copy.

---

## 3. Three-state health: `WAITING → PREFILL → DECODE` (readiness vs liveness)

**vLLM health endpoints** (`api_server.py`, VLLM.md §8.2):
- `/health` — liveness: is the HTTP server up?
- `/health/ready` (via `/v1/models` redirect) — implicit readiness: `AsyncLLM.is_running and not AsyncLLM.errored`.
- `terminate_if_errored` (watchdog) is the third state: `engine.errored and not engine.is_running` → `server.should_exit`.

**TGI health** (`router/src/server.rs`, TGI.md §10): `GET /health` + `GET /ping`. The router pings the Python shard via gRPC `Health` RPC; a shard that can't respond → 503. **Block 0 in the radix trie is reserved for health checks** (`radix.rs:45`): health probes pre-allocate one block and free it immediately; a block-allocator that can't allocate block 0 signals memory exhaustion.

**SGLang three endpoints** (`http_server.py:656`, SGLANG.md §10):
- `/health` — liveness.
- `/health_generate` — readiness: can the server accept and complete a minimal generation? This does a real (trivially short) generate call, surfacing scheduler/GPU health.
- `/get_load` — returns `{running: N, waiting: M}` for load-balancer routing decisions.

**LitServe readiness** (`server.py:1029-1051`, LITSERVE.md §2): `/health` returns 200 only when **all** workers are `READY` (`WorkerSetupStatus.READY`) AND `lit_api.health()` passes, else 503. Health reflects model pool readiness, not just HTTP server liveness.

**Lancelot implication.** Lancelot already has `/livez` + `/readyz` (M2 Tier-B). The gap: (a) `/readyz` currently reflects ASGI readiness, not "has the user model/tool loaded?" — SGLang/LitServe's pattern of a user-supplied `health()` callable that gates `/readyz` would make it model-aware; (b) Lancelot has no `/health_generate`-equivalent (a real minimal generation probe); (c) the TGI block-0-reserved-for-health pattern is a clever way to probe memory health inside the allocator — not directly applicable to Lancelot but worth noting for completeness.

---

## 4. KV-cache prefix sharing + sticky sessions (what Lancelot already has)

**The mechanism.** Every engine (vLLM, SGLang, TGI) converges on the same primitive: a prefix cache (keyed by hash of token ids) that allows incoming requests sharing a common prefix (system prompt, few-shot examples) to skip the prefill of that prefix. KV blocks are ref-counted; eviction is LRU over unlocked blocks.

**vLLM** (`kv_cache_utils.py`, VLLM.md §2): flat `block_hash → KVCacheBlock` dict. Hash = SHA-256 (or xxHash) of the block's token ids. Prefix hit = skip recompute, borrow the cached blocks. `ref_cnt > 1` = copy-on-write (shared block never mutated).

**SGLang** (`radix_cache.py`, SGLANG.md §3): radix tree (compressed trie) over token ids. Any shared prefix — not just block-aligned ones — is reusable. `inc_lock_ref` walks from a node up to the root, protecting the whole path. Cache-aware LPM queue sorting: requests that share the most cached KV run first (SGLANG §4.5).

**TGI** (`radix.rs`, TGI.md §6): radix trie over paged blocks. Hash = `DefaultHasher` on the first `block_size` token ids. Insert-on-free: when a request completes, its new prefix blocks are inserted into the trie so the next request with the same prefix gets a hit (TGI §14.3, `radix.rs:157`).

**The session-affinity connection.** "KV-cache-aware routing" = route requests with the same system prompt/prefix to the replica whose KV cache holds that prefix. `Response.set_session_affinity(key)` in Lancelot already provides this primitive. SGLang's LPM scheduling is the scheduler-side complement: once the right replica is chosen, the scheduler prioritizes the request that reuses the most cached KV.

**Lancelot implication.** `lancelot.canonical.dumps_canonical` is already positioned as "prompt-cache-stability serialization" — byte-stable prefix for KV-cache hit. `Response.set_session_affinity` is the routing half. Neither needs to be built; the framing should connect them. The one missing link: a per-key hash that clients can include in their request so the LB can route by prefix without decoding the full prompt — `lancelot.canonical.dumps_canonical(prefix) → sha256 → header` is the pattern. This is a naming/documentation gap, not a code gap.

---

## 5. Continuous batching (what Lancelot has with `@app.batch`)

**The mechanism.** All five engines do continuous (iterative) batching: at each scheduler step, the batch is recomposed from scratch; new requests are inserted whenever there is token budget; a request that generates one token re-enters the next step's batch immediately. No request holds the GPU waiting for `max_tokens` to expire.

**vLLM** (`core.py:step()`, VLLM.md §4): `schedule() → execute() → update_from_output()`. `max_num_batched_tokens=2048` default token budget per step. Chunked prefill: a long prompt is split across multiple steps so it doesn't starve decode.

**SGLang** (`scheduler.py:2388`, SGLANG.md §4.2): prefill-first — try to admit and prefill new requests every step; else advance the running decode batch. The two are interleaved, not serialized. `max_prefill_tokens=16384`, `chunked_prefill_size=2048`. The `new_token_ratio` feedback controller (§4.6) self-tunes admission aggressiveness from observed OOM pressure.

**TGI** (`backend.rs:129`, TGI.md §4): the batching loop; new batches are prefilled and then `FilterBatch` removes finished seqs; `waiting_served_ratio=0.3` controls when to pause decode to admit a prefill (legacy path); with chunked prefill enabled this heuristic is dropped entirely.

**Lancelot's `@app.batch`** (`lancelot/batch.py`): prefix-aware request coalescing, `~12× throughput / ~16× fewer upstream calls` vs a rate-limited LLM backend. This is a **request-coalescing batcher** (batch multiple inbound requests into one upstream LLM call), not a token-level continuous batcher. The distinction: inference engines batch tokens across requests for GPU efficiency; Lancelot batches requests for LLM API efficiency. These are complementary levels of abstraction, not overlapping. Lancelot does not need to be rewritten as a token-level continuous batcher — its role is the HTTP transport layer around the inference engine, not the engine itself.

**Ray Serve's `@serve.batch`** (`batching.py:919-975`, RAY_SERVE.md §R2.1): a within-replica batcher — concurrent async calls to the same handler are coalesced, the batch function runs once, results scatter back. The `max_concurrent_batches` semaphore lets batch k+1 fill on CPU while batch k runs on GPU. This is closer to Lancelot's `@app.batch` than to token-level continuous batching. The key mechanism to copy: **batch timeout caching** — `max_batch_size` and `batch_wait_timeout_s` are snapshotted at the start of each `wait_for_batch` call so a live retune can't tear a half-built batch (SGLANG.md §R2.1 `:254,375`). Lancelot's `@app.batch` should adopt this pattern.

---

## 6. `@app.batch` budget: cost-aware batching (borrow from Ray Serve)

**The mechanism.** Ray Serve's `@serve.batch` supports a `batch_size_fn` parameter: instead of counting requests by `len()`, users supply a function that computes the "cost" of a batch. If a single item's cost exceeds `max_batch_size`, it is **immediately rejected** with `RuntimeError("Size of item is greater than max_batch_size.")` rather than forming a singleton batch that could never meet the budget. The over-budget item is deferred back to the queue tail (FIFO preserved). Source: `batching.py:377-401`, RAY_SERVE.md §R2.1.

**Lancelot implication.** Lancelot's `@app.batch` uses `max_keys` as the batch size key. Adding a user-supplied `budget_fn` that computes token-count or cost per request item would let it handle LLM requests with heterogeneous token lengths (a 100-token request and a 4096-token request should not be naively co-batched). This is the batch-budget primitive the teardowns converge on.

---

## 7. In-batch prefix dedup (SGLang's scheduling trick, zero extra memory)

**The mechanism.** When many *new* requests in the waiting queue share a prefix not yet in the tree (e.g. 100 cold-start requests all beginning with the same long system prompt), naively admitting all of them recomputes the shared prefix 100×. SGLang builds a **throwaway simulated radix tree** over the waiting queue and deprioritizes requests that share `>= IN_BATCH_PREFIX_CACHING_DEPRIORITIZE_THRESHOLD = 32` tokens with an already-seen waiting request (sorted to the back via `float("inf")`), so one request runs first, populates the real cache, and the rest hit it. Source: `schedule_policy.py:160,227-273`, SGLANG.md §3.8.

Constants: `IN_BATCH_PREFIX_CACHING_CHECK_THRESHOLD = 32` (only check requests whose real-cache prefix is short), `IN_BATCH_PREFIX_CACHING_DEPRIORITIZE_THRESHOLD = 32`, LPM→FCFS cutoff = 128 (above 128 queued requests, LPM is too expensive; fall back to FCFS), all from `schedule_policy.py:70,77,204`.

**Lancelot implication.** Lancelot's `@app.batch` uses `key=` to group requests. The SGLang in-batch dedup insight maps cleanly: within a batch window, if two requests share the same `key` prefix, coalesce them (the second is a duplicate). This is already the spirit of `@app.batch`'s prefix-aware grouping — the concrete addition is the dedup logic at batch-assembly time for key hierarchies.

---

## 8. SSE streaming wire format (exact bytes, cross-engine consensus)

All five engines agree on the same SSE format. **Exact bytes** (VLLM.md §R.6, SGLANG.md §10):

- Per-chunk: `data: {json}\n\n` — the JSON is OpenAI `chat.completion.chunk` shape (`model_dump_json(exclude_unset=True, exclude_none=True)`).
- Terminal sentinel: `data: [DONE]\n\n` — literal string, not a JSON object.
- Streaming delta mode: `output_kind = RequestOutputKind.DELTA` (vLLM, VLLM.md §6) emits only new tokens per chunk. `CUMULATIVE` emits full text-so-far (more expensive). `stream_interval = 1` (default) = one chunk per token.
- SGLang `stream_interval` default: `1` (`server_args.py:439`, SGLANG.md §11) — every decode step emits a chunk. Setting `stream_interval > 1` reduces SSE overhead at the cost of chunkier output (batches N tokens per SSE event).
- Error mid-stream: emit `data: {error_json}\n\n`, then still emit `data: [DONE]\n\n`.

**LitServe OpenAI spec wire format** (`openai.py:536-583`, LITSERVE.md §9, KNOWN verbatim):

```python
yield f"data: {chunk.model_dump_json(by_alias=True)}\n\n"
# final chunk with finish_reason="stop" + usage:
yield f"data: {last_chunk.model_dump_json(by_alias=True)}\n\n"
yield "data: [DONE]\n\n"
```

Note `by_alias=True` — field aliases (e.g. `finish_reason` not `finish_reason_`) must match OpenAI's schema.

**Lancelot implication.** Lancelot's R3 pump and `@app.stream` already emit SSE via `stream_interval` and the `[DONE]` sentinel. The gap is the **error-chunk** pattern — if the user's async-gen throws, Lancelot currently lets the exception propagate up rather than injecting it as a `data: {error_json}\n\n` chunk. The missing 4 lines of exception-catching + error-chunk emission inside the streaming generator would close this.

---

## 9. Three-tier health vs the Lancelot fleet endpoint

**SGLang `/get_load`** (`http_server.py:656`, SGLANG.md §10): `GET /get_load` returns `{running: N, waiting: M}`. This is the load-balancer's routing signal — a proxy/LB polls this to decide which replica is least busy, without the heavier `/health_generate` probe.

**Lancelot's `/fleet/status`** already returns `{region, worker_id, status, in_flight, uptime_s, lancelot_version}`. `in_flight` is the equivalent of SGLang's `running`. Adding `queued` (requests waiting to be scheduled) would complete the signal. `status` already flips to `"draining"` on `initiate_shutdown()` — this is the `PREEMPTED` → re-queue state machine that SGLang/vLLM implement at the scheduler level.

---

## 10. Worker restart + in-flight request attribution (LitServe pattern)

**The mechanism** (`server.py:1602-1680`, LITSERVE.md §E.2): a daemon thread polls every 2 s. When a worker process dies:
- If `restart_workers=False` (default): **fail-stop** — one dead worker triggers graceful shutdown of the whole server.
- If `restart_workers=True`: scan `response_buffer` for entries whose `resp.worker_id == dead_worker_id` and `resp.event` is not yet set; mark them `ERROR` and `event.set()` (unblocking the parked handler with a 500 instead of hanging forever). Then relaunch the worker.

The `worker_id` is carried in every response tuple precisely to enable this attribution. Without it, a crashed GPU process would strand every connection it was serving indefinitely.

**Lancelot implication.** Lancelot's `LANCELOT_WORKERS` multiprocess model has `LANCELOT_DRAIN_SECS` for graceful drain but no mechanism to attribute in-flight requests to a specific worker and fail them on worker death. For a production multi-worker deployment, adding worker-id attribution and a fail-fast path (fail in-flight requests immediately on worker death rather than letting them time out) is the LitServe lesson.

---

## 11. Honest assessment of what Lancelot ALREADY PROVIDES from this cluster

| Serving primitive | Source | Lancelot status |
|---|---|---|
| SSE streaming with backpressure | vLLM/SGLang/TGI R3 pump | **BUILT** — `@app.stream`, R3 pump, `LANCELOT_STREAM_BUFFER` |
| Client-disconnect→upstream cancel | vLLM `with_cancellation` | **PARTIALLY BUILT** — C2 cancel via TransportPool; missing the two-task race at handler level |
| Request batching (coalescing) | vLLM/SGLang continuous batch | **BUILT** (differently) — `@app.batch` does request-coalescing for LLM APIs; not token-level continuous batching |
| Liveness + readiness probes | SGLang `/health` + `/health_generate` | **BUILT** — `/livez` + `/readyz`; missing model-aware readiness hook |
| Fleet status + in-flight count | SGLang `/get_load` | **BUILT** — `/fleet/status` with `in_flight`; missing `queued` counter |
| Session affinity (KV-cache routing) | SGLang LPM scheduling | **BUILT** — `Response.set_session_affinity`; framing gap (not described as KV-cache affinity) |
| Prompt-cache prefix stability | vLLM/SGLang block hash | **BUILT** — `lancelot.canonical.dumps_canonical` |
| TTFT metric | SGLang/vLLM serving metrics | **BUILT** — `lancelot.http.stream.ttft` |
| Error-chunk in streaming | vLLM `serving.py:989-996` | **MISSING** — exceptions propagate; not injected as SSE error frames |
| Out-of-band watchdog | vLLM `launcher.py:144-167` | **MISSING** — no equivalent `engine.errored` detector |
| Batch budget fn (cost-aware) | Ray Serve `batch_size_fn` | **MISSING** — `@app.batch` uses `max_keys`, no cost-aware budget |
| SGLang `stream_interval` knob | `server_args.py:439` | **BUILT** — Lancelot's `stream_interval` in `@app.stream` |

---

## 12. Anti-patterns to avoid

**A1. Using `request.is_disconnected()` behind middleware.** vLLM documents this as broken (`utils.py:61`). Any middleware (CORS, auth, request-id) consumes the `http.disconnect` message before the handler can poll it. The fix is the two-task race.

**A2. Raising exceptions inside a streaming generator post-200.** Once headers are sent, exceptions cannot become HTTP error codes. They must be caught inside the generator and emitted as data chunks. A naïve `raise` inside `@app.stream`'s generator will produce a broken stream with no error signal to the client.

**A3. Checking engine health at the HTTP layer only.** LitServe's `/health` returning 503 until all workers are `READY` (`WorkerSetupStatus.READY`) is the correct pattern. An HTTP server that accepts traffic before its GPU workers have loaded the model will return 500s for the first N requests. Lancelot's `/readyz` should gate on user-side readiness.

**A4. Treating `@app.batch` as equivalent to continuous batching.** They operate at different abstraction levels. `@app.batch` coalesces HTTP requests into one upstream LLM call (API efficiency). Continuous batching (vLLM/SGLang) batches tokens across requests for GPU throughput. Lancelot is the layer between clients and the inference engine; it should not try to replicate what the engine does internally.

**A5. Deprioritizing short-output requests behind long ones** (the request convoy problem). Continuous batching solves this at the token level. At Lancelot's level, the `@app.batch` key design and any priority queue for the request pool should account for estimated response size.

---

## 13. Exact constants to calibrate

| Constant | Value | Source |
|---|---|---|
| vLLM watchdog interval | `5.0 s` | `VLLM_WATCHDOG_TIME_S`, `launcher.py` |
| SGLang watchdog timeout | `300 s` | `server_args.py:447` |
| SGLang in-batch dedup threshold | `32` tokens | `schedule_policy.py:70,77` |
| SGLang LPM→FCFS queue cutoff | `128` requests | `schedule_policy.py:204` |
| SGLang `max_prefill_tokens` | `16384` | `server_args.py:411` |
| SGLang `chunked_prefill_size` | `2048` | `server_args.py:1445` |
| TGI `waiting_served_ratio` | `0.3` | `launcher/main.rs:707` |
| TGI `max_waiting_tokens` | `20` | `launcher/main.rs:754` |
| TGI `cuda_graphs` bucket sizes | `1,2,4,8,16,32` descending | `launcher/main.rs:764` |
| LitServe worker-monitor interval | `2 s` | `server.py:1602` |
| Ray Serve `@serve.batch` max_concurrent_batches default | N/A (user-set) | `batching.py:474` |
| Ray Serve multiplex push cadence | `1 s` | `multiplex.py:118` |
| Ray Serve jittered affinity timeout | `uniform(T, 2T)` | `request_router.py:593` |

---

## 14. API shape notes worth stealing

**LitServe's output normalizer** (`openai.py:678-696`, LITSERVE.md §R2.2): accepts 5 shapes (None, str, full `{role,content}` dict, partial dict, list-of-dicts taking the last) and coerces to canonical OpenAI assistant message. The "accept N output shapes → raise a precise 500 otherwise" adapter is the right ergonomic pattern for any serving layer that needs strict external compliance with a forgiving internal contract. The `pop`-usage-from-message double-parse trick is clean.

**Ray Serve `self`-as-`Depends` for class-based views** (`http_util.py:476-514`, RAY_SERVE.md §2): rebind method routes so `self` is injected via `Depends(get_current_servable_instance)`. Works because FastAPI introspects `__signature__`, not the real function signature. Pattern: `route.endpoint.__signature__ = old_sig.replace(parameters=[self_param.replace(default=Depends(factory))] + rest_kw_only)`. Not directly needed for Lancelot but a clean seam for stateful route classes.

**vLLM's uniform error envelope** (`server_utils.py:324-419`, VLLM.md §R.9): `ErrorResponse{error: ErrorInfo{message, type, param, code}}` — one Pydantic model, status code carried *inside* the body so JSON and HTTP status never diverge. Six `@app.exception_handler` registrations, each returning `JSONResponse(err.model_dump(), status_code=err.error.code)`. Lancelot should adopt this for its own error surface.

**Ray Serve Power-of-Two-Choices with jitter** (`pow_2_router.py:52-106`, RAY_SERVE.md §R2.3): for k=2 sampling, the hand-rolled `i = randrange(n); j = randrange(n-1); if j>=i: j+=1` is ~1.9× faster than `random.sample(population, 2)`. For Lancelot's multiprocess load balancer or any routing layer with high call rate, this micro-optimization matters.

---

## 15. What to skip (Tier-D for Lancelot's context)

- **PagedAttention / KV cache block management**: this is the inference engine's job (vLLM/SGLang/TGI). Lancelot is the layer in front of the engine. Do not replicate KV block accounting in Lancelot.
- **Token-level continuous batching**: same reason. `@app.batch` already handles the request-coalescing layer. Building a token-level scheduler in Lancelot would make it an inference engine, violating the AI-logic-on-the-user-side boundary.
- **Compressed-FSM jump-forward structured decoding** (SGLang §8.3): this is an inference engine optimization for constrained generation. Not Lancelot's layer.
- **CUDA graphs / GPU kernel selection**: inference engine internals.
- **Disaggregated prefill-decode** (SGLang §26, TGI §23): multi-node KV transfer architecture. Not relevant until Lancelot is deployed as a front-end to a disaggregated inference cluster, which is speculative.
- **LitServe ZMQ pub/sub broker** (LITSERVE.md §R2.1): Lancelot's multiprocess model uses `SO_REUSEPORT` and shared asyncio loops. The ZMQ XPUB/XSUB broker pattern is interesting for cross-process response routing but Lancelot doesn't have the worker-per-GPU-device problem LitServe solves — it solves transport, not model dispatch.
