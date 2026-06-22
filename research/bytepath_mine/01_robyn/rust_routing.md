# Robyn — Rust routing, FFI conversion, shared socket, WebSocket, streaming (mechanism-level teardown)

**Source:** `researchfms/robyn/Robyn/src/routers/`, `src/types/`, `src/conversion.rs`, `src/shared_socket.rs`, `src/websockets/`, `src/io_helpers/`, `src/executors/`
**Date:** 2026-06-22

---

## 1. Routing engine

### 1.1 `HttpRouter` — per-method `matchit` under `parking_lot::RwLock`

File: `src/routers/http_router.rs`

```
type RouteMap = RwLock<MatchItRouter<FunctionInfo>>;
struct HttpRouter { routes: HashMap<HttpMethod, RouteMap> }
```

- One `RwLock<MatchItRouter<FunctionInfo>>` per HTTP method, stored in a `HashMap<HttpMethod, RouteMap>` (9 buckets pre-populated in `new()`).
- `add_route` → `table.write().insert(route, fn_info)`.
- `get_route` → `table.read()` then `router.at(path)`. On a match, iterates `res.params.iter()` into an owned `HashMap<String, String>` and calls `Python::with_gil(|_| res.value.to_owned())` — the GIL is acquired even for path-param extraction (clone of `FunctionInfo` which holds `Py<>` handles). This is a GIL touch on every matched request.

**Lancelot comparison:** Lancelot's `src/router.rs` uses the same `matchit` crate, same per-method `HashMap`, but stores `usize` slot indices instead of `FunctionInfo` structs. The critical difference: Lancelot's `Router::route()` holds NO GIL — it just returns `Option<usize>`, deferring all Python-object access to after route decision. Robyn's `with_gil` inside `get_route` is a structural GIL touch per matched request.

**Status: HAVE (and better).** Lancelot avoids the per-match GIL acquire; Robyn's design bakes it in. No action needed.

### 1.2 `ConstRouter` — two-level fast path (HashMap exact + matchit parametric)

File: `src/routers/const_router.rs`

```rust
type RouteMap = RwLock<MatchItRouter<CachedResponse>>;
type FastMap = RwLock<HashMap<String, CachedResponse>>;

struct ConstRouter {
    routes: HashMap<HttpMethod, Arc<RouteMap>>,   // matchit for parametric/wildcard
    fast_routes: HashMap<HttpMethod, Arc<FastMap>>, // HashMap for exact paths
}
```

Key detail: `get_cached_route` tries `fast_routes` (plain `HashMap.get`) FIRST; falls back to matchit only if the exact hit misses (lines 208–218). For the `/livez`/`/readyz`/health case (exact paths), this avoids even the matchit trie walk — it's a hash-table lookup. The `to_http_response` function is `#[inline(always)]` and clones a pre-built `Bytes` (refcount bump) + iterates a pre-built `Arc<Vec<(String,String)>>`.

Global response headers are **baked at startup** via `bake_global_headers()`: they append to every `CachedResponse`'s header `Arc`, rebuilding the matchit router from the fast_map. This means zero per-request header-merge cost (vs. Lancelot's Prebaked which stores status + content_type only — global headers not baked in).

**Lancelot comparison (`src/service.rs:Prebaked`):** Lancelot's const tier stores `(status, content_type, body: Bytes)`. It serves ONE `Content-Type` header only. Robyn's `CachedResponse` stores arbitrary headers (including cookies, global response headers). Lancelot's const tier is structurally simpler and faster for the single-header case; it does NOT support global-response-header baking into const routes.

**Potential improvement:** if Lancelot adds global response headers (e.g. CORS, X-Frame-Options applied to all routes), the const tier's `Prebaked` would need to include those headers in its pre-built response — Robyn's `bake_global_headers()` pattern (`const_router.rs:172–199`) is the right approach. Currently a non-issue because Lancelot has no global-response-header feature.

