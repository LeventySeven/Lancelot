# Lancelot state ledger — research plans mine
**Source cluster:** CLAUDE.md · research/deep/07-stability-failure-mode-catalog.md · research/deep/09-ultimate-buildable-wbs.md · research/design/03-versatility-competitive-surface.md · docs/plans/2026-06-02-byte-path-build-order-from-founders-and-patterns.md · docs/plans/2026-05-31-deferred-candidates-panel.md · docs/plans/M5-PLAN-PROVE-VS-PEERS-AND-ADOPTABLE.md · docs/plans/M6-PLAN-SPEED-STABILITY-ADOPTION.md
**Date mined:** 2026-06-22
**Miner scope:** comprehensive ledger pass — ALREADY-BUILT, DEFERRED, REFUSED, BUILD candidates, positioning locks, gate state, uncharted surfaces.

---

## PURPOSE OF THIS FILE

This file is the GUARDRAIL for all byte-path synthesizers. Before proposing any new mechanism,
cross-check: is it in REFUSED? DEFERRED? ALREADY BUILT? This ledger answers all three.

Cite: every mechanism is cited to its source file:section or CLAUDE.md milestone. Mechanism-level, not vibes.

---

## 0. CONFIRMED VALIDATIONS (design choices — no action needed, do not re-derive)

Per 2026-06-02 `docs/plans/2026-06-02-byte-path-build-order-from-founders-and-patterns.md` confirmed-validates section:
const tier · GIL-released-across-await · `PyOnceLock` · HTTP/1.1-only rejecting h2c · retry-hint headers · bounded fanout w/ `JoinSet` · `await_capacity` fire-on-full · `cfg(test)` buffer=1 · `memory_pool` named-OOM · `SO_REUSEPORT` multiprocess (Linux-only LB) · graceful drain (no hard-kill) · `BlockingPool` sync offload + 503 valve · eager-first-chunk `defer_status` · client-disconnect → upstream cancel + `finally`/`aclose` · `cancel_on_disconnect` dual-task race · never-gzip-streaming · SSE error→`ProblemDetail` + `done_sentinel` · route-template metric labels · raw-ASGI middleware discipline · correlation-id + W3C traceparent · in-flight gauge decremented post-body · `/livez`+`/readyz`+`/fleet/status` · json depth cap · registration-time arity/DI/TypeAdapter precompute · declined `Last-Event-ID` replay.

---

## 1. TIER-D REFUSE LIST — explicit do-not-build, do not propose

These come from CLAUDE.md §Tier-D and have explicit, sourced rejections.

| Item | CLAUDE.md verdict | Reason |
|---|---|---|
| Claim small-request Rust throughput as selling point | ❌ REFUSED | uvicorn+uvloop wins; never the bottleneck |
| Per-`__anext__` bridge (re-gress R3 pump) | ❌ REFUSED | Reintroduces 16-26× cliff; the on-loop `_drain` must stay |
| Deploy 1-process-many-tokio-threads | ❌ REFUSED | Anti-scales (GIL); multiprocess is the only model |
| Optimize `@app.stream` per-chunk overhead | ❌ REFUSED | LLM is upstream-rate-limited; not the bottleneck |
| Custom JSON serializer (orjson/ujson) | ❌ REFUSED | pydantic-core already Rust-fast; orjson/ujson deprecated in FastAPI |
| OpenAPI callbacks / webhooks | ❌ REFUSED | Rare, complex, no AI/startup payoff |
| Dependency-overrides per-request rebuild | ❌ REFUSED | Test-only; simple override-by-call suffices |
| Built-in CLI (`lancelot run …`) | ❌ REFUSED | FastAPI delegates to fastapi-cli; uvicorn already works |
| Replacing AI logic (agents/vectors/prompts) | ❌ REFUSED | User-side; Lancelot owns transport only |
| Robyn-style hard-kill shutdown | ❌ REFUSED | Explicit anti-goal; graceful drain is the thesis |
| Per-chunk Pydantic validation on streams | ❌ REFUSED | Perf cliff |
| Per-chunk spawn_blocking streaming bridge | ❌ REFUSED | The 16-26× regression already fixed; never revert |
| OpenAI-compat model gateway / model routing | ❌ REFUSED | AI-logic; user-side |
| Adaptive SLA-aware batch window / dynamic model batching | ❌ REFUSED | Worker-side, user logic |
| orjson re-derivation | ❌ REFUSED | See custom JSON serializer |
| Cross-worker pub/sub + leader election | ❌ REFUSED | External bus = user-side |
| Arrow IPC / pickle serde / spill machinery | ❌ REFUSED | No HTTP analogue; pickle on edge = RCE |
| Runtime route registration + OpenAPI cache invalidation | ❌ REFUSED | Fights matchit/const-tier |
| ASGI-over-network-transport bridge | ❌ REFUSED | User-side |
| App-instance caching keyed on settings hash | ❌ REFUSED | User-side |
| Boot-time app-secret validation | ❌ REFUSED | User-side |
| Cross-process model-worker broker (R3 from deferred panel) | ❌ REFUSED | Hosts the model = user-side; 30-50h; positioning violation |
| SIMD base64 for embeddings (R2 from deferred panel) | ❌ REFUSED | `pip install pybase64` already gives the 3×; parity expected |
| HTTP/2 cleartext h2c on Rust core | ❌ REFUSED | TLS terminates at proxy; slow-loris guard is http1-only |
| Windows support | ❌ REFUSED | Linux+macOS only; documented |
| TypeScript SDK | ❌ REFUSED | Python-only |
| Snapshot/restore / durable execution (Trigger.dev-style) | ❌ REFUSED | Orchestration = user-side |
| Streaming gzip (per-chunk) | ❌ REFUSED | Per-chunk = Tier-D; GZipMiddleware is the right shape |
| Brotli (br) | ❌ REFUSED | Second dep + negotiation branch; proxy's job |
| SIMD JSON custom serialize | ❌ REFUSED | Already have simd-json on untyped paths; pydantic-core on typed |
| `lancelot.webhook` as positioning pitch | ❌ REFUSED | Durable delivery worker is off the request path |
| MCP/canonical/session-affinity/memory_pool/secrets/pagination/ratelimit as "freebie" switch reason | ❌ REFUSED | Like-for-like replacements; ratelimit is weaker than Redis-shared |
| Page[T] response envelope | ❌ REFUSED | 5-7 teams each ship a different shape; fastapi-pagination already exists |

