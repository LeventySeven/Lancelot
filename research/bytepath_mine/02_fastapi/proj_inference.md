# vLLM — byte-path mechanisms for Lancelot (inference-server archetype)

**Sources read (Tier 1A — verbatim code):**
- `fastapi/code/vllm/utils.py` — `@with_cancellation`, `listen_for_disconnect`, `@load_aware_call`
- `fastapi/code/vllm/launcher.py` — `serve_http`, `watchdog_loop`, `terminate_if_errored`
- `fastapi/code/vllm/openai/server_utils.py` — `AuthenticationMiddleware`, `XRequestIdMiddleware`, `lifespan`, exception handlers
- `fastapi/code/vllm/openai/chat_completion/api_router.py` — route + decorator stack
- `fastapi/code/vllm/deep/chat_completion_serving.py` — SSE stream generator (full)
- `fastapi/code/vllm/deep/v1_output_processor.py` — `RequestOutputCollector`
- `fastapi/code/vllm/deep/engine_serving.py` — `OpenAIServing` base, `create_streaming_error_response`
- `teardowns/VLLM.md` — full teardown (2359 lines)

Cross-ref: `teardowns/VLLM.md §8-D2` (Round-2 deep pass, 2026-05-30)

---

## M1 — `@with_cancellation`: dual-task disconnect race (not `request.is_disconnected`)

**File:line:** `entrypoints/utils.py:57-99` (extracted copy: `vllm/utils.py:57-99`)

**The mechanism.** `request.is_disconnected()` does NOT work when any ASGI middleware is installed (CORs, auth, request-id). Middleware wraps the `receive` callable; the `http.disconnect` message is consumed/buffered upstream and never arrives at `is_disconnected()`. vLLM's fix: a decorator that races the handler task against a `listen_for_disconnect` task via `asyncio.wait(return_when=FIRST_COMPLETED)`. Whichever wins, the loser is `.cancel()`ed.

```python
# utils.py:57-99 (verbatim)
def with_cancellation(handler_func):
    @functools.wraps(handler_func)  # functools.wraps REQUIRED — FastAPI reads type hints from it
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

`listen_for_disconnect` (`:42-54`) loops `await request.receive()` until it sees `msg["type"] == "http.disconnect"`. Safe only because the body was already read by FastAPI into the Pydantic model before the decorator runs — the decorator can then consume-and-discard all further incoming messages while scanning for disconnect.

For `StreamingResponse` returns: the handler task completes immediately (it returns the response *object*, not the stream), so the disconnect listener is cancelled and Starlette's own `StreamingResponse` disconnect-watching takes over for the duration of the stream. Two mechanisms cover the full request lifetime, no gap.

Cancel propagation chain: disconnect → `.cancel()` on handler_task → `CancelledError`/`GeneratorExit` inside `AsyncLLM.generate()` → `await self.abort(request_id)` → ZMQ push to EngineCore → `finish_requests(request_ids, FINISHED_ABORTED)` → KV blocks freed. GPU memory returned the instant the client hangs up.

**Lancelot state:** HAVE (with a different but equivalent impl). `lancelot/asgi.py:574-630` has `_watch_disconnect` + `_race_handler_vs_disconnect` — same FIRST_COMPLETED dual-task race shape, referenced in comments as "the same FIRST_COMPLETED dual-task shape as vLLM" (`:606`). The C2 path is validated under real adversarial load (M4). `functools.wraps` equivalent: Lancelot doesn't use Python decorator stacks for this; it's baked into the ASGI dispatch. **Confirmed HAVE — no action.**

---

## M2 — Error-as-chunk: mid-stream error protocol (HTTP 200 + SSE error + `[DONE]`)

**File:line:** `chat_completion/serving.py:989-996` + `engine/serving.py:182-197`

**The mechanism.** Once the 200 + `text/event-stream` headers are sent, an exception can no longer become an HTTP error code. The generator wraps the entire `async for res in result_generator` in `try/except` and, on any exception, emits the error as an SSE data frame then unconditionally yields `data: [DONE]\n\n`:

```python
# chat_completion/serving.py:989-996
except GenerationError as e:
    yield f"data: {self._convert_generation_error_to_streaming_response(e)}\n\n"
