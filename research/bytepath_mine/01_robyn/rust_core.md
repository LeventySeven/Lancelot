# Robyn rust_core.md — byte-path mechanism mine

Source: `researchfms/robyn/Robyn/src/` (lib.rs, server.rs, runtime.rs, asyncio.rs,
blocking.rs, callbacks.rs, executors/mod.rs, routers/const_router.rs,
routers/http_router.rs, Cargo.toml). Robyn v0.85.0.

---

## Architecture summary (the closest Lancelot peer)

Robyn is Rust+Python (pyo3 0.27.2), serving via **actix-web 4.4.2** (not hyper
directly), bridging to a Python asyncio loop via **pyo3-async-runtimes 0.27**.
The Rust side owns HTTP accept, routing, request parsing, and response
serialization. Python handlers run on the asyncio loop; sync handlers run on a
dedicated blocking-thread pool. The const-route tier serves prebaked bytes with
zero Python involvement. The core design is the closest architectural mirror to
Lancelot — same pyo3+Rust+asyncio stack, overlapping Granian-derived patterns.

---

## Mechanism 1 — actix-web vs hyper HTTP layer

**File/line:** `server.rs:30-31` (imports), `server.rs:147` (`HttpServer::new`),
`server.rs:278-283` (`.keep_alive(KeepAlive::Os).workers(workers)…`),
`Cargo.toml:22-26` (actix-web 4.4.2, actix-http 3.3.1).

Robyn's HTTP layer is **actix-web** (a full actor-model HTTP server), not
hyper. actix-web sits on top of tokio but adds its own connection management,
keep-alive negotiation, and payload config. Robyn passes `max_payload_size`
via `web::PayloadConfig::new(max_payload_size)` (server.rs:219) and
`.client_request_timeout(Duration::from_secs(0))` (disabling actix's default
header timeout, server.rs:280).

**Lancelot comparison:** Lancelot uses hyper 1 directly (service.rs). This is
a structural win for Lancelot: hyper 1 is lower-level, gives exact control over
connection lifecycle (the `hyper::http1::Builder` path), and avoids actix's
actor overhead. Robyn's `client_request_timeout(0)` means it ships with NO
slow-loris protection at the HTTP layer (contrast: Lancelot ships
`LANCELOT_HEADER_TIMEOUT_MS` defaulting to 15s). This is a real Lancelot
advantage that is already shipped.

**lancelot_check_hint:** `src/service.rs` slow-loris guard (`header_read_timeout`
in `http1::Builder`). Already have and ahead of Robyn here.

---

## Mechanism 2 — Tokio runtime: current_thread per actix worker vs Lancelot's multi_thread

**File/line:** `runtime.rs:138-143` (`default_runtime` builds
`Builder::new_current_thread()`), `server.rs:136` (`thread::spawn(move ||
actix_web::rt::System::new().block_on(…)`), `server.rs:279` (`.workers(workers)`).

Robyn's tokio runtime is `new_current_thread` (single-threaded executor),
one per actix worker. actix-web itself spawns N OS threads (`.workers(N)`),
each running its own single-thread tokio runtime. The "multi-thread" illusion
comes from OS-level thread parallelism via actix, not from tokio's multi_thread.

**Lancelot comparison:** Lancelot uses `Builder::new_multi_thread().worker_threads(
cpu::worker_count())` (lib.rs:131-132) — a single process-wide multi-thread
tokio runtime. Both patterns have GIL consequences: Robyn's per-actix-worker
current_thread isolates the asyncio loop per OS thread (each thread has its own
Python `event_loop`), but Python still serializes through the GIL. Lancelot's
single multi_thread runtime means tokio tasks can migrate across threads but all
still gate on the GIL for Python calls. Neither architecture escapes the GIL
bottleneck. Anti-scaling with threads (from CLAUDE.md) applies to both.

**lancelot_check_hint:** `src/lib.rs:131-132` (`new_multi_thread`). No change
needed — this is a different tradeoff, not an improvement to adopt.

---

## Mechanism 3 — Granian-derived `future_into_py_futlike` / `PyFutureAwaitable` bridge

