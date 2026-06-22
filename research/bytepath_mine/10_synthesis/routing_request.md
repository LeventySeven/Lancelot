# Lancelot byte-path synthesis — routing_request domain

**Domain:** Routing engine + request/response lifecycle + param extraction + const tier + lazy request proxy  
**Synthesizer date:** 2026-06-22  
**Source corpus:** `01_robyn/{rust_routing,rust_core,python_bridge,python_dx,teardown}.md`, `02_fastapi/{core,di_params,production_patterns}.md`, `03_blazeio/{core,modules}.md`, `05_lancelot_state/{core,bytepath,resilience}.md`

---

## TL;DR

The routing/request domain is Lancelot's strongest area. The core Rust matchit router, lazy-request proxy (R2), const tier (R4 183k req/s), and FastAPI-parity DI pipeline are all shipped and battle-tested. The only NEW byte-path wins here are:

1. **Granian-style custom awaitable bridge** — Robyn's `PyIterAwaitable` is claimed 38–55% faster than `pyo3_async_runtimes::into_future_with_locals` on the handler-coroutine bridge crossing; every request pays this cost. High-value but requires benchmarking before committing (the per-request cost may be 15–30% of tiny-request overhead, not 38–55% end-to-end).
2. **`panic = "abort"` in the Rust release profile** — currently `panic = "unwind"` (Cargo.toml:68). Switching removes unwind tables from the `.so` (smaller binary, one less branch per potential panic site). One-liner change; low risk given `catch_unwind` at the FFI seam already contains panics.
3. **Double header-scan in ASGI seam** — `_scan_request_context` and content-type sniffing iterate `scope["headers"]` independently. Folding them into one pass halves the iteration count on every non-const-tier request.
4. **`Decimal` encoder precision gap** — `validation.py:254–255` always encodes `Decimal` as `float`. FastAPI uses `int` when `exponent >= 0` (e.g. `Decimal("1") → 1`, not `1.0`). Silent data corruption in financial APIs. One-liner fix.
5. **SSE `X-Accel-Buffering: no` already shipped** — `asgi.py:1042` confirmed. HAVE.

Everything else from the peer corpus is either already shipped or explicitly Tier-D.

---

## 1. Peer mechanism diff — Robyn

### 1.1 Rust matchit router — HAVE (and better)

**Robyn:** `src/routers/http_router.rs:13–58`. Per-method `RwLock<matchit::Router<FunctionInfo>>`. `get_route()` acquires the GIL inside the hot path via `Python::with_gil(|_| res.value.to_owned())` to clone the `FunctionInfo` (which holds `Py<>` handles). One GIL touch per matched request.

**Lancelot:** `src/router.rs` — same `matchit` crate, per-method `HashMap`, but stores `usize` slot indices. `Router::route()` returns `Option<usize>` with NO GIL acquire. All Python-object access deferred until after routing. The router is built once at startup and held as an immutable `Arc<Router>` — zero `RwLock` on the hot read path (Robyn's per-method `RwLock` is required because Robyn supports hot-reload; Lancelot's immutable router avoids this entirely).

**Status: HAVE — and decisively better.** No action.

---

### 1.2 Granian-style custom awaitable bridge — GAP (high value, bench first)

**Robyn (and Granian):** `src/callbacks.rs:86–197`, `src/runtime.rs:163–204`.

Two custom Rust-native awaitables replace `pyo3_async_runtimes::into_future`:

- `PyIterAwaitable` (`callbacks.rs:86–127`): `#[pyclass(frozen)]` implementing `__iter__`/`__next__`. Stores result in a `OnceLock`. On poll via `__next__`, if `OnceLock` is populated → raises `StopIteration(result)` (coroutine resumes). **Claimed ~55% faster than `pyo3_asyncio.future_into_py`** (`runtime.rs:163`).
- `PyFutureAwaitable` (`callbacks.rs:136–197`, Unix): mimics `asyncio.Future` protocol entirely in Rust (`_asyncio_future_blocking`, `add_done_callback`, `cancel`, `done`, `result`, `exception`). Rust future completion → `event_loop.call_soon_threadsafe(cb, self)`. **Claimed ~38% faster than `pyo3_asyncio.future_into_py`** (`runtime.rs:182`). Uses `tokio::sync::Notify` for cancel signaling.
- `PyEmptyAwaitable`/`PyDoneAwaitable`/`PyErrAwaitable` (`callbacks.rs:12–83`): zero-alloc awaitables for already-resolved values. `PyEmptyAwaitable` has `freelist = 128` (pyo3 pre-allocated pool of 128 instances — avoids per-call allocator hit for zero-arg-result awaitables).