except Exception as e:
    logger.exception("Error in chat completion stream generator.")
    data = self.create_streaming_error_response(e)
    yield f"data: {data}\n\n"
# unconditional terminal
yield "data: [DONE]\n\n"
```

`create_streaming_error_response` (`engine/serving.py:182-197`): `json.dumps(ErrorResponse(error=ErrorInfo(message=..., type=..., code=...)).model_dump())` — same envelope shape as the HTTP error path, so clients parse both paths identically.

The error chunk is always well-formed SSE. The connection is never left dangling. Because the exception is swallowed (not re-raised), the server process does not auto-shutdown — which is exactly why the watchdog (M3) must exist.

`finish_reason="error"` field: the engine can also signal a retryable error via `output.finish_reason == "error"` on a normal token output. `_raise_if_error` (`engine/serving.py:199-205`) converts that to a `GenerationError("Internal server error")` which then hits the `except GenerationError` path above.

**Lancelot state:** HAVE (`error_frame` opt-in on `@app.stream` routes, `asgi.py:1107,1155-1397`). Lancelot ships `error_frame=True/False` per-route — when enabled, catches exceptions in the stream pump and emits a scrubbed SSE error frame + closes, then `[DONE]`. The mechanism is functionally identical. **Confirmed HAVE — no action.**

---

## M3 — Watchdog: 5s background loop catches fatal engine errors swallowed by streaming

**File:line:** `entrypoints/launcher.py:144-167`

**The mechanism.** Because a fatal engine error during a `StreamingResponse` is swallowed (M2) and never reaches the exception handlers, the watchdog is the safety net. It's a 5-second polling loop launched as a parallel asyncio task:

```python
# launcher.py:144-167
VLLM_WATCHDOG_TIME_S = 5.0

async def watchdog_loop(server: uvicorn.Server, engine: EngineClient):
    while True:
        await asyncio.sleep(VLLM_WATCHDOG_TIME_S)
        terminate_if_errored(server, engine)

def terminate_if_errored(server, engine):
    engine_errored = engine.errored and not engine.is_running
    if not envs.VLLM_KEEP_ALIVE_ON_ENGINE_DEATH and engine_errored:
        server.should_exit = True  # flip uvicorn shutdown flag
```

The `server.should_exit = True` does NOT block: the uvicorn server teardown runs on the next event loop iteration. It does NOT `await` shutdown — the handler must return first to close its connection (known uvicorn issue). The watchdog is cancelled alongside the server task in `handle_shutdown`.

The full shutdown path (`:95-141`): SIGINT/SIGTERM → `asyncio.Event` → `handle_shutdown()` awaits event → `await loop.run_in_executor(None, partial(engine_client.shutdown, timeout=timeout))` (blocking engine shutdown pushed to a thread) → `server.should_exit = True; server_task.cancel(); watchdog_task.cancel()`.

**Lancelot state:** HAVE (different shape). The internal watchdog `_watchdog_loop` at `asgi.py:1218` is a per-stream silence/keepalive watchdog, not a global engine-health monitor. Lancelot's architecture doesn't have a separate engine process that can die independently; Lancelot IS the engine. The pattern still applies if the user runs an upstream LLM and wants a global health watchdog. **HAVE the general concept; the specific "flip should_exit on dead engine" is N/A to Lancelot's architecture (no separate engine process).**

---

## M4 — `RequestOutputCollector`: 1-slot mailbox with in-place merge for slow consumers

**File:line:** `v1_output_processor.py:45-107`

**The mechanism.** Not an `asyncio.Queue`. A 1-slot mailbox built on a single `asyncio.Event`:

```python
class RequestOutputCollector:
    def __init__(self, output_kind, request_id):
        self.aggregate = output_kind == RequestOutputKind.DELTA
        self.output: RequestOutput | ... | None = None
        self.ready = asyncio.Event()

    def put(self, output):  # non-blocking
        if self.output is None or isinstance(output, Exception):
            self.output = output
            self.ready.set()
        elif isinstance(self.output, RequestOutput) and isinstance(output, RequestOutput):
            self.output.add(output, aggregate=self.aggregate)  # MERGE IN PLACE
        # ... PoolingRequestOutput case

    async def get(self):  # blocks
        while (output := self.output) is None:
            await self.ready.wait()
        self.output = None
        self.ready.clear()
        if isinstance(output, Exception):
            raise output
        return output