**Also notable:** `CachedResponse.route_header_count: usize` (line 29) separates route-level headers from global headers so `to_http_response_without_global_headers()` can skip global headers when they haven't been baked yet. Clever split-point.

### 1.3 `MiddlewareRouter` — `AtomicBool` guard for zero-cost no-middleware path

File: `src/routers/middleware_router.rs`

```rust
pub struct MiddlewareRouter {
    globals: HashMap<MiddlewareType, RwLock<Vec<FunctionInfo>>>,
    routes: HashMap<MiddlewareType, RouteMap>, // matchit, per Before/After
    has_middleware: AtomicBool,
}
```

`has_any_middleware()` reads the `AtomicBool` with `Ordering::Acquire` (line 103). The hot path in Robyn's dispatch checks this FIRST and skips all middleware lookup when false. This is an `Ordering::Release` write on `add_route` (line 31) / `add_global_middleware` (line 89).

**Lancelot comparison:** Lancelot uses a different middleware model (`@app.middleware("http")` ASGI-style, stored as Python callables). It does not have an analogous Rust-side `AtomicBool` early-exit for the "no middleware registered" case. However, Lancelot's `handle()` in `service.rs` does not call into a middleware router at all — the ASGI middleware is in the Python layer, not in Rust routing. So this is architecturally different: Robyn's middleware runs Rust-side (per-route matchit lookup + Python callback), Lancelot's runs Python-side (ASGI stack). The AtomicBool is irrelevant to Lancelot's design.

### 1.4 `WebSocketRouter` — plain `HashMap<String, HashMap<String, FunctionInfo>>` (no matchit)

File: `src/routers/web_socket_router.rs`

WebSocket route key is a plain string. The handler map per endpoint is `{"connect": FunctionInfo, "close": FunctionInfo, "message": FunctionInfo}`. No matchit trie — WS routes are expected to be exact. Stored under a `parking_lot::RwLock`.

**Lancelot comparison:** Lancelot's WebSocket support (`lancelot/websockets.py`) is Python-side via Starlette. No Rust-side WS router.

---

## 2. Rust↔Python type conversion (FFI boundary cost)

### 2.1 `Request::from_actix_request` — EAGER full materialization per request

File: `src/types/request.rs:122–194`

Every request (matched or not) goes through full body buffering, query-string parsing, header copying into `DashMap`, form/multipart parsing, IP extraction. The body is `BytesMut::new()` extended chunk by chunk → `.freeze().to_vec()` — a `Vec<u8>` copy from the `Bytes` freeze. All fields materialized BEFORE the route is resolved.

**Lancelot comparison:** Lancelot's R2 lazy request (`src/ffi/request.rs`) defers all field materializations behind `OnceLock`-cached getters. The route is resolved on BORROWED `method`/`path` from the hyper `Request<Incoming>` BEFORE any allocation (`service.rs:243`). A 404 allocates nothing. This is a direct architectural win Lancelot already has.

**Status: HAVE (and better — lazy vs. eager).**

### 2.2 `Request::into_pyobject` — body is `PyString` or `PyBytes` (no pydantic-core)

File: `src/types/request.rs:32–87`

Body crosses the FFI boundary as:
- empty → `PyString("")`
- valid UTF-8 → `PyString`
- invalid UTF-8 → `PyBytes`

No pydantic-core `from_json` on the body — JSON parsing in `PyRequest::json()` (line 258) calls `serde_json::from_str` then the recursive `json_value_to_py_with_depth()` converter (lines 280–317), building Python objects from `serde_json::Value`. This is serde → Python, not pydantic-core. The `MAX_JSON_DEPTH: usize = 128` depth guard (line 271) is a DoS defense for deeply-nested JSON.

**Lancelot comparison:** Lancelot's R4 uses pydantic-core `from_json` (a single Rust call that validates + builds Python objects in one pass). Lancelot also has `LANCELOT_JSON_MAX_DEPTH` guard in `_json_guard.py`. The pydantic-core path is faster than serde → Python conversion for validated JSON. Status: HAVE (and better).

