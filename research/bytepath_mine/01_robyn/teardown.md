# Robyn Byte-Path Teardown — Additive Mine for Lancelot

> Source: `researchfms/teardowns/ROBYN.md` (187 KB, Robyn v0.85.0 @ `d19b67f`) plus prior Lancelot
> deep-mine passes in `research/robyn/01..03` and `research/deep/04..05`.
> **Additive mandate:** HAVE tags mark items Lancelot already built; GAP tags mark what is new or
> improvable. Every claim cites peer `file:line`. Quality bar: exact knob / constant / file / fn.

---

## Prior mine coverage (what is NOT new)

The three prior Lancelot Robyn passes + deep/04-05 exhaustively covered:

- GIL choreography: `Python::with_gil` scopes (#0/#1/#2/#3), drop-GIL-across-await in
  `executors/mod.rs:519-545`. → **HAVE** (Lancelot `dispatch.rs` implements exactly this discipline,
  plus `catch_unwind` that Robyn lacks).
- Custom awaitables (5 types, 38–55% faster than `pyo3-async-runtimes`) in `callbacks.rs:12-375`.
  → Lancelot uses `pyo3-async-runtimes` stdlib path; the custom-awaitable opportunity is noted but
  not yet built.
- Granian-derived blocking pool (`blocking.rs:34-177`): `Empty/Mono/Pool`, `py.detach()` on
  `queue.recv()`, 350 µs spawn throttle, 30 s idle timeout, `crossbeam-channel::unbounded()`.
  → **HAVE** awareness; Lancelot is pure-async (no sync handlers dispatched to a pool yet).
- `SO_REUSEPORT` + backlog 16384 + `TCP_NODELAY` in `shared_socket.rs:23-29`. → **HAVE** (Lancelot
  `src/socket.rs`, same primitives via `socket2`).
- `matchit` radix-tree routing per HTTP method (`http_router.rs`). → **HAVE** (`matchit` in Lancelot
  `service.rs`).
- Const-tier prebaked bytes fast path (`const_router.rs:87-127`, `server.rs:233-251`). → **HAVE**
  (`service::Prebaked`, `lib.rs:55-93`; ZERO GIL, ZERO alloc for const slots).
- Per-request `contextvars.Context` isolation fix (`asyncio.rs:32-85`, issue #1380). → **HAVE**
  (ContextVar seam in Lancelot, with request_id isolation).
- Streaming pump weakness: per-chunk `spawn_blocking`+`with_gil`+`call_method0("__next__")`
  in `response.rs:123-148`. → **HAVE fixed** (Lancelot R3 pump: `_drain` on-loop coroutine,
  bounded-mpsc, single bridge crossing per stream).
- `FunctionInfo` precomputed `number_of_params: u8` + `is_async: bool` at registration —
  no per-request `inspect`. → **HAVE** (Lancelot `app.py` captures handler metadata at registration).
- `panic="abort"` + zero-`.unwrap()` discipline + `catch_unwind` at the FFI seam. → **HAVE** (Lancelot
  adds `catch_unwind` that Robyn lacks; `dispatch.rs:R7`).
- `TaskLocals` OnceCell + `scope_local` asyncio↔tokio binding. → **HAVE** (Lancelot uses
  `pyo3-async-runtimes` equivalently).
- `ORJSON_DUMPS` `PyOnceLock` cached once, reused for bare-dict/list fast path
  (`executors/mod.rs:26-34`). → **HAVE** (Lancelot has `intern!` for pydantic-core symbols;
  pydantic-core `to_json` replaces orjson for serialization).
- Multi-process/worker scaling math: `processes=(cpu*2)+1, workers=2` in `--fast` mode
  (`argument_parser.py:103-109`). → **HAVE** (`LANCELOT_WORKERS`).
- Build profile: `lto="fat", codegen-units=1, opt-level=3, panic="abort", strip=true`. → **HAVE**.

---

## Mechanism-level findings — NEW / IMPROVABLE (additive)

### M1 — CORS (any global middleware) silently kills the const-tier fast path

**Mechanism:** `middleware_router.rs:17` — `has_middleware: AtomicBool` is set `Release` by ANY call
to `add_global_middleware` or `add_middleware_route`. The const fast path in `server.rs:233` is gated
exclusively on `!middleware_router.has_any_middleware()` (read `Acquire`). So adding `ALLOW_CORS(app,
...)` or any global before/after middleware — including Robyn's own convenience helpers — **flips
this bool and removes the const-tier bypass for every route in the process** (`robyn/__init__.py:28`
`ALLOW_CORS` registers a global `@app.before_request`). The `global_header baking` in
`const_router.rs:172` only runs if `!has_any_middleware()` too (`server.rs:144-146`). With middleware
present, const routes still hit the const cache inside `index()` (step 7, `server.rs:561-573`), but
they now pay GIL #0 (TaskLocals clone), GIL #1 (Context alloc), and the full before/after middleware
chain per request. → **For an AI API with CORS enabled, the 183k-req/s const tier collapses to the
~59k dynamic path.**

**Lancelot check hint:** `lancelot/middleware.py:211` `CORSMiddleware`. Check whether adding it as an
ASGI middleware (Python side) vs a Rust-level gate has a different impact on const-route latency. The
ASGI path runs Python before the Rust prebaked check — same problem exists. Audit: does `Prebaked`
short-circuit before the ASGI middleware layer?

**Lancelot relevance (byte path):** Any operator using `CORSMiddleware` for an AI API that relies on
const-tier health probes under load will lose the Rust-bypass benefit silently. Document this cliff
explicitly; offer a route-scoped CORS decorator that adds CORS headers only on non-const routes.

**Cite:** `server.rs:233` (fast-path gate), `middleware_router.rs:17` (AtomicBool), `robyn/__init__.py`
`ALLOW_CORS` (global before-request registration), `const_router.rs:172` (bake_global_headers gate).

---

### M2 — Const router trailing-slash normalization gap (double-path bug)

**Mechanism:** Robyn normalizes the trailing slash **inside `from_actix_request`** at `request.rs:166-172`:
```rust
if path.ends_with("/") && path.len() > 1 { path = &path[..path.len()-1]; }
```
But the const fast path in `server.rs:238` uses `req.uri().path()` **before `from_actix_request`**,
so the const cache is keyed on the un-normalized path. A `GET /health/` misses the const cache for a
route registered as `/health` even though the dynamic path (`index()` → `from_actix_request`) would
match. The dynamic path hits the const cache at step 7 (`server.rs:561-573`) via the normalized path,
so it still serves from cache — but only after paying GIL #0 + GIL #1 + middleware chain.

**Lancelot check hint:** `src/service.rs` — the matchit route lookup and Prebaked check. Does
Lancelot normalize paths before the Prebaked slot lookup? `service.rs:264-270` — the const slot
check is done against the matchit-matched `slot`, not a raw path string. If matchit matches
`/health/` to the same slot as `/health`, the Prebaked check works. Verify whether matchit `0.8.x`
(Lancelot) matches trailing slashes identically to `0.7.3` (Robyn).

**Lancelot relevance:** Inconsistent matchit behavior across versions on trailing slashes could cause
`/health/` to 404 instead of hitting the const tier. Add a test: `GET /livez/` (trailing slash)
must return 200 and come from the const tier.

**Cite:** `request.rs:166-172` (strip trailing slash), `server.rs:233-251` (fast path uses raw path),
`server.rs:561-573` (const hit via normalized path in index()).

---

### M3 — `FunctionInfo::Clone` acquires GIL on every dynamic route lookup

**Mechanism:** `function_info.rs:62-72`:
```rust
impl Clone for FunctionInfo {
    fn clone(&self) -> Self {
        Python::with_gil(|py| Self {
            handler: self.handler.clone_ref(py), args: self.args.clone_ref(py),
            kwargs: self.kwargs.clone_ref(py), ...
        })
    }
}
```
`http_router.rs:48-53` — every `get_route` does `Python::with_gil(|_| res.value.to_owned())` to clone
the `FunctionInfo`. This is a **hidden GIL acquisition on the routing hot path**, before dispatch.
For the const tier, `CachedResponse` clone is a pure-Rust `Arc`/`Bytes` bump — no GIL. Robyn pays
one extra GIL acquisition per dynamic request for this.

**Lancelot check hint:** `service.rs:275-276` — Lancelot does `crate::gil::with_gil(|py| h.clone_ref(py))`.
Same pattern. The handler is stored as `Py<PyAny>` and cloned per request under a GIL. This is
unavoidable with `Py<PyAny>` but can be made cheaper by storing the handler in an `Arc<PyAny>`-like
structure that never needs a clone (just an Arc bump). OR: cache the `Py<PyAny>` in a `PyOnceLock`
keyed by route id so it's resolved once per process, not cloned per request. The real fix (Lancelot
already has the right shape) is to pre-index handlers into a `Vec<Option<Py<PyAny>>>` at startup and
use `Vec::get(slot)` — exactly `app.handlers.get(slot)` in `service.rs:275`. The per-request GIL cost
here is a single `clone_ref` = a Python refcount bump under the GIL, which is ~50–100 ns. Not the
bottleneck but worth noting as the only remaining GIL touch before the handler call.

**Cite:** `function_info.rs:62-72` (Clone impl), `http_router.rs:48-53` (get_route GIL clone),
`service.rs:275-276` (Lancelot equivalent).

---

### M4 — Granian-derived custom awaitables: 38–55% bridge cost reduction (not yet built in Lancelot)

**Mechanism:** `callbacks.rs:12-375` — 5 hand-rolled `#[pyclass(frozen, module="robyn._robyn")]`
awaitables replacing `pyo3_async_runtimes::tokio::future_into_py`:

- `PyEmptyAwaitable` (`:12`): `freelist=128`; `__next__` → `None` immediately (fire-and-forget).
- `PyDoneAwaitable` (`:30`): already-resolved; `__next__` raises `StopIteration(value)`.
- `PyErrAwaitable` (`:59`): already-failed; `__next__` propagates stored `PyErr`.
- `PyIterAwaitable` (`:85`): `OnceLock<PyResult>`; `__next__` returns `Some(None)` while pending
  (busy-yield), raises `StopIteration(value)` when resolved. No Python `Future` object, no
  `add_done_callback`, no `call_soon_threadsafe`. ~55% faster than the library path. Used by
  `future_into_py_iter` (`runtime.rs:165-180`).
- `PyFutureAwaitable` (`:129`): full asyncio-future protocol (`_asyncio_future_blocking` attr,
  `add_done_callback` single-slot optimization, `cancel` → `cancel_tx.notify_one()` → `biased
  select!{result | cancel}` in `runtime.rs:185-204`). ~38% faster. Used for real I/O waits
  with cancellation.

**Resolution path** for both: result marshalled on a `BlockingRunner.spawn_blocking` thread (GIL
acquired there, not on the tokio reactor thread) — reactor never blocks on marshalling.

**Lancelot check hint:** `dispatch.rs` — Lancelot uses `pyo3_async_runtimes::tokio::into_future`.
Custom awaitables are NOT yet built. The 38–55% gap is on the bridge cost per handler return, which
is measurable at very high RPS short handlers (the Blazeio-comparison regime). For AI workloads
dominated by LLM latency (800ms+), the bridge cost is amortized away. For embeddings, RAG-cache hits,
and `const_tier`-adjacent fast paths where the handler itself is ~10 µs, 38–55% bridge reduction is
the remaining win. Build `PyIterAwaitable` first (no cancellation needed for the common case, simpler,
55% win).

**Key impl detail:** `frozen` attribute removes per-instance `RefCell` borrow-check overhead from pyo3
(direct field access, no runtime borrow check). `freelist=128` on `PyEmptyAwaitable` = pool of 128
pre-allocated instances, zero alloc under normal churn. `biased select!` checks result before cancel
— the common path (result arrives before cancel) never waits.

**Cite:** `callbacks.rs:12,30,59,85,129` (class defs), `runtime.rs:163,182` (verbatim "~55%/~38%
faster" comments), `runtime.rs:165-204` (dispatch helpers), `blocking.rs:163-177` (worker GIL detach).

---

### M5 — Dedicated blocking-thread pool for sync handlers (absent in Lancelot)

**Mechanism:** `blocking.rs:34-177` — `BlockingRunner { Empty | Mono(1 thread) | Pool(N threads) }`.
The pool's key insight is `blocking_worker`:
```rust
Python::attach(|py| {
    while let Ok(task) = py.detach(|| queue.recv()) {  // GIL released while waiting
        task.run(py);                                   // GIL held while running sync Python
    }
});
```
`py.detach()` releases the GIL while blocked on `crossbeam::channel::recv()`. Without this, an idle
pool thread parks holding the GIL — a process-wide freeze. `BlockingTask` = `Box<dyn FnOnce(Python)
+ Send>` (the unit of work is "Python code needing the GIL"). Pool growth: `queue.len() > 1 &&
threads < tmax` → `spawn_thread()` throttled at ≤1 spawn per 350 µs via `spawn_tick` AtomicU64 +
single-flight `spawning: AtomicBool`. Grown threads are `blocking_worker_idle` (same but `recv_timeout`
→ die after `idle_timeout` seconds). Default: `Mono(1, 30s)` (`runtime.rs:285`). **The queue is
`crossbeam::unbounded()`** — a real production risk: sustained sync-handler overload → unbounded
queue growth → memory blowup.

**Lancelot check hint:** Lancelot is ASGI-based and runs sync handlers via Starlette's anyio
`run_in_threadpool` (shared thread pool). A dedicated, GIL-aware, isolated pool — NOT tokio's shared
`spawn_blocking` (shared with fs/DNS) — is the Granian/Robyn insight. Lancelot does not have this.
For AI apps with sync SDK handlers (sync `openai.OpenAI()`, `anthropic.Anthropic()`), the shared pool
mixes slow sync Python with file I/O. **Use a bounded crossbeam channel + rate-limited adaptive pool
(port `blocking.rs` mechanics) as a dedicated sync-handler pool.** Make the queue bounded (e.g.
`LANCELOT_SYNC_QUEUE_MAX` default 10000) to shed load (503) under sync overload instead of OOM.

**Cite:** `blocking.rs:34-177` (full pool), `blocking.rs:163-177` (GIL detach on recv), `blocking.rs:118`
(350 µs spawn throttle), `runtime.rs:285` (default Mono/1/30s).

---

### M6 — `ROBYN_MAX_PAYLOAD_SIZE` env knob controls BOTH body cap AND WebSocket frame cap

**Mechanism:** `server.rs:39-40` — `max_payload_size = std::env::var("ROBYN_MAX_PAYLOAD_SIZE")
.unwrap_or("1000000").parse::<usize>().map_err(|_| PyValueError)?` — default 1 MB, raises
`PyValueError` on bad parse. Applied to BOTH `web::PayloadConfig::new(max_payload_size)` (HTTP body,
`server.rs:308`) AND `max_frame_size=max_payload_size` for WebSocket frames (`websockets/mod.rs:65`).
One env var controls both caps, which is a single-knob simplicity tradeoff.

**Lancelot check hint:** Lancelot has `LANCELOT_MAX_BODY` for HTTP body cap (`service.rs:34-40`,
default 16 MiB). WebSocket frame cap is separate (Starlette's WS). Consider adding explicit docs
that `LANCELOT_MAX_BODY` does NOT automatically apply to WS frames — the Robyn coupling is implicit
and can surprise operators deploying AI apps that receive large tool-call payloads over WS.

**Cite:** `server.rs:39-40` (env parse), `server.rs:308` (`PayloadConfig`), `websockets/mod.rs:65`
(frame_size=max_payload_size).

---

### M7 — `client_request_timeout=0` (disabled) — explicit slowloris exposure

**Mechanism:** `server.rs:280` — `.client_request_timeout(Duration::from_secs(0))` hardcoded in the
`HttpServer` builder. Robyn docs note `client_timeout=30, keep_alive_timeout=20` as `Robyn.start()`
params but the Rust `Server::start` signature is `(socket, workers)` only at this HEAD — those two
Python params are **plumbed but not applied** (`.pyi` stub drifted from `server.rs`). Comment in the
teardown (`robyn/ROBYN.md §4`): `client_timeout`/`keep_alive_timeout` are "INFERRED divergence
between .pyi stub and server.rs." So Robyn disables the header-read timeout entirely in production,
relying on `backlog=16384` + OS limits + reverse proxy to guard against slow-loris.

**Lancelot check hint:** Lancelot HAS `LANCELOT_HEADER_TIMEOUT_MS` (`service.rs:121-127`, default
15000ms). This is a real advantage over Robyn — Robyn's zero-timeout is a known slowloris gap.
**HAVE and ahead of Robyn on this axis.** Document the edge in the positioning ("Lancelot ships
slow-loris protection out of the box; Robyn disables it").

**Cite:** `server.rs:280` (zero timeout), `robyn/ROBYN.md §4` note on stub drift, `robyn.pyi:517`
(stub shows 4 params), `processpool.py:220` (only 2 args passed to `server.start`).

---

### M8 — `io-uring` opt-in feature flag (not present in Lancelot)

**Mechanism:** `Cargo.toml:53` — `[features] io-uring = ["actix-web/experimental-io-uring"]`. Default
OFF. Only relevant on Linux kernel ≥5.1. Robyn exposes this as a cargo feature, not a runtime env
knob. The teardown notes: "OFF by default; replica exposes the same opt-in feature flag; relevant for
very high-RPS AI gateways on modern Linux" (`ROBYN.md §35`). actix-web's io-uring integration
bypasses epoll for I/O submission — reduces syscall overhead at very high connection rates.

**Lancelot check hint:** Lancelot uses hyper 1.x + tokio, not actix. The equivalent would be
`tokio-uring` (a separate crate), which is NOT part of tokio's standard multi-thread runtime. The
integration cost is high (requires `tokio-uring`'s own runtime, incompatible with standard tokio).
**Assess as Tier-D unless benchmarks on an AI gateway show epoll overhead is measurable.** The 183k
const-tier perf + uvloop embedded loop already beat Robyn without io-uring. Skip for now.

**Cite:** `Cargo.toml:53` (feature flag), `ROBYN.md §35` (table note).

---

### M9 — `bake_global_headers` startup optimization for const routes

**Mechanism:** `const_router.rs:172-199` (`bake_global_headers`): called at `server.rs:144` ONLY if
`!has_any_middleware()`. Iterates ALL `CachedResponse` entries in `fast_routes`, appends global
response headers into the existing `Arc<Vec<(String,String)>>` (rebuilds a new Arc with the merged
list), stores `route_header_count` (the original per-route header count) separately. This enables
`to_http_response_without_global_headers()` to serve `/docs`/`/openapi.json` without global headers
(the `excluded_response_headers_paths` path). At request time the baked const hit is: one `Arc<Vec>`
clone (refcount bump) + one loop over the pre-flattened pairs building a `HttpResponseBuilder` — no
`DashMap` lookup, no `set_missing` call, no Python.

**Lancelot check hint:** Lancelot's `Prebaked` (`service.rs:135-160`) pre-bakes status + body but
NOT global response headers — those come from the ASGI middleware layer (set in Python). A const-route
hit goes through `Prebaked::response()` which returns a bare hyper response without framework headers.
If global response headers (e.g. `X-Request-ID`, metrics headers) need to be on const-tier responses
too, they must be added at the Prebaked build time. Robyn's `bake_global_headers` is the right model.
**Potential improvement:** accept an optional list of (name, value) pairs at `Prebaked::new()` to bake
global response headers in at startup, avoiding any per-request header work for const routes.

**Cite:** `const_router.rs:172-199` (bake_global_headers), `server.rs:144-146` (gated by !middleware),
`const_router.rs:25-30` (CachedResponse struct with route_header_count).

---

### M10 — SSE anti-buffering headers: exact set + double-set bug in Robyn (Lancelot is cleaner)

**Mechanism:** Robyn sets SSE headers at **two sites** with inconsistent values:

1. `response.rs:107-117` (`.respond_to`): `Connection: keep-alive`, `X-Accel-Buffering: no`,
   `Cache-Control: no-cache, no-store, must-revalidate`, `Pragma: no-cache`, `Expires: 0`.
2. `response.rs:427-460` (`FromPyObject for StreamingResponse`): conditionally (only if absent)
   `Cache-Control: no-cache` and `Connection: keep-alive`.

So if both paths run, `Cache-Control` can become `"no-cache, no-cache, no-store, must-revalidate"` (the
`"no-cache"` from the FromPyObject path appends before the respond_to path's value). This is a
documented double-header bug: `test_sse.py:19` accepts either form. The teardown notes: "two header-set
sites with different values" (`deep/04 §3.2`).

**Lancelot check hint:** `lancelot/asgi.py:1041-1042` — Lancelot sets `cache-control: no-cache` and
`x-accel-buffering: no` but does NOT set `Pragma: no-cache`, `Expires: 0`, or `Connection: keep-alive`.
These are relevant for nginx ≤1.10 / older proxies. **`Pragma: no-cache` and `Expires: 0` are
HTTP/1.0 remnants** but Robyn's explicit inclusion means some operators may rely on them. Consider
documenting the full Robyn set vs. Lancelot's subset, and adding `Connection: keep-alive` to be safe
(HTTP/1.1 keep-alive is on by default but some proxies override). The lack of double-set is a Lancelot
advantage; the missing headers are a minor gap.

**Cite:** `response.rs:107-117` (respond_to headers), `response.rs:427-460` (FromPyObject conditional
headers), `asgi.py:1041-1042` (Lancelot's subset).

---

### M11 — `AsyncGeneratorWrapper` sync-loop-per-chunk bridge (latent perf cliff if user uses it)

**Mechanism:** `robyn/responses.py` `AsyncGeneratorWrapper` — wraps an async generator for Robyn's
sync-`__next__`-based stream pump. Per `__next__`:
```python
def __next__(self):
    loop = asyncio.get_event_loop()
    try: return loop.run_until_complete(self._iterator.__anext__())
    except StopAsyncIteration: raise StopIteration
```
This runs the entire asyncio event loop `run_until_complete` **per chunk**, meaning each token of an
LLM stream: creates a new `run_until_complete` task, blocks the event loop while waiting, returns.
Combined with the Rust `create_python_stream` per-chunk `spawn_blocking` + `with_gil`, an async-gen
LLM stream path pays: one `run_until_complete` per token + one `spawn_blocking` hop + one GIL acquire.

**Lancelot check hint:** Lancelot's R3 pump (`stream.rs`) completely avoids this: the `_drain`
coroutine runs ON the asyncio loop natively (single bridge crossing), `async for chunk in gen` drives
the generator, each chunk is a `sink.try_push` via bounded-mpsc. **HAVE and ahead.** But if any user
accidentally passes a sync generator to `@app.stream`, does Lancelot handle it gracefully or silently
produce empty responses? Audit the `stream.py` code path for the sync-gen edge case.

**Cite:** `responses.py` `AsyncGeneratorWrapper` (the pattern), `stream.rs:109-` (Lancelot's on-loop
_drain approach).

---

### M12 — `KeepAlive::Os` delegates keep-alive timeout to the OS TCP stack

**Mechanism:** `server.rs:278` — `.keep_alive(KeepAlive::Os)`. This means actix does NOT track or
enforce idle keep-alive at the application layer; the OS TCP stack sends TCP keep-alive probes
(default Linux: first probe after 7200s = 2h, then 9 probes at 75s intervals). For AI apps holding
many long-lived SSE streaming connections, this means a connection that goes silent (client crashes,
network partition) may sit open for hours consuming a connection slot.

**Lancelot check hint:** Lancelot is hyper 1.x + tokio. Check whether hyper's default keep-alive
config is similar or stricter. For AI streaming (long-lived SSE), a per-connection idle timeout
(distinct from the graceful drain timeout) is useful. Robyn has NO such timeout; Lancelot has
`LANCELOT_DRAIN_SECS` for graceful shutdown but not an idle-connection reaper. Low priority — this
is a proxy responsibility — but worth noting for operator guidance on resource limits.

**Cite:** `server.rs:278` (`KeepAlive::Os`), `server.rs:279` (`workers` param).

---

### M13 — Robyn's `mcp.py` auto-schema from function signatures (Lancelot already has this)

**Mechanism:** `robyn/mcp.py` `_generate_schema_from_function` — walks `inspect.signature(fn).parameters`,
maps `str→string, int→integer, float→number, bool→boolean` (no-default → `required`), returns a
JSON-schema dict. Used for `@app.mcp.tool(name, description, input_schema)` where `input_schema=None`
triggers auto-generation. Protocol version `2024-11-05`. Full MCP JSON-RPC 2.0: `initialize`,
`resources/list`, `resources/read` (URI-template regex), `tools/list`, `tools/call`, `prompts/list`,
`prompts/get`.

**Lancelot check hint:** Lancelot has `lancelot/mcp.py` and `lancelot.mcp.mount()`. Already built
to MCP spec. **HAVE.** The Robyn auto-schema is simpler than Lancelot's (Lancelot uses pydantic-core
for schema derivation, which is more complete). No gap here.

**Cite:** `robyn/mcp.py` (full file), `mcp.py protocolVersion: "2024-11-05"`.

---

### M14 — `excluded_response_headers_paths` + `to_http_response_without_global_headers` pattern

**Mechanism:** `const_router.rs:25-30` `CachedResponse { route_header_count: usize }` stores the
count of route-specific headers (before global headers were baked in). `to_http_response_without_global_headers()`
uses `route_header_count` to slice off the baked global headers when serving paths like `/docs` and
`/openapi.json`. These paths are excluded via `set_response_headers_exclude_paths(Option<Vec<String>>)`
on the `Server` pyclass. The `/docs` and `/openapi.json` routes are `const=True` so they serve from
the const cache; they use the `_without_global_headers` variant to avoid emitting framework headers
(like security headers, custom `Server:` values) on doc endpoints — a deliberate choice.

**Lancelot check hint:** Lancelot likely applies all middleware (including security headers) to `/docs`
and `/openapi.json`. There is no equivalent `excluded_response_headers_paths` concept. For Lancelot's
AI API use case, having `/livez`/`/readyz` excluded from certain headers (e.g. auth headers, per-request
cost headers) could be useful. **Low priority, operator convenience only.**

**Cite:** `const_router.rs:25-30` (route_header_count), `server.rs:144-146` (exclude paths), `openapi.py`
(registers /docs+/openapi.json as `const=True, excluded`).

---

### M15 — `500 inline body = b"Internal server error"` (const byte literal — zero alloc error path)

**Mechanism:** `response.rs:170-181` — `Response::internal_server_error` uses `const &[u8]`:
```rust
const INTERNAL_SERVER_ERROR_BODY: &[u8] = b"Internal server error";
fn internal_server_error(headers: Option<&Headers>) -> Response {
    Response { status_code: 500, body: INTERNAL_SERVER_ERROR_BODY.to_vec(), ... }
}
```
Similarly `not_found` = `b"Not found"`, `method_not_allowed` = `b"Method not allowed"` — all
`const &[u8]`. The allocation on error is exactly one `Vec::from_slice` copy (trivial). The response
body is never dynamic on the hot error path.

**Lancelot check hint:** Lancelot uses `plain(StatusCode::NOT_FOUND, b"Not found")` etc. in
`service.rs:257-261`. Same pattern — zero-alloc error bodies. **HAVE.** This is a design convergence,
not a gap.

**Cite:** `response.rs:170-181` (const error bodies), `service.rs:257-261` (Lancelot equivalent).

---

### M16 — `number_of_params==0` skips Request→Py conversion entirely (0-arg fast path)

**Mechanism:** `executors/mod.rs:168`:
```rust
if function.number_of_params == 0 { return handler.call0(); }
```
A `def ping(): return "ok"` style handler with zero parameters never pays the `Request::into_pyobject`
cost (the ~6-alloc eager materialization). The arity is captured once at registration via
`inspect.signature`, stored as `u8`.

**Lancelot check hint:** `dispatch.rs:114-145` — Lancelot's `build_result` builds a lazy `Request`
proxy for ALL handlers. For 0-param handlers, even a lazy proxy (`Py::new(py, LazyRequest{...})`) has
one allocation. The real win is to detect 0-param handlers at registration and skip even the proxy
build. This is a micro-opt (1 alloc ~50 ns) but real for high-RPS healthcheck-style handlers where
the const tier isn't used. Verify if Lancelot's di.py registration computes param counts and stores
them.

**Cite:** `executors/mod.rs:168` (0-param skip), `robyn.pyi:54-71` (FunctionInfo.number_of_params).

---

### M17 — `rustimport` user-Rust integration (operator experience, no perf impact)

**Mechanism:** `pyproject.toml:89` `"rustimport == 1.3.4"` — a runtime dep. `robyn --create-rust-file
<name>` shells out to `python -m rustimport new <name>.rs`; `robyn --compile-rust-path <dir>` to
`rustimport build`. This lets users write `.rs` files alongside their Python, compile them to `.so`s,
and import them from handler code. Reloader (`reloader.py:158`) re-triggers `rustimport build` on
`.rs` changes in dev mode.

**Lancelot check hint:** Lancelot does not ship `rustimport`. This is an operator-experience feature,
not a byte-path win. The benefit for an AI app: a handler that does heavy embedding preprocessing
can be written in Rust (via rustimport) and called from the Python handler. Niche. **Low priority;
not a byte-path item.**

**Cite:** `pyproject.toml:89` (dep), `reloader.py:158` (rebuild on .rs change), `cli.py:21-73`
(scaffolder).

---

### M18 — Query string: NO percent-decoding (explicit behavioral contract)

**Mechanism:** `request.rs:132-141` — query string parsed as `req.query_string().split('&')`, then
`split_once('=')` per pair. **No URL decoding (`percent_decode`)**. `?q=hello%20world` gives
`query_params["q"] = "hello%20world"` not `"hello world"`. This is an explicit behavioral contract
tested by `test_split_request_params.py`.

**Lancelot check hint:** Lancelot's ASGI seam inherits Starlette's query parsing which DOES
percent-decode. This is a behavior difference: a Robyn app migrating to Lancelot would see decoded
query params where it expected raw. **Not a Lancelot bug** — Lancelot's decoding is correct per
RFC 3986. Document the difference for operators migrating from Robyn.

**Cite:** `request.rs:132-141` (no percent-decode), `teardowns/ROBYN.md §37` (test reference).

---

### M19 — `multiprocess` (dill fork) vs stdlib `multiprocessing` — pickling closures

**Mechanism:** `pyproject.toml:87` `"multiprocess >= 0.70.18, < 0.71.0"`. Robyn must use the `dill`-
backed fork (not stdlib `multiprocessing`) because: (a) user-defined handler closures (`lambda`, class
methods, nested functions) are not picklable by stdlib `pickle`; (b) the `SocketHeld` PyO3 class holds
a raw socket `fd` and must survive pickling across the fork. `dill` handles both. `mp.allow_connection_pickling()`
is called before `run_processes` (`robyn/__init__.py` `Robyn.start`).

**Lancelot check hint:** Lancelot uses its own `run()` / `App.run()` which invokes the embedded
hyper+tokio server directly, not via fork+pickle. Lancelot's multiprocess deploy recommendation is
"run N separate processes pointing at the same port with SO_REUSEPORT on Linux" — no fork at all,
each process started independently. **No dill dep needed.** This is a Lancelot architectural win
(no pickle/fork complexity, no dill dep, clean process model). Note for operators: Lancelot's
`LANCELOT_WORKERS` scales Rust I/O threads within one process; multi-process scaling is external
(gunicorn, systemd, Kubernetes replicas).

**Cite:** `pyproject.toml:87` (multiprocess dep), `processpool.py:107-128` (fork + try_clone),
`robyn/__init__.py` `mp.allow_connection_pickling()`.

---

## Summary table — new gaps and their priority

| ID | Mechanism | File:line | Real win? | Lancelot status |
|----|-----------|-----------|-----------|-----------------|
| M1 | CORS kills const tier (`has_middleware` AtomicBool) | `server.rs:233`, `middleware_router.rs:17` | YES — silent perf cliff at deploy | GAP: undocumented operator cliff |
| M2 | Trailing-slash normalization inconsistency for const tier | `request.rs:166-172`, `server.rs:233-238` | MEDIUM — edge case | GAP: check matchit version behavior |
| M3 | GIL on every dynamic route lookup (FunctionInfo::Clone) | `function_info.rs:62-72`, `http_router.rs:48-53` | MICRO — ~50ns, not the bottleneck | HAVE (same pattern, Vec-indexed) |
| M4 | Custom awaitables 38-55% bridge win | `callbacks.rs:12-375`, `runtime.rs:163,182` | YES for high-RPS short handlers | GAP: not yet built |
| M5 | Dedicated blocking pool for sync handlers | `blocking.rs:34-177` | YES for AI apps with sync SDKs | GAP: missing |
| M6 | Body cap + WS frame cap share one env knob | `server.rs:39-40` | LOW — operator doc gap | GAP: doc only |
| M7 | Slowloris: `client_request_timeout=0` | `server.rs:280` | ALREADY AHEAD — Lancelot has it | HAVE (and better) |
| M8 | io-uring opt-in feature flag | `Cargo.toml:53` | LOW — hyper/tokio-uring too costly | SKIP (Tier-D) |
| M9 | `bake_global_headers` at startup for const tier | `const_router.rs:172-199` | MEDIUM — zero header work on const hit | GAP: Lancelot Prebaked doesn't bake headers |
| M10 | SSE double-header bug in Robyn; Lancelot subset | `response.rs:107-117,427-460` | MEDIUM — missing `Pragma`/`Connection` | GAP: minor, add Connection: keep-alive |
| M11 | AsyncGeneratorWrapper per-chunk run_until_complete | `responses.py` | ALREADY FIXED in Lancelot R3 | HAVE (ahead) |
| M12 | KeepAlive::Os — no idle-connection reaper | `server.rs:278` | LOW — proxy responsibility | DOC gap |
| M13 | MCP auto-schema from signatures | `mcp.py` | HAVE | HAVE |
| M14 | excluded_response_headers_paths | `const_router.rs:25-30` | LOW — operator convenience | SKIP for now |
| M15 | Zero-alloc const error bodies | `response.rs:170-181` | HAVE | HAVE (design convergence) |
| M16 | 0-param handler skips Request build | `executors/mod.rs:168` | MICRO | GAP: audit Lancelot lazy-proxy alloc for 0-param |
| M17 | rustimport user-Rust integration | `pyproject.toml:89` | LOW — operator DX | SKIP |
| M18 | No percent-decode on query string | `request.rs:132-141` | DOC ONLY — Lancelot is correct | DOC gap |
| M19 | dill fork for pickling | `pyproject.toml:87`, `processpool.py` | ALREADY BETTER in Lancelot | HAVE (no fork needed) |

---

## Actionable build candidates (by impact)

**Build: M1 documentation + const-tier guardrail.** The CORS-kills-const-tier cliff is the most
likely operator surprise. Cost: a warning at `CORSMiddleware` registration time if `const_routes` are
also registered. One-liner Python check in `middleware.py`. The Rust `Prebaked` check runs before the
Python ASGI stack — confirm the execution order in `service.rs` to determine if Lancelot's const tier
is immune or not.

**Build: M4 `PyIterAwaitable`.** The 55%-faster iter-path awaitable is ~80 lines of Rust (port
`callbacks.rs:85-127`). Use `#[pyclass(frozen)]` + `OnceLock<PyResult<Py<PyAny>>>` + `__next__`
returning `Ok(Some(py.None()))` while pending, `StopIteration(value)` when set. Drive from
`pyo3-async-runtimes::tokio::into_future` replacement in `dispatch.rs`. Gate with a criterion bench.
Real gain at high-RPS short handlers where bridge cost is the bottleneck.

**Build: M5 dedicated sync-handler pool.** Port `blocking.rs` (177 lines) with one change: bounded
`crossbeam::channel` (10000 default, configurable via `LANCELOT_SYNC_QUEUE_MAX`). Expose
`LANCELOT_SYNC_THREADS` knob. The GIL `py.detach()` on `recv()` is the non-negotiable invariant.
For AI apps calling sync SDKs (`openai.OpenAI()`, `anthropic.Anthropic()`), this isolates the
GIL-bound sync work from the Rust reactor and prevents head-of-line blocking of async handlers.

**Build: M9 `bake_global_headers` in Prebaked.** Accept `(name, value)` header pairs at
`Prebaked::new()` time; include them in the pre-built `HeaderMap`. This means `/livez` returns the
correct framework headers (e.g. `Server: lancelot/x.y.z`) from the Rust fast path with zero per-request
header computation.

**Doc: M10 SSE headers.** Add `Connection: keep-alive`, `Pragma: no-cache`, `Expires: 0` to the ASGI
SSE header set (or document why they're omitted). `Connection: keep-alive` in particular is used by
some nginx configurations.