---

## 2. DEFERRED LIST — proposed, not built, gate condition stated

| Item | Source | Gate / why deferred |
|---|---|---|
| T3.1 multi-region deploy recipes (Render/AWS/Fly.io) | CLAUDE.md M7 | Needs ~9-12h wall-clock provision + 1h soak each |
| T2.6 multi-region session-affinity verification | CLAUDE.md M7 | Gates on T3.1 |
| orjson swap experiment (T1.2) | M6 plan | Bench-gate: must show ≥ Robyn 8.1k @ 70 KB, or document bounded reason |
| GIL-releasing gzip R1 | deferred-panel | MAYBE: needs concurrency bench at c≈200/level-9 showing per-worker cliff; maintainer sign-off; stays REFUSED until then |
| Litestar-style plugin protocols (InitPlugin/SerializationPlugin/DIPlugin/OpenAPISchemaPlugin) | design/03 §4.2 | v0.x; behind AI differentiators |
| DTO/projection layer | design/03 §3.1 | v0.x; attacks FastAPI ORM-mirror boilerplate |
| Pluggable serialization beyond Pydantic (msgspec/attrs) | design/03 §3.1 | v0.x; neutralizes Litestar edge |
| Built-in rate limiting + response caching | design/03 §2.2 | v0.x; P3 from deferred panel now BUILD |
| First-class SQLAlchemy/async-DB story | design/03 §3.1 | v0.x |
| WS pub/sub channels | design/03 §6 | v0.x |
| Coalescing 1-slot mailbox opt-in `@app.stream` mode | byte-path-build-order TIER 3 | Speculative; build only on demonstrated demand |
| WebSocket byte-path depth (backpressure/ping-pong/close-codes/permessage-deflate) | byte-path-build-order ROUND 2 | Un-mined; next mine target |
| HTTP/2-3 as an evidenced decision | ROUND 2 completeness gaps | Next mine target |
| TLS/connection-setup path | ROUND 2 completeness gaps | Next mine target |
| Streaming multipart upload ingest | ROUND 2 completeness gaps | Next mine target |
| gRPC/protobuf relay | ROUND 2 completeness gaps | Next mine target |
| Cookie/CSRF edges | ROUND 2 completeness gaps | Next mine target |
| Conditional-GET/ETag/304 for dynamic responses | ROUND 2 completeness gaps | Next mine target |
| TransportPool connection lifecycle deep-mine | ROUND 2 completeness gaps | Next mine target |
| Non-Python/Rust corpora (Go/Node/Envoy) | ROUND 2 completeness gaps | Next mine target |
| OTel/Prometheus export transport | ROUND 2 completeness gaps | Next mine target |
| Litestar full `BufferedProtocol` re-wiring (Blazeio) | M6 T1.4 | High-risk; deferred to M7+ if T1.6 bench shows real gap |

---

## 3. ALREADY BUILT — M1–M19 shipped (HAVE, do not re-propose)

### Byte-path core (M0–M3)
- `matchit` O(1) routing per method — `src/router/mod.rs`
- Prebaked const-tier (Rust, zero-GIL): `const_routes` on `_native.run` → `service::Prebaked`; `LANCELOT_CONST_STRICT` auth guardrail (M19)
- R3 on-loop streaming pump: `src/stream.rs` `_drain(gen,sink)` coroutine, bounded-mpsc, same-thread `try_push`, backpressure via `await_capacity`; `LANCELOT_STREAM_BUFFER` default 256
- R2 lazy request proxy: `src/ffi/request.rs`, `src/dispatch.rs` `ReqParts`; OnceLock-cached getters
- R4 single-pass `validate_json` via pydantic-core
- R5 Rust serialize via pydantic-core `_to_json`; R3 JSON-lines
- R6 `header_read_timeout` + body cap (16 MiB) + LANCELOT_HEADER_TIMEOUT_MS
- R7 `catch_unwind` FFI panic safety; extended to R3 pull task
- `intern!` on `__anext__` / `__aiter__` / `media_type` / `pydantic_core` / `to_json` etc.
- Sized header Vec from HeaderMap.len()
- File:line on Rust↔Python boundary errors
- `cfg(test)` buffer=1; `select_biased!` for SIGTERM; `set_retry_hint`; Levenshtein 404; SIGINT poll
- CRITICAL body-DoS guard: 16 MiB `Limited` → 413
- Router `.expect()` panic → `PyValueError` at startup
- GIL-across-await verified clean; only `unsafe` = `libc::setsockopt` in `socket.rs`

