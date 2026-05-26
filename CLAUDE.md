# Lancelot — Claude session notes

This file is read by Claude Code on every session. Stays short and operational; deep history lives in `docs/plans/` and the memory store.

## What Lancelot is (positioning lock — do not drift)

A backend framework like FastAPI / Robyn that optimizes **HTTP transport / serialization / streaming** for **AI / cloud / web / startups**.

**AI logic stays on the user side** — LangGraph, agent loops, vector stores, prompt orchestration. We own the byte path; the user owns the agent. This is the user's restated rule; every "should we build X" decision flows from it.

## Core goals (the byte-path mission — what we optimize, and the proven wins)

We own the **byte path** (transport / serialization / streaming / resilience) for AI/cloud/web/startups; the user owns the agent. Concretely, lead with these *validated* wins:

- **Const tier** — fixed responses (health probes, static/cached) served prebaked from Rust, zero-GIL: ~183k req/s, **~2.8× uvicorn+uvloop** (`const_routes`).
- **Large-payload serialization** — pydantic-core `to_json` beats Python `json.dumps`: **~2.3× at 69 KB** (crossover ~1–4 KB).
- **Streaming for AI** — re-architected R3 pump (parity-to-1.3× vs Starlette), bounded backpressure, graceful drain, client-disconnect → upstream cancellation (C2). The marquee.
- **`@app.batch`** — prefix-aware request coalescing: **~12× throughput / ~16× fewer upstream calls** vs a rate-limited LLM backend. No analogue in FastAPI/Robyn/Blazeio.
- **Cloud resilience + observability** — body cap, connection cap, slow-loris, per-route timeout, graceful drain, RED metrics, **streaming TTFT** (time-to-first-chunk — the AI-streaming UX metric, `lancelot.http.stream.ttft`), request-id, **W3C `traceparent` propagation** (`context.trace_id` + `TransportPool` forwards it to the LLM call → joins the distributed trace, no OTel dep), /livez+/readyz — all validated under adversarial load.
- **FastAPI byte-parity** — 974/978 = 99.6% conformance.

**Honest perf envelope (do not oversell).** Since M10 the embedded loop runs on **uvloop** (soft-dep, falls back to stdlib asyncio, `LANCELOT_LOOP=asyncio` forces stdlib) — this lifted the tiny *handler* one-shot path from ~45k → **~59k req/s**, so the Rust server now **beats FastAPI (~41k), Robyn (~36k), and Blazeio (~59k)** on tiny and sits at ~88% of *raw* Starlette+uvloop (~67k), the only variant still ahead on that one shape (residual FFI/asyncio-bridge overhead). The **const tier** flips even that (183k). Never claim a blanket "faster server"; the honest, defensible claim is **"beats FastAPI and Robyn on every shape"** + lead with the wins above.

## Cardinal rules

