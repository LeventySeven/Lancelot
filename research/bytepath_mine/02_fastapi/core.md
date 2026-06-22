# FastAPI core — byte-path mechanism note
Source cluster: `fastapi/fastapi/{applications,routing,datastructures,requests,responses,concurrency,background,sse}.py`
FastAPI version: 0.136.1 (vendored in `researchfms/fastapi/fastapi/`)

---

## 1. `use_dump_json` fast path — Pydantic-core serialize directly to bytes, skip Python dict
**File:line:** `routing.py:688-714`
**Exact mechanism:** In `get_request_handler`, when `response_field is not None` AND `isinstance(response_class, DefaultPlaceholder)` (i.e. no explicit custom response class was set), FastAPI sets `use_dump_json = True` and calls `field.serialize_json(...)` in `serialize_response`. This calls pydantic-core's `TypeAdapter.to_json()` path, which returns `bytes` directly (Rust-core encode, no intermediate Python dict, no `json.dumps()`). The result is wrapped in a bare `Response(content=bytes, media_type="application/json")` — NOT `JSONResponse` (which would re-encode). The condition `isinstance(response_class, DefaultPlaceholder)` is the guard: any explicit `response_class=ORJSONResponse` (even if deprecated) falls through to the old Python-dict path.

**Lancelot check hint:** `lancelot/routing.py` L118: comment says "the cached TypeAdapter's `dump_json` (~4x cheaper on a large read)". Lancelot stores `_response_ta` at registration. Grep the ASGI seam (`asgi.py`) for where that TypeAdapter is actually called with `dump_json=True` vs `.model_dump()` + `json.dumps`. Verify the byte-direct path is actually taken (not just the TypeAdapter cached but still going through `_wire_json`). The FastAPI comment says "Serializes directly to JSON bytes via Pydantic's Rust core, skipping the intermediate Python dict + json.dumps() step" — check that Lancelot's path matches the same `field.serialize_json` branch.

**Byte-path relevance:** Real win on large payloads (already proven: 2.3× at 69 KB). The guard condition `isinstance(response_class, DefaultPlaceholder)` is the exact discriminator FastAPI uses — Lancelot must match it so conformance tests that explicitly pass `response_class=JSONResponse` do NOT take the bytes-shortcut (they might get extra encode otherwise).

---

## 2. `serialize_response` — sync validator runs in threadpool, async runs inline
**File:line:** `routing.py:277-317`
**Exact mechanism:** `serialize_response` checks `is_coroutine` (set from `dependant.is_coroutine_callable` at registration, not per-request). When `False` (sync endpoint), it calls `await run_in_threadpool(field.validate, response_content, {}, loc=("response",))` — the validation step itself is offloaded to avoid blocking the loop with a heavy Pydantic validate on a big model. When `True` (async), validate runs inline on the loop (presumed fast/non-blocking).

**Lancelot check hint:** `lancelot/routing.py` L565: `if route.plan.is_async: result = await route.handler(...)` else `get_pool().offload(...)`. But the *response-model validation* step that happens AFTER `result` is obtained — does it also offload for sync endpoints? FastAPI offloads `field.validate` per response for sync; Lancelot may be doing it inline in the ASGI seam for all endpoints. Low risk but verifiable.

**Byte-path relevance:** Micro-optimization: for sync endpoints with heavy response models (large lists of Pydantic objects), offloading validate prevents a GIL-blocking Pydantic scan from stalling the event loop. Non-bottleneck in most AI-serving shapes (sync handlers rare, responses usually pre-validated), but good conformance detail.

---

## 3. SSE producer/keepalive architecture — anyio task group + memory stream with timeout
**File:line:** `routing.py:534-608`
**Exact mechanism:** FastAPI's SSE path uses a three-actor pattern:
1. `_producer` task: iterates the generator (`sse_aiter`) and pushes serialized bytes into a `anyio.create_memory_object_stream[bytes](max_buffer_size=1)` — zero-copy, bounded at 1 item.
2. `_keepalive_inserter` task: reads from the producer stream, forwards to `send_keepalive` stream; wraps each receive in `anyio.fail_after(_PING_INTERVAL)` (default 15s). On `TimeoutError`, injects `KEEPALIVE_COMMENT = b": ping\n\n"` then resumes waiting. On `anyio.EndOfStream`, exits cleanly.
3. Both tasks run inside `anyio.create_task_group()`. The context manager is entered on the **request-scoped** `AsyncExitStack` (`async_exit_stack.enter_async_context`), not via `asynccontextmanager` directly, so teardown is structured (not via `GeneratorExit` thrown into an async generator). The task group's `cancel_scope.cancel()` is called when `yield receive_keepalive` returns — i.e. after the StreamingResponse body is fully consumed.
4. A checkpoint `await anyio.sleep(0)` is inserted after each item yielded from `_sse_with_checkpoints` — ensures cancellation can be delivered even when the producer is faster than the consumer.
5. Headers set: `Cache-Control: no-cache` + `X-Accel-Buffering: no` (nginx proxy buffering off).
6. `_PING_INTERVAL = 15.0` seconds (in `sse.py:222`).