### 2.3 `Response::from_py` — `downcast_exact` fast path hierarchy

File: `src/executors/mod.rs:86–153` (`extract_response_type_fast`)

Ordered fast path:
1. `downcast_exact::<PyResponse>()` — zero `getattr` calls, reads Rust struct fields directly. Note `downcast_exact` (not `downcast`) so user subclasses with overridden properties fall through.
2. `downcast::<PyDict>()` / `downcast::<PyList>()` — bare dict/list → `orjson.dumps` via cached `ORJSON_DUMPS: PyOnceLock<Py<PyAny>>`.
3. `downcast::<PyString>()` / `downcast::<PyBytes>()` — bare str/bytes → `text/plain` / `application/octet-stream`.
4. StreamingResponse duck-type extraction.
5. Slow-path `FromPyObject` getattr chain.

The `orjson.dumps` is cached via `PyOnceLock` (avoids re-importing on every dict response). `ORJSON_DUMPS` is initialized lazily in `orjson_dumps()` (line 28–34).

**Lancelot comparison:** Lancelot's `serialize.rs` uses pydantic-core `to_json` (not orjson). Lancelot's `build_result` calls `to_json_bytes(py, &result)` which uses the `PyOnceLock`-cached `pydantic_core.to_json`. Structurally similar (both cache the serializer), different underlying library. Pydantic-core is faster for Pydantic models (native Rust serializer); orjson is faster for plain dicts/lists. For Lancelot's FastAPI-compatibility use-case, pydantic-core is the right choice (validated models dominate).

**Status: HAVE. No action.**

### 2.4 `FunctionInfo.number_of_params` — skip Request→Python conversion for 0-param handlers

File: `src/executors/mod.rs:166–169`

```rust
if function.number_of_params == 0 {
    return handler.call0();
}
```

Zero-arg handlers skip `function_args.clone().into_pyobject(py)` entirely — no `Request` construction. This is a small but real FFI saving for health/probe endpoints.

**Lancelot comparison:** Lancelot always constructs a `Request` pyclass and passes it to `handler.call1((req,))` regardless of handler signature (see `dispatch.rs:126`). The 0-param optimization is NOT present. For a health endpoint that's `@app.get("/livez") async def h(): return {}`, the `Request` is built and passed (but ignored). Lancelot's const tier (`Prebaked`) sidesteps this entirely for such routes, so it's moot when const is used. But for non-const 0-param handlers, Lancelot pays the Request-build cost unnecessarily.

**Potential improvement (minor):** Detect 0-param handlers at registration and skip `build_request` in `dispatch.rs`. Low value because (a) the const tier covers the high-traffic 0-param case already, and (b) the per-request cost is small vs. GIL+handler overhead. Tag as a "nice-to-have micro-opt" only.

### 2.5 `Headers` — `DashMap` (concurrent-hashmap) vs. `Vec<(String, String)>`

File: `src/types/headers.rs`

Robyn's `Headers` struct uses `DashMap<String, Vec<String>>` (a sharding-concurrent hashmap). For per-request header reading (Actix extracts headers into this map in `from_actix_headers`) this is heavier than needed — each `insert` does a hash + shard-lock. The `apply_hashmap_headers` (`io_helpers/mod.rs:11–18`) iterates the DashMap to build response headers — an iterate-over-shards operation.

**Lancelot comparison:** Lancelot uses `Vec<(String, String)>` for request headers (allocated once with `Vec::with_capacity(hm.len())` in `service.rs:291`) and hyper's `HeaderMap` (indexed by `HeaderName`) for response. `Vec<(String,String)>` iteration is cache-friendly; `DashMap` iteration is not. Lancelot's approach is faster for the per-request read-once case.

**Status: HAVE (and better).** No action.

### 2.6 `conversion.rs` — `FutureResultToPy` helper only

File: `src/conversion.rs`

This file is tiny (29 lines) — a `FutureResultToPy` enum (`None | Err | Value`) implementing `IntoPyObject<'p>`. It's used to box async handler results for the Python side, not a core FFI path. Adapted from Granian. No novel mechanism here for Lancelot.

