# Robyn Python bridge — mechanism-level teardown

Source cluster: `robyn/__init__.py`, `robyn/router.py`, `robyn/processpool.py`,
`robyn/robyn.pyi`, `robyn/types.py`, `robyn/status_codes.py`,
`src/server.rs`, `src/shared_socket.rs`, `src/callbacks.rs`,
`src/asyncio.rs`, `src/runtime.rs`, `src/routers/const_router.rs`,
`src/executors/mod.rs`.

All line references are to the Robyn repo at
`/Users/seventyleven/Desktop/researchfms/robyn/Robyn/`.

---

## 1. Multiprocess model — socket handoff via `SocketHeld.try_clone()`

**Mechanism.** `processpool.py:33` constructs one `SocketHeld(url, port)`.
`shared_socket.rs:16–37`: the socket is created with
`socket.set_reuse_port(true)` (non-Windows), `set_reuse_address(true)`,
`set_nodelay(true)`, `bind(address)`, `listen(16384)`, `set_nonblocking(true)`.
For each worker process (`processpool.py:107–128`), `socket.try_clone()`
(`shared_socket.rs:34–37`) duplicates the file descriptor into a new
`SocketHeld` that is pickled into the child via `multiprocess.Process`.
Each child calls `spawn_process()` which runs `server.start(socket, workers)`
— the Rust `Server.start()` at `server.rs:84` calls
`socket.get_socket().into()` for actix `HttpServer::listen()`.