### Conformance (M1–M2 + pushes)
- 974/978 = 99.6% FastAPI byte-parity (floor passes=974)
- Exact `422 {"detail":[{type,loc,msg,input,ctx}]}` wire shape
- `@app.exception_handler`, `strict_content_type`, `Json[T]` Annotated preservation
- Yield-dep scope="function"/"request" semantics with separate AsyncExitStacks
- `FastAPI(dependencies=[...])` app-level deps; registration-time scope-incompatibility check
- WS metaclass for Starlette cross-stack disconnect; WS query/cookie at handshake
- `APIRoute.get_route_handler()` opt-in; `Router.route_class`; `_BodyAccessor`
- BaseHTTPMiddleware streaming adapter (M19 — streaming task-group model, no anyio dep)

### Cloud resilience (M2–M4)
- Graceful drain: `LANCELOT_DRAIN_SECS` (default 10); `acquire_many_owned(max_conns)` pattern
- Slow-loris: `LANCELOT_HEADER_TIMEOUT_MS` (default 15000)
- Connection cap: `LANCELOT_MAX_CONNECTIONS` (default 1024)
- Per-route `timeout=` kwarg → 504; yield-dep teardown preserved
- RED metrics: `lancelot.http.*`; `http.status_code` attr on every metric
- Streaming TTFT: `record_stream_ttft` → `lancelot.http.stream.ttft` (M4 + M19 extended to any-body)
- W3C `traceparent` propagation: `context.trace_id` + TransportPool auto-inject
- `/livez` + `/readyz` probes
- Request-id: `lancelot.context.request_id` ContextVar; CRLF-sanitized; 128-char bounded
- `canonical.dumps_canonical` → stable JSON bytes for KV-cache stability
- `App(max_concurrent_requests=N, drain_timeout=30.0)` etc.
- Body cap → 413; JSON depth guard; connection cap → 503

### AI primitives (M2–M7)
- `@app.batch(batch_window_ms, max_batch, max_keys, key=...)` — prefix-aware coalescing; ~12×/~16×
- C2 client-disconnect → upstream cancellation; `cancel_on_disconnect`
- `Response.set_session_affinity(key)` — KV-cache-aware routing primitive
- `lancelot.llm.TransportPool`: `spawn_child`, `fanout`, `request()` (auto-injects traceparent/request_id/agent_id), `reload_mtls()`, mTLS toggle
- `lancelot.upstream_metrics`: provider/model detection (7 providers), token extraction (3 shapes), `set_rate_table()`, structured error taxonomy, 6 OTel histograms, cost accumulation into ContextVar
- `lancelot.mcp.mount(app, path='/mcp', max_response_bytes=4096)`; T1.5 minimal-return WARN; `call_tool` positional-only `name`
- `lancelot.context` ContextVars: `cost_usd`, `tokens_in`, `tokens_out`, `agent_id`, `tenant_id`, `queue_wait_ms`
- `lancelot.tenant.TenantConcurrencyLimiter`; `LANCELOT_TENANT_LABEL_MAX`; `_bounded_tenant()`
- `lancelot.fleet`: region/worker/uptime; `/fleet/status`; `install_otel_resource_attrs()`
- `@app.get('/x', region='eu-west-1')` — region-pinned routes
- `lancelot.secrets`: 5 backends (EnvBackend/FileBackend/VaultBackend/InfisicalBackend/AwsSecretsManagerBackend); SIGHUP rotation; audit hook
- `lancelot.memory_pool.MemoryPool`: RAII `MemoryReservation`; `ResourcesExhausted(top_consumer=)`
- `X-Lancelot-Cost-USD` response header; `lancelot.http.cost.usd` histogram; `lancelot.http.queue_wait_ms`

### M8/M10/M12/M13/M14/M15 surface
- CORS creds+wildcard reject; FileBackend traversal guard; closest_path 404 DoS cap; SSE id/name CR/LF/NUL strip
- multipart cap → 400; get_running_loop fix; tenant-cap memoize; header-scan dedup
- uvloop on embedded loop: `LANCELOT_LOOP` knob; +28% tiny (44k→57k); `SO_REUSEPORT` LB Linux-only
- abi3 wheels CI; cgroup-aware workers; SIGTERM drain; lean import
- `lancelot.webhook` (Standard Webhooks sign/verify); `lancelot.net` opt-in SSRF guard; TransportPool idle_guarded_chunks + rate_limit token bucket
- GZipMiddleware M18: Rust `libdeflater` off-loop via `run_in_executor` — ~2.2× RPS, ~12× p99 at c=200/level-9/45KB; wins when no compressing proxy and single-worker; loses at c=1 or multi-worker/proxy

