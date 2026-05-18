# Robyn RE → Lancelot: Rust Routing, Request/Response Types, FFI Marshalling, WebSockets

> Source-level reverse engineering of Robyn's Rust core for **Lancelot** (Rust core + Python API, FastAPI ergonomics).
> Every claim cites `path:line` against the local tree at
> `/Users/seventyleven/Desktop/researchfms/robyn/Robyn/src/`.
> Labels: **KNOWN** = read directly from source; **INFERRED** = deduced from the code's structure/absence; **DESIGN** = Lancelot proposal.
> This file deliberately **exceeds** `teardowns/ROBYN.md` §3 — it goes per-allocation on the FFI boundary and turns each finding into a concrete Lancelot decision.

---

## 0. Executive summary (read this first)

**The const-router trick (the single best idea to steal).** Robyn lets a route be declared `const`. At *registration time* (not request time) it runs the handler exactly once on the event loop, captures the `Response`, flattens headers + cookies + body into a `CachedResponse { status: StatusCode, headers: Arc<Vec<(String,String)>>, body: Bytes, route_header_count: usize }` (`const_router.rs:25-30`), and stores it in **two** structures per HTTP method: an exact-match `HashMap<String, CachedResponse>` ("fast_routes", O(1)) and a `matchit` radix trie ("routes", for parameterized const paths). Global response headers are *baked in* at server start (`bake_global_headers`, `const_router.rs:172`). On the hot path, **if and only if zero middleware is registered** (gated by one `AtomicBool`, `middleware_router.rs:17`), a const hit returns `cached.to_http_response()` (`server.rs:248`) — which never parses the request body, never builds a `PyRequest`, never acquires the GIL, never enters Python. It is a pure-Rust static-response server for those routes. This is a ~10–50× latency win for health checks, cached API responses, static JSON, docs/openapi.

**The FFI marshalling cost (the thing to minimize in Lancelot).** Every *non-const* request pays a fixed, eager, allocation-heavy boundary crossing **whether or not the handler reads any of it**. Tracing `Request::from_actix_request` (`request.rs:122`) → `Request::into_pyobject` (`request.rs:32`) → `get_function_output` (`executors/mod.rs:155`), a single GET with 12 request headers and an empty body costs (counted in §3.4): **~30+ heap allocations and ~25+ string copies** before the handler's first line runs — query parse (1 `split` + 2 `String` per pair), `Headers::from_actix_headers` (lowercase-clone every header name + value into a `DashMap<String,Vec<String>>`, ~3 allocs/header), `headers.extend(global)` (re-clone every global header), `into_pyobject` (rebuild a *second* `Py<Headers>`, allocate empty `PyDict` for path_params, allocate two empty `PyDict`s for form_data + files even when unused, UTF-8-validate + copy the body into a `PyString`/`PyBytes`), then `Py::new(py, PyRequest)` heap-allocates the pyclass. The body is **fully buffered into a `Vec<u8>` before the handler is invoked** (`request.rs:159-163`) — no lazy/streaming body. This eager, total marshalling is the dominant per-request cost of the hybrid model and is exactly where Lancelot wins by going lazy.

**Bottom line for Lancelot:** keep the const-router idea verbatim (even improve it: allow it with middleware via a "pure-middleware-chain" check). Replace Robyn's eager `Request` marshalling with a **lazy `Request` proxy pyclass** that holds the raw actix `HttpRequest` + an un-consumed payload and materializes headers/body/query *on attribute access* from Python, so a handler that only touches `request.headers["authorization"]` pays for one header lookup, not 30 allocations. Details and code in §9.

---

## 1. File map and what each does (KNOWN)

| File | Lines | Role in the per-request path |
|---|---|---|
| `routers/mod.rs` | 25 | `Router<T,U>` trait: `add_route` / `get_route`. Two generic params: return type `T`, key type `U` (`HttpMethod` or `MiddlewareType`). |
| `routers/http_router.rs` | 75 | `HashMap<HttpMethod, RwLock<matchit::Router<FunctionInfo>>>` — one radix trie per method. |
| `routers/const_router.rs` | 220 | Two-tier const cache: exact `HashMap` + `matchit` fallback, holding pre-rendered `CachedResponse`. |
| `routers/middleware_router.rs` | 105 | Global before/after `Vec<FunctionInfo>` + per-route `matchit` + `has_middleware: AtomicBool` (the fast-path gate). |
| `routers/web_socket_router.rs` | 50 | `RwLock<HashMap<String, HashMap<String, FunctionInfo>>>` — path → {connect, close, message}. |
| `types/request.rs` | 317 | `Request` (Rust struct) ↔ `PyRequest` (pyclass). `from_actix_request`, `into_pyobject`, multipart, `json()`. |
| `types/response.rs` | 460 | `Response` / `StreamingResponse` / `ResponseType`, `Responder` impls, the Python-generator stream. |
| `types/headers.rs` | 206 | `Headers` pyclass over `DashMap<String, Vec<String>>` (case-folded keys). |
| `types/cookie.rs` | 247 | `Cookie` / `Cookies` pyclasses, RFC-6265 serialization via actix's cookie crate. |
| `types/multimap.rs` | 112 | `QueryParams` pyclass over `HashMap<String, Vec<String>>`. |
| `types/function_info.rs` | 72 | `FunctionInfo { handler: Py<PyAny>, is_async, number_of_params, args, kwargs }` + `MiddlewareType`. |
| `types/identity.rs` | 18 | `Identity { claims: HashMap<String,String> }`. |
| `types/mod.rs` | 147 | `HttpMethod`, `Url`, body/description py↔rust converters, type-check guards. |
| `executors/mod.rs` | 575 | **The FFI call boundary.** Handler invocation (sync/async), arg marshalling, result extraction fast-path chain. |
| `executors/web_socket_executors.rs` | 68 | WS handler invocation (sync inline / async into actix actor future). |
| `websockets/mod.rs` | 347 | `WebSocketConnector` actix actor, `WebSocketChannel` (Rust→Python mpsc), frame handling. |
| `websockets/registry.rs` | 123 | `WebSocketRegistry` actor: client map, `SendText`/`SendMessageToAll`/`Close`. |
| `io_helpers/mod.rs` | 33 | `apply_hashmap_headers`, `read_file` (sync, blocking). |
| `conversion.rs` | 28 | `FutureResultToPy` enum (Granian-derived awaitable result marshaller). |
| `server.rs` (context) | — | `index()` dispatcher + the const fast-path closure (`server.rs:230-276`, `index` at `:475`). |

The crates that matter: `matchit 0.7.3` (radix-trie router), `dashmap` (Headers), `parking_lot::RwLock` (http/const router tables), `actix-web` / `actix-web-actors` (HTTP + WS), `pyo3` (the whole FFI), `orjson` (Python-side, called from Rust for dict/list responses).

---

## 2. Routing subsystem

### 2.1 The `Router<T,U>` trait (KNOWN, `routers/mod.rs:11-25`)

```rust
pub trait Router<T, U> {
    fn add_route<'py>(&self, py: Python, route_type: &U, route: &str,
                      function: FunctionInfo, event_loop: Option<Bound<'py, PyAny>>) -> Result<()>;
    fn get_route(&self, route_type: &U, route: &str) -> Option<T>;
}
```

Three impls: `HttpRouter` (`T = (FunctionInfo, HashMap<String,String>)`, `U = HttpMethod`), `ConstRouter` (`T = Response`, `U = HttpMethod`), `MiddlewareRouter` (`T = (FunctionInfo, HashMap<String,String>)`, `U = MiddlewareType`). `WebSocketRouter` is **not** a `Router` impl — it's a bespoke struct (`web_socket_router.rs:11`), because WS routes carry three handlers per path, not one.

**(a) Mechanics.** Routing key is `HttpMethod` enum (9 variants, `types/mod.rs:25-35`). One independent trie/map *per method*, so method dispatch is a `HashMap<HttpMethod, …>` lookup before the path match — no method check inside the trie. `HttpMethod` derives `Hash` and has a hand-rolled `__hash__` (`function_info.rs:20-25` for `MiddlewareType`; `HttpMethod` uses the derive). `from_actix_method` (`types/mod.rs:53`) maps actix `Method` → `HttpMethod`, returning `Err("Method Not Allowed")` for anything outside the 9.

**(b) Perf cost.** Per request: one `HashMap<HttpMethod, _>::get` (cheap, 9 entries, enum hash), one `RwLock::read()` (`parking_lot`, uncontended ≈ a few ns), one `matchit::Router::at(path)` (O(path length), no regex, no backtracking). Path-param capture allocates a fresh `HashMap<String,String>` and `to_string()`s every `(key,value)` pair (`http_router.rs:48-52`) — for `/users/:id` that's 2 `String` allocs even though the handler may want an `i64`.

**(c) Steal for Lancelot.** One radix trie per method + `matchit` (or its successor `matchit 0.8`/`path-tree`) is the right call — exact, allocation-free match, predictable O(len). Keep it.

**(d) Avoid.** Path params are eagerly stringified into a `HashMap<String,String>` on *every* matched request even if the handler ignores them, and even for static routes that captured nothing (you still allocate the empty `HashMap`). Also `get_route` does `Python::with_gil(|_| res.value.to_owned())` (`http_router.rs:53`) — it acquires the **GIL just to clone a `FunctionInfo`** (because `FunctionInfo::clone` clones `Py<PyAny>` handles which is refcount work needing the GIL, `function_info.rs:62-71`). That is a GIL acquisition on the routing hot path *before* you even decide to call Python.

**(e) Lancelot reduction.**
- Store path params as `SmallVec<[(Box<str>, Range<usize>); 4]>` indexing into the original path bytes — zero `String` allocs; expose them lazily to Python (a `PathParams` pyclass that slices the still-alive path buffer on `__getitem__`).
- Don't clone the handler under the GIL on every match. Store handlers in an `arc-swap`'d `Vec<Arc<HandlerInfo>>` indexed by a `u32` route id; `get_route` returns `(route_id, params)` with **no GIL, no clone**. Acquire the GIL once, later, only when actually invoking the handler.

### 2.2 `HttpRouter` (KNOWN, `http_router.rs`)

`routes: HashMap<HttpMethod, RwLock<matchit::Router<FunctionInfo>>>`, all 9 methods inserted in `new()` (`:62-74`). `add_route`: `table.write().insert(route.to_string(), function)?` — note `matchit::insert` returns `Result` (duplicate/invalid path errors). `get_route`: `table.read()`, `table_lock.at(route)`, on `Ok` build `route_params: HashMap<String,String>` from `res.params`, then `Python::with_gil(|_| res.value.to_owned())` to clone the `FunctionInfo`, return `Some((function_info, route_params))`. On miss → `None`.

