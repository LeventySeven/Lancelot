# Lancelot — Claude session notes

This file is read by Claude Code on every session. Stays short and operational; deep history lives in `docs/plans/` and the memory store.

## What Lancelot is (positioning lock — do not drift)

A backend framework like FastAPI / Robyn that optimizes **HTTP transport / serialization / streaming** for **AI / cloud / web / startups**.

**AI logic stays on the user side** — LangGraph, agent loops, vector stores, prompt orchestration. We own the byte path; the user owns the agent. This is the user's restated rule; every "should we build X" decision flows from it.

**Honest perf envelope (do not oversell — see "Rust server perf envelope" below).** The Rust core's substantiated throughput wins are **large-payload (>~4 KB) one-shot serialization** and **streaming** (after the R3 re-architecture — now parity-to-1.3× over Starlette); its other value is **resilience** (body/conn caps, slow-loris, drain). A fair benchmark (2026-05-21) shows uvicorn+uvloop still *beats* our Rust server on tiny one-shot responses (per-request FFI/bridge overhead). Lead with large-payload serialization + streaming + resilience, never a blanket "faster server".

## Cardinal rules

1. **No bullshit, no useless logic.** Every change must change the byte path or the operator experience. If it doesn't, it shouldn't ship.
2. **Runtime dep = `pydantic>=2.7` only.** Everything else is lazy-imported or test-only. Never let `import lancelot` pull `httpx`, `opentelemetry`, `python_multipart`, etc.
3. **Vendored FastAPI tests are SACRED.** `conformance/vendor/fastapi/` is sha256-verified against the FastAPI 0.136.1 sdist. NEVER edit those test files. The "swap" in `conformance/conftest.py` is the only legal indirection.
4. **`researchfms/.git` is OFF-LIMITS.** That's a separate nested repo. Always use `git -C "$LC"` so the cwd hazard never bites.
5. **Don't tag with a red gate.** Conformance ratchet + clippy + internal tests all green before a tag is pushed.

## After every chunk

```bash
.venv/bin/python -m pytest tests/ -q               # internal smoke
make m1-conformance                                 # FastAPI byte-parity gate
$HOME/.cargo/bin/cargo clippy --release --all-targets -- -D warnings
```

If any of these go red, the chunk does not ship. The conformance floor is `passes=974` (= 99.6% of 978).

## Current milestone state (as of m3-batch-validated)

**This session (2026-05-21/22) — honest validation + M4 scoping.** Benchmarked the claimed wins under real load (oha). Found the Rust server's true envelope (wins large-payload serialization + streaming; loses tiny one-shot to uvicorn+uvloop; anti-scales w/ threads → multiprocess) AND found+fixed TWO headline features that shipped BROKEN for their real use case: the R3 streaming pump (16-26× regression → re-architected to parity-to-1.3×) and `@app.batch` (timer-flush self-cancel → 500 on awaiting handlers → fixed, validated ~12×/~16×). Tags: `m3-bench-honest`, `m3-bench-characterize`, `m3-stream-rearch`, `m3-stream-shutdown`, `m3-batch-validated`. **M2-M3 are complete; the next milestone is M4 "Prove & Harden" — see `docs/plans/M4-PLAN-PROVE-AND-HARDEN.md`** (thesis: shipped + green unit tests ≠ works under load; validate-then-harden the rest before any new surface).

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
- **R1** const-tier routing — shipped (Python-side router).
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

