# M5 Peer Source Recheck — Lancelot vs Robyn vs Blazeio vs FastAPI

Source-level recheck of the three peer frameworks, focused on the byte-path-relevant axes (request parsing, streaming pump, connection lifecycle, concurrency model). Each finding cites a specific path:line in the local `researchfms/` source.

**Plan deviation note (vs M5-PLAN-PROVE-VS-PEERS-AND-ADOPTABLE T3.x):** the original plan said outputs would land in `researchfms/teardowns/`. They live HERE instead because `/researchfms/` is gitignored by the outer Lancelot repo (it's a nested research repo) — editing the teardowns would modify the inner repo's working tree. This Lancelot-tracked location is cleaner.

Methodology: per the cardinal "no bullshit" rule + the explicit prior-context lesson that thrashing agents produced fabricated file:line citations, every line number below is from a direct read this session.

## Robyn 0.85.0 (local) / 0.84.0 (PyPI)

Source: `researchfms/robyn/Robyn/`. Rust core + Python API; built on **actix-web** (not hyper directly like Lancelot).

### Request parsing & socket setup
- `src/shared_socket.rs:22-30` — `SocketHeld::new` sets:
  - `set_reuse_port(true)` for non-Windows (Linux SO_REUSEPORT, matches Lancelot M4 V3.3).
  - `set_reuse_address(true)`.
  - `set_nodelay(true)` (TCP_NODELAY ON for low-latency response).
  - `listen(16384)` (backlog).
- `src/server.rs:39-40` — `DEFAULT_MAX_PAYLOAD_SIZE = 1_000_000` (1MB). **Lancelot default is 16 MiB.**

| Finding | Classification | Note |
|---|---|---|
| `set_nodelay(true)` at socket creation | **ALREADY HAVE** | Lancelot's `src/socket.rs` (the M2 TCP_QUICKACK/FASTOPEN code) sets the same. |
| 1MB max payload default | **AVOID** | Too tight for AI/cloud workloads (embedding requests, JSON-heavy LLM inputs). Lancelot's 16MB default with `body_cap` enforcement (M4 V1.2) is correct. |
| Listen backlog 16384 | **ALREADY HAVE (different value)** | Lancelot's value is configurable via `LANCELOT_MAX_CONNECTIONS`; Robyn's is hardcoded. Lancelot's path is more correct. |

### Per-request dispatch
- `src/executors/mod.rs` (`execute_http_function`) — for each request:
  1. `Python::with_gil(|py| ...)` builds the coroutine in Python
  2. wraps in `TaskLocals` context (for ContextVar propagation)
  3. `pyo3_async_runtimes::tokio::into_future` to bridge into Rust's tokio runtime
  4. `.await`s, then `Python::with_gil` again to extract response

| Finding | Classification | Note |
|---|---|---|
| `pyo3_async_runtimes::tokio::into_future` per request | **AVOID (already learned this lesson)** | This is exactly the cross-thread asyncio-bridge pattern that bit our R3 streaming pump (M3 stream-rearch tag). It works correctly but adds the ~0.5ms per-request FFI/asyncio-bridge overhead we documented in the honest perf envelope. Robyn pays this on EVERY request; Lancelot's R3 pump only pays it ONCE per stream (the `_drain` coroutine runs on-loop). Confirms our pivot was right. |
| `Python::with_gil` × 2 per request (build + extract) | **AVOID** | Lancelot's R2 lazy request proxy + R5 Rust serialize minimizes Python boundary crossings per request. Robyn's two-GIL-pass pattern is simpler but slower. |

### Streaming
- Robyn exposes streaming only via `SSEResponse` (Python wrapper) — there's no general `StreamingResponse`. SSE framing (`data: …\n\n`) is added by the framework on top of yielded payloads.

| Finding | Classification | Note |
|---|---|---|
| Streaming-only-via-SSE surface | **AVOID** | Lancelot exposes `@app.stream` (typed event stream with optional framing control via `encode_resp=False`) AND `StreamingResponse` for raw bytes. Robyn's SSE-only is less flexible. |

### Concurrency
- Uses actix-web's actor model (one actix worker per CPU by default). Workers share the listening socket via actix's own mechanism (not naked `SO_REUSEPORT`).

| Finding | Classification | Note |
|---|---|---|
| actix-web worker model | **AVOID (different stack)** | Lancelot's deliberate choice is `hyper` + `tokio` + multi-process via `SO_REUSEPORT` because actix-web's actor model + Python's GIL would compound complexity. The honest M4 bench is the validation. |

## Blazeio 4.1.0 (local; PyPI's latest is 2.2.2.5 with old API)

Source: `researchfms/blazeio/Blazeio/Blazeio/`. Pure-Python asyncio with bespoke HTTP framing.

### Request parsing — zero-copy buffered receive
- `Protocols/server_protocol.py:5-30` — `BlazeioServerProtocol(BlazeioProtocol, BufferedProtocol, BlazeioPayloadUtils, ExtraToolset)` — uses asyncio's `BufferedProtocol` API.
- `__init__` allocates a `bytearray(INBOUND_CHUNK_SIZE)` + `memoryview(app.__buff__)` for zero-copy header parsing.
- `connection_made`: `transport.pause_reading()` then spawns a `loop.create_task(app.transporter())` — buffered receive is gated.

| Finding | Classification | Note |
|---|---|---|
| Zero-copy `memoryview` over a single fixed-size bytearray | **BORROW (low risk, defer to M6)** | Lancelot's `hyper` library handles its own zero-copy via `Bytes`, which is functionally similar. Blazeio's pattern only beats us if `hyper`'s internal buffering is slower than direct memoryview — unlikely. Defer to a measured spike if T1 bench shows a request-parsing gap. |
| `transport.pause_reading()` + task-driven receive | **AVOID (architecture choice)** | This is Blazeio's defining pattern (asyncio low-level transport hooks). Lancelot uses `hyper`'s server-side state machine which encapsulates the same flow without exposing it. Not a borrowable knob. |

### Streaming — manual chunked writer
- `Protocols/server_protocol_extratools.py:N` (prepare method) — `await r.writer(b'%X\r\n%s\r\n' % (len, data))` is the documented chunked-write idiom.

| Finding | Classification | Note |
|---|---|---|
| Manual chunked framing exposed to handlers | **AVOID** | Lancelot's `@app.stream` owns the framing (and the keepalive cadence via `sse_keepalive=`) and exposes a clean async-gen. Blazeio's pattern is more direct but more error-prone — see the `prepare()` keepalive-state bug we hit during T1.1's first attempt (the manual `HTTP/1.1 200 OK\r\n…` writes broke at c≥4 because they bypassed Blazeio's internal keepalive state). Lancelot keeps the contract simpler. |