### M15/M18/M19 surface
- `cancel_on_disconnect` dual-task race; SSE error→ProblemDetail + done_sentinel
- `max_stream_lifetime` wall-clock cap + terminal `{type:reconnect}` sentinel (`routing.py`, `asgi.py` watchdog)
- Const-tier auth guardrail: `ConstRouteAuthBypassWarning` + `_audit_const_route_middleware_bypass` + `LANCELOT_CONST_STRICT`
- TTFT-on-any-body at ASGI send boundary: `_ttft_send_wrapper`/_record_ttft_now` (asgi.py); fires on `text/event-stream` or first `more_body=True`; never on unary
- `LANCELOT_MAX_INFLIGHT_BYTES` (AtomicI64 in `src/service.rs`): aggregate in-flight body budget; 503+Retry-After on breach; RAII `InflightGuard`; default 0=off
- Runtime/dynamic-surface conformance matrix (5 surfaces characterized)

### ROUND 2 bugs fixed (M19b)
- OpenAPI nested-Security flattening: `Security()` nested in `Depends()` → op.security + securitySchemes now populated
- Duplicate static path: first-wins or startup warning (was last-wins)
- `request.state` attribute parity: dict-AND-attribute `State` wrapper (was dict-subscript only)

---

## 4. BUILD / CONFIRM — items cross-validated and in-mission from plans

These are build candidates surfaced from the plans corpus, not yet in Tier-D, not yet shipped:

### HIGH — cross-validated, byte-path, cheap

| Mechanism | Source | Status |
|---|---|---|
| P3 inbound rate-limiter → 429 + Retry-After (per-process bucket, `_TokenBucket` reuse, Redis hook, no DSL) | deferred-panel P3; design/03 §2 | **BUILD** (4/5 judges) |
| P5 constant-time static-token gate (stdlib `hmac`/`secrets`, `scope['path']`, OPTIONS-skip, allowlist) | deferred-panel P5 | **BUILD** (3/5 judges) |
| P7 streamed cost/token/TTFT accounting (`pool.stream`/`relay` currently emit 0; scan terminal SSE frame via existing `extract_tokens` 3 shapes; opt-in `track_usage=True`) | deferred-panel P7; `llm.py:482,583` | **BUILD** (5/5 judges, silent parity bug) |
| P6 guaranteed SSE terminal + crash→error-frame (bundle with P7; route hardcoded `"internal server error"` at `asgi.py:1199` through ProblemDetail; emit cancel-arm frame only on genuine internal cancel not client disconnect) | deferred-panel P6; `asgi.py:1160-1166,1199` | **BUILD as polish, bundle with P7** |
| P4 response_model skip-validate (4× on list reads; **must refuse** combination with `exclude_unset`/`exclude_defaults`/`by_alias` at registration — they require a validated instance) | deferred-panel P4; `asgi.py:1525-1537`, `routing.py:84-86` | **BUILD, hard-fenced** |
| P9 `PageParams`+`SortParam` DoS-cap (the `limit=-1`→unbounded full-table-read footgun; `LANCELOT_PAGE_MAX_SIZE`; SortParam allowlist→422 anti-injection; **drop** Page[T] envelope) | deferred-panel P9; `database_service.py:738` | **BUILD the cap, SKIP envelope** |
| Slot-based de-correlated retry backoff in TransportPool (`llm.py:230/305` jitter-exp synchronizes competing retriers → thundering herd on 429'd LLM under batch fan-out) | byte-path-build-order TIER 3; LanceDB pattern | BUILD (cheap, in-mission) |
| Reusable/bounded gzip encode buffer: `clear()` + `shrink_to(cap)` + `cfg(test)` gzip level to exercise off-loop path without level-9 CPU | byte-path-build-order TIER 3; Zed pattern | BUILD (cheap) |
| Truncate (~2KB)+NUL-strip any captured upstream body in `upstream_metrics` | byte-path-build-order TIER 3; Polar pattern | BUILD (cheap, extends existing CRLF/NUL discipline) |
| Opt-in inbound transient-exception → 503 `Retry-After` classifier (user-supplied predicate; reuses `exceptions.py:151-168`) | byte-path-build-order TIER 3; Prefect pattern | BUILD (cheap) |
| `Server-Timing` header surfacing `queue_wait_ms` + duration (flag-gated, not on every response) | byte-path-build-order TIER 3; Turbopuffer pattern | BUILD (cheap) |
| Inbound `Idempotency-Key` replay cache (only outbound exists today) | ROUND 2 HIGH | BUILD |
| Outbound gzip on TransportPool request bodies (only response gzip shipped) | ROUND 2 HIGH | BUILD |
| Per-request bypass/429-throttle for `LANCELOT_MAX_INFLIGHT_BYTES` (M19 all-or-nothing shed, no escape hatch) | ROUND 2 HIGH | BUILD |

### MED — in-mission, slightly larger

| Mechanism | Source | Status |
|---|---|---|
| FD soft-limit raise (`RLIMIT_NOFILE`) completing the conn-cap story | ROUND 2 MED | BUILD |
| `X-Forwarded-Prefix` → `root_path` | ROUND 2 MED | BUILD |
| Magic-byte MIME sniff for static | ROUND 2 MED | BUILD |
| Emit 499 through metrics on a won disconnect race (`metrics.py:149` anticipates it but never produced) | ROUND 2 MED | BUILD |
| Honor inbound `x-should-retry` in outbound retry classifier (`llm.py:471`) | ROUND 2 ADAPT | BUILD (tiny) |
| Readiness-callback hook → `/readyz` 503 (5-project convergence) | ROUND 2 ADAPT | BUILD (tiny) |
| Name the responsible knob in the 503 shed body | ROUND 2 ADAPT | BUILD (trivial) |
| Opt-in out-of-band liveness watchdog → flip `/readyz` 503 on stuck-backend predicate | byte-path-build-order TIER 3; vLLM pattern | BUILD (conditional) |
| Method-gated RED metrics (exclude OPTIONS/HEAD) | ROUND 2 lower | BUILD (cheap) |
| SSE `seq_id` cursor on keepalive ping | ROUND 2 lower | BUILD |
| Per-event client-drain (slow-consumer) metric | ROUND 2 lower | BUILD |
| Scope-rewrite legacy-prefix → canonical seam + `X-Deprecation-Notice` | ROUND 2 lower | BUILD |

### BENCH-GATE ONLY (do not build without a measured win)
| Mechanism | Source | Condition |
|---|---|---|
| mimalloc as `#[global_allocator]` for the cdylib | byte-path-build-order TIER 3; LakeSail | Bench-gate on oha harness; GIL-serialized single loop may wash out; drop if neutral |
| GIL-releasing gzip (R1) | deferred-panel R1 | Needs concurrency bench c≈200/level-9 showing per-worker cliff; maintainer sign-off |