**Runtime layer.** `processpool.py:133–146`: on non-Windows/non-cross,
`import uvloop; uvloop.install(); uvloop.new_event_loop()`. Falls back to
`asyncio.new_event_loop()` on Windows and ARM cross-targets. This is a
**hard dep on uvloop** (unlike Lancelot's soft-dep with `LANCELOT_LOOP` knob).

**Lancelot comparison.** Lancelot's `src/socket.rs` also does
`set_reuse_port(true)` and `listen(16384)` and socket cloning for multiprocess.
The model is architecturally identical. Lancelot's `LANCELOT_WORKERS` env knob
(clamped [1,64]) plus `cpu::worker_count()` correspond to Robyn's
`config.processes` × `config.workers`. **STATUS: HAVE.**

**SIGTERM handling difference (notable).** `processpool.py:52–58`:
`SIGTERM` → `process.kill()` on every worker. Hard kill, no drain. Lancelot
has graceful drain (`acquire_many_owned(max_conns)`) — this is Lancelot's
explicit anti-goal B1 win vs Robyn.

---

## 2. Const route fast path — two-tier lookup (HashMap + matchit)

**Mechanism.** `const_router.rs:75–79`: two maps per HTTP method —
`FastMap = RwLock<HashMap<String, CachedResponse>>` for exact-path O(1)
lookup, and `RouteMap = RwLock<MatchItRouter<CachedResponse>>` for parameterized
paths. `get_cached_route()` (`const_router.rs:202–219`) tries the HashMap
first (read lock, no iteration), falls back to matchit only on miss.

**CachedResponse** (`const_router.rs:25–73`): pre-built `Bytes` body + baked
headers including global response headers (via `bake_global_headers()` called at
startup when no middlewares are registered, `server.rs:144`). Served at
`server.rs:233–251` with ZERO Python call: if no middlewares registered AND
const router hits, return `cached.to_http_response()` directly from Rust before
touching the Python event loop.

**Python side.** `__init__.py:152–221` `add_route()` accepts `is_const: bool`
(`@app.get("/path", const=True)`). The Rust executor (`const_router.rs:106–127`)
calls `execute_http_function(&Request::default(), &function, None)` ONCE at
registration time, captures the `Response`, bakes headers.

**Lancelot comparison.** Lancelot has `const_routes=[(method, path, body,
content_type, status), ...]` on `_native.run()` — the `service::Prebaked`
path in `src/service.rs`. This is architecturally equivalent (prebaked bytes from
Rust, zero-GIL). Lancelot's const tier also skips the Python event loop entirely.
The two-tier HashMap + matchit const lookup (separate from dynamic routing) is
also present in Lancelot's matchit router. **STATUS: HAVE.**

One nuance: Robyn's `bake_global_headers()` merges global response headers into
the `CachedResponse.headers` `Arc<Vec>` at startup, skipped only if middlewares
exist. Lancelot's const-tier equiv: check if Lancelot bakes global headers into
prebaked responses at registration time or applies them per-request.

---

## 3. `PyOnceLock<orjson.dumps>` callable cache

**Mechanism.** `executors/mod.rs:26–34`: `static ORJSON_DUMPS: PyOnceLock<Py<PyAny>>`.
Resolved once at first use via `ORJSON_DUMPS.get_or_try_init(py, || ...)`.
Used in `extract_response_type_fast()` whenever a bare `dict` or `list` is
returned by a handler (line 113–121): downcast to `PyDict`/`PyList`, call
`orjson_dumps(output)`, get `PyBytes`, convert to `Vec<u8>`.

**Lancelot comparison.** `src/serialize.rs:38–42`: Lancelot has the identical
`PyOnceLock<Option<Py<PyAny>>>` pattern for `orjson.dumps` and for the
pydantic-core `to_json` callable, with the same GIL-deadlock safety note
(comment at line 11–20). **STATUS: HAVE.**

---

## 4. Handler 0-param shortcut

**Mechanism.** `executors/mod.rs:168–170`: `if function.number_of_params == 0 { return handler.call0(); }`. Skips Request→Python conversion entirely for
handlers that take no arguments. `FunctionInfo.number_of_params` is the
pre-computed param count stored at registration time.

**Lancelot comparison.** Lancelot's `dispatch.rs:57–58` checks
`if !result.hasattr(pyo3::intern!(py, "__await__"))` for sync handlers but
does NOT have a dedicated `number_of_params == 0` → `call0()` shortcut.
The DI layer (`lancelot/di.py:117–123`) always builds and passes kwargs.
This is a micro-opt (avoids one Request pyobject allocation and one `call1` vs
`call0`). **STATUS: MISSING — micro-opt, low priority.**

---

## 5. `_JSON_HEADERS` / `_TEXT_HEADERS` singletons

**Mechanism.** `router.py:29–36`: two module-level `Headers` singletons
`_JSON_HEADERS = Headers({"Content-Type": "application/json"})` and
`_TEXT_HEADERS = Headers({"Content-Type": "text/plain"})`. These are passed
into every `Response(...)` built for Pydantic serialization, error responses,
and bare-dict returns — 8 call sites in `router.py`. Comment (lines 29–35)
explains: "Rust side clones them into an independent Headers struct during
extraction, so server-side header injection never mutates the singleton."

**Lancelot comparison.** Lancelot's responses go through the ASGI seam
(`lancelot/responses.py` + `src/dispatch.rs`) — check if there are
equivalent module-level header singletons for common content types.
Given Lancelot uses pydantic-core and its own serialization path, this
pattern may or may not apply on the hot path. **STATUS: CHECK.**

---

## 6. Registration-time signature inspection (zero per-request `inspect.signature`)

**Mechanism.** `router.py:149–152`: `route_param_names = parse_route_param_names(endpoint)` and `handler_params = inspect.signature(handler).parameters` called ONCE in
`add_route()`. `pydantic_params = detect_pydantic_params(handler_params)` also
at registration time (line 164). The result is closed over in `wrapped_handler`.
Per-request dispatch (`wrapped_handler` at line 177+) never calls
`inspect.signature`.

**Lancelot comparison.** Lancelot's DI layer (`lancelot/di.py:117–123`) also
calls `inspect.signature` at registration time in `build_resolver()`.
**STATUS: HAVE.**

---

## 7. `PyFutureAwaitable` freelist (`freelist = 128`) for per-request awaitable objects

**Mechanism.** `callbacks.rs:12`: `#[pyclass(frozen, freelist = 128, module = "robyn._robyn")]` on `PyEmptyAwaitable`. pyo3's `freelist` keeps up to 128
deallocated instances in a pool; the next `PyEmptyAwaitable::new()` reuses a
slot instead of allocating. This is the awaitable returned for zero-duration
operations.

**Lancelot comparison.** No `freelist` attribute found on any pyo3 `#[pyclass]`
in `src/` (grep confirmed). If Lancelot creates per-request awaitable/future
objects at high volume (e.g., the stream pump's channel objects), a freelist
annotation on the smallest/most frequent objects could reduce allocator pressure.
**STATUS: MISSING — micro-opt; only relevant on tiny-request hot path.**

---

## 8. Per-request `contextvars.Context` isolation for middleware phase chaining

**Mechanism.** `server.rs:505–522` + `asyncio.rs:47–49`:
For every request, `new_context(py)` allocates a fresh `contextvars.Context()`.
`ctx_ref = Some(&request_context)` is threaded through all middleware phases
and the route handler. Sync handlers run via `ctx.run(handler, ...)` (line 220);
async handlers are wrapped via `wrap_coro_in_context` → `_run_in_context(coro,
ctx)` which calls `loop.create_task(coro, context=ctx)` (Python 3.11+),
passing the SAME `ctx` object (not a copy) so writes in `before_request` are
visible in the route handler and `after_request`.

Comment at `asyncio.rs:57–60`: "Passing the context directly to `create_task`
tells asyncio to use the given Context object without copying it, so any
ContextVar writes performed inside coro persist in ctx."

**Lancelot comparison.** Lancelot's `context.py` defines ContextVars
(`request_id`, `cost_usd`, `agent_id`, `queue_wait_ms`, etc.) that are
populated by the ASGI seam in the dispatch path. The Lancelot ASGI seam uses
a task-local event loop (`TaskLocals`) but check whether it allocates a
fresh per-request `contextvars.Context` and threads it through both
before-middleware and after-middleware phases the same way Robyn does.
If Lancelot's middleware chaining does NOT share a single `Context` across
phases, ContextVar writes in a `@app.middleware("http")` before-handler
would NOT be visible in the route handler. **STATUS: VERIFY — potential
correctness gap, not a perf issue.**

---

## 9. Actix-web vs hyper — HTTP server runtime architecture

**Mechanism.** Robyn uses **actix-web** (`server.rs:28–32`: `use actix_web::*`,
`use actix_http::KeepAlive`). `Server.start()` spins up an `actix_web::rt::System`
on a **separate OS thread** (`thread::spawn` at line 136). Actix's
`HttpServer::new(...).workers(workers)` creates `workers` actix worker threads
inside that system. The Python event loop (`asyncio.new_event_loop` / uvloop) is
created on the CALLING thread and driven by `loop.run_forever()` (line 289).
Async handlers are bridged from actix's tokio executor back to the Python event
loop via `pyo3_async_runtimes::tokio::scope_local(task_locals, ...)` at line 257.

`server.rs:278`: `HttpServer.keep_alive(KeepAlive::Os)` — OS-managed keepalive,
not a fixed timer. `client_request_timeout` is set to 0 (disabled) at line 280.
Robyn's Python side sets `client_timeout` (default 30s) and
`keep_alive_timeout` (default 20s) and passes them to `server.start()` via
`processpool.py`, but the `.pyi` stub at line 517 shows
`start(socket, workers, client_timeout, keep_alive_timeout)` — Rust side
plumbs them in.

**Lancelot comparison.** Lancelot uses **hyper 1 + tokio** (not actix), HTTP/1.1
only (hardened at M3). Single `tokio::runtime` (multi-thread, `worker_threads =
cpu::worker_count()`). No OS-thread split — tokio drives everything.
`LANCELOT_HEADER_TIMEOUT_MS` (default 15000ms) = slow-loris guard.
`LANCELOT_DRAIN_SECS` (default 10) = graceful drain deadline.
Lancelot does NOT have a `keep_alive_timeout` operator knob; keepalive is
managed by hyper's defaults. **STATUS: POTENTIAL GAP — `LANCELOT_KEEP_ALIVE_TIMEOUT_SECS` not present. Robyn exposes it; operators may want it.**

---

## 10. `ROBYN_MAX_PAYLOAD_SIZE` body cap env var (default 1 MB)

**Mechanism.** `server.rs:39–40`: `const MAX_PAYLOAD_SIZE: &str = "ROBYN_MAX_PAYLOAD_SIZE"` / `const DEFAULT_MAX_PAYLOAD_SIZE: usize = 1_000_000`.
Applied via `app.app_data(web::PayloadConfig::new(max_payload_size))` at line 219
AND via `start_web_socket(..., max_payload_size)` at line 213.

**Lancelot comparison.** Lancelot has `LANCELOT_MAX_BODY_SIZE` (default 16 MiB,
set during M4 hardening) applied via hyper's `Limited` reader. **STATUS: HAVE
(at 16 MiB default; more permissive than Robyn's 1 MB default).**

---

## 11. `extract_response_type_fast()` — ordered fast-path dispatch on return type

**Mechanism.** `executors/mod.rs:86–153`: five-level dispatch with `#[inline]`:
1. `downcast_exact::<PyResponse>()` — zero `getattr` calls for the native pyclass.
2. `downcast::<PyDict>()` or `PyList` — orjson serialize in Rust.
3. `downcast::<PyString>()` — `to_string().into_bytes()`.
4. `downcast::<PyBytes>()` — `as_bytes().to_vec()`.
5. `output.extract::<StreamingResponse>()` — duck-typed via attrs.
6. Fallback: `output.extract::<Response>()` — `FromPyObject` chain for subclasses.

`downcast_exact` (not `downcast`) on `PyResponse` to allow user subclasses with
overridden properties to fall through. `downcast` (subclass-aware) on
`PyDict`/`PyList`/`PyString`/`PyBytes`.

**Lancelot comparison.** Lancelot's `dispatch.rs` takes a different path: the
handler returns through the ASGI seam; the Python wrapper in `lancelot/app.py`
serializes to bytes via pydantic-core before returning. The Rust side receives
a Python bytes object and does not need to type-dispatch. Check whether
Lancelot has similar inline response dispatch in `src/dispatch.rs` or the
Python app wrapper already handles this. **STATUS: DIFFERENT ARCHITECTURE —
Lancelot externalizes type dispatch to the Python DI/serialization layer;
Robyn does it in Rust. Not a gap for Lancelot's current design.**

---

## 12. `ROBYN_BROWSER_OPEN` — dev ergonomics knob

**Mechanism.** `__init__.py:706`:
`open_browser = bool(os.getenv("ROBYN_BROWSER_OPEN", self.config.open_browser))`.
If true, `webbrowser.open_new_tab(f"http://{url}:{port}/")` at startup
(`processpool.py:60–63`).

**Lancelot comparison.** Lancelot has no equivalent. **STATUS: MISSING — pure
dev ergonomics, not a byte-path win. Tier-D by lens criteria.**

---

## 13. `before_request` / `after_request` with full per-endpoint matching

**Mechanism.** `router.py:446–516`: `add_middleware()` distinguishes global
(endpoint=None → `global_middlewares`) vs per-endpoint (endpoint != None →
`route_middlewares`). Both sync and async handlers are wrapped. Global
middlewares are applied before route-specific ones (server.rs:526–557 runs
`all_before = before_middlewares; push route_before`).

**Lancelot comparison.** Lancelot has `@app.middleware("http")` (ASGI-style,
M2 Tier-C) and per-route `before`/`after` middleware via the DI injection
chain. **STATUS: HAVE (different API shape — Lancelot uses ASGI adapter).**

---

## Summary table

| # | Mechanism | File:line | Lancelot status |
|---|-----------|-----------|-----------------|
| 1 | Socket clone + multiprocess | `shared_socket.rs:34`, `processpool.py:108` | HAVE |
| 2 | Const route two-tier HashMap+matchit, baked headers | `const_router.rs:75–219` | HAVE |
| 3 | PyOnceLock orjson.dumps cache | `executors/mod.rs:26–34` | HAVE |
| 4 | 0-param handler call0() shortcut | `executors/mod.rs:168–170` | MISSING (micro-opt) |
| 5 | Module-level header singletons | `router.py:35–36` | CHECK |
| 6 | Registration-time signature inspection | `router.py:149–165` | HAVE |
| 7 | freelist=128 on PyEmptyAwaitable | `callbacks.rs:12` | MISSING (micro-opt) |
| 8 | Per-request new contextvars.Context, shared across phases | `server.rs:515`, `asyncio.rs:47` | VERIFY (correctness) |
| 9 | keep_alive_timeout operator knob | `server.rs:278`, `__init__.py:705` | POTENTIAL GAP |
| 10 | ROBYN_MAX_PAYLOAD_SIZE (1 MB default) | `server.rs:39–40` | HAVE (at 16 MiB) |
| 11 | Inline 5-level response type dispatch in Rust | `executors/mod.rs:86–153` | DIFFERENT ARCH |
| 12 | ROBYN_BROWSER_OPEN dev knob | `__init__.py:706` | MISSING (Tier-D) |
| 13 | before/after_request global + per-endpoint middleware | `router.py:446–516` | HAVE |