INFERRED: `matchit 0.7.3` `.at()` does not normalize trailing slashes — Robyn strips a single trailing slash itself in `from_actix_request` (`request.rs:166-172`: `if path.ends_with("/") && path.len() > 1 { path = &path[..len-1] }`) *before* routing, so `/x/` and `/x` route the same but the GIL-free fast path in `server.rs:238` uses `req.uri().path()` **without** that normalization — meaning a const route registered as `/health` will *not* be const-served for a request to `/health/` (it falls through to `index()` which strips the slash and then hits the const cache on the slow path at `server.rs:561`). **Lancelot: normalize the path once, before both the fast path and the dispatcher, in a single place.**

### 2.3 `ConstRouter` — the headline optimization (KNOWN, `const_router.rs`)

**Data structures.**
```rust
struct CachedResponse {                       // :25
    status: StatusCode,                        // actix StatusCode (u16 newtype)
    headers: Arc<Vec<(String, String)>>,       // pre-flattened, cheap to clone (Arc bump)
    body: Bytes,                               // actix Bytes — refcounted, clone = bump
    route_header_count: usize,                 // # of headers BEFORE global headers appended
}
struct ConstRouter {                          // :81
    routes: HashMap<HttpMethod, Arc<RwLock<matchit::Router<CachedResponse>>>>,
    fast_routes: HashMap<HttpMethod, Arc<RwLock<HashMap<String, CachedResponse>>>>,
}
```