---

## 5. KEY POSITIONING LOCKS (synthesizers must not drift from these)

From CLAUDE.md + design/03:

- **The moat:** only framework where inbound HTTP stream and outbound LLM call are one Rust-owned, backpressured, cancellation-correct system, via FastAPI's exact ergonomics.
- **Honest perf:** never claim "Rust = faster server" broadly. Claim: "beats FastAPI (~41k) and Robyn (~36k) on every shape; const tier at 183k (~2.8× uvicorn+uvloop); large-payload serialization ~2.3× at 69 KB."
- **Target audience for the pitch:** greenfield AI byte-path builders; NOT FastAPI incumbents whose bottleneck is Postgres/LLM/GPU (10/10 founders would not switch — verified M19 round-2).
- **Lead with:** streaming hardening + `@app.batch` for LLM-gateway / read-heavy-RAG-proxy builders.
- **What NOT to pitch as switch reasons:** tiny-handler throughput · large-payload serialization as broad pitch (sub-4KB + orjson shops) · TransportPool as "freebie" (only works if you route through it) · lancelot.webhook (durable delivery is off the path) · like-for-like replacements (MCP/canonical/session-affinity/memory_pool/secrets/pagination/ratelimit).

---

## 6. M1–M19 GATE STATE (for synthesizers to track floor)

- Conformance floor: passes=974 (99.6% of 978). Never regress.
- Internal test suite: currently ~594+ tests passing (M19 + M19b = 594 in the last confirmed run).
- Rust gate: `cargo clippy --release --all-targets -- -D warnings` clean.
- Cargo test: 17 lib tests (last confirmed).
- GIL-across-await invariant: `#![deny(clippy::await_holding_lock)]` workspace-wide; `gil::without_gil` takes non-async closure by type so "await inside GIL" does not compile.

---

## 7. STABILITY FAILURE-MODE CATALOG — mechanism-level (from research/deep/07)

Source: `research/deep/07-stability-failure-mode-catalog.md`. 12-gate release checklist. Every cited defect is KNOWN from source-level teardowns.