1. **No bullshit, no useless logic.** Every change must change the byte path or the operator experience. If it doesn't, it shouldn't ship. Corollary: before building, check it isn't already provided (e.g. compression = Starlette `GZipMiddleware` + the proxy) or a non-bottleneck (e.g. `@app.stream` per-chunk perf — LLM streaming is upstream-rate-limited).
2. **Validate under REAL load, not just unit tests.** Shipped + green unit tests ≠ works under load — this milestone found **FOUR** headline features broken for their real use case (R3 streaming pump, `@app.batch` self-cancel, graceful drain, C2 cancellation), every one green on in-process `TestClient` tests. Any transport/streaming/resilience feature gets a real-server (subprocess + HTTP) adversarial/load test that exercises the path the bug would live on (the *awaiting* handler, the *flooded* cap, the *disconnecting* client).
3. **Runtime dep = `pydantic>=2.7` only.** Everything else is lazy-imported or test-only. Never let `import lancelot` pull `httpx`, `opentelemetry`, `python_multipart`, etc. (Rust-side deps are fine — they're not Python runtime imports.)
4. **Vendored FastAPI tests are SACRED.** `conformance/vendor/fastapi/` is sha256-verified against the FastAPI 0.136.1 sdist. NEVER edit those test files. The "swap" in `conformance/conftest.py` is the only legal indirection.
5. **`researchfms/.git` is OFF-LIMITS.** That's a separate nested repo. Always use `git -C "$LC"` so the cwd hazard never bites.
6. **Don't tag with a red gate.** Conformance ratchet + clippy + fmt + internal tests all green before a tag is pushed.

## After every chunk

```bash
.venv/bin/python -m pytest tests/ -q               # internal smoke
make m1-conformance                                 # FastAPI byte-parity gate
$HOME/.cargo/bin/cargo clippy --release --all-targets -- -D warnings
```

If any of these go red, the chunk does not ship. The conformance floor is `passes=974` (= 99.6% of 978).

## Current milestone state (as of M7 distributed-plumbing+MCP+Rust-patterns)

**M7 (2026-05-23/24) — distributed plumbing + MCP + 14 Rust-project patterns.** 15-of-17 active items shipped inline; only T3.1 (multi-region deploy recipes) + T2.6 (multi-region session-affinity verification) deferred — they need wall-clock deploy infra. Three transcripts/teardowns are the source of every item: YC RA `TRANSCRIPTS_YC_ROOT_ACCESS.md` (T1/T2/T3), `docs/peer-source-recheck/M7-RUST-PATTERNS.md` (T4, 14 patterns from 8 Rust-project teardowns). Headline: outbound LLM transport primitives (`spawn_child`, `fanout`, `lancelot.upstream_metrics`), distributed observability (`queue_wait_ms` + per-tenant cap, `/fleet/status`, region/worker tags), multi-region plumbing (`lancelot.secrets` w/ SIGHUP rotation, mTLS on TransportPool, region-aware route registration), Rust hot-path discipline (`intern!`, file:line on errors, audit comments for cooperation + scratch-pool). 347 tests passing, conformance 978/987 = 99.1% (≥974 floor). Tag: `m7-shipped-inline`.

**This session (2026-05-21/22) — honest validation + M4 scoping.** Benchmarked the claimed wins under real load (oha). Found the Rust server's true envelope (wins large-payload serialization + streaming; loses tiny one-shot to uvicorn+uvloop; anti-scales w/ threads → multiprocess) AND found+fixed TWO headline features that shipped BROKEN for their real use case: the R3 streaming pump (16-26× regression → re-architected to parity-to-1.3×) and `@app.batch` (timer-flush self-cancel → 500 on awaiting handlers → fixed, validated ~12×/~16×). Tags: `m3-bench-honest`, `m3-bench-characterize`, `m3-stream-rearch`, `m3-stream-shutdown`, `m3-batch-validated`. **M2-M3 are complete; the next milestone is M4 "Prove & Harden" — see `docs/plans/M4-PLAN-PROVE-AND-HARDEN.md`** (thesis: shipped + green unit tests ≠ works under load; validate-then-harden the rest before any new surface).

## M7 surface shipped (2026-05-24)

**`lancelot.llm.TransportPool` extensions:**
- `pool.spawn_child(parent_request, session_affinity=...)` — sub-agent primitive that snapshots parent's traceparent + request_id + agent_id + affinity, survives ContextVar reset (T1.1).
- `pool.fanout(coros, first_n=, deadline=, cancel_laggards=True, concurrency=)` — Chroma parallel-probe pattern; partial failure shape; results in completion order (T1.2).
- `pool.request()` auto-injects `x-request-id` + `x-agent-id` + `traceparent` (T1.1 inline).
- `pool.reload_mtls()` for SIGHUP cert rotation (T3.4).
- `TransportPool(mtls_cert=, mtls_key=, mtls_ca=)` mTLS toggle (T3.4).

**`lancelot.upstream_metrics`** (T1.3 + T2.2):
- Provider/model detection (OpenAI / Anthropic / Google / Groq / DeepSeek / Mistral / Together / Fireworks).
- Token extraction across 3 provider shapes (OpenAI `prompt_tokens`, Anthropic `input_tokens`, Google `promptTokenCount`).
- `set_rate_table()` + `LANCELOT_LLM_RATES` env JSON; Lancelot does NOT ship rates.
- Structured error taxonomy: `ok` / `quota` / `timeout` / `permanent` / `transient` / `upstream_5xx`.
- 6 new OTel histograms/counters under `lancelot.upstream.*`.
- Cost accumulates into the parent's `cost_usd` ContextVar (Sim.ai end-to-end → X-Lancelot-Cost-USD trailer covers sub-LLM-calls automatically).

**`lancelot.mcp.mount(app, path='/mcp', max_response_bytes=4096)`** (T1.4 + T1.5):
- Top-level convenience that mirrors `app.mcp()`. Auto-discoverable by code-gen agents.
- T1.5 minimal-return discipline: WARN log when tool returns > 4 KB (Dex Horthy pattern). Configurable / disable with 0.
- Doc: `docs/operator/mcp-guidelines.md` (handle-and-fetch pattern, when big returns are right, schema-strict input as corollary).
- Bonus fix: `App.call_tool(name, /, **kwargs)` positional-only `name` so tools with `name=...` argument don't collide.

**`lancelot.context` ContextVars added:** `cost_usd`, `tokens_in`, `tokens_out`, `agent_id`, `tenant_id`, `queue_wait_ms` — all with sane sentinel defaults; read by ASGI seam in `finally` and passed to `record_request`.

**`lancelot.metrics`** (T2.1 + T2.4 + T2.5 + T4.8):
- `lancelot.http.cost.usd` histogram (T2.1).
- `_bounded_tenant()` + `LANCELOT_TENANT_LABEL_MAX` (T2.4) — over-cap → `_overflow_` bucket.
- `http.status_code` attr now on every metric (T2.5) — adverse-path slicing.
- `lancelot.http.queue_wait_ms` histogram (T4.8).
- `X-Lancelot-Cost-USD` response header (6-decimal) when cost_usd > 0.

**`lancelot.tenant.TenantConcurrencyLimiter`** (T4.8) — opt-in ASGI middleware. Per-tenant `asyncio.Semaphore`; tenant from header (default `X-Tenant-Id`); anon shares `_anon_` bucket; env `LANCELOT_TENANT_CONCURRENCY` or explicit kwarg; cap=0 disables.

**`lancelot.fleet`** (T2.3):
- `region()`, `worker_id()`, `started_at()`, `fleet_info(app)`, `install_otel_resource_attrs()`.
- Env: `LANCELOT_REGION` (default `default`), `LANCELOT_WORKER_ID` (default PID).
- `/fleet/status` GET endpoint auto-registered: {region, worker_id, status, in_flight, uptime_s, lancelot_version}. status flips to "draining" on initiate_shutdown().

**`@app.get('/x', region='eu-west-1')`** (T3.5) — region-pinned routes; non-matching workers silently drop the route. Zero hot-path cost; failover is the LB's job.

**`lancelot.secrets`** (T3.2):
- Backends: `EnvBackend` (default), `FileBackend`, `VaultBackend`, `InfisicalBackend`, `AwsSecretsManagerBackend`. All non-env backends lazy-import their SDK (cardinal rule).
- `lancelot.secrets.get(name)` / `configure(backend)` / `rotate()` / `install_sighup_handler()` / `set_audit_hook(fn)`.
- Audit hook is fail-safe (a raising hook is logged but does not propagate).

**`lancelot.memory_pool.MemoryPool(total_bytes)`** (T4.6) — DataFusion TrackConsumersPool pattern:
- `pool.reserve(name, n_bytes)` returns RAII `MemoryReservation` (sync `with` OR async `async with`).
- `ResourcesExhausted(top_consumer=, top_consumer_bytes=, requested_bytes=, available_bytes=)` — error names the top consumer in human bytes ("Top consumer: 'embed:acme' holding 380.0MB").

**Rust hot-path wins:**
- `intern!` on `__anext__` / `__aiter__` / `media_type` / `pydantic_core` / `to_json` / `orjson.dumps` / `asyncio` / `new_event_loop` (T4.13).
- Sized header Vec from HeaderMap.len() to avoid mid-collect realloc (T4.14 micro-improvement).
- File:line on Rust↔Python boundary errors (T4.12 — lightweight, no snafu dep).
- T4.1 cfg(test) buffer=1 (forces backpressure code path under every unit test) + T4.2 select_biased! (SIGTERM priority compile-time-guaranteed) + T4.9 set_retry_hint (Turbopuffer pattern) + T4.10 Levenshtein 404 + T4.11 SIGINT poll — all shipped earlier in this session.

**M7 deferred (need wall-clock deploy infra):**
- T3.1 multi-region deploy recipes (Render multi-region + AWS multi-AZ + Fly.io) — needs ~9-12h wall clock to provision + 1h soak each.
- T2.6 multi-region session-affinity verification — gates on T3.1.

**M4-V1 (resilience under adversarial load) IN PROGRESS** (`tests/test_m4_resilience_load.py`, real server + subprocess): V1.1 slow-loris ✓, V1.2 body-cap flood ✓, V1.3 connection-cap throttle ✓ — all held. **V1.4 graceful drain was BROKEN** (the THIRD headline feature to fail under load, after streaming + batch): on SIGTERM the Rust server cut in-flight requests at the 100ms window + cancelled them → 500, NOT drained (Robyn-style hard-kill — the exact B1 anti-goal). Fixed in `serve()`: `graceful_break` now `acquire_many_owned(max_conns)` (drains until every in-flight connection releases its permit, bounded by `drain_timeout()`) while still inside `run_until_complete` so handlers finish. New operator knobs: `LANCELOT_HEADER_TIMEOUT_MS` (slow-loris deadline, default 15000), `LANCELOT_MAX_CONNECTIONS` (default 1024), `LANCELOT_DRAIN_SECS` (graceful-drain deadline, default 10). Remaining: V1.5 per-route timeout under concurrency, then Tier-V2/V3.

- **M0** green (Rust core, hyper/tokio/pyo3)
- **M1-EXIT** hit at 90.0% conformance, tag `m1-exit-90`
- **M2 Tier-A** — wire/latency wins (TCP_QUICKACK/FASTOPEN, JSON depth guard, /livez/readyz, SSE keepalive, `stream_interval`), tag `m2-tierA`
- **M2 Tier-B** — cloud-native resilience (graceful shutdown, RED metrics, ContextVar request-id, connection cap, per-route timeout), tag `m2-tierB`
- **M2 Tier-C** — AI primitives (canonical JSON, session-affinity, transport-pool cancellation, tool output_schema, dynamic-write audit), tag `m2-tierC`
- **M2 Tier-C5** — marquee `@app.batch` for prefix-aware request grouping, tag `m2-tierC5`
- **M2 conformance push** — 876 → 910 Class-A passes (93.5%), tag `m2-conformance-push`. Unlocks: `@app.exception_handler` surface, `strict_content_type`, `Json[T]` Annotated preservation, lenient cookie parser, subclass-class-attr media_type, callable-instance + partial DI, single-Body scalar pattern, Starlette plaintext 500, Optional[list[T]] form wrapping.
- **M2 conformance push 2** — 910 → 948 Class-A passes (97.4%), tag `m2-conformance-push2`. Unlocks: yield-dep swallowed-exception → FastAPIError, SecurityScopes chain propagation + per-scope cache_key, stream item validation, lru_cache/wraps unwrap in DI, 204/205/304/1xx no-body emit, Depends/Security hashable, include_router empty-path/self guards, TRACE method, list[Model]/dict[Model] response coercion, Starlette ASGI-callable response delegation (FileResponse/RedirectResponse), @app.on_event decorator, PEP 695 TypeAliasType unwrap, lax-mode explicit-CT enforcement, WS metaclass for Starlette cross-stack disconnect, WS query/cookie at handshake.
- **M2 conformance push 4** — 971 → 974 Class-A passes (99.6%), tag `m2-conformance-push4`. Custom `APIRoute` extension: `Route.get_route_handler()` (opt-in; hot path untouched), `Router.route_class` / `APIRouter(route_class=...)`, `_BodyAccessor` (bytes-subclass that's also `await request.body()`-callable), single-Body-scalar error loc `["body"]`. **The 4 remaining failures are documented no-bullshit-boundary divergences — see "Conformance ceiling" below.**
- **M2 conformance push 3** — 948 → 971 Class-A passes (99.3%), tag `m2-conformance-push3`. Unlocks: scope="function" / "request" yield-dep semantics with separate AsyncExitStacks (function-scope tears down BETWEEN handler-return and response-emit; request-scope after); scope-aware cache_key normalization; `FastAPI(dependencies=[...])` app-level deps; registration-time scope-incompatibility check (request-scope yield-dep can't depend on function-scope yield-dep); Response-in-return-annotation → FastAPIError; `PydanticV1NotSupportedError` for pydantic.v1 anywhere in route annotations; `Annotated[..., Path/Query(default=...)]` ambiguity assertions; multi-Param constraint merge (`Annotated[int, Query(gt=2), Query(lt=10)]`); absolute Location URLs on 307 redirect_slashes; middleware-receive HTTPException catch; `_wire_json` jsonable-encoder fallback (UUID, datetime, BaseModel, Decimal, Path, set, bytes — and asyncpg-style LYING UUID classes via str()); method-aware body inference (parameterized container on POST/PUT/PATCH is body, on GET stays query); fastapi.param_functions + fastapi.routing swap surfaces; ORJSONResponse / UJSONResponse pass-through.

Public surface added this milestone:
- `lancelot.canonical.dumps_canonical(obj)` → stable JSON bytes for KV-cache stability
- `lancelot.context.request_id` ContextVar (+ `X-Request-ID` echo on incoming, CRLF-sanitized, 128-char bounded)
- `App(max_concurrent_requests=N, drain_timeout=30.0, default_response_class=..., exception_handlers={ExcCls: fn}, strict_content_type=True)`
- `App.initiate_shutdown() / drain(timeout) / install_signal_handlers()`
- `App.add_exception_handler(ExcCls, fn)` / `@app.exception_handler(ExcCls)` decorator
- `@app.stream(stream_interval=N, sse_keepalive=S)`
- `@app.batch(batch_window_ms, max_batch, max_keys, key=...)`
- `@app.middleware("http")`
- `@app.tool(...) → tool_schemas()[i]["output_schema"]` auto-derived
- `Response.set_session_affinity(key)`
- Per-route `timeout=` kwarg → 504 on miss, yield-dep teardown preserved
- Per-route / per-router `strict_content_type=True/False/None` (inherits)
- `@app.on_event("startup"/"shutdown")` (deprecated, emits FastAPIDeprecationWarning)
- `App.add_exception_handler(ExcCls, fn)` / `@app.exception_handler(ExcCls)` decorator
- TRACE HTTP method on App + APIRouter
- `Depends(dep, scope="function")` — yield-dep teardown runs BETWEEN handler-return and response-emit (vs default "request" = after the response body)
- `FastAPI(dependencies=[Depends(...)])` — app-level deps prepended to every route
- Magic-inject types: `Request`, `BackgroundTasks`, `WebSocket`, `Response`, `SecurityScopes`

## M3 — Rust hot-path swaps (COMPLETE)

All 7 R-swaps from `docs/plans/2026-05-20-...:R-swaps` are landed. Each gated by criterion bench + `clippy -D warnings` + unsafe-checker skill audit + a verification agent (the plan's per-swap discipline):
- **R1** const-tier routing — shipped as **O(1) matchit routing** (Python router + Rust `matchit`). NOTE (recheck honesty fix): this is constant-TIME routing, NOT Robyn's prebaked-bytes ConstRouter (serve a fixed response from Rust with zero GIL/Request alloc/handler call). That prebaked tier was a STUB until 2026-05-22, when it was BUILT as the const-bytes tier (`const_routes` on `_native.run` → `service::Prebaked`, served zero-GIL; 183k vs 66k uvicorn+uvloop). See "Recheck future options" #1 below.
- **R2** lazy request proxy — `m3-r2`. The M0 dispatch HARDCODED `Request("POST","/echo")`; now threads real method/path/query/headers (lazy, OnceLock-cached getters). `src/ffi/request.rs`, `src/dispatch.rs` (`ReqParts`).
- **R3** streaming pump — `m3-r3`. `src/stream.rs`: async-gen → bounded-mpsc → hyper `StreamBody`; GIL dropped across each `__anext__` await; backpressure + cancellation + shutdown-aware. `_drive_stream` in-process test helper.
- **R4** single-pass `validate_json` — via pydantic-core (`Request.json` uses `from_json`).
- **R5** Rust serialize — via pydantic-core (`serialize::_to_json`; R3 JSON-lines).
- **R6** `header_read_timeout` + body cap — shipped + audit-hardened.
- **R7** `catch_unwind` FFI panic safety — shipped; extended to the R3 pull task.

**Rust-core audit (2 deep-review agents + unsafe-checker skill) fixed real bugs:** CRITICAL unbounded-body memory-DoS on the `run()` path (→ 16 MiB `Limited` → 413); router `.expect()` panic on duplicate/malformed routes (→ `PyValueError` at startup); `_drive` event-loop fd leak; slot-bounds panic outside `catch_unwind`; stream-pump shutdown leak; latin-1 header decode. **GIL-across-await invariant verified CLEAN** in all new code. The only `unsafe` is the `libc::setsockopt` in `socket.rs` (TCP_QUICKACK/FASTOPEN, SAFETY-commented). Rust bench: `cargo bench --bench router_bench` (static route ~12ns, param ~22ns).

Rust gate (run alongside the Python gate): `$HOME/.cargo/bin/cargo clippy --release --all-targets -- -D warnings` + `cargo test --release --lib`. Wheel rebuild after Rust changes: `PATH="$HOME/.cargo/bin:$PATH" .venv/bin/maturin develop --release`.

## Conformance ceiling (the 4 remaining Class-A failures @ 99.6%)

These are left RED (not excluded/gamed). Each would require violating a cardinal rule to "fix":

1. **`test_dependency_contextmanager::test_background_tasks`** — asserts a user `@app.middleware` sees global state BEFORE request-scope yield-dep teardown. FastAPI achieves this only via `BaseHTTPMiddleware`'s anyio task-group streaming (call_next returns while the inner app keeps running). Our middleware adapter is synchronous-capture; replicating the streaming model is a large risky refactor that endangers the 11 passing yield-dep tests. Not worth it for one assertion.
2. **`test_dump_json_fast_path::test_explicit_response_class_uses_json_dumps`** — mocks `starlette.responses.json.dumps` and asserts it's called. We serialize via pydantic-core/`_native` (faster, no double-encode); we're arguably *more* correct. Matching the mock means coupling to a Starlette internal.
3. **`test_tutorial/test_templates/test_tutorial001`** — vendored test does `shutil.copytree("./docs_src/...")` assuming cwd = the FastAPI repo root. Our harness runs from `conformance/`. Pure environmental; "fixing" = harness-gaming.
4. **`test_tutorial/test_wsgi`** (collection error) — needs the optional `a2wsgi` package. Excluded under the pydantic-only runtime rule (Tier-D).

If a future change makes any of these legitimately fixable (e.g. a real BaseHTTPMiddleware streaming impl lands for other reasons), revisit. Until then, the floor stays at 974 and these stay counted as failures.

## Rust server perf envelope (honest benchmark, 2026-05-21)

`benchmarks/` (oha, Apple M5, loopback). A fairness-review agent caught the first cut as rigged (10-thread Rust vs single-thread uvicorn on its slow h11 stack, falsely "1.64× faster"). The CORRECTED benchmark (uvicorn on uvloop+httptools, matched workers):

- **Tiny 23 B response (UPDATED M10 — uvloop on the embedded loop).** Rust now **59k req/s** (was 44.8k on stdlib asyncio): **beats uvicorn-fastapi (41k), Robyn (36k), Blazeio (59k)**; raw Starlette+uvloop (67k) is ~12% ahead — the residual FFI/asyncio-bridge cost on the one tiny-handler shape. The const tier (183k) flips it. uvloop is a soft-dep (cardinal rule: pydantic-only runtime; `make_event_loop` in `src/lib.rs` prefers uvloop, falls back to stdlib; `LANCELOT_LOOP=asyncio` escape hatch). A/B verified: tiny 44.3k(asyncio)→56.9k(uvloop) = +28%; large + stream neutral (serialization-/send-bound, not loop-bound).
- **Serialization crossover ~1–4 KB** (`characterize.sh` sweep): below it uvicorn+uvloop wins; above it Rust-native pydantic-core `to_json` beats Python `json.dumps`, growing to ~2.3× at 69 KB (5.3k vs 2.3k). **This is the only substantiated Rust throughput win, and it IS the positioning.**
- **STREAMING — R3 pump RE-ARCHITECTED (was a 16–26× regression, now a win).** The original pump did one cross-thread `call_soon_threadsafe` round-trip per `__anext__` (~0.8 ms/chunk) → 64 chunks = 937 rps. Fix (`src/stream.rs`): a `_drain(gen, sink)` coroutine runs ON the asyncio loop (ONE bridge crossing for the whole stream), `async for` drives the gen natively, each chunk is a SAME-THREAD `sink.try_push` into a bounded channel; backpressure preserved via `await_capacity` (event-driven `reserve().await`, only fires when the channel fills). Now 64→15.2k (parity w/ Starlette), 256→6.8k (~1.29×), 1024→1.8k (~1.19×) — **16–30× recovery.** Buffer depth tunable via **`LANCELOT_STREAM_BUFFER`** (default 256 = the throughput knee; clamped [1,4096]). `next_chunk` retained only for the `_drive_stream` test. Cancellation: client disconnect → next `try_push` sees Closed → coro returns, gen left suspended (≤1 extra chunk). SIGTERM: `serve()` now does a graceful asyncio teardown (`graceful_loop_teardown` — cancel pending tasks + `shutdown_asyncgens`, bounded 2s) BEFORE `loop.close()`, so the user generator's `finally`/`aclose` RUNS (audit HIGH-2; verified: no "Task was destroyed" warning). Tests: `test_r3_streaming_backpressure_order_and_completeness` (5000 chunks), `test_r3_stream_disconnect_then_server_still_serves`.
- **The Rust server ANTI-SCALES with tokio threads** (1→10 workers: 44.8k→34k) — Python is GIL-serialized through one asyncio loop per process. **Deploy MULTIPROCESS** (`LANCELOT_WORKERS=1`/proc, N procs), NEVER 1-process-many-threads. **`SO_REUSEPORT` load-balancing is LINUX-ONLY** (V3.3 finding): macOS/BSD `SO_REUSEPORT` binds but delivers to ONE socket, so the benchmark's macOS "10×1" number is a no-distribution FLOOR (≈ single-process), NOT the Linux ceiling — on Linux it scales with cores. On macOS, front N procs on distinct ports with a proxy. See `docs/deploy.md`.
- New operator knob: **`LANCELOT_WORKERS`** env overrides `cpu::worker_count()` (clamped [1,64]; invalid/0 → cgroup/logical detection).
- New operator knob (M10): **`LANCELOT_LOOP`** — `asyncio` forces the stdlib loop instead of uvloop (default = uvloop when installed). For A/B perf comparison or environments where uvloop misbehaves.
- Hardening this session: server is now **HTTP/1.1-only** (`hyper::http1::Builder` replaced `AutoBuilder`, which served unguarded h2c — the slow-loris `header_read_timeout` was http1-only; this server is cleartext-behind-a-TLS-proxy so h2c was untested+unguarded). Fixed stale `alloc_probe.rs` (alloc gate=3); removed dead `gil::detach`.

Run it: `./benchmarks/run_load.sh` (4-variant, 2 shapes) + `./benchmarks/characterize.sh` (crossover + streaming sweeps); raw dumps in `benchmarks/results/`.

**`@app.batch` (C5 marquee) — validated + a CRITICAL bug fixed (`./benchmarks/run_batch.sh`).** Against a rate-limited upstream (cap 8, 50ms/call, c=200): batched 2,165 rps vs solo 179 (~12×), coalescing ratio 15.98 (≈ max_batch) — ~16× fewer upstream calls. The effectiveness bench EXPOSED that the timer-window flush cancelled its OWN task (`_flush` → `self.timer_task.cancel()` while running inside it), poisoning any **awaiting** batch handler (the real use case) with CancelledError → 500. Existing tests used non-awaiting handlers and missed it. Fixed in `lancelot/batch.py` (skip cancel when `timer is asyncio.current_task()`); regression test `test_c5_awaiting_handler_on_timer_flush_succeeds`.

## Recheck future options (2026-05-22 — rust-skill audit + FastAPI/Robyn/Blazeio alignment)

Two verification agents (Rust soundness+perf via rust skills; framework alignment) ran. **Rust core verdict: SOUND** — no UB/GIL-across-await/Send-Sync/panic-across-FFI/leak/unbounded-DoS; only the 3 low-risk perf items below, all DONE this session (tag `m4-recheck-perf`): F3 defer per-request alloc past routing (404 flood 158k vs matched 44k), F4 memoize pydantic_core to/from_json (via `PyOnceLock` — std `OnceLock` deadlocked on the GIL-releasing first import), F1 bound `shutdown_asyncgens`. Streaming/SSE confirmed at-or-ahead of FastAPI 0.136.1; `@app.batch` is a genuine Lancelot-only primitive; http1-only + no-orjson + AI-logic-boundary all confirmed deliberate.

**Two byte-path options surfaced; #1 BUILT, #2 pending:**
1. ✅ **Rust prebaked-bytes const tier — BUILT** (tag `m4-const-tier`). `_native.run(routes, host, port, const_routes=[(method, path, body, content_type, status), ...])` — a const route is served as prebaked `Bytes` straight from Rust with ZERO GIL / Request alloc / handler call (the `handle()` slot check + `service::Prebaked`). **Measured per-core: 183k req/s vs the handler path's 44k vs uvicorn+uvloop's 66k (~2.8×)** — flips our one conceded loss (tiny one-shot responses) into a decisive WIN; makes `/livez`/`/readyz`/static/cached endpoints free. Content-type/status validated at registration (no hot-path panic). Tests: `tests/test_m4_const_tier.py`.
2. ❌ **Response compression — DON'T BUILD (already covered).** Investigated 2026-05-22: `lancelot.App.add_middleware(GZipMiddleware)` (Starlette's, FastAPI-identical) works out of the box (verified — response comes back `content-encoding: gzip`). And the deploy model puts a TLS-terminating proxy in front, which compresses too. So app-layer gzip is available where it matters (the Python seam) and redundant on the Rust `_native` server (the proxy's job). Building our own gzip would be reimplementing Starlette middleware + the proxy = useless logic (now Tier-D). The loopback benchmark also can't show a compression win (it's a real-network/bandwidth win, not throughput). NOT worth a Rust dep + hot-path branch.

Everything else from the teardowns is "harden/validate what exists" (the correct bias given M4 found 4 broken features) or already Tier-D.

## Core-transcript recheck (2026-05-22 — `researchfms/core/` second pass)

A careful re-read of `core/` (esp. the 7 TRANSCRIPTS_*.md) — the prior "all AI-logic, skip" was directionally right but missed real signal:
- **BUILT: streaming TTFT metric.** The transcripts are unanimous that **TTFT (time-to-first-token) + P99, NOT total throughput, are THE user-facing-AI metrics** ("above 200-300ms users perceive lag"; DEEPLEARNINGAI:9240/3467/10377). Our RED metrics emitted total duration only — wrong number for a token stream. Added `record_stream_ttft` → `lancelot.http.stream.ttft` (recorded on the first streamed chunk; keepalive pings excluded). P99 already falls out of the duration histogram via OTel.
- **Positioning sharpeners (framing, not new code) — the field validates existing features:** `Response.set_session_affinity` IS "KV-cache-aware routing for the agent backend" (DEEPLEARNINGAI:3487 — "the LB must be cache-aware… not available out-of-the-box"); `lancelot.canonical.dumps_canonical` IS "prompt-cache-stability serialization" (DEEPLEARNINGAI:781 — byte-stable prefix → >80% cache hit, 5-10× token + TTFT win). Describe them that way.
- **Confirmed SKIPs:** durable/checkpointed execution (orchestration = user-side + Tier-D), wire-compatible model gateway / model routing (AI-logic + Tier-D), AG-UI event schema (AI-UI protocol; our SSE/WS transports already serve it), SSE `Last-Event-ID` REPLAY (stateful resume = user-side; `stream.py` already declined it), mid-stream abort (already-have — a user aborts by returning from their async-gen).

## Tier-D — explicit DO-NOT-DO list (no-bullshit anchor)

These are tempting; the research teardowns + the no-bullshit rule say not to build them. If a future agent (or human) is about to spend cycles on one of these, **stop and check this list first.**

- ❌ **Claim/pursue small-request throughput as the Rust selling point** — the honest benchmark shows uvicorn+uvloop wins there. Lead with large-payload (>~4 KB) serialization + resilience instead (see perf envelope above).
- ❌ **Regress the R3 pump back to per-`__anext__` bridging** — the on-loop `_drain` design (one bridge crossing/stream, same-thread `try_push`) is what makes streaming fast. Any change that reintroduces a cross-thread `into_future`/`call_soon_threadsafe` PER chunk re-creates the 16–26× cliff. Keep the per-chunk path same-thread; keep backpressure event-driven (only on full).
- ❌ **Deploy the Rust server 1-process-many-threads** — it anti-scales (GIL). Multiprocess via `SO_REUSEPORT` is the only scaling model.
- ❌ **Reimplement response compression (gzip/br)** — `lancelot.App.add_middleware(GZipMiddleware)` (Starlette's) already works (verified), and the TLS-terminating proxy in front compresses too. App-layer gzip on the Rust server is redundant with the proxy and can't even be benchmarked on loopback. Document the middleware, don't build a serializer.
- ❌ **Optimize `@app.stream`'s per-chunk overhead** — it's ~6.7× slower than raw Starlette at MAX chunk rate (~128k chunks/s/core vs ~14k full-streams/s), but real LLM token streaming is UPSTREAM-rate-limited (~50-200 tok/s/stream), so the framework is never the bottleneck — the LLM is. Not worth a risky keepalive-loop refactor for a non-bottleneck. (The 2026-05-22 recheck DID fix the real streaming bug there: `@app.stream` silently emitted EMPTY bodies on `bytes`/`str` yields — now coerces bytes/str/Event, tag `m4-stream-coerce`.)

- ❌ **Custom JSON serializer (orjson / ujson)** — Pydantic v2 `TypeAdapter.dump_json` is already Rust-core fast. FastAPI's own ledger marks orjson/ujson responses deprecated.
- ❌ **OpenAPI callbacks / webhooks** — rare, complex, no payoff for AI/cloud/startup positioning.
- ❌ **Dependency-overrides per-request rebuild** — useful only for tests; the simple override-by-`call` we ship is sufficient.
- ❌ **Built-in CLI (`lancelot run …`)** — FastAPI itself delegates to `fastapi-cli` (separate package). Users invoke uvicorn / hypercorn or our future Rust runner. Not core.
- ❌ **Replacing AI logic** (agents, vector stores, prompt templates) — that's user-side. Lancelot owns transport. Period.
- ❌ **Robyn-style hard-kill shutdown** — explicit anti-goal. We do graceful drain (B1).
- ❌ **Per-chunk Pydantic validation on streams** — streaming is raw-bytes + lightweight typing; any path that re-validates per chunk is a perf cliff and should be killed.

## Where to look for things

- Plan + roadmap: `docs/plans/M2-M3-PLAN-FROM-RESEARCH.md` (M2-M3, complete); `docs/plans/M4-PLAN-PROVE-AND-HARDEN.md` (M4, next — validate-under-load + harden)
- Benchmarks: `benchmarks/` (`run_load.sh`, `characterize.sh`, `run_batch.sh`) + `benchmarks/README.md` (honest perf envelope)
- Research distillations: `researchfms/{robyn,fastapi,blazeio,core,teardowns}/` (the `.git` inside this dir is OFF-LIMITS)
- Conformance harness: `conformance/{classify.py,ratchet.py,runner.py,conftest.py}`
- Floor: `conformance/parity_floor.txt` — passes=876
- Internal regression suites: `tests/test_m2_tier_{a,b,c,c5_batch}.py`

## Build env

- Python 3.13.13, venv at `.venv/`
- Cargo at `$HOME/.cargo/bin/cargo` (not on PATH by default — call by full path)
- pyo3 0.27.2 + hyper 1 + tokio + matchit + socket2 (+ libc only on Linux for the TCP knobs)
- maturin builds the cdylib; `cargo check` works on the pure-Rust crate too