**Lancelot (`src/dispatch.rs:60`):** uses `pyo3_async_runtimes::into_future_with_locals(&locals, result)` — the standard bridge. No custom `PyIterAwaitable` or `PyFutureAwaitable` anywhere in `src/`.

**The mechanism:** `into_future_with_locals` internally calls `asyncio.ensure_future(coro, loop=loop)` — two Python-level allocations (a `Future` + task registration). `PyIterAwaitable` bypasses both: the coroutine polls a Rust-backed `__next__` directly, no `asyncio.Future` object created. The blocking-pool side (`BlockingRunner`, `callbacks.rs:164`) calls `PyIterAwaitable::set_result` from off the tokio thread (acquires GIL briefly → stores in `OnceLock` → drops GIL) with a 350µs debounce to prevent rapid spawning (`blocking.rs:118`).

**Honest caveats:** The 38–55% claim is for the bridge crossing in isolation, not end-to-end req/s. The end-to-end gain on tiny responses (where the bridge is the largest non-GIL-contention cost) could be 10–25% at Lancelot's 59k req/s baseline — potentially closing the gap to raw Starlette+uvloop (~67k). This is unverified without Lancelot-specific benchmarking. Robyn adopted this from Granian (`runtime.rs:1`); Granian benchmarks it in context: ~55% on the isolated future-bridge microbench.