- **Tiny 23 B response → uvicorn+uvloop WINS** (~1.56× per-core: 69.9k vs Rust 44.8k; full-machine 124k vs 34k). The Rust path pays ~0.5 ms/request for the pyo3 FFI + cross-thread asyncio-bridge round-trip; uvloop runs the coroutine natively same-thread.
- **Serialization crossover ~1–4 KB** (`characterize.sh` sweep): below it uvicorn+uvloop wins; above it Rust-native pydantic-core `to_json` beats Python `json.dumps`, growing to ~2.3× at 69 KB (5.3k vs 2.3k). **This is the only substantiated Rust throughput win, and it IS the positioning.**
- **STREAMING — R3 pump RE-ARCHITECTED (was a 16–26× regression, now a win).** The original pump did one cross-thread `call_soon_threadsafe` round-trip per `__anext__` (~0.8 ms/chunk) → 64 chunks = 937 rps. Fix (`src/stream.rs`): a `_drain(gen, sink)` coroutine runs ON the asyncio loop (ONE bridge crossing for the whole stream), `async for` drives the gen natively, each chunk is a SAME-THREAD `sink.try_push` into a bounded channel; backpressure preserved via `await_capacity` (event-driven `reserve().await`, only fires when the channel fills). Now 64→15.2k (parity w/ Starlette), 256→6.8k (~1.29×), 1024→1.8k (~1.19×) — **16–30× recovery.** Buffer depth tunable via **`LANCELOT_STREAM_BUFFER`** (default 256 = the throughput knee; clamped [1,4096]). `next_chunk` retained only for the `_drive_stream` test. Cancellation: client disconnect → next `try_push` sees Closed → coro returns, gen left suspended (≤1 extra chunk). SIGTERM: `serve()` now does a graceful asyncio teardown (`graceful_loop_teardown` — cancel pending tasks + `shutdown_asyncgens`, bounded 2s) BEFORE `loop.close()`, so the user generator's `finally`/`aclose` RUNS (audit HIGH-2; verified: no "Task was destroyed" warning). Tests: `test_r3_streaming_backpressure_order_and_completeness` (5000 chunks), `test_r3_stream_disconnect_then_server_still_serves`.
- **The Rust server ANTI-SCALES with tokio threads** (1→10 workers: 44.8k→34k) — Python is GIL-serialized through one asyncio loop per process. **Deploy MULTIPROCESS** (`SO_REUSEPORT` via `bind_reuseport`, `LANCELOT_WORKERS=1`/proc, N procs), NEVER 1-process-many-threads.
- New operator knob: **`LANCELOT_WORKERS`** env overrides `cpu::worker_count()` (clamped [1,64]; invalid/0 → cgroup/logical detection).
- Hardening this session: server is now **HTTP/1.1-only** (`hyper::http1::Builder` replaced `AutoBuilder`, which served unguarded h2c — the slow-loris `header_read_timeout` was http1-only; this server is cleartext-behind-a-TLS-proxy so h2c was untested+unguarded). Fixed stale `alloc_probe.rs` (alloc gate=3); removed dead `gil::detach`.

Run it: `./benchmarks/run_load.sh` (4-variant, 2 shapes) + `./benchmarks/characterize.sh` (crossover + streaming sweeps); raw dumps in `benchmarks/results/`.

**`@app.batch` (C5 marquee) — validated + a CRITICAL bug fixed (`./benchmarks/run_batch.sh`).** Against a rate-limited upstream (cap 8, 50ms/call, c=200): batched 2,165 rps vs solo 179 (~12×), coalescing ratio 15.98 (≈ max_batch) — ~16× fewer upstream calls. The effectiveness bench EXPOSED that the timer-window flush cancelled its OWN task (`_flush` → `self.timer_task.cancel()` while running inside it), poisoning any **awaiting** batch handler (the real use case) with CancelledError → 500. Existing tests used non-awaiting handlers and missed it. Fixed in `lancelot/batch.py` (skip cancel when `timer is asyncio.current_task()`); regression test `test_c5_awaiting_handler_on_timer_flush_succeeds`.

## Tier-D — explicit DO-NOT-DO list (no-bullshit anchor)

These are tempting; the research teardowns + the no-bullshit rule say not to build them. If a future agent (or human) is about to spend cycles on one of these, **stop and check this list first.**

- ❌ **Claim/pursue small-request throughput as the Rust selling point** — the honest benchmark shows uvicorn+uvloop wins there. Lead with large-payload (>~4 KB) serialization + resilience instead (see perf envelope above).
- ❌ **Regress the R3 pump back to per-`__anext__` bridging** — the on-loop `_drain` design (one bridge crossing/stream, same-thread `try_push`) is what makes streaming fast. Any change that reintroduces a cross-thread `into_future`/`call_soon_threadsafe` PER chunk re-creates the 16–26× cliff. Keep the per-chunk path same-thread; keep backpressure event-driven (only on full).
- ❌ **Deploy the Rust server 1-process-many-threads** — it anti-scales (GIL). Multiprocess via `SO_REUSEPORT` is the only scaling model.

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