**File/line:** `runtime.rs:1` ("Portions adapted from Granian"), `runtime.rs:185-204`
(`future_into_py_futlike` on Unix), `callbacks.rs:136-197` (`PyFutureAwaitable`),
`callbacks.rs:241-265` (`add_done_callback` — single-callback optimization,
comment: "asyncio registers at most one done-callback per future"),
`runtime.rs:163-179` (`future_into_py_iter`, comment: "~55% faster than
pyo3_asyncio.future_into_py").

This is the most important byte-path mechanism. Rather than using
`pyo3_async_runtimes::into_future` (which creates a real `asyncio.Future` via
`loop.create_future()` + Python-side bookkeeping), Robyn uses two custom
Rust-native awaitables derived from Granian:

- **`PyIterAwaitable`** (callbacks.rs:86-127): a `#[pyclass(frozen)]` that
  implements `__iter__`/`__next__` directly. When the Rust future completes it
  calls `PyIterAwaitable::set_result` from a blocking thread, storing the result
  in a `OnceLock`. The asyncio loop polls it via `__next__` — when the OnceLock
  is populated it raises `StopIteration(result)` and the coroutine resumes.
  `runtime.rs:163`: claimed "~55% faster than pyo3_asyncio.future_into_py."

- **`PyFutureAwaitable`** (callbacks.rs:136-197, Unix only): mimics
  `asyncio.Future` at the Python protocol level (`_asyncio_future_blocking`
  attribute, `add_done_callback`, `cancel`, `done`, `result`, `exception`)
  entirely in Rust. The Rust future completes → calls
  `event_loop.call_soon_threadsafe(cb, self)` to wake the asyncio loop.
  `runtime.rs:182`: claimed "~38% faster than pyo3_asyncio.future_into_py."
  The `cancel()` path uses `tokio::sync::Notify` (`cancel_tx`) to signal the
  tokio side to drop the future.

- **`PyEmptyAwaitable`** / **`PyDoneAwaitable`** / **`PyErrAwaitable`**
  (callbacks.rs:12-83): zero-alloc awaitables for already-resolved futures
  (already-completed sync calls, errors). `PyEmptyAwaitable` has `freelist = 128`
  (callbacks.rs:12) — pre-allocated pool of 128 instances avoids per-request alloc.

**Lancelot comparison:** Lancelot uses `pyo3_async_runtimes::into_future_with_locals`
(dispatch.rs:60) — the standard bridge. Lancelot does NOT have
`PyFutureAwaitable` / `PyIterAwaitable` / the Granian-derived awaitables. This
is the most significant byte-path gap: the custom awaitables are claimed 38-55%
faster than `pyo3_asyncio.future_into_py` per bridge crossing, and every request
handler coroutine pays this crossing. At Lancelot's scale (~59k tiny req/s) a
38-55% faster bridge could meaningfully shift the tiny one-shot shape closer to
or past raw Starlette+uvloop.

**Caveat/honest check:** The `~55%` / `~38%` claims are Granian's benchmark claims
adopted by Robyn. They apply to the asyncio future bridging overhead in
isolation, not necessarily to end-to-end req/s (which is also bound by GIL
contention, serialization, connection overhead). The benefit is REAL on
coroutine-heavy paths but may be smaller than 55% on the full request. Needs
benching before shipping.

**lancelot_check_hint:** `src/dispatch.rs:60` (`into_future_with_locals`). This
is the one call Lancelot could replace with a `PyIterAwaitable`-style custom
awaitable. Not currently present anywhere in `src/`.

---

## Mechanism 4 — Blocking thread pool for sync handlers (Granian-derived)

**File/line:** `blocking.rs:1` (Granian credit), `blocking.rs:34-61`
(`BlockingRunner` enum: Empty/Mono/Pool), `blocking.rs:84-160`
(`BlockingRunnerPool` — dynamic spawn with 350µs debounce),
`blocking.rs:163-177` (`blocking_worker` / `blocking_worker_idle`),
`runtime.rs:42-82` (`RuntimeWrapper` owns `BlockingRunner`).

Robyn uses a **dedicated blocking thread pool** for all Python callbacks that
need to run with the GIL but outside the async executor. The pool:
- Starts with 1 permanent thread (always live).
- Dynamically spawns up to `max_threads` (default 1 for `RuntimeWrapper`, so
  effectively `BlockingRunnerMono`) via `spawn_thread`, with a 350µs debounce
  (`spawn_tick`; blocking.rs:118) to prevent rapid spawning.
- Idle threads exit after `idle_timeout` seconds (default 30s).
- Workers acquire GIL via `Python::attach` (blocking.rs:164), release it while
  waiting for work via `py.detach(|| queue.recv())` (blocking.rs:165).

The pattern: the tokio future completes on a tokio worker thread, then calls
`rt.spawn_blocking(move |py| PyIterAwaitable::set_result(…))` (runtime.rs:177).
This crossing is OFF the tokio worker thread (doesn't block tokio) and OFF the
asyncio loop (doesn't block Python), but DOES acquire the GIL briefly to store
the result. This is the correct pattern for the result-callback side of async
Python/Rust bridging.

**Lancelot comparison:** Lancelot has no separate blocking runner. Its dispatch
uses `crate::gil::with_gil(|py| …)` (dispatch.rs:54, 73) — which acquires the
GIL directly. For the callback/completion side, Lancelot doesn't need a separate
pool because `into_future_with_locals` handles this internally. If Lancelot were
to adopt `PyIterAwaitable`-style bridging it would also need the `BlockingRunner`
pattern to call `set_result` off the tokio thread.

**lancelot_check_hint:** `src/gil.rs` (the `with_gil` wrapper). Not needed as a
standalone mechanism unless adopting the custom awaitable bridge (Mechanism 3).

---

## Mechanism 5 — Per-request ContextVar isolation via `new_context` + `create_task(context=ctx)`

**File/line:** `asyncio.rs:10-48` (the three `PyOnceLock` statics:
`CONTEXTVARS`, `CONTEXT`, `RUN_IN_CONTEXT`), `asyncio.rs:47-49`
(`new_context` — fresh `contextvars.Context()` per request),
`asyncio.rs:63-85` (`run_in_context_helper` — Python 3.11+ uses
`loop.create_task(coro, context=ctx)` so the user coroutine's ContextVar writes
land in the shared per-request context; 3.10 fallback does plain `await coro`),
`server.rs:515-522` (allocates `new_context` per request, passed as `ctx_ref`),
`executors/mod.rs:260-265` (`wrap_coro_in_context` — calls `_run_in_context(coro, ctx)`),
`asyncio.rs:33-37` (`copy_context` — `pyo3::ffi::PyContext_CopyCurrent()` via unsafe).

Robyn allocates one fresh `contextvars.Context()` per request lifecycle. All
phases (before-middleware, handler, after-middleware) share the SAME `ctx`
object. Async handlers are scheduled via `loop.create_task(coro, context=ctx)`
(Python 3.11+), which tells asyncio to use the given context object directly
(not copy it), so `ContextVar` writes in one phase are visible to the next. Sync
handlers use `ctx.run(fn, *args)`.

Note: `copy_context` (`asyncio.rs:33-37`) is also present but used for the
initial `TaskLocals` setup (server.rs does NOT call it per-request; it calls
`new_context` instead).

**Lancelot comparison:** Lancelot's `lancelot.context` module has ContextVars
(CLAUDE.md: `cost_usd`, `tokens_in`, `tokens_out`, `agent_id`, `tenant_id`,
`queue_wait_ms`) and propagates them through the ASGI seam. The per-request
shared-context pattern (one `Context()` across middleware phases) is for Robyn's
own middleware model. Lancelot's ASGI-based middleware doesn't need this — ASGI
has its own per-request scope dict. Not a gap to fill.

**lancelot_check_hint:** `lancelot/context.py` and the ASGI seam in
`lancelot/_native.py`. Already handled by the ASGI model.

---

## Mechanism 6 — ConstRouter: dual HashMap+matchit fast path + header baking

**File/line:** `routers/const_router.rs:1-220`.

Key details:
- **Dual lookup** (const_router.rs:79, 203-219): exact path lookups go into a
  `HashMap<String, CachedResponse>` (`FastMap`) behind a `parking_lot::RwLock`.
  Parameterized const routes fall back to a `matchit::Router<CachedResponse>`.
  `get_cached_route` tries HashMap first, then matchit (const_router.rs:203-219).
- **`bake_global_headers`** (const_router.rs:172-199): called once at startup
  (server.rs:144). Global response headers are appended to EVERY cached response's
  pre-serialized header vec. After baking, each `CachedResponse.headers` is an
  `Arc<Vec<(String, String)>>` that includes global headers — no per-request
  merge needed.
- **Middleware guard** (server.rs:233): the Rust-level const fast path is only
  taken when `!middleware_router.has_any_middleware()` (checked via an
  `AtomicBool`). If any middleware is registered, ALL requests go through the
  Python path (even const routes). This is the same design as Lancelot's
  `Prebaked` tier.
- **CachedResponse.route_header_count** (const_router.rs:29): tracks how many
  headers belong to the route (vs global). Used by
  `to_http_response_without_global_headers` for excluded-path responses. This is
  a subtle detail — the baked Vec is `route_headers + global_headers` and
  `route_header_count` lets us serve only the route portion.
- **Execution at registration** (const_router.rs:106-124): `add_route` runs the
  Python handler ONCE via `execute_http_function(&Request::default(), …)` to
  capture its response body. Streaming responses are rejected at registration
  time (`PyRuntimeError`).

**Lancelot comparison:** Lancelot's `Prebaked` tier (service.rs:135, CLAUDE.md:
183k req/s) takes `(method, path, body, content_type, status)` as raw bytes from
Python-side registration — no handler execution at startup. Lancelot does NOT
have `bake_global_headers` (Robyn bakes global headers into each cached response
at startup; Lancelot would need to handle global headers per-request for const
routes). The dual HashMap+matchit pattern in Robyn is the same as Lancelot using
`matchit` for regular routes, but Robyn explicitly adds the HashMap fast path for
exact const routes. Lancelot's existing `Prebaked` paths are purely exact (no
param support), which matches Robyn's HashMap path.

The `route_header_count` pattern (serving const routes without global headers for
excluded paths) is a minor operator convenience Lancelot doesn't need to match.

**lancelot_check_hint:** `src/service.rs:264` (const-bytes fast path / `Prebaked`
slot check). Already have the essential tier. `bake_global_headers` is the one
novel pattern not currently in Lancelot.

---

## Mechanism 7 — Response extraction fast path (downcast_exact + orjson inline)

**File/line:** `executors/mod.rs:78-153` (`extract_response_type_fast`),
`executors/mod.rs:26-34` (`ORJSON_DUMPS` PyOnceLock — cached orjson.dumps),
`executors/mod.rs:12` (comment: "0-param handlers: skip Request→Python conversion").

`extract_response_type_fast` tries five paths in order:
1. `output.downcast_exact::<PyResponse>()` — zero getattr calls, reads Rust struct fields directly (NOT `downcast` — user subclasses fall through).
2. `output.downcast::<PyDict>() || downcast::<PyList>()` — passes to `orjson.dumps` inline in Rust (the `ORJSON_DUMPS` PyOnceLock stores the callable once).
3. `downcast::<PyString>()` / `downcast::<PyBytes>()` — wraps in `Response` with preset headers.
4. `extract::<StreamingResponse>()` — duck-typed streaming.
5. `extract::<Response>()` — slow-path fallback.

Also: `get_function_output` (mod.rs:156-198) short-circuits for 0-param handlers
(`function.number_of_params == 0` → `handler.call0()`, skipping request arg
conversion).

**Lancelot comparison:** Lancelot's dispatch builds a `Request` pyclass and calls
`handler.bind(py).call1((req,))` then calls `to_json_bytes` for serialization
(dispatch.rs). There is no multi-tier `downcast_exact`/`downcast::<PyDict>` fast
path in Lancelot's Rust response extraction — Lancelot goes directly to
pydantic-core `to_json`. The `orjson` fallback is in Lancelot's serialize.rs but
at the serialization stage, not the type-dispatch stage. The Robyn pattern is
more relevant for a framework that needs to handle bare `dict`/`list`/`str`/`bytes`
return types; Lancelot routes all returns through pydantic-core which is already
fast for models.

**lancelot_check_hint:** `src/dispatch.rs` (`call_async` / `build_result`). No
clear improvement for Lancelot since pydantic-core handles the common case.

---

## Mechanism 8 — `PyEmptyAwaitable` freelist (pre-alloc pool)

**File/line:** `callbacks.rs:12` (`#[pyclass(frozen, freelist = 128, …)]`).

`PyEmptyAwaitable` uses pyo3's `freelist = 128` — a recycling allocator that
keeps 128 instances in a free list. Every sync-handler that returns an empty
awaitable (startup handlers, zero-arg handlers) reuses a pre-allocated instance
instead of calling the allocator.

**Lancelot comparison:** Lancelot does not use `freelist` on any pyclass. The
opportunity is narrow (only helps for very hot zero-arg-result paths) but trivial
to add. Real byte-path benefit depends on how many zero-result awaitables are
allocated per second.

**lancelot_check_hint:** `src/ffi/` pyclass definitions. Worth considering on
any `#[pyclass]` that is allocated per-request on the hot path.

---

## Mechanism 9 — Server start guard via `AtomicBool::compare_exchange`

**File/line:** `server.rs:42` (`static STARTED: AtomicBool = AtomicBool::new(false)`),
`server.rs:94-99` (`compare_exchange(false, true, SeqCst, Relaxed)` returns
early if already started).

Robyn prevents duplicate server starts via a process-global `AtomicBool`. The
`start()` method is idempotent at the Rust level.

**Lancelot comparison:** Lancelot uses `Python::with_gil` → `run()` call
initiated from Python; no equivalent AtomicBool guard. Not a byte-path concern
but a correctness/operator-experience note.

---

## Mechanism 10 — Robyn has NO graceful drain / SIGTERM handling

**File/line:** `server.rs:289-311` (shutdown path: `event_loop.run_forever()` →
on error, run `shutdown_handler` → `exit(0)`).

Robyn's shutdown is process-exit (`exit(0)`) after calling the user's shutdown
handler. There is no drain of in-flight connections, no SIGTERM→drain→close
sequence. Any in-flight request at shutdown time is hard-killed.

**Lancelot comparison:** Lancelot ships `LANCELOT_DRAIN_SECS` + `initiate_shutdown()`
+ `graceful_break` (`acquire_many_owned(max_conns)`) — a genuine drain that waits
for all in-flight connections (CLAUDE.md: M4 found the original implementation
broken, fixed it). This is a real Lancelot win over Robyn. Not a gap to fill;
already ahead.

---

## Mechanism 11 — Release profile: codegen-units=1, fat LTO, panic=abort, strip=true

**File/line:** `Cargo.toml:44-53`.

```toml
[profile.release]
codegen-units = 1
lto = "fat"
panic = "abort"
strip = true
opt-level = 3
```

**Lancelot comparison:** Check Lancelot's `Cargo.toml`. `codegen-units=1` + fat LTO
maximizes inlining across the whole crate. `panic=abort` removes unwind tables
(binary size + branch-removal). `strip=true` removes debug symbols from the
`.so`. Worth verifying Lancelot has equivalent settings.

**lancelot_check_hint:** `/Users/seventyleven/Desktop/Lancelot/Cargo.toml` — check
`[profile.release]` section.

---

## Mechanism 12 — parking_lot RwLock on route tables

**File/line:** `routers/const_router.rs:75-79` (`type RouteMap = RwLock<MatchItRouter<CachedResponse>>`
and `type FastMap = RwLock<HashMap<…>>`), `routers/http_router.rs:13`
(`type RouteMap = RwLock<MatchItRouter<FunctionInfo>>`). Both use `parking_lot::RwLock`.

parking_lot's `RwLock` is ~2-3× faster than `std::sync::RwLock` on contended
paths (smaller per-lock overhead, no kernel calls on the uncontended path). For
route tables that are read-locked on every request and write-locked only at
startup/hot-reload, parking_lot is the right choice.

**Lancelot comparison:** Lancelot's router (`src/router.rs`) uses matchit directly
at build time (no runtime RwLock on the hot path — the router is built once and
passed as an immutable `Arc`). This is actually superior: zero lock on the hot
path. Not a gap; Lancelot is ahead.

**lancelot_check_hint:** `src/router.rs` — verify the router is fully immutable
once built (no runtime insert path). Already ahead.

---

## Summary table — gaps vs. already-have

| # | Mechanism | Lancelot status | Priority |
|---|-----------|-----------------|----------|
| 3 | Granian custom awaitables (`PyFutureAwaitable`/`PyIterAwaitable`, 38-55% faster bridge) | **GAP** — not in src/ | High (bench first) |
| 4 | `BlockingRunner` for result-callback off tokio thread | Prerequisite for #3 | With #3 |
| 8 | `freelist=128` on hot pyclass | **GAP** — trivial | Low |
| 6b | `bake_global_headers` into prebaked response at startup | **GAP** — minor | Low |
| 11 | `codegen-units=1`/fat-LTO/panic=abort/strip in release profile | **CHECK** | Low |
| 1 | actix-web HTTP layer | N/A — Lancelot uses hyper 1 (already better) | — |
| 2 | current_thread per worker | N/A — Lancelot's multi_thread is different tradeoff | — |
| 5 | Per-request context via `new_context` | N/A — Lancelot's ASGI model covers this | — |
| 7 | `extract_response_type_fast` downcast tiers | N/A — pydantic-core covers Lancelot's case | — |
| 9 | Start guard AtomicBool | Correctness, not byte-path | — |
| 10 | NO graceful drain | Lancelot AHEAD | — |
| 12 | parking_lot on route tables | Lancelot's router is lock-free post-build; AHEAD | — |