---

## 3. Shared socket model (`shared_socket.rs`)

File: `src/shared_socket.rs`

```rust
socket.set_reuse_port(true)?;   // SO_REUSEPORT (not Windows)
socket.set_reuse_address(true)?;
socket.set_nodelay(true)?;
socket.listen(16384)?;          // listen backlog = 16384
socket.set_nonblocking(true)?;
```

- `SocketHeld` is a `#[pyclass]` wrapping `socket2::Socket`.
- `try_clone()` duplicates the FD — Python's multiprocess spawner (Robyn's `processpool.py`) calls `try_clone()` per worker, passing each a cloned FD so all workers share the same listening socket.
- Listen backlog: `16384`. Robyn's backlog matches Nginx's default.
- No `TCP_QUICKACK` or `TCP_FASTOPEN` (Robyn does NOT set these; Lancelot does).

**Lancelot comparison:** Lancelot's `src/socket.rs` (`bind_reuseport`) duplicates the same `SO_REUSEPORT + SO_REUSEADDR + TCP_NODELAY + listen(16384)` setup, PLUS adds `TCP_QUICKACK` (Linux only, via raw `libc::setsockopt`) and `TCP_FASTOPEN` (queue depth 5). Both use `socket2`. Lancelot does NOT expose a `#[pyclass]` socket-clone interface (Lancelot uses a single Rust process, not Python multiprocess). Robyn's `SocketHeld` / `try_clone()` pattern is the mechanism for Python-process-level FD sharing.

**Lancelot SO_REUSEPORT note (CLAUDE.md):** SO_REUSEPORT LB is Linux-only on macOS/BSD `SO_REUSEPORT` binds but delivers to ONE socket. This is the same limitation Robyn has — it's an OS constraint, not a framework choice.

**Status: HAVE (both use SO_REUSEPORT + listen(16384)). Lancelot adds QUICKACK/FASTOPEN. No new action.**

---

## 4. Streaming pump — `create_python_stream`

File: `src/types/response.rs:123–148`

```rust
fn create_python_stream(generator: Py<PyAny>) -> Pin<Box<dyn Stream<...>>> {
    Box::pin(futures::stream::unfold(generator, |generator| async move {
        match tokio::task::spawn_blocking(move || {
            Python::with_gil(|py| {
                let gen = generator.bind(py);
                match gen.call_method0("__next__") { ... }
            })
        }).await { ... }
    }))
}
```

**CRITICAL: Robyn's streaming pump is structurally identical to Lancelot's PRE-R3 broken design.** Each chunk calls `spawn_blocking` (a new thread, or a thread from the blocking pool) + `Python::with_gil(|py| gen.call_method0("__next__"))`. This means:
- One `spawn_blocking` per chunk → one cross-thread task per chunk → one blocking-pool wakeup per chunk.
- NOT one GIL acquire per chunk: it's worse — one `spawn_blocking` future per chunk, with cross-thread scheduling.
- Robyn uses `__next__` (sync generator), not `__anext__` (async generator). The generator must be sync.

This is the design that Lancelot R3 replaced: "the original pump did one cross-thread `call_soon_threadsafe` round-trip per `__anext__` (~0.8 ms/chunk) → 64 chunks = 937 rps" (CLAUDE.md). Robyn's version uses `spawn_blocking` which queues to the tokio blocking pool — likely in the same performance class (millisecond-per-chunk regime for token streaming).

**For AI streaming (e.g. LLM token streams):** Robyn's streaming pump would be slow on high-chunk-rate streams (the same cliff Lancelot fixed in R3). For typical LLM streaming (~50–200 tok/s), the upstream rate dominates, so it's not the bottleneck in practice — but it means Robyn's framework cannot deliver fast JSON-lines or SSE without this cliff.

**Status: HAVE (Lancelot R3 is better — on-loop `_drain`, same-thread `try_push`, no per-chunk `spawn_blocking`).** Confirmed the design superiority. No action needed.