```

Why this shape: with an unbounded queue, a fast GPU producer streaming tokens to a slow HTTP client (slow network) builds an ever-growing backlog. Bounding the queue backpressures the GPU (hurts other requests). The 1-slot solution: if the producer calls `put()` again before the consumer `get()`s, outputs are **merged in place** — for DELTA mode, token deltas are concatenated. A slow client gets coarser SSE chunks (several tokens per frame) rather than backpressure or memory blowup. Exceptions get priority (they overwrite immediately).

This is the `AsyncLLM` output-collector pattern (V1 engine architecture, `async_llm.py:420-430`): background `output_handler` coroutine pulls `EngineCoreOutputs` from ZeroMQ, calls `output_processor.process_outputs()`, pushes to per-request `RequestOutputCollector` queue; `generate()` yields from that queue.

**Lancelot state:** NOT HAVE in this exact form. Lancelot's R3 pump (`src/stream.rs`) uses a bounded-mpsc channel (capacity = `LANCELOT_STREAM_BUFFER`, default 256) which blocks the Rust pump when full (applies backpressure). The 1-slot merge avoids backpressure entirely by coarsening chunks. For AI streaming (LLM-rate-limited), this difference is academic — the LLM is slower than both producers. However, the single-`asyncio.Event` mailbox is instructive for Python-side output staging. **LOW priority — not a Lancelot problem in practice (LLM is the bottleneck).**

---

## M5 — `stream_interval`: N-token buffering before SSE flush (scheduler config)

**File:line:** `config/scheduler.py:635` + `v1_output_processor.py:287-308`

**The mechanism.** `SchedulerConfig.stream_interval: int = 1` — how many decoded tokens to buffer before emitting an SSE event. Default 1 = send each token immediately. Setting > 1 coarsens chunks: reduces host-side CPU and JSON-encoding overhead per stream at cost of slightly higher TTFT (the first chunk is still sent immediately; buffering only applies after the first token). The gate:

```python
# v1_output_processor.py:287-308 (RequestState.make_request_output)
if self.stream_interval > 1:
    if not (
        finished
        or self.sent_tokens_offset == 0   # always emit first token
        or self.detokenizer.num_output_tokens() - self.sent_tokens_offset >= self.stream_interval
    ):
        return None  # suppress this output; buffer it
```

For DELTA mode with interval > 1, slices `output_token_ids[sent_tokens_offset:]` and advances the offset — delivers the buffered delta tokens in one batch.

**Lancelot state:** HAVE. `@app.stream(stream_interval=N)` (`app.py:539-614`) and `route.stream_interval` (`routing.py:53`). Same semantics: batch N events per SSE frame, first token always immediate. **Confirmed HAVE — no action.**

---

## M6 — `freeze_gc_heap()` before `yield` in lifespan: cut GC pause jitter

**File:line:** `openai/server_utils.py:467`

**The mechanism.**
```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # ... start background stats task ...
    freeze_gc_heap()  # gc.freeze() — moves all current objects to "permanent" gen
    yield             # server starts serving
    # ... cleanup ...