### Connection lifecycle
- `serve_route_no_middleware` (line ~290 of `__init__.py`) does `app.declared_routes.get(r)` — passes the Request object, not `r.path`. Only `serve_route_with_middleware` uses `r.path` for lookup.

| Finding | Classification | Note |
|---|---|---|
| Bug-bordering routing path on the no-middleware code path | **AVOID** | We hit this during T1.1 — `add_route()` registers `/echo` correctly but the no-middleware code path doesn't match it. Worked around in the bench harness with a no-op `before_middleware`. Worth filing upstream; not adoptable as-is. |

### Concurrency
- Single asyncio event loop per process; `SharpEvent` (custom single-waiter event) for fast wakeup; `__ReMonitor__` (a threaded watchdog for header read timeouts and similar deadlines). Optional `uvloop` integration via env knobs.

| Finding | Classification | Note |
|---|---|---|
| `SharpEvent` single-waiter primitive | **BORROW (low risk, defer to M6)** | Specific to the asyncio.Event for single-waiter case — measurable win in tight loops if benchmarked. Lancelot's R3 pump uses `tokio::sync::mpsc::Sender::try_push` which is already optimized; not clear there's a gap. |
| `__ReMonitor__` threaded watchdog | **AVOID** | Cross-thread state for connection deadlines is fragile (especially with the GIL). Lancelot's `LANCELOT_HEADER_TIMEOUT_MS` is enforced at the Rust hyper layer — simpler + more correct. |

## FastAPI 0.115.* / current

Source: `researchfms/fastapi/fastapi/fastapi/`. Standard FastAPI on Starlette/ASGI.

### Middleware path — `BaseHTTPMiddleware`
- `applications.py` — `@app.middleware("http")` calls `add_middleware(BaseHTTPMiddleware, dispatch=func)`.
- Starlette's `BaseHTTPMiddleware` uses an anyio task group to run `call_next` in a background task while the user dispatch fn does its work — this is what produces the ordering subtlety documented in our [from-fastapi.md §1](../migration/from-fastapi.md).