**Lancelot check hint:** `lancelot/app.py` — Lancelot's SSE path uses a different pump: `@app.stream` with `sse_keepalive=15.0` flag (from Route definition). Verify: (a) `X-Accel-Buffering: no` header is emitted for SSE streams (grep `asgi.py` for "Accel" — currently not found); (b) `Cache-Control: no-cache` is set; (c) the keepalive pump is decoupled from the generator iteration (FastAPI's anyio-based pattern avoids `CancelledError` propagating into the user generator via `fail_after` wrapping the stream receive, not the `__anext__` call — Lancelot should verify the same invariant); (d) the generator teardown happens via structured exit (AsyncExitStack), not `GeneratorExit`.

**Byte-path relevance:** The `X-Accel-Buffering: no` header is a real operator experience win — nginx/HAProxy default buffering breaks SSE (events arrive in batches instead of real-time). FastAPI emits it; Lancelot does not (confirmed by grep). This is a 1-line add with zero hot-path cost. The keepalive architecture decoupling (producer vs keepalive-inserter as separate tasks) avoids a race where `anyio.fail_after` cancels the generator's `__anext__` — Lancelot's stream.py should verify the same.

---

## 4. JSONL streaming — native `AsyncGenerator[T, None]` return type auto-detected as stream
**File:line:** `routing.py:620-651`
**Exact mechanism:** In `APIRoute.__init__`, if `isinstance(response_class, DefaultPlaceholder)` AND `dependant.is_async_gen_callable` (the handler is an `async def` that uses `yield`), FastAPI sets `self.is_json_stream = True`. In `get_request_handler`, this takes a dedicated JSONL path: each item is serialized via `_serialize_data(item) + b"\n"`, and the response is `StreamingResponse(..., media_type="application/jsonl")`. The `stream_item_field` (a `ModelField` built from the return type's item type, e.g. `AsyncGenerator[Item, None]` → `Item`) validates each item via pydantic-core before serializing. The checkpoint `await anyio.sleep(0)` after each chunk allows cancellation.

**Lancelot check hint:** `lancelot/routing.py` — Lancelot has `@app.stream` for explicit streaming but does not appear to auto-detect `AsyncGenerator[T, None]` return type on a plain `@app.get` handler as a JSONL stream. FastAPI's auto-detect uses `get_stream_item_type()` from `dependencies/utils.py` on the return annotation. Check if Lancelot's `compile_dependant` or route init does this inspection. The conformance suite for this feature would be in `conformance/vendor/fastapi/tests/test_json_streaming.py` or similar.

**Byte-path relevance:** AI-shaped use case: a handler `async def get_chunks() -> AsyncGenerator[Chunk, None]: yield Chunk(...)` automatically streams JSONL with per-item validation, no user-side `StreamingResponse` boilerplate. If Lancelot lacks this auto-detect, users must explicitly use `@app.stream` or wrap in `StreamingResponse`. Not a hot-path win but a significant operator experience gap.

---

## 5. Dual AsyncExitStack scoping — `fastapi_inner_astack` (request) vs `fastapi_function_astack` (function)
**File:line:** `routing.py:96-135` (`request_response`), `routing.py:116-121`
**Exact mechanism:** FastAPI's `request_response` wrapper installs TWO AsyncExitStacks in the request scope:
- `scope["fastapi_inner_astack"]` = request-scoped stack: yield-dep teardown runs AFTER the response body is fully sent. Entered first, exits last (after `await response(scope, receive, send)` completes).
- `scope["fastapi_function_astack"]` = function-scoped stack: yield-dep teardown runs BETWEEN handler return and response emit (the `response(...)` call). Entered inside, exits before response is sent.
The outer `ServerErrorMiddleware` also places its own stack via `AsyncExitStackMiddleware` at `scope["fastapi_middleware_astack"]` — for auto-closing files/forms from `request.form()`. These three stacks implement Depends(scope="function") vs Depends(scope="request") semantics, ensuring yield-dep teardown order is spec-compliant even with streaming responses.

**Lancelot check hint:** `lancelot/routing.py` L552: `fstack = _cl.AsyncExitStack()` — single stack. Lancelot has `scope="function"` vs `scope="request"` yield-dep semantics (CLAUDE.md M2 conformance push 3 shipped it). Verify the *dual stack* pattern in Lancelot's `solve_async` — whether it uses the same scope mechanism as FastAPI's `fastapi_inner_astack`/`fastapi_function_astack` scoping or a different implementation.

**Byte-path relevance:** This is primarily a correctness/conformance issue. The dual-stack guarantees that a `scope="function"` yield-dep's `finally` block can still access the not-yet-sent response (useful for: commit on success, rollback on exception, measure response timing). Not a throughput win; already largely shipped by Lancelot (99.6% conformance), but good to verify the exact stack naming is present for any code doing `scope["fastapi_inner_astack"]` introspection.

---

## 6. Endpoint context cache — `_endpoint_context_cache: dict[int, EndpointContext]` at module level
**File:line:** `routing.py:251-274`
**Exact mechanism:** `_endpoint_context_cache` is a module-level `dict[int, EndpointContext]` keyed by `id(func)`. `_extract_endpoint_context(func)` is called once per unique function (first call does `inspect.getsourcefile` + `inspect.getsourcelines` — both are I/O with `stat()` calls), then the `EndpointContext(file=, line=, function=)` is stored and reused. The cache is hit on every subsequent request for the same endpoint. The `EndpointContext` is used only for error messages (`RequestValidationError`, `ResponseValidationError`), not for the happy path — it only matters when there's a validation failure.

**Lancelot check hint:** `lancelot/routing.py` — no `_endpoint_context_cache` found by grep. When Lancelot raises `RequestValidationError`, it doesn't include endpoint location context. This is a developer-experience improvement, not a hot-path win (only fires on errors).

**Byte-path relevance:** DX / operator experience only — helps developers find the broken route faster. Zero hot-path cost (cached after first call). Not a byte-path win.

---

## 7. `DefaultPlaceholder` pattern — distinguishes "not set" from "set to the default value"
**File:line:** `datastructures.py:153-186`
**Exact mechanism:** `DefaultPlaceholder` is a sentinel wrapper. `Default(JSONResponse)` returns `DefaultPlaceholder(JSONResponse)`. `isinstance(x, DefaultPlaceholder)` is used throughout the hot path to decide between:
- "user explicitly set `response_class=JSONResponse`" → re-encode Python dict via `JSONResponse.render`
- "user did NOT set response_class" (still `Default(JSONResponse)`) → use `dump_json` bytes fast path
This is also used for `strict_content_type`, `generate_unique_id_function`, etc. The `__bool__` and `__eq__` on `DefaultPlaceholder` make it transparent in most boolean checks.

**Lancelot check hint:** `lancelot/routing.py` — `Default` and `DefaultPlaceholder` are imported from Lancelot's own `datastructures.py` (checked: it re-exports the same pattern). The `use_dump_json` guard (`isinstance(response_class, DefaultPlaceholder)`) must match exactly — verify `lancelot/asgi.py` uses the same sentinel check before invoking the bytes path vs the encode path.

**Byte-path relevance:** The exact mechanism for the fast-path gate. If Lancelot's sentinel check differs from FastAPI's, users who accidentally pass `response_class=JSONResponse` explicitly would get different behavior (one framework takes fast path, the other doesn't). Pure correctness.

---

## 8. UJSONResponse / ORJSONResponse — deprecated, pydantic-core already faster
**File:line:** `responses.py:27-98`
**Exact mechanism:** Both `UJSONResponse` and `ORJSONResponse` are marked `@deprecated` in FastAPI 0.136.1 with the note "FastAPI now serializes data directly to JSON bytes via Pydantic when a return type or response model is set, which is faster." The deprecation `stacklevel=2` means it fires at import time in the user's code (not at call time). Both classes still work but emit `FastAPIDeprecationWarning`. The deprecation message points users toward the response_model / return type annotation path.

**Lancelot check hint:** `lancelot/responses.py` — Lancelot has pass-through for `ORJSONResponse` / `UJSONResponse` (from CLAUDE.md M2 conformance). If Lancelot has NOT added the `@deprecated` warning, it diverges from FastAPI 0.136.1's documented behavior. Check whether the conformance suite tests for the `FastAPIDeprecationWarning` on these classes.

**Byte-path relevance:** Operator experience only — users importing these classes should get the migration hint. No hot-path effect.

---

## 9. `contextmanager_in_threadpool` — `__exit__` with unlimited CapacityLimiter
**File:line:** `concurrency.py:17-41`
**Exact mechanism:** `contextmanager_in_threadpool` wraps a sync context manager for use in async code. Key detail: `cm.__exit__` is called via `anyio.to_thread.run_sync` with `limiter=CapacityLimiter(1)` — a fresh per-call limiter with no upper bound on wait time. The comment explains: the `__exit__` of a context manager that itself has an internal thread pool (e.g. a DB connection pool) could deadlock if it waits for a free slot from the global anyio thread limiter. The per-call `CapacityLimiter(1)` bypasses the global limit for the exit path only.

**Lancelot check hint:** `lancelot/concurrency.py` — not found (grep returned nothing for `CapacityLimiter`, `contextmanager_in_threadpool`). Lancelot uses `get_pool().offload()` for sync handlers. If Lancelot supports sync context manager dependencies (e.g. `def get_db(): with Session() as s: yield s`), the `__exit__` needs the same unlimited-limiter pattern to avoid deadlock under concurrent load.

**Byte-path relevance:** Real correctness risk under load if Lancelot runs sync yield-deps without the unlimited exit limiter. Not a hot-path win, but a potential deadlock under high concurrency when `anyio`'s default thread pool fills up.

---

## 10. `is_body_allowed_for_status_code` — force `response.body = b""` for 204/205/304/1xx
**File:line:** `routing.py:716-717`
**Exact mechanism:** After serializing the response content, FastAPI checks `if not is_body_allowed_for_status_code(response.status_code): response.body = b""`. This prevents the framework from sending a body for HTTP status codes that must not have one (204 No Content, 205 Reset Content, 304 Not Modified, 1xx informational). The check is called `is_body_allowed_for_status_code` from `fastapi.utils`.

**Lancelot check hint:** `lancelot/asgi.py` — search for `204\|205\|304\|no.body`. This was shipped in M2 conformance push 2 per CLAUDE.md. Verify it applies in the `get_request_handler` equivalent, not just in the Starlette response `__call__`.

**Byte-path relevance:** Correctness / conformance. Already shipped per CLAUDE.md notes (HAVE).

---

## 11. Lifespan merging — `_merge_lifespan_context` for include_router
**File:line:** `routing.py:209-223`, `routing.py:1826`
**Exact mechanism:** When `include_router()` is called, the included router's lifespan is MERGED with the parent router's lifespan using `_merge_lifespan_context`. The merge function runs both lifespans nested (outer → inner), yielding the union of their state dicts. This means every `include_router()` call produces a new merged lifespan — each sub-app's startup/shutdown events run as part of the main app's lifespan, not as isolated events. The `_DefaultLifespan` (for `on_startup`/`on_shutdown` backward-compat) is also wrapped.

**Lancelot check hint:** `lancelot/routing.py` L1822+: `include_router` method. Check if Lancelot properly chains lifespans from included routers. From CLAUDE.md M15 note: "mount lifespans never ran" was a bug found and fixed. Verify that `include_router` with a `lifespan=` kwarg merges correctly into the parent's lifespan (not replacing it).

**Byte-path relevance:** Correctness / operator experience. If sub-router lifespans silently don't run, startup DB connections / warm-up caches are never established. Already noted as fixed in M15 but worth verifying the `_merge_lifespan_context` exact behavior is matched.

---

## Summary table

| # | Mechanism | Lancelot status | Action |
|---|-----------|-----------------|--------|
| 1 | `use_dump_json` bytes fast path | Likely HAVE (TypeAdapter cached) | Verify exact condition gate matches `isinstance(response_class, DefaultPlaceholder)` |
| 2 | Sync endpoint: response validate in threadpool | UNKNOWN | Check ASGI seam for sync endpoints |
| 3 | SSE: `X-Accel-Buffering: no` header | MISSING (confirmed by grep) | 1-line add — high operator value |
| 4 | JSONL auto-detect from `AsyncGenerator[T]` return type | LIKELY MISSING | Check `compile_dependant` / route init |
| 5 | Dual AsyncExitStack scoping | HAVE (conformance 99.6%) | Verify naming/protocol matches |
| 6 | `_endpoint_context_cache` | MISSING | DX only, low priority |
| 7 | `DefaultPlaceholder` gate | HAVE | Verify exact sentinel check in ASGI seam |
| 8 | `ORJSONResponse`/`UJSONResponse` deprecation warnings | UNKNOWN | Check if Lancelot emits `FastAPIDeprecationWarning` |
| 9 | `contextmanager_in_threadpool` unlimited exit limiter | MISSING | Real deadlock risk for sync yield-deps under load |
| 10 | `is_body_allowed_for_status_code` body wipe | HAVE | Already shipped M2 |
| 11 | Lifespan merging on include_router | HAVE (M15 fix) | Verify exact behavior |

### Highest-value actionables
- **SSE `X-Accel-Buffering: no`** — 1 line, zero hot path cost, real operator win (nginx/HAProxy).
- **JSONL auto-detect** (`AsyncGenerator[T]` → JSONL stream) — operator experience; avoids boilerplate.
- **`contextmanager_in_threadpool` unlimited exit limiter** — correctness risk for sync yield-deps under load.
- **`use_dump_json` gate verification** — confirm the pydantic-core bytes fast path is gated correctly.