### 7.1 FULLY ADDRESSED (shipped, validated M4)
| Gate | Mechanism | Lancelot file:fn | Peer defect closed |
|---|---|---|---|
| §1 Graceful drain | SIGTERM → stop accept → `acquire_many_owned(max_conns)` drain bounded by `LANCELOT_DRAIN_SECS` | `src/serve.rs` | Robyn `processpool.py:55` `process.kill()` (SIGKILL) |
| §3 Header-receipt deadline | `hyper::http1::Builder::header_read_timeout(30s default)` via `LANCELOT_HEADER_TIMEOUT_MS` | `src/service.rs` | Blazeio O(n·k) re-scan + no header deadline; Robyn `client_request_timeout(0)` |
| §3 Body cap | `tower-http::limit::RequestBodyLimit` default 16 MiB | `src/service.rs` | Robyn eager full-body buffer (10MB upload = 20MB resident) |
| §3 Max-connections cap | per-worker semaphore `LANCELOT_MAX_CONNECTIONS` 1024 → 503+Retry-After | `src/service.rs` | FastAPI `CapacityLimiter(40)` silent hang; Robyn unbounded |
| §5 R3 streaming pump | on-loop `_drain`, bounded-mpsc, `try_push` same-thread, backpressure via `await_capacity`, SPSC ring | `src/stream.rs` | Robyn per-token `spawn_blocking`+`with_gil`; FastAPI sync-gen thread-hop per chunk |
| §5 SSE framing + keepalive | Rust-side `data: %b\n\n` framing; timer-wheel keepalive wraps ring receive (NOT `__anext__`); `Cache-Control: no-cache` + `X-Accel-Buffering: no` | `src/stream.rs` / `src/codec/sse.rs` | FastAPI `routing.py:616-618` Python keepalive task |
| §5 Coalescing | flush when ≥ 2 KiB OR ≥ 25 ms | `src/stream.rs` | Blazeio 30 KiB threshold (holds ~5000 tokens before flush) |
| §6 Client-disconnect → upstream abort | Rust socket watch → cancel producer task → `CancelledError` into LLM generator → `InFlight::abort()` | `src/response/stream.rs` + cancel path | All three peers: none reliably stop upstream LLM when client leaves |
| §7 Per-request `contextvars.Context` | One `Context` per request; async via `loop.create_task(coro, context=ctx)`; sync via `ctx.run(handler, req)` | `src/ffi/dispatch.rs` | Robyn pre-#1380 cross-request ContextVar leak (tenant A auth bleeding into tenant B) |
| §7 GIL-across-await invariant | `#![deny(clippy::await_holding_lock)]`; `without_gil` takes non-async closure by type | `src/gil.rs` | Robyn "obeyed only by convention" |
| §8 Lazy request proxy | `LazyRequest` `#[pyclass(frozen)]`; ~1–3 allocs/req; materialize on access only | `src/request/lazy.rs` | Robyn ~30–66 allocs/req; FastAPI ~12–16 allocs |
| §8 Max-body-size | `RequestBodyLimit` 16 MiB; per-route override | `src/service.rs` | Robyn eager full-buffer no per-route override |
| §9 Streaming resource teardown structurally request-scoped | Rust drop-schedule `Vec<TeardownOp{slot,scope,is_async}>`; unwound after response future completes; LIFO; PEP-789 ordering | `src/ffi/dispatch.rs` | FastAPI GC-finalization edge; yield-dep DB/LLM client lifetime correctness |
| §9 `scope="function"` vs `scope="request"` ordering | Preserved exactly: function-scope teardown BEFORE body sent; request-scope AFTER | `routing.py:84-86`; `asgi.py` | FastAPI semantic on which dependant code relies |
| §10 Exact 422 wire shape | `{"detail":[{type,loc,msg,input,ctx}]}` MRO-based handler lookup | `lancelot/validation.py` | Robyn blanket 500; Blazeio exception-as-control-flow |
| §10 POST never auto-retried | `InFlight`; POST = 0 retries (connect-only pre-first-byte) | `src/client/policy.rs` | Blazeio's correct rule ported; prevents double-charge |
| §11 No `call_next`/`BaseHTTPMiddleware` dispatch | Pure-ASGI or Rust/tower chain; `BaseHTTPMiddleware` streaming adapter (M19) | `lancelot/middleware.py` | FastAPI `base.py:100-199` task-group+memory-stream per request |
| §11 Detached background tasks | Rust-owned worker pool; request returns immediately | `src/background/pool.rs` | FastAPI `BackgroundTasks` sequential post-send blocking connection release |
| §12 `/livez` const-tier (never Python) | Rust const-tier; zero-GIL; passes under saturated Python pool | `src/router/const_tier.rs` | None of three split liveness/readiness |
| §12 cgroup-aware worker count | Read `cpu.max` or `sched_getaffinity`; `clamp(cgroup_cpus, 2, 4)` | `src/runtime/cgroup.rs` | Robyn `os.cpu_count()` ignores cgroup; `input()` on port conflict hangs containers |
| §12 Fail-fast boot | Port conflict → non-zero exit; never `input()` | `src/config/boot.rs` | Robyn `__init__.py:712` stdin prompt hangs container pods |

### 7.2 PARTIALLY ADDRESSED — check or confirm
| Gate | What is shipped | What is NOT confirmed shipped | Check location |
|---|---|---|---|
| §2 Supervisor respawn-on-crash | `catch_unwind` at FFI seam (R7); `panic="abort"` + zero-`unwrap` clippy-deny | `max_restarts=5` / 30s window / exponential backoff parent supervisor loop — WBS U7 spec'd but NOT confirmed in CLAUDE.md milestones | `src/supervisor/mod.rs` — verify if rate-capped respawn loop is live |
| §4 Bounded sync-handler queue + load-shed | conn cap + 503; BlockingRunner wired to HTTP path | `sync_queue_max=10_000` bounded crossbeam channel explicitly confirmed? WBS specifies `crossbeam-channel::bounded(10_000)` | `src/blocking/runner.rs` |
| §9 WS bounded inbound channel + per-app reaper | WS via tokio-tungstenite; bounded mpsc | WS task registry max + idle-reap deadline confirmation | `src/ws/mod.rs` |
| §10 Upstream LLM failure → typed 502 + circuit breaker | TransportPool retry policies shipped | Per-host circuit breaker with half-open state confirmation | `src/client/policy.rs` |