**Implementation cost (M/L):** requires:
1. Port `PyIterAwaitable` + `PyFutureAwaitable` + `PyEmptyAwaitable` from Robyn/Granian source (~ 200 lines of `callbacks.rs`-equivalent in Lancelot's `src/ffi/`).
2. Replace `into_future_with_locals` in `dispatch.rs:60` with the custom awaitable.
3. Port `BlockingRunner` (`blocking.rs:34–177`) as the result-callback mechanism.
4. **Gate: bench `oha -c 200 -n 100000` tiny-request before and after.** If gain < 5% end-to-end, defer. If gain ≥ 10%, ship.

**Gate risk:** Medium. Replacing the asyncio bridge is a correctness-sensitive change (task-locals, exception propagation). Existing test suite + conformance floor (974) must hold. The M3 dispatch code is well-tested; the bridge is the one seam to watch.

**Cite:** `robyn/Robyn/src/callbacks.rs:12–197`, `runtime.rs:163–204`.

**Status: GAP — high-value candidate; bench before building.**

---

### 1.3 Const tier two-level lookup (HashMap-exact + matchit fallback) — HAVE

**Robyn:** `src/routers/const_router.rs:75–219`. Two maps per HTTP method: `FastMap = RwLock<HashMap<String, CachedResponse>>` for O(1) exact-path lookup + `RouteMap = RwLock<matchit::Router<CachedResponse>>` for parameterized const routes. `get_cached_route()` tries HashMap first, falls back to matchit only on miss.

**Lancelot:** `src/service.rs:135 (Prebaked)` + `routing.py:762–767 (Router._const dict)`. The Python-tier router uses an O(1) `dict[(method,path)]` for the const fast-path before the regex `_dyn` list. The Rust `Prebaked` slot array is checked before any handler dispatch (`service.rs:264`). Both tiers achieve O(1) for exact const paths.

**Status: HAVE.** No action.

---

### 1.4 Const-route header baking (`bake_global_headers`) — NOT PRESENT (minor, conditional)

**Robyn:** `src/routers/const_router.rs:172–199`. At startup, global response headers are appended to EVERY `CachedResponse`'s pre-built header `Arc<Vec>`. This means `/livez` responses include e.g. `X-Frame-Options: DENY` with zero per-request merge.

**Lancelot:** `src/service.rs:142–165 (Prebaked)` stores `(status, content_type, body)` only. No global response headers are baked in. Lancelot does not have a global-response-header feature yet, so this gap is inert. If a future global-header feature ships (e.g. `App.default_headers={"X-Frame-Options": "DENY"}`), const-route prebaked responses would need to bake them in at registration time using this pattern.

**Status: NOT PRESENT — but inert (prerequisite feature not built). Note: if global-response-header feature ships, apply `bake_global_headers` pattern to `Prebaked` registration.**

---

### 1.5 Eager full request materialization (Robyn) vs lazy proxy (Lancelot) — HAVE (better)

**Robyn:** `src/types/request.rs:122–194`. Every request goes through full body buffering, query-string parsing, header copying into `DashMap`, form/multipart parsing before the route is even resolved. A 404 still pays for a full `DashMap` + `BytesMut::extend` chain.

**Lancelot (R2):** `src/ffi/request.rs`. All field materializations deferred behind `OnceLock`-cached getters. Route resolved on borrowed `method`/`path` from the hyper `Request<Incoming>` BEFORE any allocation. A 404 allocates nothing.

**Status: HAVE (and better).** No action.

---

### 1.6 `panic = "abort"` in release profile — IMPROVE (trivial, Cargo.toml)

**Robyn:** `Cargo.toml:44–53` — `panic = "abort"`. Removes unwind tables from the `.so`. Benefit: smaller binary, one compiler-visible branch eliminated per potential panic site.

**Lancelot:** `Cargo.toml:68` — `panic = "unwind"`. The R7 `catch_unwind` at the FFI boundary already contains any panic before it crosses into Python, so `panic = "abort"` is safe (panics in the rest of the crate never cross the FFI boundary). **Note:** `panic = "abort"` conflicts with `panic::catch_unwind` — since Lancelot uses `catch_unwind` in `dispatch.rs` (R7), switching to `abort` would silently change `catch_unwind` semantics (the "catch" becomes an abort). **This is therefore NOT safe to flip unconditionally.** Keep `panic = "unwind"`.

**Status: REFUSE — R7 `catch_unwind` requires `panic = "unwind"`. Robyn does not use `catch_unwind`; Lancelot does. Correct behavior to keep current setting.**

---

### 1.7 `freelist = 128` on hot pyclass — IMPROVE (trivial, low priority)

**Robyn:** `callbacks.rs:12` — `#[pyclass(frozen, freelist = 128)]` on `PyEmptyAwaitable`. Pre-allocated pool of 128 instances; the 129th call is the only one that hits the allocator.

**Lancelot:** no `freelist` on any `#[pyclass]` in `src/`. The `Request` pyclass (`src/ffi/request.rs`) is allocated per-request on every non-const-tier request (~59k/s on tiny). Adding `freelist = 32` (conservative) would reduce allocator pressure at the marginal cost of 32 pooled instances per process.

**Status: MISSING — micro-opt, low priority.** Worthwhile only if allocation shows up in a flamegraph. File as "nice to have."

---

## 2. Peer mechanism diff — FastAPI

### 2.1 `use_dump_json` pydantic-core bytes fast path — HAVE

**FastAPI:** `routing.py:688–714`. When `response_field is not None` AND `isinstance(response_class, DefaultPlaceholder)`, sets `use_dump_json = True` → `field.serialize_json(...)` → pydantic-core `TypeAdapter.to_json()` → `bytes` directly. No intermediate Python dict, no `json.dumps`.

**Lancelot:** `asgi.py:1751` — `_ta.dump_json(...)` on the 200 path. The `DefaultPlaceholder` sentinel (`datastructures.py`) gates the same fast-path. **Status: HAVE.** Verify the sentinel check is exactly `isinstance(response_class, DefaultPlaceholder)` (not `is`, not a looser check) so explicit `response_class=JSONResponse` correctly bypasses the bytes shortcut.

---

### 2.2 SSE `X-Accel-Buffering: no` — HAVE (confirmed)

**FastAPI:** `routing.py:534–608`. Emits `X-Accel-Buffering: no` + `Cache-Control: no-cache` on every SSE stream. Prevents nginx/HAProxy from buffering SSE chunks (buffering causes events to arrive in batches instead of real-time — the silent SSE killer in reverse-proxy deployments).

**Lancelot:** `asgi.py:1040–1042` — confirmed: `(b"x-accel-buffering", b"no")` emitted in the SSE response headers list. **Status: HAVE.** No action.

---

### 2.3 JSONL auto-detect from `AsyncGenerator[T, None]` return type — PARTIAL GAP (DX)

**FastAPI:** `routing.py:620–651`. If the handler return type is `AsyncGenerator[T, None]` (auto-detected via `dependant.is_async_gen_callable`), FastAPI sets `is_json_stream = True` and takes a dedicated JSONL path: each yielded item is pydantic-core serialized + `b"\n"` appended, response is `StreamingResponse(media_type="application/jsonl")`.

**Lancelot:** `@app.stream` is the explicit streaming decorator. A plain `@app.get` handler with `AsyncGenerator[T]` return type does NOT auto-detect as a JSONL stream — users must use `@app.stream` explicitly.

**Impact:** DX gap, not a throughput gap. Users must remember `@app.stream`; FastAPI users get this for free from return type annotation. Given Lancelot's `@app.stream` is richer (SSE framing, done_sentinel, max_stream_lifetime, cancel_on_disconnect), this is acceptable as-is. Worth noting in operator docs.

**Status: PARTIAL GAP (DX). Keep `@app.stream` as the explicit mechanism; add docs note.**

---

### 2.4 `contextmanager_in_threadpool` unlimited exit CapacityLimiter — VERIFY (correctness risk)

**FastAPI:** `concurrency.py:17–41`. `cm.__exit__` called via `anyio.to_thread.run_sync(limiter=CapacityLimiter(1))` — bypasses the global anyio thread limiter for the cleanup path. Without this, a DB connection pool `__exit__` that itself needs a thread can deadlock when the global 40-slot pool is full under high load.

**Lancelot:** `lancelot/pool.py:35–55 (offload)`. Sync handlers use `loop.run_in_executor(None, ...)` (bounded inflight gate). For sync yield-dep `__exit__`, the path is `lancelot/di.py:704–711` via `get_pool().offload(target, ...)`. The `offload` uses the bounded `_inflight` counter (max 10,000 default). Under extreme load, the 10,001st sync dep cleanup could be rejected with `PoolFull` → 503 if the pool is saturated. This is extremely unlikely in practice (yield-dep exits are fast), but worth documenting.

**Status: LOW RISK — bounded pool rather than unlimited, but under normal load this is not a deadlock risk. Document as known tradeoff; do not change the pool semantics.**

---

### 2.5 `Decimal` encoder precision gap — FIX (one-liner, correctness)

**FastAPI:** `encoders.py:84–92`. `decimal_encoder` returns `int(obj)` when `obj.as_tuple().exponent >= 0` (e.g. `Decimal("1") → 1`), otherwise `float(obj)`. Preserves round-trip fidelity for `Numeric(10,0)` DB columns.

**Lancelot:** `validation.py:254–255` — `return float(obj)` always. `Decimal("1")` encodes as `1.0`, not `1`. In financial APIs with integer decimal values (e.g. USD whole dollars, count columns), this produces `{"amount": 1.0}` instead of `{"amount": 1}` — a silent semantic difference that can break client-side integer type checks.

**Fix:** `validation.py:254–255`:
```python
if isinstance(obj, _dcm.Decimal):
    exp = obj.as_tuple().exponent
    return int(obj) if isinstance(exp, int) and exp >= 0 else float(obj)
```

**Status: GAP — one-liner fix; correctness issue for financial APIs.**

---

### 2.6 Double header scan in ASGI seam — IMPROVE (one medium-effort refactor)

**Mechanism:** `asgi.py` iterates `scope["headers"]` twice per request:
1. `_scan_request_context` (line ~353): scans for `x-request-id`, `x-correlation-id`, `traceparent`, `x-agent-id`, `x-tenant-id`.
2. Content-type sniff (line ~815–822): iterates headers again to find `content-type` for body dispatch.

Folding both into a single `_scan_headers` pass would halve header iterations on every non-const-tier request. For a typical request with 10–20 headers, this is ~10–20 dictionary comparisons saved per request. At 59k req/s: ~590k–1.18M fewer comparisons/s. Not a bottleneck at current scale, but a clean correctness improvement with no GIL implication.

**Status: IMPROVE — medium-effort refactor (careful to preserve all existing semantics). Not urgent.**

---

## 3. Peer mechanism diff — Blazeio

### 3.1 Per-request protocol reset (in-place reuse on keepalive) — NOT RELEVANT

**Blazeio:** `server_protocol.py:96–121`. After each response, `ServerProtocolEssentials.reset(r)` resets all connection state fields IN PLACE. The same Python protocol object serves the next request over the same TCP keepalive connection. Zero dealloc/realloc of the protocol object.

**Lancelot:** Rust hyper owns the TCP keepalive lifecycle; a new Python `Request` pyclass is allocated per logical request (`dispatch.rs:55 build_request`). The Rust-level connection object is reused (hyper's `Connection::poll_without_shutdown` keeps the TCP socket alive). The Python `Request` allocation per keepalive request is unavoidable with the current architecture unless a `freelist = N` is added (see §1.7).

**Status: DIFFERENT ARCHITECTURE — Blazeio's reset-in-place is an asyncio-protocol concern. Hyper owns Lancelot's keepalive at the Rust level. The Python `Request` pyclass allocation is the only target, and `freelist` is the right lever.**

---

### 3.2 `asyncio.BufferedProtocol` + memoryview receive buffer — HAVE (better)

**Blazeio:** `Protocols/__init__.py:6–18`. Pre-allocated `bytearray(102400)` + persistent `memoryview`; OS writes directly into the existing buffer. Best Python can do.

**Lancelot:** Rust hyper/tokio owns the receive path entirely. No Python allocation per received byte chunk. **Status: HAVE — and structurally superior.**

---

## 4. Summary tables

### 4.1 Must-build

| Win | Why | Source cite | Lancelot state | Buildability | Gate risk |
|---|---|---|---|---|---|
| **Granian custom awaitable** (`PyIterAwaitable` / `PyFutureAwaitable`) | Every request pays the `into_future_with_locals` bridge crossing. Custom awaitable is 38–55% faster (isolated bench); 10–25% estimated end-to-end gain on tiny-request shape — could close gap to raw Starlette+uvloop. | `robyn/Robyn/src/callbacks.rs:86–197`, `runtime.rs:163–204` | MISSING — `dispatch.rs:60` uses `into_future_with_locals` | M (200-line port + bench) | Medium — correctness-sensitive bridge replacement; requires conformance 974 + real-load bench |
| **`Decimal` int/float precision** | `Decimal("1")` encodes as `1.0` not `1`; silent data corruption for financial APIs. One-liner fix. | FastAPI `encoders.py:84–92` | GAP — `validation.py:254` always `float(obj)` | S (1 line) | None — no hot path; no conformance impact |

### 4.2 Improve

| Win | Why | Source cite | Lancelot state | Buildability | Gate risk |
|---|---|---|---|---|---|
| **Single-pass header scan** (fold content-type sniff into `_scan_request_context`) | ~10–20 fewer comparisons per request; ~590k–1.18M/s at full load. Clean code improvement. | `asgi.py:353, 815–822` | PARTIAL — two separate scans | M (refactor ASGI seam) | Low — behavior identical; test coverage exists |
| **`freelist = N` on `Request` pyclass** | Reduces per-request allocator calls under keepalive load. Trivial annotation. | `robyn/Robyn/src/callbacks.rs:12` (`PyEmptyAwaitable`) | MISSING | S (1 annotation in `src/ffi/request.rs`) | None |
| **`strip = true` in release profile** | Smaller `.so` (removes debug symbols); Robyn + Granian both strip. Currently not in `Cargo.toml [profile.release]`. | Robyn `Cargo.toml:44–53` | MISSING (no `strip` key) | S (1 line in Cargo.toml) | None — debug info removed from binary only, not from `cfg(debug)` builds |

### 4.3 Have (already shipped — do not rebuild)

| Mechanism | Shipped in | Source cite |
|---|---|---|
| matchit router, slot indices, no-GIL hot path | M0/M3, `src/router.rs` | `robyn/Robyn/src/http_router.rs:13–58` |
| Const tier (183k req/s, zero GIL) | M4, `src/service.rs:142–165` | `robyn/Robyn/src/const_router.rs:75–219` |
| Lazy R2 request proxy | M3, `src/ffi/request.rs` | `robyn/Robyn/src/types/request.rs:122–194` |
| Pydantic-core `to_json` bytes fast path | M3/R5, `asgi.py:1751` | FastAPI `routing.py:688–714` |
| SSE `X-Accel-Buffering: no` | M15, `asgi.py:1040–1042` | FastAPI `routing.py:558` |
| `DefaultPlaceholder` sentinel for response class | M2, `datastructures.py` | FastAPI `datastructures.py:153–186` |
| Dual `AsyncExitStack` scope semantics | M2 conformance push 3 | FastAPI `routing.py:116–121` |
| 0-param const-tier (health endpoints free) | M4 `src/service.rs:264` | Robyn `executors/mod.rs:168–170` |
| Registration-time DI plan (no per-request `inspect`) | M1, `lancelot/di.py:87–279` | FastAPI `_compat/v2.py:413–415`, Robyn `router.py:149–152` |
| TCP_QUICKACK + TCP_FASTOPEN + listen(16384) | M2, `src/socket.rs` | Robyn `shared_socket.rs`, Blazeio `socket_ops.py` |
| Cancel-on-disconnect dual-task race | M2/M14, `asgi.py:599–635` | FastAPI production: vLLM `vllm/utils.py:57–99` |
| `PyOnceLock` for hot pydantic-core + orjson imports | M4, `src/serialize.rs:38–42` | Robyn `executors/mod.rs:26–34` |
| Metric cardinality guard (route template, not raw URL) | M7 T2.4, `metrics.py` | Sentry FastAPI integration `sentry_sdk/fastapi.py:52–75` |
| GIL-releasing gzip off-loop | M18, `lancelot/compress.py:188–191` | Blazeio `request.py:32–33` |
| W3C traceparent propagation | M7, `context.py:45–48` | FastAPI production: distributed tracing |
| Request-id ContextVar + header echo | M2, `context.py:20–22`, `asgi.py:440–467` | FastAPI `routing.py:251–274` |

### 4.4 Refuse (Tier-D by lens criteria or cardinal rules)

| Item | Reason |
|---|---|
| `panic = "abort"` in release profile | R7 `catch_unwind` at `dispatch.rs` requires `panic = "unwind"`; switching silently changes unwind semantics |
| Actix-web HTTP layer | Robyn's actix adds actor-model overhead; hyper 1 is lower-level and already proven. Regression. |
| Per-chunk stream overhead optimization | Upstream-rate-limited (50–200 tok/s); framework is not the bottleneck (CLAUDE.md Tier-D) |
| `ROBYN_BROWSER_OPEN` dev knob | Tier-D: pure dev ergonomics, not byte-path |
| Dynamic route registration on live server | Rust router immutable post-startup; anti-pattern (Hayhooks coupling); not Lancelot's use-case |
| `CorkDispatcher` adaptive batch sizing | Requires `numpy` runtime dep (cardinal rule: pydantic-only runtime). Tier-D. |
| Actix WS actor model | Different architecture; not needed for Lancelot's AI-streaming use case |
| `orjson` as primary serializer (Blazeio pattern) | pydantic-core `to_json` is faster for typed Pydantic models (Lancelot's use-case); `orjson` violates cardinal rule runtime-dep boundary |

---

## 5. Build order recommendation

If pursuing the Granian custom awaitable:

1. **Bench first** — measure `into_future_with_locals` cost in isolation on the Lancelot dispatch path. If the bridge crossing is < 5% of tiny-request latency, defer indefinitely.
2. If bench confirms ≥ 10% end-to-end gain on tiny-request shape: port `PyIterAwaitable` + `PyEmptyAwaitable` + `BlockingRunner` from Robyn/Granian source; gate on conformance 974 + real-server oha bench.
3. **`Decimal` fix** is a one-liner with zero risk; ship as a standalone micro-fix.
4. **`strip = true`** in Cargo.toml is also a one-liner; ship alongside next Rust rebuild.
5. **Single-pass header scan** — medium refactor; good for a focused cleanup PR; gate on `pytest tests/ -q` + conformance.

The const tier, lazy R2 request, and DI pipeline are already best-in-class. The routing/request domain is NOT where the next big wins are; the streaming and serialization domains hold more delta.