---

## 5. WebSocket implementation (Actix actor model)

Files: `src/websockets/mod.rs`, `src/websockets/registry.rs`, `src/executors/web_socket_executors.rs`

Robyn's WebSocket is built on `actix` (actor framework) + `actix_web_actors`:

- `WebSocketConnector`: an `actix::Actor` that holds the WS connection state. Each client connection is one Actor.
- `WebSocketRegistry`: a `SystemService` (global Actix system service) with a `HashMap<Uuid, Addr<WebSocketConnector>>`. Broadcast = iterate map and `do_send` to each client. Per-client send = `registry.try_send(SendText { recipient_id, ... })`.
- **Channel seam:** `mpsc::unbounded_channel::<Option<String>>()` (tokio unbounded mpsc). The Actor's `StreamHandler` on `ws::Message::Text` sends into the `tx`; Python handlers call `await channel.receive()` to dequeue. This is the Rust↔Python handoff for incoming WS messages.
- **Handler dispatch (`execute_ws_function`):** async handlers → `pyo3_async_runtimes::into_future_with_locals` bridged to Actix's actor future via `into_actor(ws).map(...)`. Sync handlers → `Python::with_gil` inline.
- **Frame size:** configurable via `max_frame_size` kwarg on `start_web_socket`. Passed to `ws::WsResponseBuilder::new(...).frame_size(max_frame_size)`.
- **Registry per endpoint:** `REGISTRY_ADDRESSES: OnceCell<RwLock<HashMap<String, Addr<WebSocketRegistry>>>>` keyed by endpoint string. One registry actor per WS endpoint.

**Lancelot comparison:** Lancelot's `lancelot/websockets.py` uses Starlette's WebSocket (pure Python ASGI). No Rust actor model, no Actix dependency. This is architecturally simpler (no second runtime — Actix runs its own executor alongside tokio) but also means no Rust-native broadcast or per-client actor. For Lancelot's use case (AI streaming, not chat fan-out), the Starlette-level WebSocket is adequate.

**Notable Robyn WS pattern for Lancelot:** The `WebSocketChannel` (mpsc unbounded receiver exposed to Python via `pyo3_async_runtimes::tokio::future_into_py`) is a clean pattern for Rust-backed async message queues exposed to Python. If Lancelot ever needed a high-performance WS fan-out (e.g. streaming LLM responses to multiple WS clients), a similar mpsc channel bridge could be used.

**Status: DIFFERENT design. Robyn's Actix actor model is more complex but enables built-in fan-out. Not applicable to Lancelot's current design.** No action.

---

## 6. Response extraction fast path — `extract_response_type_fast` vs. Lancelot

Key contrast summary (see §2.3 for detail):
- Robyn: `downcast_exact::<PyResponse>` → orjson → str/bytes → StreamingResponse → getattr fallback.
- Lancelot: `to_json_bytes` via pydantic-core `to_json` — a single call that handles all pydantic models, dicts, and primitives. The Python-side FastAPI compat layer serializes into a Python object; pydantic-core serializes it to bytes.

The `downcast_exact` guard in Robyn is notable: it prevents subclass override semantics from being silently short-circuited. Lancelot's `serialize.rs` uses pydantic-core which naturally handles model subclasses via their pydantic schema.

---

## 7. `io_helpers` — synchronous file read

File: `src/io_helpers/mod.rs:28–33`

`read_file` is synchronous `std::fs::File::open` + `file.read_to_end`. Comment says "ideally this should be async." For static file serving, this blocks the tokio thread. Lancelot's `lancelot/staticfiles.py` uses `aiofiles` for async reads.

**No action for Lancelot** — this is a Robyn weakness Lancelot doesn't share.

---

## 8. Mechanisms NOT present in Robyn (Lancelot advantages)