### 7.3 KEY MECHANISM CONSTANTS (from §07 catalog — must match these or deviate with documented reason)
- Header-receipt deadline: 30s default (0 = disable, documented for streaming). `LANCELOT_HEADER_TIMEOUT_MS`.
- Body-read timeout: 60s (separate from header). `LANCELOT_DRAIN_SECS`.
- Total-request timeout: 120s. Per-route override.
- Max connections: 1024 default. `LANCELOT_MAX_CONNECTIONS`.
- Sync queue max: 10 000 (crossbeam BOUNDED). `src/blocking/runner.rs`.
- SPSC ring size: 1 default (lowest latency + tightest backpressure). Tunable via `LANCELOT_STREAM_BUFFER` (default 256).
- SSE coalesce: 2 KiB OR 25 ms (whichever first). NOT Blazeio's 30 KiB size-only.
- SSE keepalive: 15s (match FastAPI `_PING_INTERVAL = 15.0`). Payload `b": ping\n\n"`.
- Graceful drain: 30s total (`LANCELOT_DRAIN_SECS`). SSE finishes current event only.
- Max-body-size: 16 MiB default. `LANCELOT_MAX_BODY`. Per-route override.
- WS close_handler join: 5s (Robyn's value; configurable).
- Supervisor: `max_restarts=5`, `restart_window=30s`, backoff base 200ms ×2 (cap 10s).
- Python floor: 3.11 (hard error below; `loop.create_task(context=)` requires ≥3.11).
- Clippy deny: `unwrap_used`, `expect_used`, `panic`, `unreachable`, `indexing_slicing` on request-path modules.

---

## 8. WBS MECHANISM INVENTORY (from research/deep/09) — build targets NOT confirmed shipped

Source: `research/deep/09-ultimate-buildable-wbs.md`. Entries below are specified in the WBS but NOT confirmed in CLAUDE.md milestone state. These are either in-progress, not started, or need status confirmation.

| WBS unit | Mechanism | Files | Status |
|---|---|---|---|
| U7 supervisor | Rate-capped exponential backoff respawn: `max_restarts=5`, 30s window; alert on permanent failure | `src/supervisor/mod.rs`, `src/supervisor/signals.rs` | NEEDS CONFIRMATION |
| U29 MCP Streamable HTTP | `Last-Event-ID` resumability reusing ring + event-id cursor; `Mcp-Session-Id` TTL store; `MCP-Protocol-Version` negotiation (400 on unsupported) | `src/mcp/server.rs` | Partial (mount + tool call confirmed; resumability confirmation unclear) |
| U30 `@app.agent` loop | `AgentContext.run(model,tools,messages)` → framework-owned LLM↔tool loop; `AgentStep` types; `session=True SessionStore`; `durable=True` checkpoint; max_steps=10; tool-failure→ToolResult(error) fed back | `lancelot/agent.py` | UNCLEAR if shipped |
| U34 `/startupz` probe | 200 only after lifespan complete (distinct from `/livez` and `/readyz`) | `src/observ/probes.rs` | UNCLEAR; `/livez`+`/readyz` confirmed; `/startupz` unclear |
| U35 OTLP SERVER span in Rust | SERVER span created at request ENTRY (covers zero-GIL parse, not just handler); W3C traceparent; OTLP http/protobuf default | `src/observ/trace.rs` | OTel histograms confirmed; full Rust-side SERVER span unclear |
| U36 GenAI semantic-convention spans | `{operation} {model}` CLIENT span; `gen_ai.client.operation.duration` + `token.usage` histograms; streaming `time_to_first_token` + `time_per_output_chunk`; EXACT bucket boundaries from D04 §5.3; content-capture OFF default + redaction hook | `src/observ/genai.rs` | `upstream_metrics` histograms shipped; full OTel CLIENT span + exact buckets unclear |
| U37 `lancelot init` scaffolds | `lancelot init --docker --k8s --lambda`; Dockerfile.distroless + Dockerfile.alpine + Dockerfile.lambda (AWS LWA layer, `/livez` readiness); K8s `deployment.yaml` with 3 probes+preStop+grace40+maxSurge1/maxUnavailable0+securityContext; HPA v2; KEDA ScaledObject | `lancelot/cli.py`, `templates/` | Docs show `lancelot init` spec'd; confirmation of actual templates unclear |
| U42 Plugin protocols | `InitPlugin.on_app_init(AppConfig)` master hook; `SerializationPlugin`/DTO; `DIPlugin`; `OpenAPISchemaPlugin`; `MiddlewarePlugin` two-tier; `LifecyclePlugin` | `lancelot/plugins.py` | NOT shipped (v0.x roadmap) |
| U31 RAG/embeddings | `@app.rag`, `@app.embeddings`, `@app.limit(key,max)`, `app.gather(*coros,concurrency=)`, `@app.embedding_cache`; `VectorStore` ABC | `lancelot/rag.py` | UNCLEAR |
| U45 Free-threaded wheels | `cp313t`/`cp314t` ×5 platforms in wheel matrix; `python3.14t` CI; `sys._is_gil_enabled()` assertion | `.github/workflows/wheels.yml` | CI skeleton confirmed; ft-clean proof unclear |

### WBS crate constants (from §09 exec summary — pinned, do not drift without reason)
`pyo3 0.27` · `pyo3-async-runtimes 0.27 (tokio-runtime)` · `tokio 1 (full)` · `hyper 1 (http1,server)` · `hyper-util 0.1 (server,server-auto,tokio)` · `http-body-util 0.1` · `tower 0.5` · `tower-http 0.6` · `tokio-tungstenite 0.24` · `socket2 0.5` · `matchit 0.8` · **`crossbeam-channel 0.5 (BOUNDED)`** · `dashmap 6` · `parking_lot 0.12` · `arc-swap 1` · `bytes 1` · `simd-json 0.13`/`serde_json 1` · `ulid 1` · `tracing 0.1` + `tracing-subscriber 0.3` · `anyhow 1`.

---

## 9. COMPETITIVE SURFACE KEY FACTS (from research/design/03)

Source: `research/design/03-versatility-competitive-surface.md`.

### Table-stakes for FastAPI migration (must have ALL or "this is a rewrite not a migration")
Typed path/query/body params · Pydantic v2 request+response validation · `Depends()` DI + yield-teardown + `dependency_overrides` · Auto OpenAPI 3.1 + Swagger + Scalar/ReDoc · `include_router` flatten-at-registration · Two-tier middleware (Rust/ASGI-pure + ASGI-compat escape hatch) · Lifespan startup/shutdown (PEP-789) · CORS · Sync+async handlers (sync offloaded, bounded, no 40-cliff) · Exact FastAPI 422 wire-shape · `TestClient` (httpx-compatible) · WebSockets (bounded) · Static files (ETag/Range/304/`sendfile(2)`) · pydantic-settings compatible · Background tasks that actually background (Rust-owned detached pool).

**ALL of the above are confirmed shipped M1–M19 except check: `/startupz` + WS per-app reaper + detached background pool queue bound.**

### Differentiators (Lancelot-unique structural position)
- Per-token zero-alloc streaming backpressure (Rust SSE/JSONL codec + SPSC ring): `src/stream.rs` + `src/codec/sse.rs`.
- Mid-stream DI lifetime (yield-dep open through SSE stream, PEP-789): Rust drop-schedule.
- Client-disconnect → cancel → abort upstream LLM: wired `select!` in stream pump.
- Built-in MCP JSON-RPC server (`@app.mcp.tool`): `lancelot/mcp.py` + `src/mcp/server.rs`.
- Proper pooled async LLM client (HTTP/2, no POST-retry, circuit breaker): `src/client/pool.rs` + `lancelot/llm/client.py`.
- Const/static-response tier (zero GIL/Python/alloc): `src/router/const_tier.rs`.
- No 40-thread sync cliff: Rust bounded `BlockingRunner` + 503 `Retry-After`: `src/blocking/runner.rs`.
- Lazy request marshalling (~1–3 allocs/req): `src/request/lazy.rs`.

### Litestar-parity gaps (v0.x roadmap — NOT Tier-D; check before each release)
- Pluggable serialization beyond Pydantic (msgspec/attrs/dataclass — via `SerializationPlugin`): design/03 §3.1. NOT built.
- Built-in rate limiting: design/03 §2.2. P3 from deferred-panel now BUILD.
- Response caching: design/03 §2.2. NOT built.
- SQLAlchemy/async-DB story: design/03 §3.1. NOT built.
- WS pub/sub channels: design/03 §6. NOT built.
- Plugin protocols (4 Litestar-shaped): design/03 §4.2. NOT built.
- DTO/projection layer: design/03 §3.1. NOT built.
- Compression: gzip done (M18); brotli REFUSED.

### Granian / server-only positioning (do not conflate)
Granian is the strongest validation of the Rust-server thesis — AND the strongest argument for Lancelot's framework-level position. "Granian makes your framework's server fast; Lancelot makes the framework itself disappear into Rust." Per design/03 §3.2: Granian + FastAPI still pays DI re-solve, double-parse validation, `jsonable_encoder`, 40-cliff — Granian can't touch any of it. Lancelot owns both server and framework → end-to-end optimization.

---

## 10. UNCHARTED SURFACES (next mine targets, per 2026-06-02 round-2 completeness audit)

Not yet in any teardown or plan. High-value for next byte-path harvest pass:

1. **WebSocket byte-path depth** — backpressure model, ping/pong semantics, close-codes, `permessage-deflate`. WBS specifies bounded mpsc + reaper; needs source-level evidence on what the optimal Rust model is.
2. **HTTP/2 cleartext decision** — currently HTTP/1.1-only. No source-level evidence brief for *why* h2c was rejected beyond "TLS terminates at proxy." A concise evidence doc would anchor the documented divergence.
3. **TLS/connection-setup path** — cert loading, session resumption, ALPN negotiation cost. Relevant for mTLS (M7 shipped) and for the "TLS proxy is assumed" positioning.
4. **Streaming multipart upload ingest** — body streaming with backpressure for large uploads. WBS U4 specifies streaming-body async-iter; no teardown evidence on optimal chunked-ingest pattern.
5. **gRPC/protobuf relay** — zero-copy upstream→downstream for binary-framed streams. `src/client/relay.rs` does SSE relay; gRPC path unmined.
6. **Cookie/CSRF edges** — full cookie parser behavior under edge cases. M8 shipped CRLF sanitization; deeper cookie security edges unmined.
7. **Conditional-GET/ETag/304 for dynamic responses** — static-file ETag confirmed (tower-http); dynamic response ETags (hash of body) unmined.
8. **`TransportPool` connection lifecycle deep-dive** — stale-detect-before-handout, least-in-flight balancing, idle-reap thresholds, DNS TTL cache. M7 ships mTLS but no deep mechanism doc.
9. **Non-Python/Rust corpora** — Go (Envoy, grpc-go), Node.js (express-style), Envoy proxy patterns. Could surface compression/backpressure/circuit-breaker knobs not yet in the Lancelot corpus.
10. **OTel/Prometheus export transport path** — OTLP batch exporter, prom scrape cost model, cardinality guards beyond `_bounded_tenant()`. M7 ships `lancelot.upstream.*`; export path mechanics unmined.