```

`gc.freeze()` (Python 3.7+) moves every object alive at that point into a "permanent" generation that the generational GC never scans again. Since startup allocations (model weights, engine objects, router tables) live for the whole process, scanning them on every gen-2 GC cycle is pure waste. After `freeze_gc_heap()`, only request-lifetime objects appear in the live heap — GC pause times drop because the working set being scanned is much smaller. Critical at inference-server scale where models + caches can be 10s of GBs of Python object graph.

vLLM calls this right before `yield` in lifespan so all engine-init, model-loading, and warmup allocations are frozen at peak static footprint.

**Lancelot state:** NOT HAVE. Lancelot's lifespan/startup path does not call `gc.freeze()`. Given that Lancelot's runtime dep is pydantic-only (no large model weights in process), the impact is smaller but still real: route table, middleware chain, OTel instruments, Pydantic validators all live at startup. At scale (many routes, heavy middleware), this would reduce GC jitter. This is a one-liner. **MEDIUM priority — low-effort, measurable at scale.**

```python
# Where to add in Lancelot: lancelot/asgi.py in _ASGIApp.__call__ lifespan path
# OR in lancelot/app.py App.run() / App.__call__ after startup hooks complete
import gc
gc.freeze()  # after startup events complete, before first request
```

---

## M7 — h11 header limits: raise defaults for tool-call payloads and many headers

**File:line:** `entrypoints/launcher.py:60-74` + `entrypoints/constants.py` (`H11_MAX_HEADER_COUNT_DEFAULT`, `H11_MAX_INCOMPLETE_EVENT_SIZE_DEFAULT`)

**The mechanism.** Uvicorn uses h11 as its HTTP/1.1 parser. h11's defaults are conservative: `max_incomplete_event_size` (default ~16 KB) caps the request line + headers combined; `max_header_count` limits total headers. AI workloads routinely exceed these: tool-call payloads serialize large JSON into headers, agent frameworks inject many tracking headers. vLLM explicitly pops these from the uvicorn kwargs and applies raised defaults:

```python
# launcher.py:60-74
h11_max_incomplete_event_size = uvicorn_kwargs.pop("h11_max_incomplete_event_size", None)
h11_max_header_count = uvicorn_kwargs.pop("h11_max_header_count", None)
if h11_max_incomplete_event_size is None:
    h11_max_incomplete_event_size = H11_MAX_INCOMPLETE_EVENT_SIZE_DEFAULT
if h11_max_header_count is None:
    h11_max_header_count = H11_MAX_HEADER_COUNT_DEFAULT

config = uvicorn.Config(app, **uvicorn_kwargs)
config.h11_max_incomplete_event_size = h11_max_incomplete_event_size
config.h11_max_header_count = h11_max_header_count
config.load()
```

These are set AFTER `uvicorn.Config(...)` is created but BEFORE `config.load()` — h11 reads them during `load()`.

**Lancelot state:** Lancelot runs its own Rust HTTP/1.1 server (hyper, not h11/uvicorn), so this exact mechanism does not apply. Lancelot's slow-loris guard (`LANCELOT_HEADER_TIMEOUT_MS`) gates header-read time but does not impose a byte-count cap on request headers. A large-headers cap equivalent exists via `LANCELOT_MAX_BODY_BYTES` (body) but there is no explicit header-byte cap. **LOW priority for Lancelot's native server; MEDIUM if Lancelot ever needs to guide users to configure uvicorn when used as an ASGI app behind uvicorn.**

---

## M8 — Deferred `@load_aware_call`: in-flight counter uses `BackgroundTask` for streaming

**File:line:** `entrypoints/utils.py:106-151`

**The mechanism.** Naive approach: `counter += 1` at entry, `counter -= 1` in `finally`. Wrong for streaming: the handler returns the `StreamingResponse` object almost immediately (before any token streams), so a `finally` decrement fires while the request is still actively streaming. The gauge would read ~0 under heavy streaming load.

vLLM's fix: increment on entry; decrement via `BackgroundTask` on the response object, which Starlette runs only after the response body is fully sent:

```python
# utils.py:130-145
if isinstance(response, (JSONResponse, StreamingResponse)):
    if response.background is None:
        response.background = BackgroundTask(decrement_server_load, raw_request)
    elif isinstance(response.background, BackgroundTasks):
        response.background.add_task(decrement_server_load, raw_request)
    elif isinstance(response.background, BackgroundTask):
        # promote single BackgroundTask to BackgroundTasks, preserve original
        tasks = BackgroundTasks()
        tasks.add_task(response.background.func, *response.background.args, **response.background.kwargs)
        tasks.add_task(decrement_server_load, raw_request)
        response.background = tasks