Items in Lancelot that Robyn lacks (confirmed by reading the source):
1. **Body byte-cap (`Limited` + 413)** — Robyn's `from_actix_request` does `BytesMut::new(); while chunk { extend }` with NO size limit. Memory-DoS vector. Lancelot has `Limited::new(req.into_body(), max_body())` (service.rs:330) + `LANCELOT_MAX_BODY` env.
2. **Connection-count semaphore** — Robyn spawns an Actix worker per connection (Actix handles its own threadpool). Lancelot has `conn_limit: Semaphore` with `LANCELOT_MAX_CONNECTIONS` (service.rs:428–429).
3. **Slow-loris header deadline** — Robyn's Actix config sets `header_read_timeout` to **zero** effectively (comment in `CLAUDE.md` noted this). Lancelot has `header_timeout_ms()` / `LANCELOT_HEADER_TIMEOUT_MS` (service.rs:124).
4. **Graceful drain** — Robyn hard-kills (no SIGTERM drain; Robyn-style hard-kill is explicitly noted as Lancelot's B1 anti-goal). Lancelot drains via `conn_limit.acquire_many_owned(max_conns)`.
5. **Const tier two-level lookup** — Robyn's `ConstRouter` has HashMap-exact + matchit fallback (§1.2). Lancelot's `Prebaked` is simpler (slot check, no second lookup). This is fine for Lancelot's use case (const routes are registered as slots, slot lookup is O(1)).
6. **`TCP_QUICKACK` + `TCP_FASTOPEN`** — Robyn does not set these. Lancelot does (Linux-only, best-effort).

---

## Summary table

| Mechanism | Robyn file:line | Status | Action |
|---|---|---|---|
| Per-method matchit router | `http_router.rs:13-58` | HAVE | None |
| GIL touch per matched route (FunctionInfo clone) | `http_router.rs:53` | LANCELOT BETTER | None |
| Const tier: HashMap-exact + matchit fallback | `const_router.rs:203-218` | HAVE (simpler slot approach) | Consider global-header baking if feature added |
| `bake_global_headers()` — global headers in const responses | `const_router.rs:172-199` | NOT PRESENT | Add if global-response-header feature ships |
| AtomicBool no-middleware guard | `middleware_router.rs:102-104` | N/A (different middleware model) | None |
| Eager full request materialization | `request.rs:122-194` | LANCELOT BETTER (lazy R2) | None |
| serde_json → Python depth-guarded recursive conversion | `request.rs:271-317` | HAVE (pydantic-core, better) | None |
| downcast_exact fast path for response extraction | `executors/mod.rs:93-107` | HAVE (pydantic-core path) | None |
| orjson cached via PyOnceLock for dict/list | `executors/mod.rs:26-34` | N/A (pydantic-core instead) | None |
| 0-param handler: skip Request construction | `executors/mod.rs:166-169` | NOT PRESENT (micro) | Micro-opt; const tier covers it |
| DashMap for headers | `headers.rs:9-11` | LANCELOT BETTER (Vec) | None |
| SO_REUSEPORT + listen(16384) shared socket | `shared_socket.rs:23-28` | HAVE | None |
| TCP_QUICKACK + TCP_FASTOPEN | not in shared_socket.rs | LANCELOT AHEAD | None |
| Per-chunk spawn_blocking streaming pump | `response.rs:123-148` | LANCELOT BETTER (R3 on-loop drain) | None |
| Actix WS actor + mpsc channel seam | `websockets/mod.rs` | DIFFERENT model | Not needed now |
| Configurable WS frame size | `websockets/mod.rs:316` | CHECK Lancelot WS | Minor gap |
| Unbounded body (no cap) | `request.rs:157-163` | LANCELOT BETTER (Limited+413) | None |

---

## One item to check

**WS max frame size:** Robyn exposes `max_frame_size` as a parameter to `start_web_socket` (mod.rs:315), which maps to `WsResponseBuilder::frame_size`. This is a byte-path operator knob for large WS messages (e.g. streaming large tool outputs). Check whether Lancelot's Starlette WebSocket exposes this. `lancelot/websockets.py` delegates to Starlette — check if Starlette's `WebSocket` exposes frame size control.
