# Robyn RE Pass-2 — The FFI Conversion Code, Error/Panic Propagation & the Hybrid-Model Behavioral Contract Lancelot Must Replicate

> **Scope.** Pass-1 (`research/robyn/01..03`, 2,682 lines) covered: crate stack, process/socket model, GIL choreography (where the GIL is held), the const-router, `matchit` routing, the *allocation count* of request marshalling, the `FunctionInfo` ABI shape, the `mcp.py`/`ai.py` modules, packaging. Pass-1 deliberately **deferred the actual conversion code** — it counted allocations but did not transcribe the `IntoPyObject`/`FromPyObject`/`#[pyclass]` bodies, the custom-awaitable poll machinery, the `#[pymodule]` export list, or the integration/unit-test behavioral contract.
>
> **This file is pass-2: the verbatim conversion code + every error/panic path across the Rust↔Python seam + every behavior the tests assert about that seam.** Stack is locked (`DESIGN-DECISIONS §1`): `hyper 1.x` + `tower` + `tokio` multi-thread + `pyo3 0.27`. Robyn is actix-based; we do **not** copy actix. We copy/improve its *FFI conversion strategy, its error model, and its observable HTTP contract* — the parts that are engine-independent.
>
> Labels: **KNOWN** (verbatim from source, `path:line`) · **INFERRED** (deduced from code) · **DESIGN** (Lancelot's hyper+pyo3 plan). Every architecture claim cites `src/...:line` of the local checkout `robyn/Robyn/`.

---

## Executive summary (12 lines)

1. Robyn's request/response FFI is **eager and allocation-heavy**: `Request::into_pyobject` (`request.rs:32-87`) builds a `PyRequest` `#[pyclass]` with **two materialized `PyDict`s (form_data, files), a `Py<Headers>`, a `Py<PyDict>` path_params, and a str/bytes body decode** on *every* request, even if the handler reads nothing. Lancelot: a lazy proxy that does ~0 of this until attribute access.
2. The body conversion is **UTF-8-sniffed**: `str::from_utf8` succeeds → Python `str`, fails → `bytes` (`request.rs:40-47`). This is an observable contract (`test_request_object`, `test_binary_output`) Lancelot must preserve byte-for-byte.
3. Headers are a `DashMap<String,Vec<String>>` lowercased on every insert/get (`headers.rs:11,43,196`). Every Python read rebuilds a `PyList`/`PyDict` via `into_pyobject(py).unwrap()` — **`.unwrap()` at the FFI seam is a panic vector**. Lancelot: never `.unwrap()` a conversion inside a `#[pymethods]` body.
4. Error propagation is **two-tier and asymmetric**: the Rust `index()` seam only ever produces a *generic* `Response::internal_server_error` (`server.rs:585`); the *meaningful* exception→response mapping (`@app.exception`, 422, 400) happens in a **Python wrapper closure** (`router.py:280-330`), not Rust. The boundary contract is "Rust catches `PyErr`→500 text/plain; Python catches `Exception`→user handler."
5. Panics are **not** caught at the seam — `panic=abort` (Cargo) + no `catch_unwind` means any Rust panic (every `.unwrap()`/`.expect()` in `headers.rs`/`request.rs`) kills the worker with no supervisor. Lancelot's #1 stability fix: `catch_unwind` at the FFI seam + respawn supervisor.
6. The custom awaitables (`callbacks.rs`, 409 lines) are a **5-type hand-rolled `__await__`/`__iter__`/`__next__` state machine** that beats `pyo3-async-runtimes` by 38–55% by never allocating a Python `Future` for the common cases. The poll model is transcribed in full in §5.
7. `PyFutureAwaitable` implements the full asyncio future protocol (`_asyncio_future_blocking`, `add_done_callback`, `cancel`, `result`, `exception`) with **a single-callback optimization** (only the most recent done-callback is stored — `callbacks.rs:238`). This is a correctness-vs-speed tradeoff Lancelot must understand before copying.
8. The `#[pymodule]` ABI surface is exactly **20 classes + 1 function** (`lib.rs:32-69`). That list is the entire Python-visible contract; everything else is internal. Lancelot's ABI should be *smaller* (lazy proxy collapses Headers/QueryParams/Url into the request).
9. `FunctionInfo` (`function_info.rs:27-72`) is the whole handler ABI — `handler, is_async, number_of_params:u8, args:PyDict, kwargs:PyDict` — and its `Clone` impl **takes the GIL** (`function_info.rs:62-72`). Cloning it on the hot path is a hidden GIL acquisition. Lancelot: store handler metadata in an `Arc<HandlerInfo>` that never needs the GIL to clone.
10. JSON parsing (`request.rs:258-317`) is `serde_json` → recursive `json_value_to_py` with a **128-deep recursion guard** (`MAX_JSON_DEPTH`) and exact int/uint/float/null/bool/str/array/object type preservation — a behavioral contract `test_json_types.py` asserts (8 type-preservation tests). Lancelot delegates to pydantic-core but must keep the same untyped-`request.json()` type mapping.
11. The streaming seam is the known catastrophe: `create_python_stream` (`response.rs:123-148`) does `spawn_blocking` + `Python::with_gil` + `gen.call_method0("__next__")` + `extract::<String>()` **per token** — one thread hop + one GIL acquire + one String copy per SSE event. Lancelot's async-generator pump must replace this wholesale.
12. The test suite is the **executable spec of the boundary**: header lowercasing, `server: robyn` + `global_after` on every response (`http_methods_helpers.py:13-16`), 404="Not found"/text/plain, 405 on unknown method, 500="error msg: value error" via the Python exception handler, SSE `Cache-Control` exact strings, JSON type preservation. All transcribed in §7 as Lancelot's conformance checklist.

---

## 1. The `#[pymodule]` ABI surface — the entire Python-visible contract (KNOWN — `lib.rs:1-69`)

The whole Rust→Python boundary is one `#[pymodule]` (`lib.rs:37-69`). This is the *complete* list of types Python can touch; anything not here is `pub(crate)` internal.

```rust
#[pymodule]
pub fn robyn(_py: Python, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(get_version, m)?)?;        // env!("CARGO_PKG_VERSION")
    m.add_class::<Server>()?;
    m.add_class::<Headers>()?;          // DashMap<String,Vec<String>>
    m.add_class::<Cookie>()?;
    m.add_class::<Cookies>()?;
    m.add_class::<CookiesIter>()?;      // iterator pyclass — exposed only for `for k in cookies`
    m.add_class::<WebSocketRegistry>()?;
    m.add_class::<WebSocketConnector>()?;
    m.add_class::<WebSocketChannel>()?;
    m.add_class::<SocketHeld>()?;       // the SO_REUSEPORT fd holder
    m.add_class::<FunctionInfo>()?;     // the handler ABI dataclass
    m.add_class::<Identity>()?;
    m.add_class::<PyRequest>()?;        // name="Request"
    m.add_class::<PyResponse>()?;       // name="Response"
    m.add_class::<PyStreamingResponse>()?;  // name="StreamingResponse"
    m.add_class::<Url>()?;
    m.add_class::<QueryParams>()?;      // HashMap<String,Vec<String>>
    m.add_class::<MiddlewareType>()?;   // enum BEFORE_REQUEST=0 / AFTER_REQUEST=1
    m.add_class::<HttpMethod>()?;       // enum GET..TRACE
    // 5 awaitables — the asyncio bridge:
    m.add_class::<callbacks::PyEmptyAwaitable>()?;
    m.add_class::<callbacks::PyDoneAwaitable>()?;
    m.add_class::<callbacks::PyErrAwaitable>()?;
    m.add_class::<callbacks::PyIterAwaitable>()?;
    m.add_class::<callbacks::PyFutureAwaitable>()?;
    Ok(())
}
```

**KNOWN facts the ABI surface reveals:**
- **20 classes + 1 function.** That is the entire contract. The `.pyi` (pass-1 `03 §1.3`, hand-written, drift-prone) describes *this* surface.
- `PyRequest`/`PyResponse`/`PyStreamingResponse` are exported under the *renamed* names `Request`/`Response`/`StreamingResponse` (`request.rs:196`, `response.rs:227,244`). The Rust struct name ≠ the Python name. Lancelot: pick the rename at `#[pyclass(name=...)]` to keep wire/test compat.
- The awaitables are first-class module members because asyncio must be able to `isinstance`-check / introspect them (`_asyncio_future_blocking` getter is read by the event loop — §5.4).
- `module = "robyn._robyn"` on each awaitable (`callbacks.rs:12,30,...`) — pins the `__module__` so pickling/repr is stable. Lancelot: set `module="lancelot._lancelot"` consistently or asyncio debug reprs leak the crate name.

> **DESIGN — Lancelot's ABI should be *smaller*.** The lazy-request decision (`SYNTHESIS steal #4`) collapses `Headers`, `QueryParams`, `Url`, `Identity` into lazily-computed attributes of one `Request` proxy. Target exported surface: `Server`, `Request` (lazy), `Response`, `StreamingResponse`, `SSEResponse`, `FunctionInfo`-equivalent, `SocketHeld`-equivalent, the awaitable set, the WS set. Fewer pyclasses = fewer `#[pymethods]` panic surfaces = a tighter contract.

---

## 2. The request conversion code — verbatim, with allocation/panic accounting (KNOWN — `request.rs`)

### 2.1 The `Request` struct & its `FromPyObject` derive (`request.rs:16-30`)

```rust
#[derive(Default, Debug, Clone, FromPyObject)]
pub struct Request {
    pub query_params: QueryParams,
    pub headers: Headers,
    pub method: String,
    pub path_params: HashMap<String, String>,
    #[pyo3(from_py_with = get_body_from_pyobject)]   // body extracted via a custom fn
    pub body: Vec<u8>,
    pub url: Url,
    pub ip_addr: Option<String>,
    pub identity: Option<Identity>,
    pub form_data: Option<HashMap<String, String>>,
    pub files: Option<HashMap<String, Vec<u8>>>,
}
```

- **`#[derive(FromPyObject)]`** auto-generates a field-by-field `getattr`-and-`extract` from any Python object that has these attributes. Used by middleware that returns a (mutated) `Request` — `execute_middleware_function` calls `output.extract::<Request>()` (`executors/mod.rs:296`). That is a **full re-marshal of every field back into Rust** on every before-middleware that returns a request. Pass-1 noted the cost; here is the mechanism: the derived impl does ~10 `getattr` + ~10 `extract` calls.
- **`#[pyo3(from_py_with = get_body_from_pyobject)]`** (`types/mod.rs:109-118`): body is `str`→`into_bytes()`, `bytes`→`to_vec()`, anything else → **silently `Ok(vec![])`** (logs `debug!`). This is a *lossy, non-erroring* conversion — a handler that sets `request.body = 123` gets an empty body, no exception. Observable: not directly tested but a latent footgun.

> **DESIGN.** Lancelot's lazy `Request` proxy must NOT `derive(FromPyObject)` — the derive forces eager full-object materialization the moment any middleware round-trips a request. Instead: the proxy owns the raw `hyper::Request` parts + an `Option<Bytes>` body; middleware mutates *the proxy in place* (Rust-side setters), never round-trips through `extract`. Round-tripping through Python `FromPyObject` is the single biggest avoidable FFI cost in Robyn's middleware path.

### 2.2 `Request::into_pyobject` — the eager build, line by line (`request.rs:32-87`)

This runs on **every dynamic request** before the handler is called. Verbatim:

```rust
impl<'py> IntoPyObject<'py> for Request {
    type Target = PyAny; type Output = Bound<'py, Self::Target>; type Error = PyErr;
    fn into_pyobject(self, py: Python<'py>) -> Result<Self::Output, Self::Error> {
        let headers: Py<Headers> = self.headers.into_pyobject(py)?.extract()?;          // ALLOC 1: Py<Headers>
        let path_params = self.path_params.into_pyobject(py)?.extract()?;               // ALLOC 2: PyDict (HashMap→dict)

        let body = if self.body.is_empty() {
            PyString::new(py, "").into_any()                                            // ALLOC 3: empty PyString
        } else {
            match std::str::from_utf8(&self.body) {                                     // UTF-8 SNIFF
                Ok(s)  => PyString::new(py, s).into_any(),                              //   → str  (copies bytes)
                Err(_) => PyBytes::new(py, &self.body).into_any(),                      //   → bytes (copies bytes)
            }
        };

        let form_data: Py<PyDict> = match self.form_data {
            Some(data) if !data.is_empty() => {
                let dict = PyDict::new(py);                                             // ALLOC 4: PyDict
                for (key, value) in data { dict.set_item(key, value)?; }               //   + N set_item
                dict.into()
            }
            _ => PyDict::new(py).into(),                                                // ALLOC 4': empty PyDict (ALWAYS, even if None)
        };

        let files: Py<PyDict> = match self.files {
            Some(data) if !data.is_empty() => {
                let dict = PyDict::new(py);                                             // ALLOC 5: PyDict
                for (key, value) in data {
                    let bytes = PyBytes::new(py, &value);                              //   + N PyBytes (copies each file)
                    dict.set_item(key, bytes)?;
                }
                dict.into()
            }
            _ => PyDict::new(py).into(),                                                // ALLOC 5': empty PyDict (ALWAYS)
        };

        let request = PyRequest {
            query_params: self.query_params,    // QueryParams moved (HashMap<String,Vec<String>>)
            path_params, headers, body: body.into(),
            method: self.method, url: self.url, ip_addr: self.ip_addr,
            identity: self.identity, form_data, files,
        };
        Ok(Py::new(py, request)?.into_bound(py).into_any())                             // ALLOC 6: Py<PyRequest>
    }
}
```

**KNOWN allocation accounting (matches pass-1's ~30–66 figure, here itemized):**
- Minimum **6 Python heap objects per request even for a handler that reads nothing**: `Py<Headers>`, path-params `PyDict`, body `PyString`, empty `form_data` `PyDict`, empty `files` `PyDict`, `Py<PyRequest>`. The two empty dicts are allocated *unconditionally* (the `_ =>` arm).
- Body is **copied** (`PyString::new`/`PyBytes::new` both copy the slice). For a 1 MB POST this is a 1 MB copy into the Python heap before the handler decides whether it even reads `request.body`.
- `self.headers.into_pyobject(py)?.extract()?` — round-trips `Headers` *through Python and back into a `Py<Headers>`*. The `extract()` is a downcast, cheap, but the `into_pyobject` allocates a `Py<Headers>` wrapping the `DashMap`.

> **DESIGN — Lancelot's hyper+pyo3 equivalent.** A `#[pyclass] struct Request` holding `parts: http::request::Parts`, `body: OnceCell<Bytes>`, `path_params: SmallVec<[(Box<str>,Box<str>);4]>`, and `OnceCell`s for the lazily-built Python views. `into_pyobject` for the proxy = **`Py::new` of an empty shell, 1 alloc.** `request.headers` is a `#[getter]` that builds the header view on first access into a `OnceCell` (most handlers read 0–2 headers; building the whole map is waste). `request.body`/`.json()` decode lazily. Target: **1 alloc/request** vs Robyn's 6+. **Better:** for the const-tier and 0-param handlers, *don't build the proxy at all* (`executors/mod.rs:168` already special-cases `number_of_params == 0` — keep that, generalize it to "params the handler actually declared").

### 2.3 `from_actix_request` — parse-side conversion & the trailing-slash normalization (`request.rs:122-193`)

KNOWN, the engine-specific intake. Lancelot replaces actix specifics with hyper, but **two behaviors are part of the contract and must port**:

- **Query parsing** (`request.rs:132-141`): `req.query_string().split('&')`, each `split_once('=')`, **no percent-decoding**, empty value when no `=`. Stored as `QueryParams` multimap. `test_split_request_params.py` depends on this. Lancelot: same multimap semantics; *add* percent-decoding only if a test demands it (Robyn does not decode — preserve to stay byte-compatible unless we deliberately fix it).
- **Trailing-slash normalization** (`request.rs:166-172`): `if path.ends_with("/") && path.len() > 1 { path = &path[..path.len()-1] }`. `/foo/` is routed as `/foo`; `/` is preserved. This is **the** const-router trailing-slash bug pass-1 flagged. The `url.path` the handler sees is the *stripped* path (`test_http_request_info_get` asserts `path == "/sync/http/param"`). Lancelot: normalize once, deterministically, *before* routing AND const-tier lookup (Robyn normalizes before routing but the const-router has its own keying — the source of the bug).
- **Body intake** (`request.rs:146-164`): if `content-type` contains `multipart/form-data` → `handle_multipart` (streams fields, fills `files`/`form_data`, also appends raw to `body`); else → drain the whole payload into a `BytesMut`, `.freeze().to_vec()` (**full buffering, no streaming intake**). `chunk.expect("Failed to read chunk from payload")` (`request.rs:160`) — **a panic on a read error mid-body**, with `panic=abort` this kills the worker. Lancelot: never `.expect()` a body chunk; map to a `400`/`499` Response.
- **Header merge** (`request.rs:143-144`): `Headers::from_actix_headers` then `.extend(global_headers)` — request headers get the *global request headers* merged in (so handlers see injected globals). Multimap append semantics.

### 2.4 `PyRequest` — the `#[pyclass]` the handler actually sees (`request.rs:196-268`)

```rust
#[pyclass(name = "Request")] #[derive(Clone)]
pub struct PyRequest {
    #[pyo3(get, set)] pub query_params: QueryParams,
    #[pyo3(get, set)] pub headers: Py<Headers>,
    #[pyo3(get, set)] pub path_params: Py<PyDict>,
    #[pyo3(get, set)] pub identity: Option<Identity>,
    #[pyo3(get)]      pub body: Py<PyAny>,        // str | bytes
    #[pyo3(get)]      pub method: String,
    #[pyo3(get)]      pub url: Url,
    #[pyo3(get)]      pub ip_addr: Option<String>,
    #[pyo3(get, set)] pub form_data: Py<PyDict>,
    #[pyo3(get, set)] pub files: Py<PyDict>,
}
```

- `#[pyo3(get, set)]` generates a descriptor pair per field. `body` is get-only with a **custom `set_body` that type-checks** (`request.rs:251-256`): `check_body_type` rejects non-str/bytes with `PyValueError("Could not convert specified body to bytes")` (`types/mod.rs:131-138`). This is the only *validated* setter — every other field can be set to garbage.
- `Clone` is derived: cloning a `PyRequest` clones all the `Py<...>` handles (refcount bumps, **needs the GIL** — `Py::clone_ref` is implicit in `#[derive(Clone)]` for `Py<T>` fields under pyo3 0.27 only via `py-clone`; pass-1 `01 §0` notes the `py-clone` feature requirement). Cloning on the hot path = GIL refcount traffic.
- `json()` (`request.rs:258-267`): downcasts `body` to `PyString`, `serde_json::from_str`, `json_value_to_py`. **Errors** as `PyValueError("Invalid JSON: {e}")`. If body is `bytes` (not str), the `downcast_bound::<PyString>` fails and the **inner `PyErr` is propagated as-is** (`Err(e) => Err(e.into())`) — i.e. a binary body to `request.json()` raises `TypeError`, not a clean JSON error. Observable nuance in `test_request_json.py`: malformed JSON string → handler catches `ValueError` → returns `None` (the route swallows it). Lancelot: keep `request.json()` raising `ValueError` on bad JSON (handlers `except ValueError`), but route binary-body→`request.json()` to a `ValueError` too (cleaner than Robyn's `TypeError` leak).

### 2.5 `json_value_to_py` — the untyped JSON type-preservation contract (`request.rs:270-317`)

KNOWN, verbatim mapping (this *is* the spec `test_json_types.py` enforces):

```rust
const MAX_JSON_DEPTH: usize = 128;                       // recursion guard → ValueError if exceeded
match value {
    Value::Null      => py.None(),                       // → None     (type "NoneType")
    Value::Bool(b)   => b.into_pyobject(py)?,            // → bool     (type "bool")
    Value::Number(n) => if i64 → int, elif u64 → int, elif f64 → float, else ValueError,
    Value::String(s) => s,                               // → str
    Value::Array      => PyList, recursing depth-1,      // → list
    Value::Object     => PyDict, recursing depth-1,      // → dict
}
```

**KNOWN behavioral contract (8 tests in `test_json_types.py`, sync+async = 16 cases):** integers stay `int` (570 not "570"), `null`→`None` (not "null"), `true/false`→`bool`, floats→`float`, strings stay `str`, arrays→`list`, nested objects→`dict`, **top-level arrays parse** (issue #1145 — `["a","b"]` → list, empty `[]` → `[]`). Depth >128 → `ValueError("JSON nesting depth exceeds maximum allowed limit")`.

> **DESIGN.** Lancelot delegates *typed* body validation to pydantic-core (`DESIGN-DECISIONS §1 fork#4`), but **untyped `request.json()` is a separate path** and must reproduce this exact `serde_json::Value`→Python mapping incl. the 128-depth guard and top-level-array support. **Better:** make the depth limit configurable (`app.json_max_depth`, default 128) and surface it as a `413`/`422`-shaped error if the request came over the wire (vs. a raw `ValueError` for in-handler `.json()`). Keep `int` vs `float` discrimination identical (pydantic-core's `lax`/`strict` differs — the *untyped* path must match Robyn/`json`-module semantics, not pydantic's).

---

## 3. The response conversion code — the fast-path chain & its panic vectors (KNOWN — `response.rs`, `executors/mod.rs`)

### 3.1 `extract_response_type_fast` — the 5-tier return-value dispatch (`executors/mod.rs:86-153`)

This is the **handler-return→Rust conversion**, the hottest conversion on the response side. Order matters (verbatim logic):

1. **`downcast_exact::<PyResponse>()`** — exact type (not subclass) → read fields off the Rust struct directly, **zero `getattr`**. Subclasses fall through (preserves overridden properties). Reads `description` via `get_description_from_pyobject` (str/bytes/else→`vec![]`), clones `headers`/`cookies` out of their `Py<...>` (`.borrow(py).clone()` — clones the whole `DashMap`/`HashMap`).
2. **`downcast::<PyDict>()` or `downcast::<PyList>()`** (subclass-aware) → call cached `orjson.dumps` (`ORJSON_DUMPS` `PyOnceLock`, `executors/mod.rs:26-34`), `downcast::<PyBytes>()`, `.to_vec()`, wrap with `content-type: application/json` (`json_headers()`).
3. **`downcast::<PyString>()`** → bytes, `text/plain`.
4. **`downcast::<PyBytes>()`** → bytes, `application/octet-stream`.
5. **`extract::<StreamingResponse>()`** (duck-typed: has `content`+`media_type`).
6. **Fallback `extract::<Response>()`** (the `FromPyObject` getattr chain) — handles user `Response` subclasses; else `TypeError("Function must return a Response, StreamingResponse, dict, list, str, or bytes")`.

**KNOWN observable contract:** bare `dict`/`list` → `content-type: application/json` via orjson (`test_json_types.py` `test_json_dict_response_auto_serialization`, `test_json_list_response_serialization` — asserts `true/false` lowercased, no Python `True`); bare `str` → `text/plain`; bare `bytes` → `application/octet-stream` (`test_binary_output.py` — `b"sync octet"` → `Content-Type: application/octet-stream`, body `"sync octet"`); explicit `Response(...)` honored as-is.

> **DESIGN.** Keep this exact tier order — it is wire-compatible and the tests pin it. **Better:** (a) cache the `orjson.dumps` handle the same way (`PyOnceLock`) but also expose a Rust-side `serde_json`/`simd-json` path for the *typed* return so we never enter Python for `dict`/`list` from a typed handler; (b) `downcast_exact` vs `downcast` distinction is load-bearing (subclass semantics) — replicate precisely; (c) the `octet-stream` for bare `bytes` is a real contract — do not "improve" it to sniff content-type.

### 3.2 `Response` ↔ `PyResponse` conversion + the `FromPyObject` impl (`response.rs:197-425`)

`IntoPyObject for Response` (`response.rs:197-225`): description `Vec<u8>` → `str` if UTF-8 else `PyBytes` (same sniff as request body); headers `into_pyobject().extract()` → `Py<Headers>`; cookies → `Py<Cookies>`; build `PyResponse`, `Py::new`. ~3 allocs.

`FromPyObject for Response` (`response.rs:396-425`) — **manual, not derived**, and it **type-name-gates**:
```rust
let type_name = obj.get_type().name()?;
if type_name != "Response" {
    return Err(PyTypeError::new_err(format!("Expected Response, got {}", type_name)));
}
// then 6 getattr+extract: status_code:u16, response_type:String, headers:Headers,
// description via get_description_from_pyobject, file_path:Option<String>, cookies:Cookies
```
- **The `type_name != "Response"` string check is brittle**: a subclass renamed or a proxy with the right attrs but wrong `__class__.__name__` is rejected here — but tier-6 of `extract_response_type_fast` only reaches this for the *fallback*, and `unit_tests/test_unsupported_types.py` confirms: `Response(...)` passed as a `description=` (i.e. nested) raises `ValueError` (via `check_body_type`, not this gate). Lancelot: prefer a structural/protocol check over a `__name__` string compare (string compare breaks `functools.wraps`, subclassing, and proxies).

`FromPyObject for StreamingResponse` (`response.rs:427-460`): duck-typed (`hasattr("content") && hasattr("media_type")`), not name-gated; force-sets `Content-Type: text/event-stream` and *conditionally* (only if absent) `Cache-Control: no-cache` / `Connection: keep-alive` when `media_type=="text/event-stream"`. **Note the asymmetry**: the `respond_to` path (`response.rs:100-120`) *also* appends streaming headers and uses `Cache-Control: no-cache, no-store, must-revalidate` — so there are **two header-set sites with different values**, which is exactly why `test_sse.py:19` accepts *either* `"no-cache, no-store, must-revalidate"` *or* the doubled `"no-cache, no-cache, no-store, must-revalidate"`. This is a KNOWN bug-as-contract. Lancelot: set streaming headers **exactly once**, deterministically, in one place.

### 3.3 Cookie serialization (`cookie.rs:107-143`) — the one conversion with real validation

`Cookie::to_header_value` uses actix's `cookie` crate builder → RFC 6265 string. `same_site` parsed case-insensitively (`strict`/`lax`/`none`), invalid → `CookieError::InvalidSameSite` → in `Response::respond_to` the error is **swallowed with a `log::debug!` and the cookie is dropped** (`response.rs:72-81`), not surfaced to the client. `expires` is **accepted but ignored** (only `max_age` is serialized; `expires` logs a deprecation `debug!` — `cookie.rs:126-130`). `Cookie::deleted()` = empty value, path `/`, `max_age=0`.

> **DESIGN.** Replace the actix cookie crate with the `cookie` crate directly (engine-independent — actix re-exports it). Keep RFC-6265 builder semantics. **Better:** do *not* silently drop an invalid cookie — that is a silent data-loss footgun; surface a 500 or a structured warning header in dev, log-and-drop only in prod behind a flag. Honor `expires` (some clients/proxies still need it) instead of Robyn's "ignored for API compat."

---

## 4. Error & panic propagation across the seam — the asymmetric two-tier model (KNOWN)

This is the single most important section for boundary stability. The model is **not symmetric** and Lancelot must understand exactly where each kind of failure is caught.

### 4.1 Rust→Python: how a `PyErr` becomes an HTTP response

The Rust seam in `index()` (`server.rs:475-644`) handles errors at **four points**, and *every one collapses to a generic response*:

| Failure point | `server.rs` line | Result | Body |
|---|---|---|---|
| Unsupported HTTP method | 486, 491 | `Response::method_not_allowed(None)` | `"Method not allowed"`, 405, text/plain |
| `from_actix_request` fails (body read error) | 497-500 | `Response::internal_server_error(None)` | `"Internal server error"`, 500, text/plain |
| `new_context` (contextvars) fails | 517-520 | `internal_server_error` | 500 |
| before-middleware raises | 544-554 | `internal_server_error` (logs traceback) | 500 |
| **handler raises (`execute_http_function` Err)** | 579-586 | `internal_server_error` (logs `get_traceback`) | **generic 500, NOT the user's message** |
| route not found | 589 | `Response::not_found(None)` | `"Not found"`, 404, text/plain |
| after-middleware raises | 625-637 | `internal_server_error(Some(&std_response.headers))` | 500, keeps prior headers |

`Response::internal_server_error` (`response.rs:170-181`): hard-coded `b"Internal server error"`, status 500, `default_text_headers()` = `Content-Type: text/plain`. `not_found` = `b"Not found"` 404 text/plain. `method_not_allowed` = `b"Method not allowed"` 405 text/plain. These are `const &[u8]` — **zero-alloc error bodies** (good; steal this).

`get_traceback` (`server.rs:646-658`): `error.traceback(py).format()` → logged string, never sent to client.

**The critical KNOWN fact:** the Rust seam does **not** know about `@app.exception`, 422, or 400. It only ever emits the three canned text bodies. So how does `test_exception_handling.py` get `"error msg: value error"` with status 500?

### 4.2 The real exception→response mapping lives in a Python closure (`router.py:280-330`)

Robyn wraps every handler at registration in `async_inner_handler`/`inner_handler` (`router.py:280-330`). **This closure — not Rust — is what `execute_http_function` actually calls.** Verbatim structure:

```python
try:
    response = self._format_response(await wrapped_handler(*args, **kwargs))
except QueryParamValidationError as err:
    response = Response(400, _TEXT_HEADERS, str(err))                         # 400 text/plain
except PydanticBodyValidationError as err:
    response = Response(422, _JSON_HEADERS, jsonify(err.error_detail))        # 422 JSON
except Exception as err:
    if exception_handler is None: raise                                      # → Rust sees PyErr → generic 500
    response = self._format_response(exception_handler(err))                 # @app.exception result
return response
```

So the boundary contract is precisely:
- **User exception + `@app.exception` registered** → caught in Python, mapped to the handler's `Response` (status 500 in the test: `handle_exception` returns `Response(500, ..., f"error msg: {error}")` — `base_routes.py:952-954`). Rust sees a normal `Response`, never an error. → `test_exception_handling.py` gets `"error msg: value error"`.
- **User exception + NO `@app.exception`** → `raise` re-raises → propagates as `PyErr` across `into_future`/`with_gil` → Rust `execute_http_function` returns `Err` → `server.rs:585` generic 500 `"Internal server error"`. → `test_status_code.py::test_sync_500_internal_server_error` (`/sync/raise` has no specific handler context but the global `@app.exception` IS registered in base_routes, so actually returns the handler body — note: `test_500_error_content_type` only asserts `text/plain`, satisfied either way).
- **Pydantic body invalid** → `PydanticBodyValidationError` → 422 + `jsonify(error_detail)` JSON, **in Python** (`router.py:292-297`). Rust never sees it.
- **Query param coercion fails** → `QueryParamValidationError` → 400 text/plain, in Python.

> **DESIGN — Lancelot's hyper+pyo3 equivalent should do X (better: Y).**
> X (replicate): the *contract* — `@app.exception`→user Response; missing handler→generic 500 text/plain; pydantic→422 JSON; query coercion→400. Keep the canned `const &[u8]` zero-alloc bodies for 404/405/500 (`response.rs:158,172,184`).
> Y (better): **(1)** Do the exception→response mapping in **Rust**, not a per-handler Python closure. The closure is allocated per route and adds a Python frame to every request even on the success path. Lancelot's registration compiler knows the handler's pydantic/dep shape at import; the request executor can `catch` the `PyErr` *at the seam*, classify it (`PyErr::is_instance_of::<...>`), and build the 400/422/500 Response in Rust — one place, no per-route closure, no extra frame. **(2)** 422 must be the *exact FastAPI wire shape* `{"detail":[{type,loc,msg,input,ctx}]}` (`SYNTHESIS steal #17`) — Robyn's `jsonify(err.error_detail)` is not FastAPI-shaped; we must be. **(3)** Stream the exception classification off a single Rust `match err.get_type(py)` rather than Python `except` ladder.

### 4.3 Python→Rust panic propagation — the unguarded seam (KNOWN, the #1 hazard)

There is **no `catch_unwind` anywhere at the FFI seam.** `Cargo.toml` (pass-1 `01 §0`) sets `panic = "abort"`. Consequences, all KNOWN from source:

- Every `.unwrap()` / `.expect()` in a conversion path is a process-killer:
  - `headers.rs:62` `py_values.expect("get-all failed")` — inside `get_all`, a `#[pymethod]` called from Python.
  - `headers.rs:60,89` `value.into_pyobject(py).unwrap()` — in `get_all`/`get_headers`, runs on every header read that goes through these.
  - `headers.rs:73` `values.last().unwrap()` — `get()` on a key whose `Vec` is empty (can happen if `append` logic ever leaves an empty vec) panics.
  - `request.rs:160` `chunk.expect("Failed to read chunk from payload")` — a client that resets mid-body → panic → **worker dies, no respawn**.
  - `multimap.rs:94-95` `key.extract::<String>().unwrap()` / `value.extract::<String>().unwrap()` in `from_py_dict` — non-string keys → panic.
  - `callbacks.rs:215` `pyself.result.get().unwrap()` — relies on the state machine invariant; a logic bug → panic in `__next__`.
- `panic=abort` means **even if a `catch_unwind` existed it would not unwind** — abort short-circuits unwinding. So Robyn's panics are unconditionally fatal. Pass-1 flagged "no crashed-worker supervisor (fatal with `panic=abort`)" — here are the exact panic sites.

> **DESIGN — non-negotiable for Lancelot.**
> 1. Build profile: keep `lto=fat, codegen-units=1, opt-level=3, strip` (Robyn's, good) but **`panic="unwind"`** at least at the FFI crate boundary, OR keep `panic="abort"` *and* run every handler invocation through `std::panic::catch_unwind` *with an `AssertUnwindSafe` shim* — note `catch_unwind` is a no-op under `abort`, so the real fix is `panic="unwind"` + `catch_unwind` at the seam → convert caught panic to a `PyErr`/500.
> 2. **A respawn supervisor** (Robyn has none — `processpool.py` `process.kill()`, pass-1 `03 §0`). Lancelot's Rust supervisor must `SIGCHLD`-reap and respawn a dead worker, with a crash-loop backoff.
> 3. **Zero `.unwrap()`/`.expect()` in any `#[pymethods]` body or any conversion fn.** Lint-enforce: `#![deny(clippy::unwrap_used, clippy::expect_used)]` in the crate, allow only in `#[cfg(test)]`. Every `into_pyobject`/`extract` returns `PyResult`, never panics. This single rule eliminates every panic site listed above.
> 4. Body-read errors → a typed `Response` (a `499`/`400`), never `.expect()`.

### 4.4 The `FutureResultToPy` error channel (`conversion.rs:7-28`)

The async result type that crosses back from a tokio task into the awaitable:
```rust
pub(crate) enum FutureResultToPy { None, Err(PyResult<()>), Value(Py<PyAny>) }
impl IntoPyObject for FutureResultToPy {
    fn into_pyobject(self, py) -> Result<Bound<PyAny>, PyErr> {
        match self {
            None      => Ok(py.None().into_bound(py)),
            Err(res)  => Err(res.err().unwrap()),     // <-- .unwrap() on the error arm
            Value(v)  => Ok(v.bind(py).clone()),
        }
    }
}
```
- `Err(res)` arm does `res.err().unwrap()` — sound *only* because the enum is always constructed as `Err(Err(pyerr))` (`runtime.rs:308`). A `FutureResultToPy::Err(Ok(()))` would panic. Fragile-by-construction. Lancelot: model this as `enum { None, Err(PyErr), Value(Py<PyAny>) }` — carry the `PyErr` directly, no nested `Result`, no `.unwrap()`.

---

## 5. The custom-awaitable poll machinery — full transcription (KNOWN — `callbacks.rs`, 409 lines)

Pass-1 (`01 §6`) listed the 5 awaitables and the "38–55% faster" claim but did **not** transcribe the poll state machine. Here it is in full — this is the asyncio bridge Lancelot reimplements on hyper+pyo3.

### 5.1 The five awaitables and their poll contracts

All are `#[pyclass(frozen, module="robyn._robyn")]`. Each implements the **iterator-as-coroutine** protocol: `__await__` returns self, `__iter__` returns self, `__next__` drives one poll step. `frozen` = no `&mut self` from Python (interior mutability via atomics/`OnceLock`/`RwLock`).

**1. `PyEmptyAwaitable`** (`:12-28`) — `freelist=128` (pooled allocation, 128 instances reused). `__next__` → `None` (immediately "done", yields nothing, no value). Used for fire-and-forget completions. KNOWN: `freelist=128` is the only pooled one.

**2. `PyDoneAwaitable`** (`:30-57`) — holds `PyResult<Py<PyAny>>`. `__next__` immediately raises `StopIteration(value)` (success) or re-raises the error. An *already-resolved* awaitable. `__next__`:
```rust
self.result.as_ref().map_err(|v| v.clone_ref(py))
    .map(|v| Err(PyStopIteration::new_err(v.clone_ref(py))))?
```
(double-`?`: outer propagates the stored `PyErr`; inner is the `StopIteration` carrying the success value — the asyncio "return value" convention).

**3. `PyErrAwaitable`** (`:59-83`) — holds a `PyErr`. `__next__` → `Err(self.err.clone_ref(py))`. An already-failed awaitable. (Cheaper than `PyDoneAwaitable` for the error case — no `Result` indirection.)

**4. `PyIterAwaitable`** (`:85-127`) — the **"iter" fast path** (`runtime.rs:165`, "~55% faster than pyo3_asyncio.future_into_py"). State = `OnceLock<PyResult<Py<PyAny>>>`. `set_result` (called from a `spawn_blocking` once the tokio future completes) sets the `OnceLock` and `drop_ref`s the self-handle. `__next__`:
```rust
if let Some(res) = self.result.get() {
    return res.as_ref().map_err(clone).map(|v| Err(StopIteration(v.clone_ref(py))))?;
}
Ok(Some(py.None()))    // not ready → yield None (asyncio re-polls next loop tick)
```
**The poll model:** while pending, `__next__` returns `Some(None)` — i.e. it yields `None` to the event loop, which means "not done, poll me again." When the tokio task finishes and `set_result` fires, the next `__next__` raises `StopIteration(value)`. **No Python `Future` object, no `add_done_callback`, no event-loop callback** — pure busy-yield. This is why it's fastest *and* why it's only safe for very-soon-to-resolve futures (a long pending op would spin the loop). Robyn uses it for the iter path; uses `PyFutureAwaitable` for the real future-like path.

**5. `PyFutureAwaitable`** (`:129-375`) — the **full asyncio Future protocol** (`runtime.rs:185`, "~38% faster than pyo3_asyncio"). The complex one. State machine:

```rust
#[repr(u8)] enum PyFutureAwaitableState { Pending=0, Completed=1, Cancelled=2 }
struct PyFutureAwaitable {
    state: AtomicU8,
    result: OnceLock<PyResult<Py<PyAny>>>,
    event_loop: Py<PyAny>,
    cancel_tx: Arc<Notify>,                       // tokio Notify — Rust side cancellation signal
    cancel_msg: OnceLock<Py<PyAny>>,
    py_block: AtomicBool,                          // backs _asyncio_future_blocking
    ack: RwLock<Option<(Py<PyAny>, Py<PyDict>)>>,  // the SINGLE done-callback + its context kwargs
}
```

### 5.2 `set_result` — the completion path (`callbacks.rs:165-197`)

Called from `spawn_blocking` (`runtime.rs:198`) when the tokio future resolves:
1. `result.set(value.into_pyobject(py).map(unbind))` — store.
2. `state.compare_exchange(Pending→Completed, Release, Relaxed)` — atomic transition; **if it fails (already Cancelled/Completed) → `drop_ref` and return** (lost race, drop the self-handle).
3. If a done-callback was registered (`ack.read()`), call `event_loop.call_method("call_soon_threadsafe", (cb, self), context_kwargs)` — schedule the asyncio callback **thread-safely** (set_result runs on a blocking-pool thread, not the loop thread).
4. `drop_ref`.

### 5.3 `__next__` — the poll (`callbacks.rs:209-222`)

```rust
fn __next__(pyself: PyRef<Self>) -> PyResult<Option<PyRef<Self>>> {
    if pyself.state.load(Acquire) == Completed {
        return pyself.result.get().unwrap().as_ref()
            .map_err(clone).map(|v| Err(StopIteration(v.clone_ref(py))))?;
    }
    Ok(Some(pyself))                 // pending → yield self (asyncio future protocol)
}
```
**The poll contract:** pending → yield *self* (not `None` — the asyncio future protocol: a future yields itself, the loop sees `_asyncio_future_blocking=True`, registers `add_done_callback`, and suspends the task properly *without busy-spinning*). On completion → `StopIteration(value)`.

### 5.4 The asyncio Future protocol surface (`callbacks.rs:224-375`)

- `_asyncio_future_blocking` getter/setter (`:224-232`) — atomic bool. **This is the magic attribute** the event loop's `Task.__step` reads: if a yielded object has `_asyncio_future_blocking=True`, the loop treats it as a future, sets it `False`, and calls `result.add_done_callback(task.__wakeup)`. This is *the* mechanism that makes a Rust object a valid asyncio awaitable.
- `add_done_callback(cb, context=None)` (`:241-266`) — **single-callback optimization** (the comment, `:238-240`, is explicit): only the *most recent* callback is stored in `ack: RwLock<Option<(cb, kwctx)>>`. If already non-Pending, schedule `call_soon(cb, self)` immediately. **KNOWN tradeoff:** asyncio in practice registers exactly one done-callback per future, so storing one is sound *for Robyn's internal use* — but it is **not** a general-purpose Future (gather/shield/wait register multiple). The doc-comment says it is intentional and internal.
- `remove_done_callback(cb)` (`:271-280`) — clears the single slot regardless of which cb is passed (returns 1 if there was one, else 0). Not cb-matching.
- `cancel(msg=None)` (`:283-320`) — `compare_exchange(Pending→Cancelled)`; on success `cancel_tx.notify_one()` (wakes the tokio `select!` in `future_into_py_futlike`, `runtime.rs:199`, which drops the awaitable) and schedules the done-callback via `call_soon`.
- `done()`/`result()`/`exception()` (`:322-374`) — standard: `result()` returns value / raises `CancelledError(msg)` / raises `InvalidStateError("Result is not ready.")`; `exception()` symmetric.

### 5.5 How the awaitable is driven from the tokio side (`runtime.rs:145-204`)

```rust
// ~55% faster: iter path
fn future_into_py_iter(rt, py, fut) -> Bound<PyAny> {
    let aw = Py::new(py, PyIterAwaitable::new())?;
    rt.spawn(async move { let r = fut.await; rth.spawn_blocking(move |py| PyIterAwaitable::set_result(aw, py, r)); });
    Ok(aw)                       // returned to Python as the awaitable
}
// ~38% faster: future-like path (unix)
fn future_into_py_futlike(rt, py, fut) -> Bound<PyAny> {
    let (aw, cancel_tx) = PyFutureAwaitable::new(event_loop).to_spawn(py)?;
    rt.spawn(async move {
        tokio::select! { biased;
            result = fut => rth.spawn_blocking(move |py| PyFutureAwaitable::set_result(aw, py, result)),
            () = cancel_tx.notified() => rth.spawn_blocking(move |py| aw.drop_ref(py)),  // cancelled
        }
    });
    Ok(aw)
}
```
- The result is marshalled back to Python **on the blocking pool** (`spawn_blocking` → `BlockingRunner`, `blocking.rs`), not on the tokio reactor thread — so the GIL acquisition for `set_result` never blocks the reactor. This is the key perf structure (pass-1 `01 §6.3` named it; here is the exact `select!{biased; result | cancel}` and the `spawn_blocking` GIL-detach).
- Windows path (`runtime.rs:206-279`) uses a *real* `loop.create_future()` + `set_result`/`set_exception` via `call_soon_threadsafe` (no custom awaitable) — because the custom-awaitable trick interacts badly with the Windows proactor loop. **KNOWN platform split.**
- `future_into_py` (`runtime.rs:289-332`): if a tokio runtime is current → `future_into_py_futlike`; else → fall back to `pyo3_async_runtimes::tokio::future_into_py` (the slow library path). So the fast awaitables are used only when called from within the tokio runtime.

> **DESIGN — Lancelot's hyper+pyo3 equivalent should do X (better: Y).**
> X (replicate): the **two-tier awaitable strategy** — a cheap `IterAwaitable`-style busy-yield for futures that resolve almost immediately (e.g. const-tier, cache hits, already-buffered bodies) and a full `FutureAwaitable` with the `_asyncio_future_blocking` protocol for genuine I/O waits. Marshal results back **on a blocking pool thread, not the reactor**. `biased select!{ result | cancel }` for cancellation. `frozen` + atomics for the state. `freelist` for the empty/immediate ones.
> Y (better): **(1)** Implement a *correct multi-callback* `add_done_callback` (Robyn's single-slot breaks `asyncio.gather`/`shield` on these objects — Lancelot exposes streaming/`background.spawn` futures to user code that *will* be gathered). Store `SmallVec<[(cb,ctx);1]>` — 1 inline, spills to heap only under gather. **(2)** Wire `cancel` → not just drop the awaitable but **propagate cancellation into the Rust future** (the disconnect→abort-LLM moat, `DESIGN-DECISIONS §4.5`): `cancel_tx` must abort the upstream HTTP/2 LLM request, not merely stop yielding. **(3)** Carry `PyErr` directly in the result `OnceLock` (no `FutureResultToPy` nested-`Result` `.unwrap()`). **(4)** Keep the pyo3-async-runtimes fallback for the no-tokio case but log a one-time warning (it is the slow path; we want to know if we hit it).

---

## 6. Supporting-type conversions — `Headers`, `QueryParams`, `Cookie`, `FunctionInfo`, `Identity`, `Url`, `HttpMethod` (KNOWN)

### 6.1 `Headers` = `DashMap<String, Vec<String>>`, lowercased everywhere (`headers.rs`)

- `#[pyclass(name="Headers")]`, `DashMap<String,Vec<String>>` (concurrent map — chosen so middleware on other threads can mutate; but the request path is single-task, so the concurrency is mostly unused overhead vs a plain `HashMap`/`IndexMap`).
- **Every key is `.to_lowercase()`'d** on `new`/`set`/`append`/`get`/`contains`/`populate_from_dict`/`from_actix_headers` (`:23,43,48,54,70,98,103,196`). So header lookup is case-insensitive but **the stored/serialized key is whatever lowercase form was first inserted from the wire** (`from_actix_headers` lowercases the wire name). Observable: `http_methods_helpers.py:15` asserts `"server" in response.headers` (lowercase) and `res.headers.get("Content-Type")` works case-insensitively via requests' own CIMultiDict — but Robyn emits **lowercased header names on the wire** (HTTP/1.1 allows this; HTTP/2 requires it). `test_binary_output.py` asserts `r.headers.get("Content-Type")` — `requests` is case-insensitive so this passes regardless, but a raw-socket client would see `content-type`.
- `into_pyobject` for a header value list uses `.into_pyobject(py).unwrap()` (`:60,89`) — panic vector (see §4.3).
- `set` **replaces** (`insert(key, vec![value])`, `:43`); `append` **adds** (`entry().or_default().push`, `:48`). `get` returns the **last** value (`:73`); `get_all` returns the `PyList` of all.
- `__eq__` (`:120-144`): order-independent multiset comparison per key.
- `set_missing` (`:183-190`): insert only if key absent — **this is the global-response-header merge** (`server.rs:598`): middleware-set headers win over global defaults. The fix for duplicate `Access-Control-Allow-Origin` (`:180-182` comment). KNOWN contract: `http_methods_helpers.py:14` `response.headers.get("global_after") == "global_after_request"` — global after-header always present unless excluded.

> **DESIGN.** Use `http::HeaderMap` (hyper-native, already lowercase-normalized, `HeaderName`/`HeaderValue` are validated & cheap to clone, supports multi-value). Do **not** round-trip header names through `String::to_lowercase()` per access — `HeaderMap` keys are pre-normalized. Expose a Python `Headers` view that is **lazy** over the `HeaderMap` (don't build a `DashMap` mirror). `DashMap` is the wrong structure: the request is single-task; use `HeaderMap`. Keep the observable semantics: case-insensitive get, `set`=replace, `append`=add, `get`=last, `get_all`=list, `set_missing` merge precedence, order-independent `__eq__`. **Better:** preserve original-case on the wire for HTTP/1.1 (some legacy clients/proxies are case-sensitive in practice) while comparing case-insensitively — `HeaderMap` already does the right thing here.

### 6.2 `QueryParams` = `HashMap<String, Vec<String>>` (`multimap.rs`)

`#[pyclass(subclass)]` (subclassable from Python). Plain `HashMap` multimap. `get` = last value, `get_first` = first, `get_all` = clone of the vec, `to_dict` → `PyDict` of `PyList`s. `from_py_dict` (`:91-99`) does `.extract::<String>().unwrap()` on key and value — **panic if non-string** (§4.3). Lancelot: `IndexMap<Box<str>, SmallVec<[Box<str>;1]>>` for insertion-order stability (matters for OpenAPI/round-trip determinism), no `.unwrap()` in conversion.

### 6.3 `FunctionInfo` — the handler ABI, and its GIL-taking `Clone` (`function_info.rs:27-72`)

```rust
#[pyclass] #[derive(Debug)]
pub struct FunctionInfo {
    #[pyo3(get,set)] handler: Py<PyAny>,
    #[pyo3(get,set)] is_async: bool,
    #[pyo3(get,set)] number_of_params: u8,
    #[pyo3(get,set)] args: Py<PyDict>,
    #[pyo3(get,set)] kwargs: Py<PyDict>,
}
impl Clone for FunctionInfo {
    fn clone(&self) -> Self {
        Python::with_gil(|py| Self {                       // <-- Clone ACQUIRES THE GIL
            handler: self.handler.clone_ref(py), is_async: self.is_async,
            number_of_params: self.number_of_params,
            args: self.args.clone_ref(py), kwargs: self.kwargs.clone_ref(py),
        })
    }
}
```
- The entire handler contract is these 5 fields. `number_of_params:u8` drives the arg-dispatch in `get_function_output` (`executors/mod.rs:156-199`): 0 → `call0()` (skip request conversion entirely — **the key fast path**); 1 → `call1((args,))` *unless* `kwargs` has `global_dependencies`/`router_dependencies` then `call((),Some(kwargs))`; else → `call((args,),Some(kwargs))`.
- **`Clone` takes the GIL** (`:62-71`). The router stores/clones `FunctionInfo` on the routing path (pass-1 `02 §3`). Any clone = an implicit `Python::with_gil` = GIL contention even for routing. **KNOWN hidden cost.**
- `MiddlewareType` enum has a manual `__hash__` (`:17-25`) because "pyo3 doesn't support hashing enums from Python" (`:19` comment) — `DefaultHasher` over the discriminant.

> **DESIGN.** Split the ABI: an `Arc<HandlerMeta>` holding `is_async`, arity, the compiled DI/validator plan, and the param names — **GIL-free to clone** (it's an `Arc`); plus a separate `Py<PyAny>` handler handle cloned *only when actually invoking*. The router stores `Arc<HandlerMeta>`; routing never touches the GIL (Robyn's does, via `FunctionInfo::clone`). Keep the arity fast paths (0-param → call0, the `global_/router_dependencies` kwargs branch) — they are real wins and the tests (`test_dependency_injection.py`) depend on the kwargs-injection behavior. Don't expose `args`/`kwargs` as mutable `PyDict`s on the hot path; bake them into the compiled plan at registration.

### 6.4 `Identity` / `Url` / `HttpMethod` (`identity.rs`, `types/mod.rs`)

- `Identity` (`identity.rs`): `#[pyclass]` wrapping `HashMap<String,String>` claims, `get,set`. Trivial. Lancelot: same, or fold into request as a lazy attr.
- `Url` (`mod.rs:86-107`): `#[pyclass]` `{scheme,host,path}` all `#[pyo3(get)]` String. Built in `from_actix_request` from `connection_info()` + normalized path. `test_basic_routes.py::test_http_request_info_get` pins `{scheme:"http", host:"127.0.0.1:8080", path:"/sync/http/param"}`. Lancelot: build from `hyper::Uri` + `Host` header; **host must include port** (Robyn's does — `connection_info().host()` returns `127.0.0.1:8080`).
- `HttpMethod` (`mod.rs:22-84`): `#[pyclass]` enum GET..TRACE, `is_supported`/`from_actix_method` (unknown → `Err("Method Not Allowed")` → 405), `Display` impl. `test_status_code.py::test_405` sends method `"NONSTANDARD"` → expects 405 text/plain. Lancelot: `http::Method` is already an enum; map unknown/extension methods → 405 exactly (Robyn rejects anything outside the 9 standard methods — `mod.rs:38-51`; note this rejects valid extension methods like `PROPFIND` — preserve unless we deliberately widen).
- `get_body_from_pyobject`/`get_description_from_pyobject` (`mod.rs:109-129`): str→bytes, bytes→bytes, **else→`Ok(vec![])` (silent)**. `check_body_type`/`check_description_type` (`:131-147`): the **erroring** counterparts — non-str/bytes → `PyValueError("Could not convert specified body to bytes")`. `unit_tests/test_unsupported_types.py` is the spec: `Response(description=X)` for `X in [None,1,True,A,{...},[...],Response(...)]` → **`ValueError`**; `X in ["OK", b"OK"]` → OK. Lancelot: replicate exactly — the `Response` constructor validates description type and raises `ValueError` for anything not `str`/`bytes`; the *getter*-side conversion stays lossy-silent for back-compat OR (better) we make both erroring and accept the minor breaking change with a clear message, since silent-empty-body is a debugging nightmare.

---

## 7. The hybrid-model behavioral contract — what the integration/unit tests assert about the seam (KNOWN — Lancelot's conformance suite)

Every item below is a test-enforced invariant of the Rust↔Python boundary. This is the **executable spec** Lancelot's calibration suite must pass to be wire-compatible. (`integration_tests/`, 5,250 lines; the load-bearing assertions:)

### 7.1 Universal response invariants (`helpers/http_methods_helpers.py:8-16`)
Every single response (all routes, all methods) is checked for:
- `response.headers.get("global_after") == "global_after_request"` — the global `@app.after_request` (`base_routes.py:202-204`) sets this; proves after-middleware runs on **every** response including 404/500/streaming. **Contract: global after-middleware must execute even on error/not-found responses** (`server.rs:602-641` runs after-middleware on the `ResponseType::Standard` 404/500 too).
- `"server" in response.headers and == "robyn"` — a global response header injected via `set_missing` (`server.rs:598`). Lancelot: a configurable `Server:` header default, present on every response, mergeable/overridable by handler (precedence: handler > global, per `set_missing`).

### 7.2 Status codes & error bodies (`test_status_code.py`)
- `/404` → 404 (handler returns `Response(404,...)`); `/real/404` (no route) → 404 body **exactly `"Not found"`**, `Content-Type: text/plain` (`response.rs:158`, `:152-155`).
- `/202` → 202 (`Response(status_code=202,...)` honored).
- `/{sync,async}/raise` → 500 (global `@app.exception` returns a 500 body). `Content-Type: text/plain`.
- Unknown method `"NONSTANDARD"` on `/` → **405**, body `"Method not allowed"`, `text/plain` (`response.rs:184`, `mod.rs:64`).

### 7.3 Exception mapping (`test_exception_handling.py`)
- `/{sync,async}/exception/{get,put,post}` raise `ValueError("value error")`; with global `@app.exception` (`base_routes.py:952-954` → `Response(500, ..., f"error msg: {error}")`) the response is **status 500, body exactly `"error msg: value error"`**. Confirms: exception caught in the **Python wrapper closure** (`router.py:298-303`), not Rust; `str(ValueError("value error"))` == `"value error"`. Lancelot's Rust-side exception classifier must produce byte-identical output: `f"error msg: {error}"` where `error` is the Python exception's `str()`.

### 7.4 Body & type round-trip (`test_request_json.py`, `test_json_types.py`, `test_binary_output.py`, `test_request_object.py`)
- POST `'{"hello":"world"}'` → `request.json()` is `dict`; malformed `'{"hello":"world"` → handler `except ValueError` → returns `None` → body `"None"`. So **`request.json()` raises `ValueError` on bad JSON** (not TypeError, not silent) — `request.rs:262` `PyValueError::new_err(format!("Invalid JSON: {e}"))`.
- JSON type preservation (16 cases): `int`↔`int`, `null`↔`None`, `bool`↔`bool`, `float`↔`float`, `str`↔`str`, `[..]`↔`list`, `{..}`↔`dict`, top-level arrays, mixed. Exact `json_value_to_py` mapping (§2.5).
- Bare `bytes` return → `Content-Type: application/octet-stream`, body decodes to the text (`test_binary_output.py`). Bare `dict`/`list` → `application/json`, JSON booleans lowercased (`"true"`/`"false"`, never Python `True`).
- `unit_tests/test_request_object.py`: `Request(...)` constructible from Python with `Headers({"Content-Type":"application/json"})`, `Url(scheme,host,path)`, `path_params={}`, `body=""`, etc.; `request.headers.get("Content-Type")` works (case-insensitive lookup, lowercased store). Confirms the `#[new]` constructor signature & getter contract (`request.rs:222-249`).

### 7.5 Streaming / SSE (`test_sse.py`, 443 lines)
- `Content-Type: text/event-stream` (forced).
- `Cache-Control` ∈ {`"no-cache, no-store, must-revalidate"`, `"no-cache, no-cache, no-store, must-revalidate"`} — the **double-set bug accepted as contract** (`test_sse.py:19`). Two header-write sites (`response.rs:111` builder vs `:285`/`:449` the `FromPyObject`/ctor). Lancelot: write once → emit exactly one canonical value (we *fix* this; our test asserts the single clean value).
- SSE framing: lines `data: <payload>\n` then blank line (`\n\n` separates events); optional `event: <type>\n`, `id: <n>\n`, `retry: <ms>\n` *before* data (`responses.py:167-200` `SSEMessage`). Multi-line data → one `data: ` line per split line. Empty data → `data: \n`.
- Both sync `def` generators (`yield`) and `async def` generators supported; async wrapped by `AsyncGeneratorWrapper` which **runs a nested `loop.run_until_complete` per `__next__`** (`responses.py:106-121`) — the catastrophic per-token cost pass-1 flagged, here is the exact mechanism: `create_python_stream` (`response.rs:123-148`) `spawn_blocking` → `with_gil` → `gen.call_method0("__next__")` → `extract::<String>()` → `Bytes::from(string)`, repeat. **One thread hop + one GIL acquire + one full String copy + (for async) one `run_until_complete` per SSE event.**

> **DESIGN — the streaming seam must be replaced wholesale.** Lancelot's pump: a native Rust `Stream` that, for an async-generator handler, drives `__anext__` via the *same custom-awaitable machinery* (§5) on the tokio runtime — **no `spawn_blocking` per token, no nested event loop, no per-token GIL round-trip for the await itself**. Coalesce on time+size (~25 ms / 2 KiB, `DESIGN-DECISIONS §4.5`) so the GIL is acquired ~once per 10 ms not once per token. SSE framing in Rust (`b"data: %b\n\n"`), zero-copy from `Bytes`. Single canonical `Cache-Control`. Disconnect → `cancel_tx.notify` → cancel the Rust stream → abort upstream LLM (the moat). Keep the *observable* SSE wire format byte-identical (the `test_sse.py` parsing must still pass) while deleting the per-token Python/thread/GIL tax.

### 7.6 Middleware & context (`test_middlewares.py`, contextvars routes)
- `before_request` can **short-circuit** by returning a `Response` (`base_routes.py:263-265` `/sync/middlewares/401` → `Response(401,...)`); the route handler is then skipped (`server.rs:540-543` `early_response`). Contract: before-middleware returning a Response = early exit, 401 body.
- `before_request` mutates `request.headers` and the handler sees it (`base_routes.py:222-237` sets `before` header, route asserts it). Confirms the request object is **shared/threaded** through phases (the per-request `contextvars.Context`, `server.rs:515-522`, `asyncio.rs:47-49`).
- Per-request context isolation: `ContextVar` written in `before_request` visible in handler & `after_request` (Python ≥3.11 via `loop.create_task(coro, context=ctx)`, `asyncio.rs:63-85`); on 3.10 it falls back to plain `await` (writes NOT visible — Robyn's documented degradation). Lancelot requires ≥3.11 (`DESIGN-DECISIONS §1`), so always the strong contract — no fallback, no degraded mode.

---

## 8. The exact FFI conversion plan for Lancelot (the 6-bullet synthesis — DESIGN)

1. **Lazy `#[pyclass] Request` over `hyper` parts, 1 alloc/request.** Owns `http::request::Parts` + `OnceCell<Bytes>` body + `SmallVec` path params; `headers`/`query`/`json`/`body`/`form`/`files` are `#[getter]`s that materialize into per-field `OnceCell`s on first touch. **No `#[derive(FromPyObject)]`** (forces eager re-marshal in middleware) — middleware mutates the proxy in place via Rust setters. 0-param and const-tier handlers never build the proxy at all (generalize `executors/mod.rs:168`). Target: 1 alloc vs Robyn's 6+. Body decode UTF-8-sniff (str-if-valid-else-bytes) preserved byte-for-byte. Header view backed by `http::HeaderMap` (no `DashMap` mirror, no per-access `to_lowercase`).
2. **Response: keep the exact 5-tier `extract_response_type_fast` order** (`downcast_exact::<Response>` → dict/list via cached `orjson`-equiv → str/bytes → StreamingResponse → getattr fallback), it is wire-pinned by the tests. Replace the `__name__`-string gate in `FromPyObject for Response` with a structural/`isinstance` check. Add a Rust-side typed serialization path (pydantic-core `dump_json`) so typed handlers never re-enter Python. Canned `const &[u8]` zero-alloc bodies for 404/405/500 (steal verbatim).
3. **Exception mapping moves into Rust at the seam, not a per-route Python closure.** One `catch` of the `PyErr` after the handler `.await`, classify via `err.is_instance_of::<...>`: pydantic→**FastAPI-exact 422 JSON** (`{"detail":[{type,loc,msg,input,ctx}]}`), query-coerce→400 text, `@app.exception`-registered→call user handler, none→generic 500 `"Internal server error"` text/plain. Output for the `@app.exception` path must be byte-identical to Robyn (`f"error msg: {str(exc)}"`). Eliminates the per-handler wrapper closure & its extra Python frame on the success path.
4. **Two-tier custom awaitables (port `callbacks.rs`’s model, fix its bugs).** `IterAwaitable`-style busy-yield (`freelist`, `OnceLock`) for immediately-resolving futures; full `FutureAwaitable` with `_asyncio_future_blocking` + `add_done_callback` + `cancel`/`result`/`exception` for real I/O. **Fix:** multi-callback `add_done_callback` (`SmallVec<[_;1]>`, so `asyncio.gather`/`shield` work); `cancel` propagates into the Rust future and **aborts the upstream LLM** (the moat); result channel carries `PyErr` directly (kill `FutureResultToPy`’s nested-`Result` `.unwrap()`). Marshal results back **on the blocking pool, not the reactor** (`biased select!{ result | cancel }`). Keep the no-tokio `pyo3-async-runtimes` fallback but warn-once.
5. **Zero-panic FFI seam + supervisor.** `#![deny(clippy::unwrap_used, clippy::expect_used)]` crate-wide (test-only allow). Every `into_pyobject`/`extract`/`#[pymethods]` returns `PyResult`, never panics — eliminates every Robyn panic site (`headers.rs:60,62,73,89`, `request.rs:160`, `multimap.rs:94`, `callbacks.rs:215`). `panic="unwind"` at the FFI crate + `catch_unwind(AssertUnwindSafe)` wrapping each handler invocation → caught panic becomes a 500 `PyErr`, not a dead worker. A Rust supervisor that `SIGCHLD`-reaps and respawns workers with crash-loop backoff (Robyn has none).
6. **Smaller, stable ABI surface.** `#[pymodule]` exports only what Python must see: `Server`, lazy `Request`, `Response`, `StreamingResponse`/`SSEResponse`, an `Arc<HandlerMeta>`-backed registration type (not a GIL-cloning `FunctionInfo`), `SocketHeld`-equiv, the awaitable set, the WS set. Fold `Headers`/`QueryParams`/`Url`/`Identity` into lazy request attributes. `module="lancelot._lancelot"` pinned on every pyclass. Routing stores `Arc<HandlerMeta>` (GIL-free clone) — never `FunctionInfo::clone` (which takes the GIL, `function_info.rs:62`). Auto-generate the `.pyi` from the Rust definitions (Robyn's hand-written one drifts).

---

## 9. Top 3 boundary-stability hazards (ranked — the things that will break Lancelot if copied naively)

**Hazard #1 — `panic=abort` + `.unwrap()`/`.expect()` in conversion code = unkillable correlated worker death.** Robyn has ~8 panic sites directly on the request/header/streaming path (`headers.rs:60,62,73,89`; `request.rs:160`; `multimap.rs:94-95`; `callbacks.rs:215`; `conversion.rs:21`), `panic="abort"`, **no `catch_unwind`, no respawn supervisor**. A single malformed header value, a client RST mid-body, or a non-string query key takes the whole worker down and it never comes back — under load this cascades (kernel rebalances accept to surviving workers → they hit the same input → die too). **Mitigation is non-optional:** lint out every `unwrap/expect`, `panic="unwind"` + `catch_unwind` at the seam, respawn supervisor with backoff. This is the difference between a toy and production.

**Hazard #2 — the exception/error contract is split across Rust and a per-route Python closure, with the *meaningful* mapping only in Python.** The Rust seam (`server.rs:485-644`) can only ever say "500 / 404 / 405 text/plain"; 422/400/`@app.exception` all live in `router.py:280-330`’s wrapper. If Lancelot moves error handling into Rust (which it should, for the per-request frame win) it must **exactly reproduce**: the FastAPI-shaped 422, the 400 text for query coercion, the `f"error msg: {str(exc)}"` for the user handler, the generic 500 when no handler — *and* preserve "after-middleware and global response headers run even on the error/404 path" (`server.rs:597-641`, asserted on every test via `http_methods_helpers.py:13-16`). Getting the *status* right but the *body bytes* or the *header presence* wrong silently fails the entire calibration suite. The double-set `Cache-Control` (`test_sse.py:19`) is a microcosm: two header-write sites, divergent values, accepted-as-contract — Lancelot must consolidate to one site and update the conformance assertion to the single clean value, deliberately and documented, not accidentally.

**Hazard #3 — eager FFI marshalling is load-bearing for *correctness* of middleware, not just a perf cost.** `Request::into_pyobject` eagerly builds `form_data`/`files`/`headers`/body, and `#[derive(FromPyObject)] for Request` round-trips the *whole object* back into Rust whenever a before-middleware returns a (mutated) request (`executors/mod.rs:296`, `server.rs:537-539`). A naive "lazy proxy" that defers materialization will **break the middleware contract** (`test_middlewares.py`: before-middleware sets a header, handler reads it; before-middleware can return a `Response` to short-circuit; per-request `contextvars.Context` threads mutations across phases — `server.rs:515-522`). Lancelot's lazy proxy must support **in-place mutation that is visible across phases without a Rust↔Python round-trip** (mutate the `HeaderMap`/proxy directly; never `extract::<Request>()` to re-read it) — otherwise the lazy optimization silently regresses middleware semantics that the test suite enforces. Laziness is correct only if "the proxy *is* the shared mutable request," not "the proxy is rebuilt per phase."

---

## 10. The response *write* path — `Responder`, `apply_hashmap_headers`, and the static-file conversion (KNOWN — `response.rs`, `io_helpers/mod.rs`)

Pass-1 counted the *read* side (handler-return → Rust). The *write* side (Rust `Response` → HTTP bytes) is also a conversion seam with its own contract, and it is where headers/cookies get serialized onto the wire.

### 10.1 `apply_hashmap_headers` — the header-flatten write (`io_helpers/mod.rs:11-19`)

```rust
#[inline]
pub fn apply_hashmap_headers(response: &mut HttpResponseBuilder, headers: &Headers) {
    for iter in headers.headers.iter() {                 // DashMap iter
        let (key, values) = iter.pair();
        for value in values {
            response.append_header((key.clone(), value.clone())); // CLONE key+value PER value
        }
    }
}
```
- **One `String` clone for the key and one for each value, per header line.** A response with 10 headers averaging 1 value each = 20 `String` allocations on the write path *after* the handler already paid for the `Py<Headers>` on the read path. `append_header` (not `insert_header`) → **multi-value headers emit one line each** (RFC-correct for `Set-Cookie`, etc.).
- Header name is whatever lowercase string is in the `DashMap` (from `from_actix_headers`/`set`). So **Robyn emits lowercased header names** (`content-type`, `server`, `global_after`). HTTP/1.1 tolerates this; HTTP/2 mandates it. The test client (`requests`) is case-insensitive so tests pass, but a byte-level wire diff vs FastAPI (which preserves `Content-Type` case via Starlette) would differ. KNOWN observable-if-you-look.

### 10.2 `Responder for Response` — status + headers + cookies (`response.rs:62-85`)

```rust
fn respond_to(self, _req) -> HttpResponse<BoxBody> {
    let mut b = HttpResponseBuilder::new(
        StatusCode::from_u16(self.status_code).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR)); // bad status → 500
    apply_hashmap_headers(&mut b, &self.headers);
    for (name, cookie) in &self.cookies.cookies {
        match cookie.to_header_value(name) {
            Ok(hv) => { b.append_header(("Set-Cookie", hv)); }
            Err(e) => { log::debug!("Skipping invalid cookie '{}': {}", name, e); } // SILENT DROP
        }
    }
    b.body(self.description)                              // Vec<u8> → body, moved (no copy)
}
```
- **`StatusCode::from_u16(x).unwrap_or(INTERNAL_SERVER_ERROR)`** — a handler returning `Response(status_code=999)` silently becomes a **500**, not an error. `test_status_code.py` only exercises valid codes (202, 307, 404, 405, 500); the 999→500 coercion is untested behavior Lancelot can either replicate or improve (better: reject invalid status at `Response.__new__` with `ValueError`, fail-fast at construction not at write).
- Cookies iterate a `HashMap` → **non-deterministic `Set-Cookie` order** across responses. `test` only asserts presence/value (`/cookie/multiple` sets `session`+`theme`, `/cookie/overwrite` sets `session` twice → HashMap keeps last → one `Set-Cookie`). Robyn's `Cookies` is keyed by name so "overwrite" is `insert` semantics (`cookie.rs:162-164`) — the test `test cookie overwrite` depends on this (`base_routes.py:723-728`: two `set_cookie("session",...)` → final value wins). Lancelot: keep name-keyed overwrite semantics; use `IndexMap` for deterministic emission order.
- `b.body(self.description)` — the `Vec<u8>` body is **moved**, not copied, into the actix body. Good (the one zero-copy spot on the write side). Lancelot: same — own the body `Bytes`, hand it to hyper by move.

### 10.3 Static-file conversion — `read_file` + `set_file_path` (`io_helpers/mod.rs:28-33`, `response.rs:349-361`)

```rust
pub fn read_file(file_path: &str) -> Result<Vec<u8>> {           // BLOCKING std::fs, full read into Vec
    let mut f = File::open(file_path)?; let mut buf = vec![]; f.read_to_end(&mut buf)?; Ok(buf)
}
// PyResponse::set_file_path:
self.response_type = "static_file"; self.file_path = Some(path);
match read_file(path) { Ok(c) => self.description = PyBytes::new(py, &c).into(),  // FULL FILE → Python heap
                        Err(e) => Err(PyIOError::new_err(format!("Failed to read file: {}", e))) }
```
- **The entire file is read synchronously into a `Vec<u8>`, then copied into a `PyBytes` on the Python heap, then (on response) copied again into the actix body.** No `sendfile`, no streaming, no `Range`, no `ETag`, no `304`. `io_helpers/mod.rs:27` even admits `// ideally this should be async`. `SYNTHESIS §3` already flagged Robyn's static as weak; here is the exact cost: **2 full copies of the file through the GIL-heap per request.** `test_static_files_with_api_routes.py`/`test_file_download.py` only assert content presence, not range/conditional.
- `FileResponse`/`serve_file` (`responses.py:45-62`) sets `Content-Disposition: attachment; filename=...` and guesses MIME via `mimetypes`. Observable: download tests check the disposition + body.

> **DESIGN — Lancelot's hyper+pyo3 equivalent should do X (better: Y).**
> X (replicate): name-keyed cookie overwrite; `append_header` multi-value (one line per `Set-Cookie`); body moved not copied; invalid-status fallback *behavior* visible to tests (none invalid tested → free to fix).
> Y (better): **(1)** Reject invalid status codes at `Response` construction (`ValueError`), not silent→500 at write. **(2)** Header write directly over `http::HeaderMap` — names are already `HeaderName` (no `String::clone` per line; `HeaderName`/`HeaderValue` clone is a refcount/inline copy, not a heap alloc). **(3)** Static files via `tokio::fs` + `sendfile`/`tower-http::ServeDir`: zero-copy kernel path, real `ETag`/`Last-Modified`/`Range`/`304`/`HEAD` — never read a file into the Python heap. The `Response.set_file_path` API stays for compat but routes to the Rust static tier (the file never crosses the FFI seam). **(4)** Deterministic header/cookie emission order (`IndexMap`) so wire output is reproducible (matters for CDN cache keys and golden-file calibration vs FastAPI). **(5)** Do not silent-drop invalid cookies in prod without at least a structured log+metric; in dev, surface a 500.

---

## 11. The WebSocket boundary — a *different* FFI model (actor + Rust channel) (KNOWN — `websockets/mod.rs`, `web_socket_executors.rs`)

The WS path does **not** use the request/response conversion at all — it is a separate seam Lancelot must replicate independently. Pass-1 `02 §7` sketched the actor model; here is the exact crossing code.

### 11.1 The two crossing primitives (`websockets/mod.rs:27-63`)

```rust
#[pyclass] pub struct WebSocketChannel {                       // Rust→Python pull channel
    receiver: Arc<tokio::sync::Mutex<mpsc::UnboundedReceiver<Option<String>>>>,
}
#[pymethods] impl WebSocketChannel {
    fn receive<'py>(&self, py) -> PyResult<Bound<'py,PyAny>> {
        let receiver = self.receiver.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {  // <-- SLOW library path, NOT the custom awaitable
            let mut rx = receiver.lock().await;
            match rx.recv().await { Some(Some(m)) => Ok(Some(m)), _ => Ok(None) }  // None = closed
        })
    }
}
#[pyclass] pub struct WebSocketConnector {                     // the actix actor, exposed to Python
    id: Uuid, router: HashMap<String,FunctionInfo>, task_locals: TaskLocals,
    registry_addr: Addr<WebSocketRegistry>, query_params: QueryParams,
    message_sender: Option<mpsc::UnboundedSender<Option<String>>>,   // stays in the actor
    message_channel: Option<Py<WebSocketChannel>>,                   // handed to Python
}
```
- **The message crossing is an `mpsc::UnboundedSender<Option<String>>`**: actor side `send`s, Python side `await channel.receive()` pulls. `None` sentinel = connection closed. **Unbounded** → a fast producer (flooding client) with a slow Python consumer = **unbounded memory growth** (no backpressure). KNOWN defect; Lancelot must use a *bounded* channel with a drop/close policy.
- `WebSocketChannel::receive` uses `pyo3_async_runtimes::tokio::future_into_py` — the **slow library awaitable**, not the custom `PyFutureAwaitable`. So every WS message receive pays the ~38–55% awaitable overhead Robyn eliminated everywhere else. Inconsistent. Lancelot: route WS receive through the *same* fast awaitable as HTTP.
- Message payload is **`String` only** (`Option<String>`) — binary WS frames are not modeled here. `test_web_socket_json` sends/receives JSON *as text* (`json.loads(ws.recv())`). Contract: text frames; the handler returns `Option<String>` → sent as a text frame (`web_socket_executors.rs:44-46,60`).

### 11.2 The handler dispatch (`web_socket_executors.rs:9-68`)

```rust
pub fn execute_ws_function(function, task_locals, ctx, ws) {
    if function.is_async {
        let fut = Python::with_gil(|py| {
            let handler = function.handler.bind(py).downcast()?;
            let result = handler.call1((ws.clone(),))?;             // pass the connector
            pyo3_async_runtimes::into_future_with_locals(task_locals, result)
        })?;                                  // errors → log + return (connection silently does nothing)
        let f = async move { match fut.await {
            Ok(out) => Python::with_gil(|py| out.extract::<Option<String>>(py).unwrap_or(None)),
            Err(e)  => { log::error!("Async WebSocket handler failed: {}", e); None }
        }}.into_actor(ws).map(|res,_,ctx| if let Some(m)=res { ctx.text(m); });
        ctx.spawn(f);                         // spawned on the actor's context
    } else { Python::with_gil(|py| { /* sync: call1, extract Option<String>, ctx.text */ }); }
}
```
- **Errors are swallowed to a `log::error!` and the connection just doesn't respond** (`web_socket_executors.rs:22-25,36-38`). No close frame, no error frame — a raising WS handler looks like a hung connection to the client. KNOWN defect.
- `out.extract::<Option<String>>(py)` — handler must return `str` or `None`. Returning anything else → `unwrap_or(None)` (sync path logs; async path `unwrap_or(None)` silently). Contract from `test_web_sockets.py`: handler returns a string → client `ws.recv()` gets exactly that string; returns `None` → no message. The broadcast test (`test_web_socket_raw_benchmark`) confirms the registry `SendMessageToAll`/`SendText` path (`registry.rs`) is the multi-client fan-out, order **not** guaranteed (`sorted(...)` in the test).
- DI works on WS handlers too (`test_websocket_di`): `global_dependencies`/`router_dependencies` kwargs injected via the same `FunctionInfo.kwargs` mechanism as HTTP (`base_routes.py:111-131`).

> **DESIGN.** Lancelot's WS seam on hyper: `tokio-tungstenite` (locked, `DESIGN-DECISIONS §1`) not actix actors. Replace the `mpsc::UnboundedSender` with a **bounded** `tokio::sync::mpsc` (config'd depth, default 64) + an explicit close on overflow (slow-consumer protection — the AI moat's backpressure principle applies to WS too). Route `channel.receive()` through the **same custom `PyFutureAwaitable`** as HTTP (consistency + the 38–55% win). Model **binary frames** (`Message::Binary(Bytes)` → Python `bytes`), not text-only. On handler error: send a WS close frame with a policy code + structured log, never silently hang. Keep the `handler.call1((connector,))` shape and the `Option<String>`(+`Option<Bytes>`) return contract so `test_web_sockets.py`-style suites pass. DI via the compiled plan, not per-call `FunctionInfo.kwargs` dict probing.

---

## 12. Reimplementable Lancelot skeleton — the FFI crate, end to end (DESIGN, KNOWN-derived)

This is the concrete code shape that satisfies §1–§11. Every decision traces to a cited Robyn behavior it replicates or fixes.

```rust
// Cargo.toml (FFI crate) — locked stack, fixed profile
// [dependencies] pyo3="0.27"(extension-module,abi3-py311,py-clone)  pyo3-async-runtimes="0.27"(tokio-runtime)
//   tokio="1"(full)  hyper="1"  hyper-util  http-body-util  http  tower="0.5"  tower-http(fs,trace)
//   tokio-tungstenite  matchit="0.8"  serde_json  bytes  smallvec  indexmap  socket2  crossbeam-channel
// [profile.release] lto="fat" codegen-units=1 opt-level=3 strip=true panic="unwind"  // <-- unwind, NOT abort (§4.3)
#![deny(clippy::unwrap_used, clippy::expect_used)]                    // kills every Robyn panic site (§4.3)

// ---- ABI surface: deliberately smaller than Robyn's 20 (§1) ----
#[pymodule] fn lancelot(_py: Python, m: &Bound<PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(version, m)?)?;
    m.add_class::<Server>()?; m.add_class::<Request>()?; m.add_class::<Response>()?;
    m.add_class::<StreamingResponse>()?; m.add_class::<SocketHeld>()?;
    m.add_class::<WsChannel>()?; m.add_class::<WsConnector>()?;
    m.add_class::<EmptyAwaitable>()?; m.add_class::<DoneAwaitable>()?;
    m.add_class::<ErrAwaitable>()?; m.add_class::<IterAwaitable>()?; m.add_class::<FutureAwaitable>()?;
    Ok(())                          // Headers/Query/Url/Identity/Cookie folded into Request (lazy attrs)
}

// ---- Handler ABI: GIL-free clone (fixes function_info.rs:62) ----
struct HandlerMeta {                 // Arc'd; routing clones the Arc, never the GIL
    is_async: bool, arity: u8,
    plan: CompiledPlan,              // DI DAG + validators + serializer, compiled at registration (FastAPI steal)
    declared: SmallVec<[ParamSlot;4]>,
}
#[derive(Clone)] struct Handler { meta: Arc<HandlerMeta>, callable: Py<PyAny> }

// ---- Lazy request proxy: 1 alloc, no derive(FromPyObject) (§2.1-2.4) ----
#[pyclass(name="Request", module="lancelot._lancelot")]
struct Request {
    parts: http::request::Parts,                 // hyper-native; HeaderMap already lowercase-normalized
    body: OnceCell<Bytes>,                        // not decoded until .body/.json touched
    path_params: SmallVec<[(Box<str>,Box<str>);4]>,
    py_headers: OnceCell<Py<PyAny>>,              // built on first .headers access
    py_json: OnceCell<Py<PyAny>>,                 // built on first .json() call
}
#[pymethods] impl Request {
    #[getter] fn headers(&self, py: Python) -> PyResult<Py<PyAny>> {
        // case-insensitive get / set=replace / append=add / get_all=list / set_missing-merge (§6.1),
        // backed directly by self.parts.headers (no DashMap mirror, no per-access to_lowercase)
        Ok(self.py_headers.get_or_try_init(|| build_header_view(py, &self.parts.headers))?.clone_ref(py))
    }
    #[getter] fn body(&self, py: Python) -> PyResult<Py<PyAny>> {
        let b = self.body.get().ok_or_else(|| PyValueError::new_err("body not buffered"))?;
        Ok(match std::str::from_utf8(b) {            // EXACT Robyn UTF-8 sniff (§2.2) — wire-compatible
            Ok(s)  => PyString::new(py, s).into_any().unbind(),
            Err(_) => PyBytes::new(py, b).into_any().unbind(),
        })
    }
    fn json(&self, py: Python) -> PyResult<Py<PyAny>> {  // ValueError on bad JSON (§2.4-2.5), depth-128 guard
        let b = self.body.get().ok_or_else(|| PyValueError::new_err("body not buffered"))?;
        let v: serde_json::Value = serde_json::from_slice(b)
            .map_err(|e| PyValueError::new_err(format!("Invalid JSON: {e}")))?;  // matches request.rs:262
        json_value_to_py(py, &v, 128)                    // exact int/uint/float/null/bool/list/dict mapping
    }
}

// ---- The seam: ONE place catches PyErr → classified Response (§4.1-4.2 moved into Rust) ----
async fn invoke(h: &Handler, req: &Request, ctx: &Py<PyAny>) -> ResponseType {
    let call = std::panic::AssertUnwindSafe(run_handler(h, req, ctx));
    match futures::FutureExt::catch_unwind(call).await {        // panic → 500, never a dead worker (§4.3)
        Ok(Ok(rt)) => rt,
        Ok(Err(pyerr)) => classify(pyerr),                      // ↓ the whole error contract, in Rust
        Err(_panic)    => ResponseType::Standard(canned_500()), // catch_unwind boundary
    }
}
fn classify(py_err: PyErr) -> ResponseType { Python::with_gil(|py| {
    if py_err.is_instance_of::<PydanticValidationError>(py) {
        return json_422(fastapi_shaped(&py_err, py));           // {"detail":[{type,loc,msg,input,ctx}]} (§4.2 Y)
    }
    if py_err.is_instance_of::<QueryParamError>(py) {
        return text_400(py_err.value(py).str().map(|s|s.to_string()).unwrap_or_default());
    }
    if let Some(uh) = USER_EXC_HANDLER.get() {                  // @app.exception equivalent
        if let Ok(r) = call_user_handler(py, uh, &py_err) { return r; }   // body == f"error msg: {str(exc)}"
    }
    ResponseType::Standard(canned_500())                        // const b"Internal server error", text/plain
})}
// canned_404/405/500 = `const &[u8]` zero-alloc bodies, text/plain (steal response.rs:158,172,184 verbatim)

// ---- Two-tier awaitable: port callbacks.rs model, fix the bugs (§5) ----
#[pyclass(frozen, freelist=128, module="lancelot._lancelot")] struct EmptyAwaitable;     // immediate, pooled
#[pyclass(frozen, module="lancelot._lancelot")]
struct FutureAwaitable {
    state: AtomicU8,                         // 0 Pending 1 Completed 2 Cancelled (callbacks.rs:129-134)
    result: OnceLock<Result<Py<PyAny>, PyErr>>,   // PyErr DIRECT — no FutureResultToPy nested-Result (§4.4)
    event_loop: Py<PyAny>, cancel_tx: Arc<Notify>,
    callbacks: RwLock<SmallVec<[(Py<PyAny>,Py<PyDict>);1]>>,  // MULTI-callback (fixes single-slot §5.4)
    py_block: AtomicBool,
}
// __next__: Completed → StopIteration(value); Pending → yield self (asyncio future protocol, callbacks.rs:209)
// _asyncio_future_blocking getter/setter; add_done_callback pushes (multi); cancel → cancel_tx.notify
//   AND propagates into the Rust future to ABORT the upstream LLM (the moat, §5 Y / DESIGN-DECISIONS §4.5)
// driver: rt.spawn(select!{ biased; r = fut => spawn_blocking(set_result),
//                                    () = cancel.notified() => spawn_blocking(abort+drop) })
//   result marshalled on the BLOCKING POOL, never the reactor (runtime.rs:198 model)

// ---- Streaming: native async-gen pump, NOT spawn_blocking-per-token (§7.5) ----
fn stream_body(gen: Py<PyAny>) -> impl http_body::Body {
    // drive gen.__anext__() via FutureAwaitable on the tokio runtime; coalesce time+size (~25ms/2KiB);
    // SSE frame in Rust: b"data: %b\n\n"; zero-copy Bytes; ONE canonical Cache-Control (fixes test_sse.py:19);
    // client-disconnect → cancel_tx → abort gen + abort upstream LLM request
}
```

Every line above is traceable: lazy proxy ← `request.rs:32-87` cost; GIL-free `Arc<HandlerMeta>` ← `function_info.rs:62`; `classify()` ← `router.py:280-330` + `server.rs:585`; canned bodies ← `response.rs:158-194`; awaitable ← `callbacks.rs:129-375`; multi-callback fix ← `callbacks.rs:238`; streaming pump ← `response.rs:123-148`; `panic="unwind"`+`catch_unwind` ← the §4.3 panic-site list.

---

## 13. Conformance checklist — the exact byte-level assertions Lancelot's calibration suite must pass (KNOWN, distilled)

Run these against both Robyn (golden) and Lancelot; bytes must match (except the deliberate `Cache-Control` single-value fix, §3.2/§7.5, which is documented divergence):

| # | Stimulus | Expected (byte-exact) | Source |
|---|---|---|---|
| C1 | any response, any route | header `server: robyn`(→`lancelot`), `global_after: global_after_request` present | `http_methods_helpers.py:13-16`, `server.rs:598` |
| C2 | GET unknown route | 404, body `Not found`, `Content-Type: text/plain` | `test_status_code.py:13-16,33-38`, `response.rs:158` |
| C3 | method `NONSTANDARD` on `/` | 405, body `Method not allowed`, `text/plain` | `test_status_code.py:51-56`, `mod.rs:64`, `response.rs:184` |
| C4 | handler `raise ValueError("value error")` + `@app.exception` | 500, body exactly `error msg: value error` | `test_exception_handling.py:25-26`, `router.py:298-303`, `base_routes.py:952-954` |
| C5 | `Response(status_code=202,...)` | status 202 honored | `test_status_code.py:19-20` |
| C6 | handler returns `b"sync octet"` | `Content-Type: application/octet-stream`, body `sync octet` | `test_binary_output.py:18-21`, `executors/mod.rs:132-138` |
| C7 | handler returns `["four", 1, True, None]`-style list | `application/json`, JSON `true/false`/`null` lowercased, no Python `True` | `test_json_types.py:172-216`, `executors/mod.rs:113-121` |
| C8 | POST `{"lid":570,"start":null,"active":true,"price":19.99}` echo types | `int`/`NoneType`/`bool`/`float`/`str`/`list`/`dict` preserved | `test_json_types.py:7-119`, `request.rs:280-317` |
| C9 | POST top-level array `["a","b"]` to `request.json()` | parsed as `list`, `[]`→`[]` | `test_json_types.py:125-166` |
| C10 | POST malformed `'{"hello":"world"'`, handler `except ValueError: return None` | body `None` (`.json()` raised `ValueError`) | `test_request_json.py:11`, `request.rs:262` |
| C11 | `?hello=robyn&a=1&b=2` → echo query_params | `{"hello":["robyn"],"a":["1"],"b":["2"]}`, empty→`{}` | `test_split_request_params.py:9-15`, `request.rs:132-141` |
| C12 | before-middleware returns `Response(401,...)` | 401, handler skipped | `test_middlewares.py`, `base_routes.py:263-265`, `server.rs:540-543` |
| C13 | before-middleware sets header, handler reads it | header visible in handler (shared request) | `base_routes.py:222-237`, `server.rs:537` |
| C14 | CORS preflight `OPTIONS` w/ `Access-Control-Request-Method` | **204**, `Access-Control-Allow-Origin` exactly once, `...-Credentials: true` | `test_cors_preflight.py:74-111` |
| C15 | SSE endpoint | `Content-Type: text/event-stream`; `data: <p>\n\n` framing; `event:`/`id:`/`retry:` before data | `test_sse.py:11-120`, `responses.py:167-200` |
| C16 | `unit: Response(description=<non str/bytes>)` | raises `ValueError` | `test_unsupported_types.py:27-34`, `mod.rs:131-138` |
| C17 | `unit: Request(...)` constructed in Python, `request.headers.get("Content-Type")` | case-insensitive hit, `method=="GET"`, `url.scheme/host` set | `test_request_object.py`, `request.rs:222-267` |
| C18 | `/sync/http/param` | JSON `{method:"GET", url:{host:"127.0.0.1:8080", path:"/sync/http/param", scheme:"http"}}` | `test_basic_routes.py:67-99`, `mod.rs:86-107` |
| C19 | WS: send → handler returns str | client `recv()` == that exact str; returns `None` → no frame | `test_web_sockets.py:10-30`, `web_socket_executors.rs:44-46,60` |
| C20 | WS broadcast to N clients | all receive, order **not** guaranteed | `test_web_sockets.py:16-18`, `registry.rs` |

This 20-row table is the executable definition of "Lancelot is wire-compatible with Robyn/FastAPI at the FFI boundary." It is the acceptance gate referenced by `DESIGN-DECISIONS §3` (the non-negotiable v0 surface) and the calibration plan.

---

*Pass-2 generated 2026-05-19 from line-by-line read of `robyn/Robyn/src/{callbacks,conversion,io_helpers,lib,runtime,blocking,asyncio}.rs`, `src/types/{function_info,identity,multimap,mod,headers,cookie,request,response}.rs`, `src/executors/mod.rs`, `src/server.rs`, `src/router.py`, `robyn/responses.py`, and `integration_tests/`+`unit_tests/`. Stack-locked to hyper+tower+tokio+pyo3 per `DESIGN-DECISIONS §1`. `Lancelot/` is gitignored. Cross-ref: pass-1 `research/robyn/01..03`, `SYNTHESIS §1-4`, `DESIGN-DECISIONS §3-5`.*