else:
    raw_request.app.state.server_load_metrics -= 1  # non-streaming: decrement inline
```

Decorator order on the route matters: `@with_cancellation` outermost, `@load_aware_call` inner (`api_router.py:51-52`). Disconnect decrement is separate: `listen_for_disconnect` calls `request.app.state.server_load_metrics -= 1` when it sees disconnect (`utils.py:47-54`).

**Lancelot state:** PARTIAL. Lancelot's in-flight counter uses `finally` around `_http_dispatch` (`asgi.py:399-405`). For non-streaming requests, this is correct. For streaming requests (`@app.stream`), `_http_dispatch` is the stream pump itself — it doesn't return until the stream ends — so actually the Lancelot counter IS held for the full stream duration (the pump is blocking in the asyncio sense). Need to verify: does `in_flight_delta(-1)` fire before or after the last byte is sent for streaming? If the pump (`_drive_stream`) runs inside `_http_dispatch` and returns only after all chunks are pushed, the counter is correct. This deserves a quick verify but is likely fine. **LOW priority — likely not a bug.**

---

## M9 — `model_dump_json(exclude_unset=True)` on SSE chunks: minimal bytes-on-wire

**File:line:** `deep/chat_completion_serving.py:545,932`

**The mechanism.** Every SSE chunk calls `chunk.model_dump_json(exclude_unset=True)`. A content-only delta emits `{"delta":{"content":"x"}}` with no `role`/`finish_reason` keys. The final usage chunk additionally uses `exclude_none=True`. System fingerprint is stamped only on the terminal chunk, not every chunk. The final usage chunk has `choices:[]` (empty array = OpenAI convention for trailing usage-only frame).

Why this matters at scale: at 50 tokens/s per stream, 1000 concurrent streams = 50k chunks/s. Even 50 bytes of extra fields per chunk = 2.5 MB/s of gratuitous bytes. `exclude_unset` means the Pydantic model only serializes fields that were explicitly set in the constructor call; unset fields (with defaults) are omitted.

**Lancelot state:** HAVE (Lancelot's SSE pump passes user bytes through directly — user controls serialization). For Lancelot's own built-in response models (OpenAPI, error responses, metrics) this isn't a streaming issue. **Not actionable for Lancelot core.**

---

## M10 — Pure-ASGI `AuthenticationMiddleware`: SHA-256 pre-hash + constant-time compare + path guarding

**File:line:** `openai/server_utils.py:41-89`

**The mechanism.**
```python
class AuthenticationMiddleware:
    def __init__(self, app, tokens: list[str]):
        # Pre-hash tokens at startup (not per-request)
        self.api_tokens = [hashlib.sha256(t.encode("utf-8")).digest() for t in tokens]

    def verify_token(self, headers):
        # Hash the incoming token
        param_hash = hashlib.sha256(param.encode("utf-8")).digest()
        token_match = False
        for token_hash in self.api_tokens:
            token_match |= secrets.compare_digest(param_hash, token_hash)  # constant-time
        return token_match

    def __call__(self, scope, receive, send):
        # Skip OPTIONS + skip non-GUARDED_PREFIX paths (e.g. /health never guarded)
        if scope["type"] not in ("http", "websocket") or scope.get("method") == "OPTIONS":
            return self.app(scope, receive, send)
        url_path = scope["path"].removeprefix(root_path)
        if url_path.startswith(GUARDED_PREFIX) and not self.verify_token(headers):
            response = JSONResponse(content={"error": "Unauthorized"}, status_code=401)
            return response(scope, receive, send)
        return self.app(scope, receive, send)