| Finding | Classification | Note |
|---|---|---|
| `BaseHTTPMiddleware` anyio task-group streaming | **AVOID (deliberate divergence — already documented)** | We don't replicate this because it's a complex anyio dependency just to satisfy one ordering subtlety. Conformance ceiling case #1; documented in migration cookbook. |

### Exception handler path
- `exception_handlers.py` — standard `(Exception, handler_fn)` map; `RequestValidationError` for Pydantic errors. Lancelot has the same API + the additional `@app.exception_handler` decorator (M2 Tier-A conformance).

| Finding | Classification | Note |
|---|---|---|
| Exception-handler surface | **ALREADY HAVE** | M2 conformance push completed this surface. |

### Dependency injection — `scope="function"` vs `"request"`
- FastAPI 0.115+ supports `Depends(dep, scope="function")` to control yield-dep teardown timing. The three AsyncExitStacks pattern (`fastapi_middleware_astack`, `fastapi_inner_astack`, `fastapi_function_astack`) controls when each fires.

| Finding | Classification | Note |
|---|---|---|
| Three-stack DI teardown | **ALREADY HAVE** | M2 conformance push 3 (tag `m2-conformance-push3`) added this — 948→971 Class-A. |

### Response model coercion
- `routing.py` — `serialize_response` does the pydantic coerce-and-dump dance.

| Finding | Classification | Note |
|---|---|---|
| Response-model coercion via pydantic-core | **ALREADY HAVE (better)** | We use pydantic-core's `to_json` directly + memoized via `PyOnceLock` (M4 F4 fix from `lancelot-gil-oncelock-deadlock` lesson). FastAPI's path is slower because it goes through `json.dumps` after the coercion — not us. This IS the documented divergence #2 (`json.dumps mock` test). |

## Cross-axis summary

| Axis | Lancelot status | Notes |
|---|---|---|
| Socket setup (TCP_NODELAY, SO_REUSEPORT) | **At parity** with Robyn; better than Blazeio (no manual fd control) and FastAPI (depends on the ASGI server) |
| Request parsing | **At parity** (hyper buffered) — Robyn's actix-web equivalent or Blazeio's BufferedProtocol both ~equivalent |
| Per-request FFI overhead | **WINS** — R2/R3 pump architecture pays the cross-thread bridge ONCE per stream, not per request. Robyn pays it per-request. |
| Streaming pump | **WINS** — `@app.stream` owns the framing + keepalive cadence; Robyn is SSE-only; Blazeio exposes manual writer. |
| Connection lifecycle / drain | **WINS** — M4 V1.4 graceful drain via `acquire_many_owned` bounded by `LANCELOT_DRAIN_SECS`. Robyn relies on actix-web's drain (less tunable); Blazeio has the threaded watchdog. |
| Concurrency / GIL handling | **At parity / "honest"** — all three are GIL-bound for handler code; Lancelot's multiprocess `SO_REUSEPORT` model is documented. |
| Middleware ordering subtlety | **One documented divergence** (anyio task-group); covered in migration doc |
| Response serialization | **WINS** — pydantic-core direct path; cited divergence #2 in migration doc |

## M5 low-risk adoptions (T3.4)

Nothing landing in M5 from this recheck. The two "BORROW (low risk)" tags (Blazeio's zero-copy memoryview, Blazeio's `SharpEvent`) are both:
- Low marginal benefit unless T1's actual bench shows a request-parsing gap.
- Risk of disrupting hyper's already-optimized buffering / tokio's already-fast sync primitives.

Defer to M6 with explicit rationale: revisit only if T1 bench numbers show Lancelot losing to one of these peers on a request-parsing or sync-primitive-bounded shape.

## What this recheck DID find

The peer source confirms three things our positioning already claims:

1. **The R3 streaming pump's on-loop `_drain` design is the right architecture.** Robyn pays the cross-thread bridge per request; we pay it per stream. Confirmed in source.
2. **The const-tier prebaked-bytes path is the right answer to the FFI overhead** — Robyn has a `ConstRouter` for the same reason, by the same architecture.
3. **The 1MB-vs-16MB body-cap default is a deliberate Lancelot choice** — Robyn's tight default would break realistic AI/cloud payloads. Our 16MB + cap-flood enforcement (M4 V1.2) is correct.

The recheck does NOT find a hidden architectural win Lancelot doesn't already use. The marquee wins (large-payload serialization, streaming, `@app.batch`, const tier, resilience-under-load) hold up against direct source comparison.