**Registration-time pre-execution (`add_route`, `:87-127`).** When a route is registered as const, `event_loop` **must** be provided (`.context("Event loop must be provided…")`, `:104`). Robyn then *runs the handler once now*:
```rust
pyo3_async_runtimes::tokio::run_until_complete(event_loop, async move {
    let output = execute_http_function(&Request::default(), &function, None).await.unwrap();   // :107
    match output {
        ResponseType::Standard(response) => {
            let cached = CachedResponse::from_response(&response);
            table.write().insert(route.clone(), cached.clone()).unwrap();   // matchit trie
            fast_table.write().insert(route, cached);                       // exact HashMap
        }
        ResponseType::Streaming(_) => return Err(... "Streaming responses are not supported for const routes")
    }
})
```
The handler receives a `Request::default()` (empty) — so a const handler **must not depend on request data** (it can't see any). The result is captured into *both* tables. The handler is **never invoked again** for the process lifetime.

`CachedResponse::from_response` (`:33-54`): iterate `response.headers.headers` (the `DashMap`), push every `(key.clone(), value.clone())` into a `Vec`; append `set-cookie` per cookie via `cookie.to_header_value(name)`; record `route_header_count = headers.len()`; `status = StatusCode::from_u16(...).unwrap_or(500)`; `body = Bytes::from(response.description.clone())` (one copy of the body, done once at startup, never again).

**Global-header baking (`bake_global_headers`, `:172-199`).** Called from `server.rs:144` **only if `!has_any_middleware()`**. It flattens the global response headers into `extra_headers: Vec<(String,String)>`; if empty, returns early. Otherwise for every method's `fast_routes` map it clones each `CachedResponse.headers` Vec, `.extend()`s the globals, wraps in a fresh `Arc`, then **rebuilds the entire `matchit` trie** from the updated map (`new_router.insert(route, cached)` in a loop, `:192-196`). After this, a const hit needs *zero* per-request header work.

**Lookup (`get_cached_route`, `:202-219`, `#[inline(always)]`).** Try exact `fast_routes.read().get(route)` → clone+return on hit (clone = `Arc` bump + `Bytes` bump + `StatusCode` copy + `usize` copy — **no string allocation**). Miss → `routes.read()`, `router.at(route).ok().map(|m| m.value.clone())` (matchit fallback for parameterized const routes).

**Serving (`to_http_response`, `:56-72`, `#[inline(always)]`).**
```rust
let mut builder = HttpResponseBuilder::new(self.status);
for (k, v) in self.headers.as_ref() { builder.append_header((k.as_str(), v.as_str())); }
builder.body(self.body.clone())     // Bytes clone = refcount bump, ZERO copy
```
`to_http_response_without_global_headers` is identical but `.take(self.route_header_count)` — used for `response_headers_exclude_paths` (e.g. `/docs`, `/openapi.json`) so docs pages don't inherit global headers.

**Where it short-circuits (`server.rs:230-251`).** In the single `default_service` closure, *before* `Request::from_actix_request`, before any GIL:
```rust
if !middleware_router.has_any_middleware() {
    if let Ok(http_method) = HttpMethod::from_actix_method(req.method()) {
        if let Some(cached) = const_router.get_cached_route(&http_method, req.uri().path()) {
            if excluded.contains(path) { return cached.to_http_response_without_global_headers(); }
            return cached.to_http_response();          // <-- never enters Python
        }
    }
}
```
There is a **second** const check inside `index()` (`server.rs:561`) for the case where middleware *is* registered — there the request is fully parsed and middleware runs, but the body of the response still comes from the cache (rebuilt into a fresh `Response`, `:562-573`). So const routes still skip the *handler* even with middleware; they only skip *everything* (parse + GIL + Python) when middleware count is zero.

**(b) Perf cost.** Fast path is: `AtomicBool::load(Acquire)` + `HashMap<HttpMethod>::get` + `RwLock::read` + `HashMap<String,_>::get` + `Arc`/`Bytes` clones + build `HttpResponseBuilder` + append N pre-computed headers + `Bytes::clone` body. No body parse, no UTF-8 validation, no `PyDict`, no `Py::new`, no GIL. This is within a small constant of a hand-written hyper static server.

**(c) Steal for Lancelot — verbatim, then improve.** This is the #1 steal. Keep the two-tier (exact map O(1) + radix fallback), keep pre-rendered `Bytes` body, keep `Arc<Vec<(HeaderName, HeaderValue)>>` headers, keep the `AtomicBool` gate, keep header-baking at startup.

**(d) Avoid / limitations in Robyn's version.**
1. **Const + middleware kills the fast path entirely** (one global middleware ⇒ *every* request, including const, pays full parse + GIL). The `has_any_middleware()` gate is all-or-nothing.
2. **Trailing-slash mismatch** between fast path (`req.uri().path()`, unnormalized) and slow path (normalized) — `/health/` misses the GIL-free fast path (§2.2).
3. Headers stored as `Vec<(String,String)>` → `append_header((k.as_str(), v.as_str()))` re-parses the header name into actix's `HeaderName` **on every request**. Could pre-build `actix_http::header::HeaderMap` once at startup and clone that (HeaderMap clone is cheaper than re-parsing N names).
4. `from_response` clones `response.description` into `Bytes::from(Vec)` — one copy at startup (acceptable, one-time).
5. The matchit trie is **rebuilt from scratch** in `bake_global_headers` (`:192`) — fine at startup, but it means const routes can't be added after global headers are set without another rebuild.

**(e) Lancelot reduction.**
- Pre-build the full `HeaderMap` (typed `HeaderName`/`HeaderValue`) inside `CachedResponse` at startup so the hot path does `builder.headers_mut().extend(cached.headers.iter().cloned())` with **no name re-parse**.
- Gate the fast path per-route, not globally: store on each route whether it has any middleware; a const route with no route-scoped and no global middleware is fast even if *other* routes have middleware. (Track `global_mw_count` separately; if a const route was registered and `global_mw_count == 0` *and* route has no middleware ⇒ fast.)
- Normalize the path once at the top of the closure and feed the normalized form to both the fast path and `index()`.
- Allow `const` to also cache *deterministic* small handlers by hashing inputs (optional `@cache(key=...)` decorator) — a generalization of the same trick to memoized dynamic routes.

### 2.4 `MiddlewareRouter` (KNOWN, `middleware_router.rs`)

`globals: HashMap<MiddlewareType, RwLock<Vec<FunctionInfo>>>` (Before/After), `routes: HashMap<MiddlewareType, RwLock<matchit::Router<FunctionInfo>>>` (per-endpoint), `has_middleware: AtomicBool`. `add_route` and `add_global_middleware` both `self.has_middleware.store(true, Ordering::Release)`. `has_any_middleware()` = `load(Acquire)`. `get_global_middlewares` clones the whole `Vec<FunctionInfo>` (`.to_vec()`, `:99`) **per request** even when empty-after-clone is needed — that's N `Py<PyAny>` refcount clones under no GIL guard… actually `FunctionInfo::clone` itself acquires the GIL (`function_info.rs:63`), so `to_vec()` on a non-empty middleware list acquires+releases the GIL once per `clone()`. **Avoid:** store middlewares as `Arc<[Arc<FunctionInfo>]>` and clone the `Arc`, not the contents — zero GIL, zero refcount churn. Lancelot must not GIL-clone handler lists on the hot path.

Note `middleware_router.rs` uses `std::sync::RwLock` (not `parking_lot`) — inconsistent with the other routers; `.write().unwrap()` can poison. Lancelot: use `parking_lot` or `arc-swap` uniformly; never `unwrap()` a poisoned lock on the request path.

### 2.5 `WebSocketRouter` (KNOWN, `web_socket_router.rs`)

`web_socket_routes: RwLock<HashMap<String, HashMap<String, FunctionInfo>>>` — outer key = path, inner key ∈ {`"connect"`,`"close"`,`"message"`} (stringly-typed, `:46-48`). `add_websocket_route` inserts all three via a closure. There is **no `matchit` for WS** — WS paths are exact-string only (no `:id` params on WS routes in Robyn). The whole inner `HashMap<String,FunctionInfo>` is cloned into each `WebSocketConnector` at connect (`websockets/mod.rs:312, :333`). Lancelot: an enum `WsPhase { Connect, Close, Message }` instead of `&str` keys; allow path params on WS routes (agent chat backends frequently want `/ws/conversations/:id`).

---

## 3. Request marshalling — the FFI cost, quantified (KNOWN unless noted)

This is the section that matters most for Lancelot. We trace one ordinary `GET /api/thing?q=1` with 12 request headers and no body, and count every allocation/copy before the handler runs.

### 3.1 The `Request` struct and `PyRequest` pyclass (`request.rs:16-30, 196-219`)

```rust
#[derive(Default, Debug, Clone, FromPyObject)]
struct Request {
    query_params: QueryParams,             // HashMap<String, Vec<String>>
    headers: Headers,                       // DashMap<String, Vec<String>>
    method: String,
    path_params: HashMap<String, String>,
    #[pyo3(from_py_with = get_body_from_pyobject)]
    body: Vec<u8>,
    url: Url,                               // {scheme:String, host:String, path:String}
    ip_addr: Option<String>,
    identity: Option<Identity>,
    form_data: Option<HashMap<String, String>>,
    files: Option<HashMap<String, Vec<u8>>>,
}
#[pyclass(name="Request")] #[derive(Clone)]
struct PyRequest {
    query_params: QueryParams,              // #[pyo3(get,set)]
    headers: Py<Headers>,                   // a SECOND Headers object
    path_params: Py<PyDict>,                // dict, even if empty
    identity: Option<Identity>,
    body: Py<PyAny>,                        // str or bytes
    method: String,
    url: Url,
    ip_addr: Option<String>,
    form_data: Py<PyDict>,                  // dict, even if no form
    files: Py<PyDict>,                      // dict, even if no files
}
```
Two representations of the same data; the Rust `Request` is built first, then *fully re-materialized* into `PyRequest`.

### 3.2 Step 1 — `Request::from_actix_request` (`request.rs:122-193`)

```rust
let mut query_params = QueryParams::new();          // HashMap alloc
let mut form_data = HashMap::new();                  // alloc (often unused)
let mut files = HashMap::new();                      // alloc (often unused)

if !req.query_string().is_empty() {                  // "q=1"
    for s in req.query_string().split('&') {
        let (k,v) = s.split_once('=').unwrap_or((s,""));
        query_params.set(k.to_string(), v.to_string());  // 2 String allocs PER pair
    }
}

let mut headers = Headers::from_actix_headers(req.headers());   // see 3.3 — heavy
headers.extend(global_headers);                                  // re-clone every global header

let body: Vec<u8> = if content-type contains "multipart/form-data" {
    // stream multipart, fill files/form_data/body  (request.rs:89-120)
} else {
    let mut body_local = BytesMut::new();
    while let Some(chunk) = payload.next().await {               // FULLY BUFFER body
        body_local.extend_from_slice(&chunk);
    }
    body_local.freeze().to_vec()                                  // freeze THEN .to_vec() = COPY
};

let route_path = strip one trailing slash;
let url = Url::new(scheme, host, route_path);     // 3 String allocs (scheme,host,path)
let ip_addr = req.peer_addr().map(|v| v.ip().to_string());        // String alloc

Ok(Self { query_params, headers, method: req.method().as_str().to_owned(), // String
          path_params: HashMap::new(),  // ANOTHER empty HashMap alloc
          body, url, ip_addr, identity: None,
          form_data: Some(form_data), files: Some(files) })
```

**Cost, counted (empty-body GET, 12 headers, 1 query pair, no globals):**
- `QueryParams::new()` → 1 `HashMap` alloc; per query pair: 2 `String` + 1 `Vec<String>` (in `multimap.rs:24` `entry().or_default().push`) ≈ 3 allocs.
- `form_data`, `files` HashMaps → 2 allocs (unused for this request).
- `Headers::from_actix_headers` → see §3.3: `DashMap::default()` (alloc + shard array) + per header: `key.to_string().to_lowercase()` (2 allocs: to_string + to_lowercase produces a new String) + `value.to_str()...to_string()` (1 alloc) + `entry().or_default()` (`Vec<String>` alloc on first) + push. ≈ **~4 allocs × 12 headers ≈ 48 allocs/copies**.
- body: `BytesMut::new()` then for empty body the loop doesn't run, `freeze().to_vec()` → empty `Vec` (cheap) — **but for a non-empty body it is `extend_from_slice` into `BytesMut` (copy #1) then `.to_vec()` (copy #2): the body is copied twice in Rust before it ever reaches Python.**
- `Url::new` → 3 `String` allocs (scheme, host, path each `.to_string()`).
- `method.to_owned()` → 1 `String`.
- `ip_addr` → 1 `String` (`SocketAddr::ip().to_string()`).
- `path_params: HashMap::new()` → 1 alloc (always, even for static routes).

**Subtotal before crossing into Python: ≈ 60 allocations / string copies for a trivial GET**, *none of which the handler may use*. And the **entire request body is buffered in memory** before the handler is called — no streaming/chunked request bodies, no backpressure to the handler. For a 10 MB upload that's a 10 MB `BytesMut` + a 10 MB `Vec<u8>` copy = 20 MB resident before Python sees byte zero (`request.rs:158-163`).

### 3.3 Step 1b — `Headers::from_actix_headers` (`headers.rs:192-205`) — the worst offender

```rust
let headers = Headers::default();                  // DashMap::new() — shard array alloc
for (key, value) in req_headers {
    let key = key.to_string().to_lowercase();      // to_string() -> String, to_lowercase() -> ANOTHER String
    let value = match value.to_str() { Ok(v)=>v.to_string(), Err(_)=>continue };  // String
    headers.headers.entry(key).or_default().push(value);   // Vec<String> alloc + push
}
```
`DashMap<String, Vec<String>>` (`headers.rs:11`). Every single request header name is allocated **twice** (`to_string` then `to_lowercase` allocates a fresh `String`), the value once, plus a `Vec<String>` per distinct header. Then `headers.extend(global_headers)` (`request.rs:144`) iterates the global `Headers` DashMap and **clones every global key + every global value again** (`headers.rs:172-178`: `key.clone()` + `values.iter().cloned()`).

**This is pure overhead for the common case** where the handler reads zero or one header. Lancelot must not do this.

### 3.4 Step 2 — `Request::into_pyobject` (`request.rs:32-87`) — building `PyRequest`

This runs **under the GIL** (called from `get_function_output` while the GIL is held). For our GET:

```rust
let headers: Py<Headers> = self.headers.into_pyobject(py)?.extract()?;   // wrap Headers as pyclass (Py::new alloc)
let path_params = self.path_params.into_pyobject(py)?.extract()?;        // HashMap -> PyDict (empty dict alloc)
let body = if empty { PyString::new(py,"") }                            // empty PyString object
           else { match str::from_utf8(&body) { Ok(s)=>PyString::new(py,s)   // UTF-8 validate + COPY into PyString
                                                  Err(_)=>PyBytes::new(py,&body) } }; // COPY into PyBytes
let form_data: Py<PyDict> = match self.form_data {
    Some(d) if !d.is_empty() => { let dict=PyDict::new(py); for (k,v) in d { dict.set_item(k,v)?; } dict.into() }
    _ => PyDict::new(py).into()                                           // ALWAYS alloc an empty PyDict
};
let files: Py<PyDict> = ... same pattern ...                              // ALWAYS alloc an empty PyDict
let request = PyRequest { query_params: self.query_params, path_params, headers,
                          body: body.into(), method: self.method, url: self.url,
                          ip_addr, identity, form_data, files };
Ok(Py::new(py, request)?.into_bound(py).into_any())                       // heap-alloc the pyclass
```

**GIL-held cost for the trivial GET:**
- `self.headers.into_pyobject(py)` — `Headers` derives the pyclass conversion; constructs a `Py<Headers>` = one CPython object allocation wrapping the **already-built** `DashMap` (the DashMap is *moved*, good — but it's a *second* Headers object distinct from any global). 1 obj alloc.
- `path_params` `HashMap` → `PyDict`: even empty, `PyDict::new(py)` = 1 object alloc.
- `body`: empty → `PyString::new(py,"")` = 1 obj. Non-empty UTF-8 → `str::from_utf8` (validate, O(n)) + `PyString::new` copies the whole body into a freshly allocated CPython str object (**copy #3** of the body; #1 BytesMut, #2 Vec, #3 PyString). Binary → `PyBytes::new` (copy #3).
- `form_data` → `PyDict::new(py)` **always allocated even with no form** = 1 obj alloc.
- `files` → `PyDict::new(py)` **always allocated even with no files** = 1 obj alloc.
- `Py::new(py, request)` → heap-allocate the `PyRequest` pyclass instance = 1 obj alloc.
- `query_params` (`QueryParams` pyclass) moved into `PyRequest` — when the handler accesses `request.query_params` it's the pyclass; cheap here.

So `into_pyobject` adds **≥6 CPython object allocations** (Headers, path_params dict, body str/bytes, form_data dict, files dict, PyRequest) for a request that uses none of them. Combined with §3.2/§3.3 the **all-in per-request marshalling for a trivial GET is ~60 Rust allocs + ~6 CPython object allocs + 1–3 full body copies**, all eager, all before the handler's first statement.

### 3.5 Step 3 — argument dispatch `get_function_output` (`executors/mod.rs:155-199`)

```rust
let handler = function.handler.bind(py).downcast()?;
if function.number_of_params == 0 { return handler.call0(); }              // 0-arg: SKIPS into_pyobject entirely
let kwargs = function.kwargs.bind(py);
let function_args = function_args.clone().into_pyobject(py)?...unbind();    // <-- Request::into_pyobject happens HERE
match function.number_of_params {
    1 => if kwargs has global_dependencies|router_dependencies { handler.call((), Some(kwargs)) }
         else { handler.call1((function_args,)) },
    _ => handler.call((function_args,), Some(kwargs)),
}
```

**Critical detail (KNOWN, the one real existing optimization):** `if function.number_of_params == 0 { return handler.call0(); }` (`:168`) — **a handler declared with zero parameters skips `Request::into_pyobject` completely.** `function_args.clone().into_pyobject(py)` is only called for handlers that take ≥1 arg. So `def index(): return "ok"` pays *none* of §3.4 (but still pays §3.2/§3.3 because `from_actix_request` ran before dispatch in `index()`). This is the key lever: **the cost is gated on handler arity, not on what the handler uses.** A handler that takes `request` pays for *all* of it even if it reads one header.

Also note `function_args.clone()` (`:174`) — `Request` derives `Clone`, and `Request::clone` deep-clones the `DashMap`, both `HashMap`s, the body `Vec<u8>`, etc. **The entire Request is cloned before `into_pyobject` consumes it.** For a request with a body that's a full body copy *again* (copy #2.5). This clone is pure waste — `into_pyobject` takes `self` by value; the clone exists only because `get_function_output` borrows `function_args: &T` and calls `.clone()`.

### 3.6 The `json()` method (`request.rs:258-317`)

`request.json()` downcasts `self.body` to `PyString`, `serde_json::from_str`, then `json_value_to_py` recursively rebuilds the parsed value as Python objects with a depth guard `MAX_JSON_DEPTH = 128` (`:271`). So JSON crosses the boundary **twice**: bytes→PyString (in `into_pyobject`), then PyString→Rust `&str`→`serde_json::Value`→Python objects. That's parse + full rebuild of a Python object tree. Lancelot: parse JSON in Rust once into a typed model (Pydantic-style) and hand the handler the typed object directly, never the round-trip.

### 3.7 Summary table — per-request FFI cost (KNOWN, counted above)

| Phase | What | Allocs/copies (trivial GET) | Skippable? |
|---|---|---|---|
| query parse | `split('&')`, 2 `String`/pair | ~3 | only if no query string |
| `from_actix_headers` | 2 String/name + 1/value + Vec/header | ~48 (12 hdrs) | **never** (always runs in `index()`) |
| `headers.extend(global)` | clone every global k+v | 0 here / 2·G | only if no global headers |
| body buffer | `BytesMut` + `.to_vec()` | 0 (empty) / 2·N bytes | **never** (eager, full buffer) |
| `Url::new` | 3 `String` | 3 | never |
| method/ip/path_params | 2 `String` + empty `HashMap` | 4 | never |
| `Request::clone` (in executor) | deep clone whole struct + body | ~all of the above again + N | **wasteful, removable** |
| `into_pyobject` | Headers obj, 3 empty `PyDict`, body str/bytes, PyRequest | ≥6 CPython objs + body copy | **only if 0-arg handler** |

The dominant, non-skippable costs are **(1) header marshalling into a DashMap, (2) full body buffering, (3) the redundant `Request::clone`, (4) eager `PyRequest` construction**. Lancelot's design (§9) attacks all four.

---

## 4. Response marshalling — handler return → wire (KNOWN, `executors/mod.rs:78-153`, `response.rs`)

### 4.1 The fast-path extraction chain (`extract_response_type_fast`, `executors/mod.rs:86-153`)

Order matters; comment in source is verbatim. Given the handler's return value `output: Bound<PyAny>`:

1. **`output.downcast_exact::<PyResponse>()`** (`:93`) — exact pyclass downcast (NOT `downcast`, so user `Response` subclasses fall to step 5). Reads fields directly off the Rust struct: `get_description_from_pyobject(borrowed.description)` (str/bytes → `Vec<u8>`, **one copy**), `borrowed.headers.borrow(py).clone()` (clone the `Headers` DashMap), `borrowed.cookies.borrow(py).clone()`. Zero `getattr`. → `ResponseType::Standard(Response{...})`.
2. **`output.downcast::<PyDict>() || downcast::<PyList>()`** (`:113`) — bare dict/list: call cached `orjson.dumps` (`ORJSON_DUMPS` `PyOnceLock`, `:26-34`) → `PyBytes` → `.as_bytes().to_vec()` (copy) → `Response` with `content-type: application/json`. **orjson is invoked from Rust**, not Python-side.
3. **`output.downcast::<PyString>()`** (`:125`) → `.to_string().into_bytes()` (copy) → `text/plain`.
4. **`output.downcast::<PyBytes>()`** (`:132`) → `.as_bytes().to_vec()` (copy) → `application/octet-stream`.
5. **`output.extract::<StreamingResponse>()`** (`:141`) — duck-typed (`hasattr content`+`media_type`, `response.rs:432`).
6. **`output.extract::<Response>()`** (`:147`) — slow `FromPyObject` getattr chain (`response.rs:396-425`): 6 `getattr` calls (`status_code`, `response_type`, `headers`, `description`, `file_path`, `cookies`) + type-name string check `if type_name != "Response"`. For user subclasses / `FileResponse`.

**(b) Perf cost.** Best case (return a `Response` object): one `downcast_exact` (a pointer/type-id check, ~free) + one body copy + two DashMap clones. Returning a raw `dict`: an `orjson.dumps` FFI call + a `PyBytes`→`Vec` copy (the JSON bytes are copied out of the Python bytes object). Returning `str`: one copy. Worst (subclass): 6 `getattr`s + a type-name allocation+compare.

**(c) Steal for Lancelot.** The ordered fast-path chain with `downcast_exact` first and an in-Rust cached `orjson.dumps` is excellent — keep it. Caching the `orjson.dumps` callable in a `PyOnceLock` (`:26`) avoids re-importing per request.

**(d) Avoid.** Every text/bytes/json path does a **copy out of the Python object into a `Vec<u8>`** then later that `Vec<u8>` is handed to actix as the body (another move, plus `Bytes::from`). The `getattr`-based slow path allocates a `String` for the type name on every subclass response. `borrowed.headers.borrow(py).clone()` clones the entire header DashMap per response.

**(e) Lancelot reduction.** For `str`/`bytes` responses, wrap the Python buffer **without copying**: hold the `Py<PyBytes>`/`Py<PyString>` alive and implement actix `MessageBody` over a type that borrows the CPython buffer (via `PyBuffer`/`as_bytes`) and keeps the `Py` alive until the body is fully written — true zero-copy response for the most common case (return a JSON string from orjson, send those exact bytes on the socket). Don't clone the header map; move it or wrap it.

### 4.2 `Response` / `Responder` (`response.rs:62-85`)

`Response::respond_to`: `HttpResponseBuilder::new(StatusCode::from_u16(code).unwrap_or(500))`, `apply_hashmap_headers` (`io_helpers/mod.rs:12-19`: iterate DashMap, `append_header((key.clone(), value.clone()))` — **clone every header name+value again** when emitting), emit `Set-Cookie` per cookie (`cookie.to_header_value` builds via actix cookie crate, allocates a `String`), `response_builder.body(self.description)` (moves the `Vec<u8>`). So a response's headers are cloned at least: once into `Response.headers` (from handler), once in `apply_hashmap_headers` when serializing. Lancelot: build the actix `HeaderMap` directly, no intermediate DashMap, no re-clone at serialize time.

### 4.3 Streaming responses — the agent/chat angle (KNOWN, `response.rs:87-148`)

```rust
impl Responder for StreamingResponse {
  fn respond_to(self, _req) -> HttpResponse {
    let mut b = HttpResponseBuilder::new(status);
    apply_hashmap_headers(&mut b, &self.headers);
    b.append_header(("Connection","keep-alive"))
     .append_header(("X-Accel-Buffering","no"))                    // disable nginx buffering
     .append_header(("Cache-Control","no-cache, no-store, must-revalidate"))
     .append_header(("Pragma","no-cache")).append_header(("Expires","0"));
    let stream = create_python_stream(self.content_generator);     // :116
    b.streaming(stream)
  }
}
fn create_python_stream(generator: Py<PyAny>) -> Pin<Box<dyn Stream<Item=Result<Bytes,Error>>+Send>> {
  Box::pin(futures::stream::unfold(generator, |generator| async move {
    match tokio::task::spawn_blocking(move || {                    // <-- each chunk on a BLOCKING thread
      Python::with_gil(|py| {
        let gen = generator.bind(py);
        match gen.call_method0("__next__") {                        // pull one item
          Ok(value) => value.extract::<String>().ok().map(|s| (s, generator)),
          Err(e) => { if !e.is_instance_of::<PyStopIteration>(py) { log::error!(...) } None }
        }
      })
    }).await {
      Ok(Some((s, gen))) => Some((Ok(Bytes::from(s)), gen)),         // String -> Bytes (copy)
      _ => None,
    }
  }))
}
```

**(a) Mechanics.** A `StreamingResponse` wraps a Python **generator** (`content_generator: Py<PyAny>`, `response.rs:33`). Robyn drives it with `futures::stream::unfold`: each poll does `spawn_blocking(|| Python::with_gil(|py| gen.call_method0("__next__")))`, extracts a `String`, converts to `Bytes`, yields it; `StopIteration` ⇒ stream ends. SSE headers are forced (`X-Accel-Buffering: no` defeats nginx buffering — essential for token streaming). `PyStreamingResponse::new` (`response.rs:259-300`) defaults `media_type="text/event-stream"` and sets `Content-Type/Cache-Control/Connection` for SSE.

**(b) Perf cost.** Each chunk: a `spawn_blocking` hop (moves work to tokio's blocking pool, ~µs scheduling), a GIL acquire/release, a `__next__` call, a `String` extract (copy out of Python), `Bytes::from(String)` (move, no copy). For LLM token streaming (one token per yield) that's a `spawn_blocking` + GIL cycle **per token** — high per-token overhead but it does *not* block the actix worker (the worker is free while the blocking pool pulls). It also only supports **sync generators** here (`call_method0("__next__")`); `__anext__`/async generators are not handled in `create_python_stream`.

**(c) Steal for Lancelot.** The forced SSE header set (esp. `X-Accel-Buffering: no`) is exactly right for agent/chat token streaming behind nginx — keep verbatim. Driving a Python generator as a Rust `Stream` via `unfold` is the right shape.

**(d) Avoid.** Per-token `spawn_blocking` + GIL is heavy when tokens are tiny and frequent. Async generators unsupported. Each yielded chunk must be a `str` (no `bytes` chunks → can't stream binary/SSE with raw bytes). One GIL acquisition per chunk serializes all streaming responses against each other and against request handling on the GIL.

**(e) Lancelot reduction.**
- Support **async generators**: detect `__aiter__`/`__anext__`, drive via `pyo3_async_runtimes::tokio::into_future` so the coroutine runs on the asyncio loop (no `spawn_blocking`, no per-token thread hop), GIL held only to schedule/await.
- **Batch tokens**: coalesce N `__next__`/`__anext__` results under a single GIL acquisition (pull until the generator would block or a small batch size / time budget is hit), reducing GIL cycles ~Nx for token streams.
- Accept `bytes` and `str` chunks (and SSE `data:`-framed objects); zero-copy the `bytes` payload into the response body via a `Py`-keep-alive `MessageBody`.
- This is *the* hot path for AI backends (every chat completion is a stream) — it deserves a dedicated, async-native, batched implementation, not Robyn's sync-generator + per-token-spawn_blocking design.

---

## 5. Handler execution model — sync vs async (KNOWN, `executors/mod.rs:518-545`)

```rust
pub async fn execute_http_function(request:&Request, function:&FunctionInfo, context:Option<&Py<PyAny>>)
  -> PyResult<ResponseType>
{
  if function.is_async {
    let output = Python::with_gil(|py| {                                  // GIL #1
      let coroutine = get_function_output(function, py, request)?;        // call handler -> coroutine
      let awaitable = match context { Some(ctx)=>wrap_coro_in_context(py,ctx.bind(py),coroutine)?,
                                       None=>coroutine };
      pyo3_async_runtimes::tokio::into_future(awaitable)                   // coroutine -> Rust Future
    })?.await?;                                                            // GIL RELEASED for the await
    Python::with_gil(|py| extract_response_type(output, py))               // GIL #2: marshal result
  } else {
    Python::with_gil(|py| {                                               // GIL held for WHOLE sync handler
      let output = match context { Some(ctx)=>get_function_output_in_context(function,py,ctx.bind(py),request)?,
                                    None=>get_function_output(function,py,request)? };
      extract_response_type_bound(output)
    })
  }
}
```

**(a) Mechanics.** Async handler: GIL acquired only to (i) call the handler and turn the returned coroutine into a Rust future, then **released for the entire `await`**, then re-acquired to marshal the result. This is the crux of "non-blocking async Python": while coroutine A awaits I/O, the actix worker runs other work and the GIL is free. Sync handler: GIL held for the **entire** handler execution — including the arg `into_pyobject` and result extraction — all under one `Python::with_gil`.

**(b) Perf cost / the big finding.** **There is NO thread-pool offload for sync HTTP handlers.** The sync branch (`:537-544`) runs the handler inline under `Python::with_gil` on the actix worker future. `blocking.rs` has a `BlockingRunner`/`BlockingRunnerPool` (Granian-derived) and `runtime.rs` wires it for the awaitable machinery, but **`execute_http_function`'s sync path never calls `spawn_blocking`/`BlockingRunner`**. (KNOWN: the sync branch has no offload; the docs claim "Sync Handlers: Executed in a thread pool" — that is **inaccurate for the HTTP path**, INFERRED from the absence.) Consequence: a synchronous blocking handler (`requests.get`, sync `openai`, a slow CPU loop) **holds the GIL and blocks that worker** for its full duration; concurrency for sync handlers comes only from `--processes` (multiple OS processes, each its own GIL) and `--workers`.

`get_function_output_in_context` (`:207-250`) wraps the call in `ctx.run(handler, args)` so per-request `contextvars` writes land in a shared `Context` created once per request in `index()` (`server.rs:515`, `crate::asyncio::new_context`). Async handlers are wrapped via `wrap_coro_in_context` → `loop.create_task(coro, context=ctx)` so ContextVar writes persist across before/handler/after phases (issue #1380 fix).

**(c) Steal for Lancelot.** The "GIL only at the crossings, released across the await" pattern is correct and must be kept. Per-request `contextvars.Context` shared across middleware+handler is a clean way to make `ContextVar` (request id, auth) work — keep it. Caching `orjson.dumps`, `run_in_context_helper`, the asyncio context helper in `PyOnceLock` is the right pattern for any hot Python callable.

**(d) Avoid.** Sync handlers blocking the worker with no offload is a correctness/perf trap (a single sync handler doing I/O tanks an entire worker's throughput). The `Request::clone()` inside `get_function_output` (§3.5) is wasted work on every ≥1-arg call.

**(e) Lancelot reduction.**
- **Offload sync handlers to a bounded blocking pool by default** (`tokio::task::spawn_blocking` or a dedicated GIL-thread pool à la Granian's `BlockingRunner`), so a blocking handler can't starve the async worker. Detect `async def` vs `def` (Robyn already records `is_async` in `FunctionInfo`) and route accordingly.
- Remove the `Request::clone()` — pass the lazy request proxy by `Py` handle (it's already a CPython object), no Rust-side deep clone.
- Keep GIL released across `await`; additionally release the GIL across the *body read* (make body read async and lazy, §9) so request parsing doesn't hold the GIL at all.

---

## 6. Supporting pyclass types (KNOWN)

### 6.1 `Headers` — `DashMap<String, Vec<String>>` (`headers.rs`)
Case-folded keys (`.to_lowercase()` everywhere: `set` `:43`, `get` `:70`, `contains` `:98`). `get` returns the **last** value (`:73`). `get_all` builds a `PyList` of every value. `get_headers` builds a `PyDict` of `key -> PyList[values]`. `from_actix_headers` is the heavy ingest path (§3.3). `extend` (`:172`) clones k+v; `set_missing` (`:183`) only inserts keys not present (used so middleware headers beat global defaults — `server.rs:598`). `__eq__` is O(n·m) (`values.iter().all(|v| other.contains(v))`, `:135`). DashMap is sharded for concurrent access — but a single request's headers are not concurrently accessed, so DashMap's sharding/atomic overhead is **pure cost** here vs a plain `Vec<(HeaderName,HeaderValue)>`.
**Lancelot:** request headers → borrow actix's already-parsed `HeaderMap` (no copy, typed, case-insensitive by spec); response headers → small `Vec<(HeaderName,HeaderValue)>` or build actix `HeaderMap` directly. Reserve a concurrent map only for genuinely shared state, never per-request headers.

### 6.2 `QueryParams` — `HashMap<String, Vec<String>>` (`multimap.rs`)
`set` appends (multi-value), `get` returns last, `get_first` returns first, `get_all` clones the `Vec`, `to_dict` builds `PyDict[str, PyList]`. `from_py_dict` (`:91`) `unwrap()`s extraction — a non-string value panics. Lazy-parse from the raw query string on first access in Lancelot; don't pre-split every request.

### 6.3 `Cookie` / `Cookies` (`cookie.rs`)
`Cookie` pyclass with RFC-6265 attrs; `to_header_value` (`:114-142`) delegates to actix's `cookie` crate builder (handles validation/encoding). `Cookie::deleted()` = `max_age=0` (`:86`). `expires` is **accepted but ignored** in serialization (only `max_age` used, `:128-130`) — API-compat cruft. `Cookies` is a `HashMap<String,Cookie>` with `__iter__`/`__getitem__`/`__contains__`. Lancelot: keep actix-cookie delegation; don't carry dead `expires`.

### 6.4 `FunctionInfo` (`function_info.rs`)
`{ handler: Py<PyAny>, is_async: bool, number_of_params: u8, args: Py<PyDict>, kwargs: Py<PyDict> }`. **`Clone` acquires the GIL** (`:62-71`, `Python::with_gil(|py| handler.clone_ref(py)...)`) — this is why router `get_route` paths and middleware `to_vec()` acquire the GIL just to clone handler metadata. `number_of_params` drives the arg-passing branch (0 ⇒ `call0`, 1 ⇒ `call1`, else ⇒ kwargs). Lancelot: wrap handler+meta in `Arc<HandlerInfo>` so clone = `Arc` bump, no GIL; resolve arity once at registration.

### 6.5 `Identity` / `Url` / `HttpMethod` (`identity.rs`, `types/mod.rs`)
`Identity{claims:HashMap<String,String>}` (auth middleware writes it onto the request). `Url{scheme,host,path}` — 3 owned `String`s built per request (`request.rs:174`). `HttpMethod` 9-variant enum with `Display` and `from_actix_method`. All cheap except `Url`'s 3 allocations/request — Lancelot can borrow scheme/host/path as `&str` slices off the live request via a lazy `url` proxy.

---

## 7. WebSockets — per-connection state and message crossing (KNOWN, `websockets/`)

### 7.1 Actor model (`websockets/mod.rs`)
Each WS connection is an actix actor `WebSocketConnector` (`:53`):
```rust
struct WebSocketConnector {
  id: Uuid,
  router: HashMap<String, FunctionInfo>,           // {connect,close,message} for THIS path (cloned in)
  task_locals: TaskLocals,                          // asyncio loop binding
  registry_addr: Addr<WebSocketRegistry>,           // per-endpoint registry actor
  query_params: QueryParams,
  message_sender: Option<mpsc::UnboundedSender<Option<String>>>,   // Actix side
  message_channel: Option<Py<WebSocketChannel>>,                    // Python side
}
```
`start_web_socket` (`:309-347`) parses query params (same `split('&')` loop as HTTP), gets/creates a per-**endpoint** `WebSocketRegistry` actor (`get_or_init_registry_for_endpoint`, `:289`, a global `OnceCell<RwLock<HashMap<endpoint, Addr>>>`), and starts the actor with `ws::WsResponseBuilder::new(...).frame_size(max_frame_size).start()`.

### 7.2 The Rust→Python message channel (`WebSocketChannel`, `:30-49`)
On `started()` (`:69-96`): register with the registry, create a `tokio::sync::mpsc::unbounded_channel::<Option<String>>()`, keep the `Sender` in the actor, wrap the `Receiver` in `Arc<Mutex<>>` inside a `WebSocketChannel` pyclass exposed to Python. Python handlers do `await channel.receive()` → `pyo3_async_runtimes::tokio::future_into_py(py, async { rx.lock().await.recv().await })` returning the message `str` or `None` on close (`:39-48`). Incoming frames: `StreamHandler::handle` (`:142-183`) — `Text(t)` → `sender.send(Some(t.to_string()))`; `Binary` → UTF-8 try → send as text else echo back; `Close` → drop sender (so `receive()` returns `None`) + `ctx.stop()`; `Ping`→`pong`. So **every inbound WS message crosses the boundary as a `String` over an unbounded mpsc channel**, pulled into Python via an awaitable — clean, non-blocking, but `unbounded` = no backpressure (a fast client can OOM the channel) and binary frames are coerced to UTF-8 strings (no raw-bytes path to Python).

### 7.3 Handler dispatch (`web_socket_executors.rs`)
`execute_ws_function` (`:9-68`): async handler → `handler.call1((ws.clone(),))` → `into_future_with_locals` → spawn as an actix actor future (`.into_actor(ws).map(|res,_,ctx| if Some(msg)=ctx.text(msg))`), so a handler returning a `str` auto-sends it. Sync handler → call inline under GIL, send returned `Option<String>` via `ctx.text`. `connect`/`close` handlers fire from `Actor::started`/`stopped` (`:90-104`). **`ws.clone()` on every handler call** clones the whole `WebSocketConnector` (incl. `task_locals.clone_ref` under GIL, `:111-124`) — heavy per-invocation.

### 7.4 Registry / broadcast (`registry.rs`)
`WebSocketRegistry` is an actix `SystemService`/`Supervised` actor holding `clients: HashMap<Uuid, Addr<WebSocketConnector>>`. Messages: `Register`, `SendText{recipient_id}`, `SendMessageToAll{sender_id}` (iterates all clients, `do_send` to each — O(n) per broadcast, clones the message `String` per client, `:91-99`), `Close` (sends `"Connection closed"` sentinel then removes). One registry **per endpoint** (`get_or_init_registry_for_endpoint`) so broadcasts are endpoint-scoped. From Python: `sync_send_to`/`async_send_to`/`sync_broadcast`/`async_broadcast`/`close`/`get_id`/`get_query_params`/`get_message_channel` on `WebSocketConnector`.

### 7.5 Steal / avoid / Lancelot
- **Steal:** the `Rust mpsc → WebSocketChannel pyclass → await channel.receive()` pattern is *exactly* the right shape for agent/chat backends — the Python handler is a clean `async def` loop `while (msg := await channel.receive()) is not None:` with no callback spaghetti. Per-endpoint registry for scoped broadcast is good for chat rooms.
- **Avoid:** unbounded channel (no backpressure — a chat client streaming fast can OOM); binary frames forced to UTF-8 (can't send protobuf/audio frames to an agent backend); `ws.clone()` (full connector clone incl. GIL) per handler call; `String`-per-client clone on broadcast.
- **Lancelot:** bounded channel with explicit backpressure policy; `enum WsFrame { Text(String), Binary(Bytes) }` across the boundary (audio/agents need bytes); pass the connector by `Arc`/handle not by clone; broadcast with `Arc<str>`/`Arc<[u8]>` shared payload (one alloc, N refcount bumps); allow path params on WS routes (`/ws/chat/:room`); support async handler returning an async generator to *stream* into the socket (token streaming over WS for chat).

---

## 8. Cross-cutting RE findings (KNOWN/INFERRED)

1. **`Request::clone` on every ≥1-arg request** (`executors/mod.rs:174`) — deep-clones DashMap + 2 HashMaps + body Vec; pure waste. INFERRED-removable.
2. **Headers ingested twice** — once `from_actix_headers` (DashMap build), `extend(global)`, then in `into_pyobject` wrapped as a second `Py<Headers>`; on the response side cloned again in `apply_hashmap_headers`. The same header data is allocated/copied 3–4×.
3. **Body copied up to 3×** before the handler sees it: `BytesMut` (copy 1) → `.to_vec()` (copy 2) → `Request::clone` (copy 2.5) → `PyString/PyBytes::new` (copy 3). KNOWN from `request.rs:158-163` + `executors/mod.rs:174` + `request.rs:43-47`.
4. **Empty `PyDict`s always allocated** for `path_params`, `form_data`, `files` even when unused (`request.rs:49-71`, `:38`). 3 CPython object allocs/request, always.
5. **No sync-handler offload in the HTTP path** (§5b) — sync handler blocks the worker + holds GIL; docs claim otherwise.
6. **Trailing-slash inconsistency** between GIL-free fast path and `index()` (§2.2) — defeats the const fast path for `/route/`.
7. **GIL acquired on the routing hot path** just to clone `FunctionInfo` (`http_router.rs:53`, `middleware_router.rs:51`, `:99`) — before any decision to call Python.
8. **`std::sync::RwLock` in middleware_router** vs `parking_lot` elsewhere — poisoning risk + slower; inconsistent.
9. **Const + any middleware ⇒ no fast path for anyone** (all-or-nothing `AtomicBool` gate).
10. **Streaming = sync generators only, per-token `spawn_blocking`+GIL** (§4.3) — wrong cost profile for LLM token streams.

---

## 9. Lancelot proposal — minimize per-request FFI cost (DESIGN)

**Thesis:** Robyn's per-request cost is dominated by *eager total marshalling* — it builds the entire `Request` (headers DashMap, buffered body, query map, 3 empty dicts, a cloned copy, then a pyclass) before the handler runs, regardless of what the handler reads. Lancelot's win is **laziness + zero-copy + arity-aware skipping**.

### 9.1 Lazy `Request` proxy pyclass
Replace eager `Request` + `into_pyobject` with a single pyclass that **holds the live actix `HttpRequest` and an un-consumed payload handle**, and materializes fields on attribute access:

```rust
#[pyclass] struct Request {
    req: actix_web::HttpRequest,                 // borrows parsed method/headers/uri — already in memory
    body: OnceCell<Bytes>,                       // filled lazily on first .body()/.json() (async read)
    body_rx: Mutex<Option<web::Payload>>,        // consumed at most once
    path_params: SmallVec<[(Box<str>, Box<str>); 4]>,  // moved from matchit, no PyDict until accessed
    json_cache: OnceCell<Py<PyAny>>,             // memoize parsed JSON
}
#[pymethods] impl Request {
    #[getter] fn method(&self) -> &str { self.req.method().as_str() }            // zero alloc
    #[getter] fn path(&self) -> &str { self.req.uri().path() }                   // zero alloc
    fn header(&self, k: &str) -> Option<&str> {                                   // one map lookup, no DashMap build
        self.req.headers().get(k).and_then(|v| v.to_str().ok())
    }
    #[getter] fn headers(&self, py: Python) -> Py<HeadersView> { /* lazy view, no copy */ }
    fn query(&self, k: &str) -> Option<String> { /* parse query string lazily, cache */ }
    fn path_param(&self, k:&str)->Option<&str> { /* slice, no PyDict */ }
    async fn body(&self) -> PyResult<Py<PyBytes>> { /* read payload ONCE, GIL released during read */ }
    async fn json(&self) -> PyResult<Py<PyAny>> { /* parse in Rust, build once, memoize */ }
}
```
**Effect:** a handler that reads one header pays **one `HeaderMap::get`** (no DashMap, no 48 allocs). A handler that never reads the body never buffers it (and never copies it 3×). The 3 empty `PyDict`s are never allocated. No `Request::clone`.

### 9.2 Keep & generalize the const-router
Keep Robyn's two-tier `CachedResponse` cache verbatim, plus the improvements in §2.3(e): pre-built typed `HeaderMap`, per-route fast-path gating (not global `AtomicBool`), single path normalization feeding both fast and slow paths, optional memoized-dynamic `@cache` generalization.

### 9.3 Zero-copy response body
For `str`/`bytes`/orjson-dict returns, do **not** copy out into a `Vec<u8>`. Implement an actix `MessageBody` that holds the `Py<PyBytes>`/`Py<PyString>` alive and yields its buffer slice directly (`PyBytes::as_bytes()` is a borrow). The exact bytes orjson produced go on the socket with zero intermediate copy. Drop the `Py` when the body is fully written.

### 9.4 Arity- and need-aware argument passing
Robyn already skips `into_pyobject` for 0-arg handlers (`executors/mod.rs:168`). Generalize: at registration, introspect the handler signature (FastAPI-style). If it declares `request: Request`, pass the lazy proxy (cheap — it's just the `HttpRequest` + cells). If it declares typed params (`id: int`, `body: PydanticModel`), **parse/validate in Rust** and pass the typed values directly — the handler never touches a generic `Request`, and JSON is parsed once in Rust (no PyString→str→serde→PyObject round-trip of §3.6). This is the FastAPI-ergonomics + Rust-speed combination that is Lancelot's reason to exist.

### 9.5 Sync-handler offload + async-native streaming
- Route `def` handlers to a bounded blocking pool (Granian-style `BlockingRunner` already exists in Robyn's tree to copy) so they never starve the async worker; `async def` stays on the asyncio loop with the GIL released across `await`.
- Streaming: support **async generators**, **batch** N chunks per GIL acquisition, accept `bytes` chunks zero-copy, keep Robyn's forced SSE headers (`X-Accel-Buffering: no` etc.). This is the AI hot path — make it async-native, not sync-generator + per-token `spawn_blocking`.

### 9.6 Handler/metadata storage without GIL on the routing path
Store handlers as `Arc<HandlerInfo>` keyed by `u32` route id; `get_route` returns `(route_id, params)` with **no GIL, no clone**. Acquire the GIL once, at actual invocation. Middleware lists as `Arc<[Arc<HandlerInfo>]>` (clone = Arc bump). Eliminates findings §8.1, §8.7.

### 9.7 Expected impact (qualitative, INFERRED from the allocation counts in §3)
For a trivial `GET` whose handler reads ≤1 header and returns a JSON string:
- Robyn: ~60 Rust allocs + ~6 CPython obj allocs + 1–3 body copies + 1 `Request::clone` + header DashMap build, **every request**.
- Lancelot (lazy proxy + zero-copy + Arc handlers): ~**1–3 allocs** (path-params SmallVec if any, the proxy pyclass, the response `Py` keep-alive), **0 body copies** (zero-copy out), **0 header marshalling** (borrow actix `HeaderMap`), **0 GIL on routing**. The boundary cost collapses from "build everything" to "touch only what's read."

---

## 10. Top 5 steals (ranked) and the one proposal

1. **Const-router** (`const_router.rs`): pre-execute const handlers at startup, store pre-rendered `CachedResponse{status, Arc<headers>, Bytes body}` in exact-`HashMap`-then-`matchit` tiers, gate a fully GIL-free Rust static-serve path on an `AtomicBool` "no middleware" check, bake global headers at startup. ~10–50× on cacheable routes. Steal verbatim; improve per §2.3(e).
2. **GIL only at the crossings, released across `await`** (`executors/mod.rs:524-535`): acquire GIL to call handler + make the future, release for the entire await, re-acquire to marshal. The core of non-blocking hybrid async.
3. **Ordered fast-path response extraction with `downcast_exact` first + in-Rust cached `orjson.dumps`** (`executors/mod.rs:86-153, :26`): `PyResponse` exact downcast → dict/list→orjson → str → bytes → streaming → slow getattr. Avoids `getattr` chains for the common cases; `PyOnceLock`-cached callable.
4. **Forced SSE header set for streaming** (`response.rs:108-113`): `Connection: keep-alive`, `X-Accel-Buffering: no` (defeats nginx buffering), `Cache-Control: no-cache,no-store,must-revalidate`, `Pragma: no-cache`, `Expires: 0`. Exactly right for LLM token streaming behind a proxy.
5. **Rust mpsc → `WebSocketChannel` pyclass → `await channel.receive()`** (`websockets/mod.rs:30-49, :142-183`): turns inbound WS frames into a clean `async def` pull loop in Python with no callbacks — ideal for agent/chat backends. Per-endpoint registry for scoped broadcast.

---

## 11. The full `index()` request pipeline — every branch (KNOWN, `server.rs:475-644`)

This is the single dispatcher every non-const, non-static, non-WS request flows through. Reconstructed verbatim from source so Lancelot can replicate the exact ordering (the ordering is load-bearing — middleware/const/handler precedence determines observable behavior).

```
index(router, const_router, payload, middleware_router, g_req_hdrs, g_resp_hdrs, excl_paths, req):
 1. if !HttpMethod::is_supported(req.method()) -> ResponseType::Standard(Response::method_not_allowed(None))   (:485)
 2. http_method = from_actix_method(req.method())  else -> method_not_allowed                                   (:489)
 3. request = Request::from_actix_request(&req, payload, &g_req_hdrs).await                                     (:494)
        on Err -> error!() + Response::internal_server_error(None)
 4. route = format!("{}{}", req.method(), request.url.path)   // e.g. "GET/api/thing"  -- method PREPENDED      (:503)
 5. request_context = Python::with_gil(crate::asyncio::new_context)   // one contextvars.Context per request    (:515)
        on Err -> internal_server_error
        ctx_ref = Some(&request_context)
 6. before_middlewares = middleware_router.get_global_middlewares(BeforeRequest)                                (:525)
    route_before        = middleware_router.get_route(BeforeRequest, &route)                                    (:527)
    early_response: Option<Response> = None
    if !before_middlewares.is_empty() || route_before.is_some():                                                (:530)
        all_before = before_middlewares (+ push route_before.0 ; request.path_params = route_before.1)          (:531-535)
        for mw in all_before:
            request = match execute_middleware_function(&request, &mw, ctx_ref).await:                          (:537)
                Ok(Request(r))  -> r
                Ok(Response(r)) -> early_response = Some(r); break    // short-circuit                          (:540-543)
                Err(e)          -> get_traceback / internal_server_error
 7. response =
        if let Some(r) = early_response             -> ResponseType::Standard(r)                                (:559)
        else if const_router.get_cached_route(method, &request.url.path) is Some(cached):                       (:561)
              // SLOW const path (middleware exists): rebuild a fresh Response from CachedResponse
              resp = Response{ status, response_type:"text", headers:Headers::new(None), body:cached.body.to_vec(),
                               file_path:None, cookies:Cookies::new() }
              for (k,v) in cached.headers: resp.headers.set(k,v)                                                 (:570-572)
              ResponseType::Standard(resp)
        else if let Some((function, route_params)) = router.get_route(method, &request.url.path):               (:574)
              request.path_params = route_params
              execute_http_function(&request, &function, ctx_ref).await  (Ok->r ; Err->internal_server_error)   (:577)
        else  -> ResponseType::Standard(Response::not_found(None))                                              (:589)
 8. is_excluded = excl_paths.contains(&request.url.path)                                                        (:592)
    if !is_excluded: response.headers_mut().set_missing(&g_resp_hdrs)   // globals fill ONLY missing keys       (:597-599)
 9. after_middlewares = get_global_middlewares(AfterRequest) ; route_after = get_route(AfterRequest, &route)    (:602-603)
    if !after_middlewares.is_empty() || route_after.is_some():
        all_after = after_middlewares (+ push route_after.0)                                                    (:606-609)
        for mw in all_after:
            if let ResponseType::Standard(std_response) = response:                                             (:611)
                response = match execute_after_middleware_function(&request, &std_response, &mw, ctx_ref).await:
                    Ok(Request(_))  -> error! "After middleware returned a request" + internal_server_error     (:620-622)
                    Ok(Response(r)) -> ResponseType::Standard(r)
                    Err(e)          -> internal_server_error(Some(&std_response.headers))                        (:634)
            // NOTE: streaming responses are NOT passed through after-middleware (the `if let Standard` gate)
10. return response                                                                                            (:643)
```

**Load-bearing details for Lancelot:**
- **Route key is `method + path`** (`format!("{}{}", req.method(), request.url.path)`, `:503`) — middleware route lookup is keyed by the concatenation, *not* by `(HttpMethod, path)`. So per-route middleware is method-specific via string prefix. Lancelot should key by `(HttpMethod, &str)` to avoid the per-request `format!` allocation (one `String` per request just to build the lookup key).
- **`set_missing` semantics** (`headers.rs:183-190`): global response headers fill **only keys the response doesn't already have** — middleware/handler headers win over globals. This prevents duplicate `Access-Control-Allow-Origin`. Replicate exactly (a naive `extend` would emit duplicates and break CORS).
- **Const-with-middleware path** (`:561-573`) rebuilds a *fresh* `Response` from the `CachedResponse` (a `cached.body.to_vec()` copy + per-header `resp.headers.set()` which lowercases + allocs). So const routes still skip the *handler* under middleware but pay a full response rebuild + body copy. The fully-free path is only the `server.rs:248` closure (zero middleware).
- **Streaming responses bypass after-middleware** silently (`if let ResponseType::Standard` gate at `:611`) — an `after_request` hook never sees a `StreamingResponse`. Lancelot should decide deliberately (probably: allow after-middleware to wrap the stream, or document the skip).
- **Error path always 500 with a fresh `Response`** — no custom error handlers in the Rust path; exception → `get_traceback` (`:646-658`, formats the Python traceback via `err.traceback(py).format()`) → log → generic 500. Lancelot wants pluggable exception handlers (FastAPI's `@app.exception_handler`).
- The per-request `contextvars.Context` (`:515`) is allocated **before** middleware so before/handler/after share ContextVar writes (request-id, auth, tenant). This is the correct primitive for AI request tracing (trace id propagation through an agent call chain).

---

## 12. The asyncio / contextvars machinery (KNOWN, `asyncio.rs`)

Three `PyOnceLock`-cached Python objects (resolved once, reused every request — the right caching pattern for any hot Python callable):

```rust
static CONTEXTVARS:    PyOnceLock<Py<PyAny>>;   // the `contextvars` module
static CONTEXT:        PyOnceLock<Py<PyAny>>;   // a contextvars.Context() (empty_context, unused/dead)
static RUN_IN_CONTEXT: PyOnceLock<Py<PyAny>>;   // a compiled `_run_in_context(coro, ctx)` coroutine fn
```

- `new_context(py)` (`:46-49`): `contextvars.Context()` — a fresh empty context per request. INFERRED cost: one Python attribute lookup (cached module) + one `call0()` = one CPython object alloc/request. Cheap relative to §3 but still per-request.
- `copy_context(py)` (`:32-38`): `unsafe { pyo3::ffi::PyContext_CopyCurrent() }` — raw CPython C-API call, no Python-level dispatch. Used by `TaskLocals::copy_context` at startup, not per request.
- `run_in_context_helper` (`:63-85`): **compiles a Python module from a string literal at first use** and caches the `_run_in_context` coroutine function. The source (verbatim):
  ```python
  import asyncio, sys
  if sys.version_info >= (3, 11):
      async def _run_in_context(coro, ctx):
          task = asyncio.get_running_loop().create_task(coro, context=ctx)
          return await task
  else:
      async def _run_in_context(coro, ctx):
          return await coro
  ```
  Python 3.11+ passes `context=ctx` to `create_task` so ContextVar writes inside the user coroutine **persist in `ctx`** (visible to later phases). On 3.10 it degrades to `await coro` (no cross-phase ContextVar propagation — issue #1380 is unfixed there). `wrap_coro_in_context` (`executors/mod.rs:260-266`) just calls this cached fn with `(coroutine, ctx)`.

**Steal for Lancelot:** the "compile a tiny Python helper from a string once, cache in `PyOnceLock`, call it per request" pattern is the cleanest way to express asyncio-specific glue (task creation with explicit context) that's awkward from pure Rust/pyo3. Use it for: per-request context binding, async-generator driving, exception-group handling. The `sys.version_info` branch in the source string is a clean way to support multiple CPython versions without `#[cfg]`.

**Avoid:** allocating a fresh `contextvars.Context()` per request even when the app has no ContextVars/middleware is wasted work. Lancelot: only allocate the per-request context if (a) any middleware is registered, or (b) the app declared it uses request-scoped ContextVars. Otherwise pass `None` (Robyn already threads `Option<&Py<PyAny>>` everywhere — the machinery to skip it exists).

---

## 13. The Granian-derived async runtime (KNOWN, `runtime.rs`, `conversion.rs`, `blocking.rs`)

Robyn's awaitable-bridging is lifted from Granian (BSD-3, attributed in file headers). Key facts for Lancelot:

- **`default_runtime`** (`runtime.rs:138`): `RuntimeBuilder::new_current_thread().max_blocking_threads(blocking_threads).enable_all()`. A **current-thread** tokio runtime per actix worker (actix runs its own `System`), with a bounded blocking pool sized by `blocking_threads`.
- **Custom awaitables, ~38–55% faster than `pyo3_asyncio.future_into_py`** (verbatim source comments, `runtime.rs:163, :182`):
  - `future_into_py_iter` (`:165`): `PyIterAwaitable` — for simple await; spawns the future, on completion `spawn_blocking(|py| set_result)`.
  - `future_into_py_futlike` (`:185`, unix): `PyFutureAwaitable` bound to the Python event loop, `tokio::select!{ biased; result = fut => set_result, cancel = cancel_tx.notified() => drop_ref }` — supports **cancellation** (the `cancel_tx` arm drops the awaitable if the Python side cancels).
  - `empty/done/err_future_into_py` — zero-alloc fast awaitables for already-resolved results (`PyEmptyAwaitable`, `PyDoneAwaitable`, `PyErrAwaitable`).
- **`FutureResultToPy`** (`conversion.rs:7-28`): a 3-variant enum (`None`/`Err`/`Value`) implementing `IntoPyObject` — the marshalling boundary for "Rust future result → Python awaitable result". `Err` unwraps into a `PyErr`, `Value` re-binds the `Py<PyAny>`.
- **`BlockingRunner`** (`blocking.rs:34-60`): `Empty | Mono(thread) | Pool(threads)` — a crossbeam-channel-fed pool of GIL-holding threads. `BlockingTask` is a `Box<dyn FnOnce(Python)+Send>`. **This is the offload primitive Lancelot should use for sync handlers (§5e) — Robyn ships it but doesn't use it on the HTTP route path.**

**Steal:** the custom awaitables (don't pay `pyo3_asyncio`'s overhead — write `PyIterAwaitable`/`PyFutureAwaitable` directly), the cancellation-aware `select!{biased; ...}` pattern (a cancelled HTTP client should cancel the in-flight Python coroutine — important for expensive LLM calls: client disconnects → cancel the OpenAI request), and the `Empty/Done/Err` zero-alloc awaitables for sync-result fast paths.

**Lancelot AI angle:** the `biased` select with a `cancel_tx.notified()` arm is exactly how you make "client closed the SSE connection → stop generating tokens / cancel the upstream LLM stream" work. Robyn already has the primitive in `future_into_py_futlike`; Lancelot must wire HTTP-disconnect → `cancel_tx` so a closed chat connection tears down the model call instead of burning tokens into the void.

---

## 14. Server startup wiring & static files (KNOWN, `server.rs:84-287`)

- **One-shot start guard:** `STARTED.compare_exchange(false,true,SeqCst,Relaxed)` (`:93`) — `start()` is idempotent (second call returns `Ok(())`).
- **Event loop:** `asyncio.new_event_loop()` + `set_event_loop`, wrapped in `pyo3_async_runtimes::TaskLocals::new(loop).copy_context(py)`, stored in a process-global `OnceCell<TaskLocals>` (`:117-122`). Every request clones it under GIL (`TASK_LOCALS.get().clone_ref(py)`, `server.rs:256`) — a per-request GIL acquisition just to clone task-locals. Lancelot: store `TaskLocals` in a way that doesn't need a GIL clone per request (it's effectively immutable after startup — hold an `Arc` or thread-local).
- **Max payload:** env var `ROBYN_MAX_PAYLOAD_SIZE` (default constant), parsed once; `web::PayloadConfig::new(max_payload_size)` (`:219`). This caps the buffered body (§3.2) — there is *no* streaming above this size, requests over it are rejected.
- **The actix `System` runs on a dedicated `thread::spawn`** (`:135`) with `actix_web::rt::System::new().block_on(...)` — the Python thread calls `event_loop.run_forever()` (`:289`) while the Rust HTTP server runs on its own thread. Startup handler runs before the server binds; `bake_global_headers` runs iff no middleware (`:143-145`).
- **Routing is 100% Robyn's, not actix's:** `default_service(web::route().to(closure))` (`:220`) — *all* dynamic traffic hits one closure; actix does no routing. Only static `Files` services (`:151-189`, with a `fn_guard` that `std::fs::metadata` checks the path is a regular file — a blocking syscall per static request) and WS routes (`app.route(&endpoint, web::get().to(...))`, `:198-216`) get real actix routes. Lancelot can keep this (one default service + Robyn-owned matchit) — it makes the router fully controllable, but the static-file `fn_guard` doing a sync `std::fs::metadata` per request is a blocking syscall on the actix worker (avoid: cache stat results or use actix-files' built-in handling).
- **`keep_alive(KeepAlive::Os)`, `client_request_timeout(0)`** (`:278-280`) — OS-level keepalive, no request timeout (an idle slow-loris isn't timed out by actix; rely on OS/proxy). Lancelot should set a sane request timeout (LLM backends especially — a hung upstream shouldn't pin a connection forever; use a configurable deadline).

---

## 15. Multipart / form / file handling depth (KNOWN, `request.rs:89-120, 146-164`)

```rust
async fn handle_multipart(mut payload: Multipart, files, form_data, body) {
  while let Some(item) = payload.next().await {
    let mut field = item?;
    let mut data = Vec::new();
    while let Some(chunk) = field.next().await { data.extend_from_slice(&chunk?); }   // buffer field fully
    let cd = field.content_disposition();
    let field_name = cd.get_name().unwrap_or_default();
    let file_name  = cd.get_filename().map(|s| s.to_string());
    body.extend_from_slice(&data);                                                    // ALSO append to body
    if let Some(name) = file_name { files.insert(name, data); }                        // move
    else if let Ok(text) = String::from_utf8(data) { form_data.insert(field_name.to_string(), text); }
  }
}
```
Triggered when `content-type` contains `"multipart/form-data"` (`request.rs:146-150`). Every field is **fully buffered into a `Vec`**, then *also* `extend_from_slice`'d into the combined `body` (so a 50 MB file upload is held **twice**: once in `files[name]`, once appended to `body`), then files moved into `files: HashMap<String, Vec<u8>>`, text fields UTF-8-decoded into `form_data`. In `into_pyobject` each file becomes a `PyBytes` (another copy, §3.4). So an uploaded file is resident **3×** (field `data`, appended `body`, `PyBytes`). No streaming multipart, no temp-file spillover, no size cap per field (only the global payload cap).

**Avoid / Lancelot:** stream multipart fields to disk (temp file) or to the handler as an async iterator; never double-store in both `files` and `body`; expose files as a lazy handle (path or async stream), materialize `bytes` only if the handler asks. For AI backends that take audio/video/image uploads this triple-buffering is a real memory problem (a 100 MB audio file = 300 MB resident).

---

## 16. Reimplementable Lancelot core skeletons (DESIGN)

These are the concrete Rust shapes Lancelot should build, distilled from the RE above. They are the "write the server code" deliverable for this subsystem.

### 16.1 Lazy request proxy (replaces `request.rs` eager marshalling)

```rust
#[pyclass]
pub struct Request {
    // Owns the parsed request head (method/uri/headers already live in actix, zero re-parse).
    req: actix_web::HttpRequest,
    // Payload consumed at most once; None after body() taken.
    payload: parking_lot::Mutex<Option<actix_web::web::Payload>>,
    body: tokio::sync::OnceCell<bytes::Bytes>,         // filled by body(); GIL released during read
    json: pyo3::sync::PyOnceLock<Py<PyAny>>,            // memoized parsed json
    // path params: slices into an Arc<str> of the matched path, no String allocs
    path: std::sync::Arc<str>,
    params: smallvec::SmallVec<[(std::ops::Range<usize>, std::ops::Range<usize>); 4]>, // (key,val) byte ranges
}

#[pymethods]
impl Request {
    #[getter] fn method(&self) -> &str { self.req.method().as_str() }            // 0 alloc
    #[getter] fn path(&self) -> &str { self.req.uri().path() }                   // 0 alloc
    fn get_header(&self, k: &str) -> Option<&str> {                              // 1 HeaderMap::get, 0 DashMap
        self.req.headers().get(k)?.to_str().ok()
    }
    fn path_param(&self, k: &str) -> Option<&str> {                              // slice, no PyDict
        self.params.iter()
            .find(|(kr,_)| &self.path[kr.clone()] == k)
            .map(|(_,vr)| &self.path[vr.clone()])
    }
    fn query(&self, k: &str) -> Option<String> {                                 // lazy parse, no pre-split
        url::form_urlencoded::parse(self.req.query_string().as_bytes())
            .find(|(qk,_)| qk == k).map(|(_,v)| v.into_owned())
    }
    fn body<'p>(&self, py: Python<'p>) -> PyResult<Bound<'p, PyAny>> {           // async, GIL released
        let payload = self.payload.lock().take();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let mut p = payload.ok_or_else(|| PyRuntimeError::new_err("body already consumed"))?;
            let mut buf = web::BytesMut::new();
            while let Some(c) = p.next().await { buf.extend_from_slice(&c?); }    // ONE copy, not 3
            Ok(Python::with_gil(|py| PyBytes::new(py, &buf).unbind()))
        })
    }
    // json(): read body once, serde_json in Rust, build Python tree once, memoize in `self.json`
}
```
Cost for `GET` reading one header: `HeaderMap::get` (no alloc) + the proxy pyclass alloc (1). Body never read ⇒ never buffered. vs Robyn's ~60+6 allocs.

### 16.2 GIL-free routing with `Arc<HandlerInfo>`

```rust
struct HandlerInfo { handler: Py<PyAny>, is_async: bool, arity: u8, /* resolved signature plan */ }
type Trie = parking_lot::RwLock<matchit::Router<u32>>;          // route -> handler id
struct HttpRouter {
    tries: [Trie; 9],                                           // one per HttpMethod (index by enum as usize)
    handlers: arc_swap::ArcSwap<Vec<Arc<HandlerInfo>>>,         // id -> handler, snapshot read, no lock
}
impl HttpRouter {
    fn get_route(&self, m: HttpMethod, path: &str)
        -> Option<(Arc<HandlerInfo>, matchit::Params)> {        // NO Python::with_gil, NO clone of Py
        let t = self.tries[m as usize].read();
        let matched = t.at(path).ok()?;
        let h = self.handlers.load()[*matched.value as usize].clone();  // Arc bump only
        Some((h, matched.params /* borrow path, no String */))
    }
}
```
Removes findings §8.1 (no `Request::clone`), §8.7 (no GIL on match), and the §11 per-request `format!` key (route by `(HttpMethod, &str)`).

### 16.3 Const router (steal Robyn, add typed HeaderMap + per-route gate)

```rust
struct CachedResponse {
    status: actix_http::StatusCode,
    headers: Arc<actix_http::header::HeaderMap>,    // TYPED — no per-request name re-parse
    body: bytes::Bytes,                              // pre-rendered, clone = refcount
    route_header_count: usize,
    fast_eligible: bool,                             // per-route: no route mw AND global_mw==0
}
// hot path (normalized path, computed ONCE for fast+slow):
if cached.fast_eligible {
    let mut r = HttpResponse::build(cached.status);
    *r.headers_mut() = cached.headers.as_ref().clone();   // HeaderMap clone, no re-parse
    return r.body(cached.body.clone());                   // Bytes clone, 0 copy, 0 GIL
}
```

### 16.4 Async-native batched streaming (the AI hot path)

```rust
fn create_python_stream(gen: Py<PyAny>, is_async_gen: bool, batch: usize, cancel: CancelToken)
  -> impl Stream<Item = Result<Bytes, Error>>
{
    futures::stream::unfold(gen, move |gen| async move {
        if cancel.is_cancelled() { return None; }                 // client disconnect -> stop the model
        // Acquire GIL once, pull up to `batch` items (sync: __next__, async: drive __anext__ futures),
        // coalesce into one Bytes, yield. async generators driven via into_future (no spawn_blocking).
        // Accept both str and bytes chunks; bytes path is zero-copy (keep Py alive).
    })
}
```
Wire `cancel` to actix connection-close so a closed SSE/chat stream cancels the upstream LLM call (§13 AI angle). This is the most important single piece of code for an agent/chat backend and the place Robyn is weakest (sync-only, per-token spawn_blocking, no cancellation, no bytes).

### 16.5 Sync-handler offload (use Robyn's own unused `BlockingRunner`)

`async def` → asyncio loop, GIL released across `await` (keep Robyn's `executors/mod.rs:524-535`). `def` → `BlockingRunner::run(move |py| { /* call handler under GIL on a pool thread */ })` so a blocking handler can't starve the worker. Detect via the already-recorded `is_async`. Bound the pool; surface saturation as 503 (don't queue unbounded).

---

## 17. Consolidated KNOWN-vs-INFERRED-vs-DESIGN ledger

| # | Claim | Label | Cite |
|---|---|---|---|
| 1 | Const routes pre-execute handler once at registration, store `CachedResponse` in exact-map + matchit | KNOWN | `const_router.rs:87-127` |
| 2 | Fully GIL-free const serve only when `!has_any_middleware()` | KNOWN | `server.rs:233-251`, `middleware_router.rs:17,32` |
| 3 | Global response headers baked into const cache at startup (iff no middleware) | KNOWN | `const_router.rs:172-199`, `server.rs:143-145` |
| 4 | `Request` fully marshalled eagerly before handler (headers DashMap, buffered body, 3 PyDicts, pyclass) | KNOWN | `request.rs:122-193, 32-87` |
| 5 | Handler with 0 params skips `into_pyobject` entirely | KNOWN | `executors/mod.rs:168` |
| 6 | `Request::clone()` deep-clones whole struct + body on every ≥1-arg call | KNOWN | `executors/mod.rs:174` |
| 7 | Body buffered fully (1–3 copies) before handler; cap = `ROBYN_MAX_PAYLOAD_SIZE` | KNOWN | `request.rs:158-163`, `server.rs:219` |
| 8 | No sync-handler thread-pool offload in HTTP path (docs claim is wrong) | INFERRED (absence) | `executors/mod.rs:537-544` |
| 9 | GIL acquired on routing path just to clone `FunctionInfo` | KNOWN | `http_router.rs:53`, `middleware_router.rs:51,99`, `function_info.rs:62-71` |
| 10 | Trailing-slash mismatch defeats const fast path for `/x/` | INFERRED | `server.rs:238` vs `request.rs:166-172` |
| 11 | Streaming = sync generators only, per-chunk `spawn_blocking`+GIL, str-only | KNOWN | `response.rs:123-148` |
| 12 | Forced SSE headers incl. `X-Accel-Buffering: no` | KNOWN | `response.rs:108-113` |
| 13 | Inbound WS frames cross as `String` over unbounded mpsc; binary coerced to UTF-8 | KNOWN | `websockets/mod.rs:142-183` |
| 14 | Custom awaitables ~38–55% faster than pyo3_asyncio; cancellation-aware select | KNOWN (source comments) | `runtime.rs:163,182,195-199` |
| 15 | Per-request `contextvars.Context` allocated before middleware (3.11+ propagation) | KNOWN | `server.rs:515`, `asyncio.rs:46-85` |
| 16 | Multipart files held 3× (field data + appended body + PyBytes) | KNOWN | `request.rs:89-120, 64-67` |
| 17 | Routing is 100% Robyn's matchit; actix only does static/WS | KNOWN | `server.rs:220` |
| 18 | Lazy proxy + zero-copy + Arc handlers collapses ~66 allocs → ~1–3 | DESIGN | §9, §16 |

This subsystem's RE is sufficient to write Lancelot's `routing/`, `types/`, `executors/`, and `websockets/` modules from these notes alone.

**The one concrete proposal to minimize per-request FFI cost:** replace Robyn's *eager total `Request` marshalling* (≈60 Rust allocs + ≈6 CPython obj allocs + 1–3 body copies + a redundant deep `Request::clone`, paid every request regardless of use) with a **lazy `Request` proxy pyclass that owns the live actix `HttpRequest` + an un-consumed payload** and materializes headers/query/body/json/path-params only on attribute access (§9.1), combined with **zero-copy response bodies** (`Py`-keep-alive `MessageBody`, §9.3), **`Arc<HandlerInfo>` routing with no GIL on the match path** (§9.6), and **arity/type-aware argument passing that parses typed params in Rust** (§9.4). Net: per-request boundary cost goes from "build everything eagerly" to "touch only what the handler reads," collapsing the dominant overhead of the hybrid model while keeping FastAPI ergonomics.