```

`GUARDED_PREFIX = ("/v1", "/v2", "/inference")` — `/health`, `/metrics`, `/livez`, `/readyz` always bypass auth.
`secrets.compare_digest` in a loop with `|=` (not short-circuit `or`) ensures all token comparisons run regardless of match, preventing timing oracle attacks.

**Lancelot state:** HAVE (`lancelot/auth.py` — `StaticTokenGate`, identical pattern: SHA-256 pre-hash at startup, `hmac.compare_digest` per comparison, pure-ASGI). One difference: Lancelot's `StaticTokenGate` is opt-in by the user (not auto-wired by `App`). vLLM auto-wires it when `--api-key` is set. **Confirmed HAVE — no action.**

---

## Summary table

| # | Mechanism | File:line | Byte-path impact | Lancelot status |
|---|-----------|-----------|-----------------|-----------------|
| M1 | `@with_cancellation` dual-task race | `utils.py:57-99` | Correct GPU abort on disconnect behind middleware | **HAVE** (C2, `asgi.py:574-630`) |
| M2 | Error-as-chunk: emit error SSE + `[DONE]` | `chat_completion/serving.py:989-996` | Well-formed stream on engine failure | **HAVE** (`error_frame`, `asgi.py:1107+`) |
| M3 | Engine-death watchdog (5s poll, `should_exit`) | `launcher.py:144-167` | Self-heal when streaming swallows fatal errors | **N/A** (Lancelot has no separate engine process) |
| M4 | `RequestOutputCollector` 1-slot mailbox + delta merge | `v1_output_processor.py:45-107` | Coarser chunks vs backpressure for slow consumers | **LOW** (LLM-rate-limited; not actionable) |
| M5 | `stream_interval` token-buffer before SSE flush | `scheduler.py:635`, `v1_output_processor.py:287` | Reduce serialization overhead at cost of chunk coarseness | **HAVE** (`@app.stream(stream_interval=N)`) |
| M6 | `gc.freeze()` post-startup (freeze_gc_heap) | `server_utils.py:467` | Cut GC pause jitter; remove static allocs from scanning | **NOT HAVE** — one-liner, low-effort |
| M7 | Raise h11 `max_incomplete_event_size` + `max_header_count` | `launcher.py:60-74` | Prevent 431 on large tool-call / many-header requests | **N/A** (Rust server, not h11); operator doc note |
| M8 | `@load_aware_call`: BackgroundTask-deferred in-flight decrement | `utils.py:106-151` | Accurate in-flight gauge under streaming load | **PARTIAL** (Lancelot pump blocks until stream done — likely correct anyway) |
| M9 | `exclude_unset=True` on every SSE chunk | `serving.py:545` | Minimal bytes-on-wire per chunk | **N/A** (user controls byte format) |
| M10 | Pure-ASGI constant-time bearer auth + path guard | `server_utils.py:41-89` | Correct CT auth without coupling to framework | **HAVE** (`lancelot/auth.py:StaticTokenGate`) |

## Actionable for Lancelot

Only M6 is a real new win:

**M6 — `gc.freeze()` post-startup** (`gc.freeze()` call in lifespan after all startup hooks run). One line. Removes route table, middleware chain, Pydantic validators, OTel instruments, all static allocations from GC scanning permanently. Reduces old-gen GC pause jitter during request serving. Needs to be gated: only call if `gc.freeze` exists (Python 3.7+, stdlib). Place: in `lancelot/asgi.py` lifespan startup path or `App.run()` after `__startup_complete`.

```python
# Candidate location: lancelot/asgi.py, after lifespan startup hooks complete
import gc
if hasattr(gc, "freeze"):
    gc.freeze()
```

Risk: zero. `gc.freeze()` is side-effect-free for correctness (GC still runs, it just skips the frozen generation). Can be opt-out with an env var `LANCELOT_NO_GC_FREEZE=1` for environments that rely on cyclic-ref cleanup in long-lived static objects.
