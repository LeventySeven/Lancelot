# Lancelot — The Ultimate Buildable Work-Breakdown Structure (Parallel-Agent Execution Plan)

> The buildable engineering-plan feeder. This is the concrete, reimplementable blueprint a fleet
> of parallel coding agents executes concurrently to ship Lancelot in hours. Everything here
> traces to the design corpus: `DESIGN-DECISIONS.md` (forks resolved), `SYNTHESIS.md` (steal-list),
> `research/design/01-core-performance-stability.md` (§ = `D01 §`), `02-deployment-cloud-native.md`
> (`D02 §`), `03-versatility-competitive-surface.md` (`D03 §`), `04-ai-backend-patterns-observability.md`
> (`D04 §`). Labels: **KNOWN** (from a teardown/source/cited URL via the corpus), **INFERRED**
> (reasoned from KNOWN), **DESIGN** (a Lancelot build decision made here for executability).
>
> Reading order: exec summary (gates + stack) → §A repo/crate layout → §B module trees →
> §C the parallel-agent WBS (the dependency-ordered unit list) → §D milestone gates →
> §E M0 vertical-slice spec → §F test/CI/release pipeline → §G `lancelot init` contents.

---

## Executive summary — milestone gates + crate stack (12 lines)

1. **M0 (vertical slice, gate = 1 measurable e2e):** `hyper` accept → `matchit` route → `with_gil` call a Python `async def` → `pydantic-core dump_json` → response, GIL dropped across the `.await`, on one process. **Gate:** `curl` a JSON echo handler returns 200 with correct body; `clippy::await_holding_lock` clean; 1 alloc-profiled request ≤ 5 framework allocs.
2. **M1 (table-stakes, gate = FastAPI migration MVP):** D03 §6 v0 list 1–15 (typed params, Pydantic v2 in+out, `Depends()`+yield, OpenAPI 3.1, `include_router`, two-tier middleware, lifespan, CORS, sync+async w/ bounded pool, exact 422 wire-shape, `TestClient`, WebSockets, static files, settings, real background tasks). **Gate:** conformance harness ≥ 90 % (§D).
3. **M2 (AI tier, gate = thesis demo):** AI streaming tier (`AISSEResponse` codec=openai/anthropic/vercel_ai, Rust SSE codec, SPSC ring, disconnect→cancel→abort), const-tier, `app.llm`, `@app.mcp`, `@app.agent`, `background.spawn`. **Gate:** disconnect-aborts-upstream proven by a test; const-tier `/livez` passes under saturated pool.
4. **M3 (ops/cloud, gate = production-deployable):** SIGTERM drain, worker supervisor + respawn, cgroup-aware count, 3 probes, `/metrics`+OTLP+JSON logs, GenAI spans, `lancelot init` Docker+K8s, `$PORT`, sub-50 ms cold start. **Gate:** the D02 §5.7-equivalent 12-point stability checklist all green; cold-start benchmark < 50 ms.
5. **M4 (conformance-green, gate = ship):** conformance harness ≥ 99 % vs FastAPI; wheel matrix builds (abi3-py311 + cp313t/cp314t × 4 platforms); benchmark-vs-FastAPI published; all `[R]` items in D02 §7 done.
6. **Crate stack (pinned, D01 §7.1):** `pyo3 0.27` (`extension-module,py-clone`, 0.28-clean idiom) · `pyo3-async-runtimes 0.27` (`tokio-runtime`) · `tokio 1` (`full`) · `hyper 1` (`http1,http2,server`) · `hyper-util 0.1` (`server,server-auto,tokio`) · `http-body-util 0.1` · `tower 0.5` · `tower-http 0.6` · `tokio-tungstenite 0.24` · `socket2 0.5` · `matchit 0.8` · `crossbeam-channel 0.5` (BOUNDED) · `dashmap 6` · `parking_lot 0.12` · `arc-swap 1` · `bytes 1` · `simd-json 0.13`/`serde_json 1` (non-typed paths) · `ulid 1` · `tracing 0.1`+`tracing-subscriber 0.3` · `anyhow 1`.
7. **Python deps:** `pydantic>=2.7` + `pydantic-core` (validation/serialization seam) · `pydantic-settings>=2` (config) · `typing-extensions` · `uvloop` (GIL-build only, conditional) · runtime needs **no** starlette/anyio/h11/httptools/uvicorn (the size/cold-start win).
8. **Packaging:** `maturin` + `abi3-py311` (1 wheel/platform: macOS-universal2 + manylinux_2_28 x86_64/aarch64 + musllinux + win-x64) **and** `cp313t`/`cp314t` free-threaded wheels; `crate-type=["cdylib","rlib"]`; `lto=fat,codegen-units=1,panic=abort,opt-level=3,strip=true`; auto-generated `.pyi`; version = `env!("CARGO_PKG_VERSION")`.
9. **Repo:** one cargo workspace `lancelot-core` (the `.so`, module name `_lancelot`) + the Python package `lancelot/` + `conformance/` (dogfood harness) + `bench/` + `templates/` (init scaffolds) + `tests/`.
10. **The one non-negotiable invariant (D01 §2.2):** no `Python::with_gil`/`attach` scope contains an `.await`; clippy-enforced workspace-wide (`#![deny(clippy::await_holding_lock)]`) + the `gil::without_gil(|| …)`/`gil::with_gil(|py| …)` wrappers take non-async closures so "await inside GIL" doesn't compile.
11. **Parallelization shape (§C):** ~46 units across 9 tracks. The dependency spine is **U0 FFI seam → U1 hyper server → U2 router → U3 FunctionInfo ABI → U4 lazy request → U5 GIL choreography**; everything else (validation, DI, OpenAPI, streaming, AI layer, ops, middleware, testclient, CLI, packaging) hangs off stable interfaces and parallelizes massively after M0.
12. **Free-threaded-clean from day one (D01 §3.3a):** every `#[pyclass]` is `frozen` + interior-synced; no `GILProtected`; routing tables frozen into `arc-swap`'d `Arc<ImmutableRouter>` after boot; `python3.14t` in CI from the first commit.

---

## §A. Repo & cargo-workspace layout

DESIGN. One git repo, gitignored at `Lancelot/` per house rule. The repo root is `lancelot/`
(the project), distinct from the Python import package `lancelot/` which lives under it.

```
lancelot/                              # repo root
├── Cargo.toml                         # workspace manifest
├── pyproject.toml                     # maturin build-backend, deps, wheel matrix
├── rust-toolchain.toml                # pinned toolchain (clippy/rustfmt versioned)
├── .cargo/config.toml                 # target features, abi3 env
├── crates/
│   └── lancelot-core/                 # THE compiled extension (module name _lancelot)
│       ├── Cargo.toml
│       ├── build.rs                   # abi3 / Py_MOD_GIL slot / cargo-auditable
│       └── src/                       # → §B.1 Rust module tree
├── lancelot/                          # THE Python import package → §B.2
│   ├── __init__.py
│   ├── _lancelot.pyi                  # AUTO-GENERATED from the real ABI (never hand-written)
│   └── ...                            # subpackages → §B.2
├── conformance/                       # the dogfood FastAPI-conformance harness (§F.2)
│   ├── corpus/                        #   FastAPI reference apps + recorded golden responses
│   ├── runner.py                      #   runs corpus on FastAPI and on Lancelot, diffs
│   └── report.py                      #   emits the % gate number
├── bench/                             # benchmark-vs-FastAPI + cold-start + streaming microbench
│   ├── coldstart.py  vs_fastapi.py  stream_1k.py  const_tier.py
├── templates/                         # what `lancelot init` scaffolds (§G)
│   ├── docker/{Dockerfile.distroless,Dockerfile.alpine,Dockerfile.wolfi,.dockerignore,docker-compose.yml,Dockerfile.lambda}
│   ├── k8s/{deployment.yaml,pdb.yaml,hpa.yaml,scaledobject.yaml}
│   └── app/{main.py,settings.py,.env.example}
├── tests/                             # pytest: unit + integration + the conformance gate
├── examples/                          # chat-sse, rag, mcp-server, agent, migrate-from-fastapi
├── docs/specs/                        # the approved spec (post-review; out of this file's scope)
└── .github/workflows/{ci.yml,wheels.yml,bench.yml}
```

`Cargo.toml` (workspace):

```toml
[workspace]
members = ["crates/lancelot-core"]
resolver = "2"

[workspace.package]
edition = "2021"
rust-version = "1.79"            # ≥ pyo3 0.27 MSRV; hyper keeps MSRV ≥6mo old (D01 §1.1)

[profile.release]
codegen-units = 1
lto           = "fat"
panic         = "abort"          # ONLY with the supervisor + zero-unwrap deny (D01 §5.2)
strip         = true
opt-level     = 3

[profile.dev]
opt-level = 1                    # the .so is slow at opt-level=0; dev iteration needs ≥1
```

`crates/lancelot-core/Cargo.toml` (the pinned stack — D01 §7.1, verbatim + the additions the WBS needs):

```toml
[package]
name = "lancelot-core"
version = "0.1.0"
edition.workspace = true

[lib]
name = "_lancelot"               # Python: from lancelot._lancelot import Server, FnInfo, …
crate-type = ["cdylib", "rlib"]  # cdylib = the wheel; rlib = unit-testable in cargo

[dependencies]
pyo3                = { version = "0.27", features = ["extension-module", "py-clone"] }
pyo3-async-runtimes = { version = "0.27", features = ["tokio-runtime"] }
tokio               = { version = "1",   features = ["full"] }
hyper               = { version = "1",   features = ["http1", "http2", "server"] }
hyper-util          = { version = "0.1", features = ["server", "server-auto", "tokio"] }
http-body-util      = "0.1"
tower               = { version = "0.5", features = ["util"] }
tower-http          = { version = "0.6", features = ["timeout","limit","trace","cors","compression-gzip","fs"] }
tokio-tungstenite   = "0.24"
socket2             = { version = "0.5", features = ["all"] }
matchit             = "0.8"
crossbeam-channel   = "0.5"      # BOUNDED sync-pool queue (Robyn used unbounded = the bug)
dashmap             = "6"
parking_lot         = "0.12"     # replaces removed GILProtected (0.28-clean)
arc-swap            = "1"        # RCU swap of the frozen router/const-map (ft-safe, D01 §3.3a)
once_cell           = "1"
bytes               = "1"        # zero-copy body buffers (D01 §2.3)
simd-json           = "0.13"     # untyped/OpenAPI/MCP JSON paths (typed path = pydantic-core)
serde               = { version = "1", features = ["derive"] }
serde_json          = "1"        # fallback / config parse
ulid                = "1"        # X-Request-ID generation (D02 §4.2c)
httpdate            = "1"        # static files: ETag/Last-Modified/Range (D03 v0 #13)
mime_guess          = "2"        # static files content-type
tracing             = "0.1"
tracing-subscriber  = { version = "0.3", features = ["json","env-filter"] }
anyhow              = "1"
thiserror           = "1"

[build-dependencies]
pyo3-build-config = "0.27"
```

`pyproject.toml` (maturin + dual-build, D01 §2.4 / §7.1, D02 §1):

```toml
[build-system]
requires      = ["maturin>=1.7,<2"]
build-backend = "maturin"

[project]
name = "lancelot"
requires-python = ">=3.11"                 # D01 §3 / SYNTHESIS steal #14: contextvars(context=)
dependencies = [
  "pydantic>=2.7",                         # the validation/serialization seam (pydantic-core)
  "pydantic-settings>=2",                  # config (D02 §5)
  "typing-extensions>=4.10",
]
[project.optional-dependencies]
fast = ["uvloop>=0.20; platform_system != 'Windows' and python_version < '3.14'"]  # GIL-build loop
dev  = ["pytest>=8", "pytest-asyncio>=0.24", "httpx>=0.27", "fastapi", "uvicorn"]  # conformance/bench

[project.scripts]
lancelot = "lancelot.cli:main"

[tool.maturin]
module-name      = "lancelot._lancelot"
features         = ["pyo3/extension-module"]
python-source    = "."
strip            = true
# Dual-build is driven by CI (wheels.yml): the abi3 matrix passes
#   --features abi3  (maps to pyo3/abi3-py311)  → 1 wheel/platform
# the free-threaded matrix builds against python3.13t/3.14t with NO abi3 feature
[tool.maturin.target.x86_64-unknown-linux-musl]    # explicit musllinux (D02 §1.2 — Alpine reflex)
```

`crates/lancelot-core/build.rs` (DESIGN — the abi3/ft branch + the version-source + auditable):

```rust
fn main() {
    pyo3_build_config::use_pyo3_cfgs();
    // abi3 only on the GIL build; pyo3 ignores abi3 under free-threaded anyway (D01 §2.4)
    if std::env::var("CARGO_FEATURE_ABI3").is_ok() { println!("cargo:rustc-cfg=lancelot_abi3"); }
    // single version source: env!("CARGO_PKG_VERSION") is used in lib.rs get_version()
    println!("cargo:rerun-if-changed=build.rs");
}
```

`Cargo.toml` feature wiring (the abi3 toggle CI flips):

```toml
[features]
default = []
abi3    = ["pyo3/abi3-py311"]   # CI abi3 matrix passes --features abi3; ft matrix does not
```

---

## §B. Module trees (the build surface — every file an agent owns)

### §B.1 Rust crate tree — `crates/lancelot-core/src/`

DESIGN, derived from D01 §4.3 request lifecycle + the Robyn `FunctionInfo`/`matchit`/`SocketHeld`
shapes (`research/robyn/01-03`) + D04 streaming/AI substrate. Each module is an independently
ownable unit with a stated interface; the dependency edges are in §C.

```
src/
├── lib.rs                  # #[pymodule] _lancelot — registers every #[pyclass] + fns.
│                           #   exports: Server, FnInfo, LazyRequest, Response,
│                           #   SSEResponseRs, ChunkedResponseRs, WsConn, awaitables,
│                           #   get_version(); declares gil_used=false post-audit.
├── gil.rs                  # THE invariant wrapper. without_gil(F:FnOnce->R) -> R  (allow_threads
│                           #   on 0.27 / detach on 0.28); with_gil(F:FnOnce(Python)->R).
│                           #   F: non-async by type → "await inside GIL" cannot compile (D01 §2.2).
├── runtime/
│   ├── mod.rs              # tokio multi-thread builder: worker_threads=clamp(cgroup,2,4),
│   │                       #   max_blocking_threads=512 (fs/DNS ONLY) (D01 §4.2).
│   ├── task_locals.rs      # OnceCell<TaskLocals> (process-global GIL build /
│   │                       #   thread-local ft build); per-req clone_ref + scope_local (D01 §4.3).
│   └── cgroup.rs           # read /sys/fs/cgroup/cpu.max ("$MAX $PERIOD"|"max"|single),
│                           #   v1 cfs_quota/period; before fork (D02 §2.4) — NOT psutil.
├── supervisor/
│   ├── mod.rs              # parent process: fork N (SO_REUSEPORT) | spawn N threads (ft);
│   │                       #   poll children, respawn dead w/ backoff cap (D01 §5.2).
│   ├── signals.rs          # SIGTERM→drain, SIGQUIT→fast-drain, 2nd SIGTERM→stop,
│   │                       #   SIGINT (D02 §2.3 / §7.2). exec-form PID-1 contract.
│   └── shared_socket.rs    # SocketHeld: socket2 SO_REUSEPORT+SO_REUSEADDR+TCP_NODELAY,
│                           #   listen(16384), try_clone per fork (Robyn verbatim).
├── server/
│   ├── mod.rs              # hyper-util auto h1/h2 serve loop over the SocketHeld fd;
│   │                       #   tower::ServiceBuilder stack; hyper GracefulShutdown.
│   ├── service.rs          # the tower::Service<Request<Incoming>> — the hot path entry:
│   │                       #   parse→route→const?→lazyreq→dispatch (D01 §4.3).
│   ├── timeouts.rs         # header_read_timeout(30s default,0=off), body 60s, total 120s,
│   │                       #   per-route override (D01 §5.3).
│   ├── connlimit.rs        # per-worker max-conn cap + accept backpressure (D01 §5.3).
│   └── graceful.rs         # drain: stop accept, Connection:close, drain timer, SSE finish-
│                           #   current-event, run shutdown lifespan, then exit (D01 §5.1).
├── router/
│   ├── mod.rs              # per-HTTP-method matchit::Router<RouteId>; arc-swap'd
│   │                       #   Arc<ImmutableRouter> frozen after boot (D01 §3.3a).
│   ├── const_tier.rs       # exact-HashMap + trie of pre-baked CachedResponse; matched
│   │                       #   BEFORE any Python, zero GIL/alloc; serves /livez etc.
│   │                       #   (SYNTHESIS steal #5; fix Robyn trailing-slash bug).
│   └── params.rs           # extract path params (matchit captures) + raw query (lazy).
├── ffi/
│   ├── fn_info.rs          # #[pyclass(frozen)] FnInfo{handler:Py<PyAny>, is_async:bool,
│   │                       #   n_params:usize, params:Py<PyDict>, deps:Py<PyDict>,
│   │                       #   kind:HandlerKind}. The entire Py→Rust ABI (SYNTHESIS #10).
│   ├── convert.rs          # arg-tuple build for vectorcall (Rust tuple, NOT PyTuple);
│   │                       #   cast() not extract() on hot path (D01 §2.3).
│   ├── dispatch.rs         # async: with_gil{call→coro→into_future} DROP GIL .await
│   │                       #   with_gil{marshal}. sync: route to BlockingRunner.
│   │                       #   catch_unwind / zero-unwrap at this seam (D01 §5.2).
│   └── awaitables.rs       # PyEmptyAwaitable/PyDoneAwaitable/PyErrAwaitable/
│                           #   PyIterAwaitable/PyFutureAwaitable (Robyn's 5; poll model,
│                           #   no per-call Python Future alloc) (SYNTHESIS steal #15).
├── request/
│   ├── lazy.rs             # #[pyclass(frozen)] LazyRequest: holds raw hyper parts +
│   │                       #   unconsumed Bytes; headers/query/json/body materialize
│   │                       #   ON ATTRIBUTE ACCESS only (SYNTHESIS steal #4 — the
│   │                       #   ~1–3 alloc/req mechanism).
│   └── body.rs             # pooled BytesMut intake, pause-by-default streaming-body
│                           #   async-iter w/ backpressure (Blazeio model in Rust).
├── response/
│   ├── mod.rs              # #[pyclass(frozen)] Response (bytes/str/json fast downcast),
│   │                       #   header builder, status; cast not extract.
│   ├── json.rs             # typed path = pydantic-core dump_json bytes (1 pass);
│   │                       #   untyped/OpenAPI/error path = simd-json in Rust (NO
│   │                       #   jsonable_encoder ever — SYNTHESIS steal #6).
│   └── stream.rs           # SSEResponseRs / ChunkedResponseRs #[pyclass(frozen)]:
│                           #   producer↔writer bounded SPSC ring(size 1 default),
│                           #   timer-wheel keepalive (15s : ping), per-chunk cancel
│                           #   checkpoint, boolean send-gate (no Future/token),
│                           #   first-chunk-flush-immediately + 25ms/2KiB coalesce
│                           #   (D04 §1.3 / SYNTHESIS steal #8,#9).
├── codec/
│   ├── sse.rs              # raw SSE framing data:%b\n\n + event:/id:/retry:/:comment.
│   ├── openai.rs           # emit + parse chat.completion.chunk / [DONE] /
│   │                       #   stream_options.include_usage (D04 §1.2 KNOWN).
│   ├── anthropic.rs        # emit + parse named events; split usage (message_start
│   │                       #   input_tokens / final message_delta output_tokens) +
│   │                       #   ping passthrough (D04 §1.2 KNOWN).
│   ├── vercel_ai.rs        # x-vercel-ai-ui-message-stream:v1 typed parts (D04 §1.2).
│   └── jsonl.rs            # NDJSON/raw chunked for ChunkedResponse.
├── blocking/
│   └── runner.rs           # 3-variant (Empty/Mono/Pool) GIL-detaching BlockingRunner,
│                           #   crossbeam BOUNDED(10_000), py.detach() on recv,
│                           #   adaptive spawn, load-shed → 503 Retry-After BEFORE the
│                           #   Python frame; WIRED into HTTP path (fixes Robyn) (D01 §4.4).
├── background/
│   └── pool.rs             # detached Rust-owned worker pool: bounded queue, configurable
│                           #   concurrency; request returns immediately (D04 §3.5 —
│                           #   fixes FastAPI BackgroundTasks bug).
├── ws/
│   └── mod.rs              # tokio-tungstenite over hyper upgrade (NO actor system);
│                           #   bounded mpsc, raw-bytes + text path, matchit :id WS
│                           #   routing (D01 §6 — fixes Robyn WS defects).
├── asgi/
│   ├── inbound.rs          # mount_asgi: Lancelot Request → ASGI scope/receive/send
│   │                       #   for a mounted subtree (migration IN) (D02 §6.2).
│   └── outbound.rs         # app.asgi(): expose a spec ASGI 3.0 callable + __rsgi__
│                           #   shim (migration OUT / tooling) (D02 §6.2).
├── client/
│   ├── pool.rs             # app.llm transport: per-(scheme,host,port) pool, stale-
│   │                       #   detect-before-handout, least-in-flight, idle-reap<provider
│   │                       #   timeout, HTTP/2 default, verify=True default, DNS cache,
│   │                       #   TCP_NODELAY, max_conns~32 (D04 §4.1-4.2).
│   ├── policy.rs           # POST-never-retried invariant; per-(provider,model) circuit
│   │                       #   breaker w/ half-open; per-provider semaphore→503;
│   │                       #   Retry-After/x-ratelimit parse (D04 §4.3).
│   └── relay.rs            # zero-copy upstream→downstream splice (same codec=byte
│                           #   splice; cross codec=normalize+reframe) (D04 §4.4).
├── mcp/
│   └── server.rs           # Streamable HTTP (2025-06-18/2025-11) POST/GET/DELETE on
│                           #   one endpoint, Mcp-Session-Id, MCP-Protocol-Version
│                           #   negotiation, Last-Event-ID resumability (reuses ring),
│                           #   Origin validation, JSON-RPC dispatcher (D04 §2).
├── observ/
│   ├── metrics.rs          # Prometheus /metrics: http_* RED + lancelot_inflight /
│   │                       #   blocking_pool_* / workers; route-template labels;
│   │                       #   const-tier-adjacent cheap scrape (D02 §4.2a).
│   ├── trace.rs            # OTLP http/protobuf default; SERVER span created in Rust at
│   │                       #   request entry (covers the zero-GIL parse) (D02 §4.2b).
│   ├── logs.rs             # JSON-to-stdout, trace_id/span_id/request_id inject,
│   │                       #   X-Request-ID accept|ULID (D02 §4.2c).
│   └── genai.rs            # gen_ai.* CLIENT spans + token/cost/TTFT histograms on
│                           #   the client; content-capture OFF default (D04 §5).
├── config/
│   └── boot.rs             # validate operational config in Rust BEFORE fork; bind
│                           #   $PORT|LANCELOT_PORT; fail-fast non-zero exit, NEVER
│                           #   input()/stdin (D02 §5).
└── errors.rs               # PyErr↔HTTP mapping; the central try/except/finally funnel
                            #   shape (steal Blazeio's finally-guaranteed cleanup,
                            #   REJECT success-via-raise) (D04 §1.9).
```

### §B.2 Python package tree — `lancelot/`

DESIGN, mapping D03 §6 v0 table-stakes + D04 §7 AI surface onto the registration-time-compile
discipline (SYNTHESIS steal #1: all reflection at decoration, execute-only at request).

```
lancelot/
├── __init__.py             # public API: App, Depends, Security, Query, Path, Body,
│                           #   Header, Cookie, status, exports SSEResponse,
│                           #   AISSEResponse, ChunkedResponse, JSONResponse,
│                           #   Response, WebSocket, BackgroundTasks, TestClient.
├── _lancelot.pyi           # AUTO-GENERATED (CI fails if drifted) — never hand-edit.
├── app.py                  # App: route decorators, include_router, lifespan,
│                           #   middleware registration; builds the Rust Server +
│                           #   feeds FnInfo per route at start() (Robyn __init__ shape).
├── routing.py              # APIRouter, Route, include_router flatten-at-registration
│                           #   (FastAPI semantics), path-template parse, WS routes.
├── params.py               # Query/Path/Body/Header/Cookie/Form markers + the
│                           #   Default() sentinel (FastAPI byte-compat, SYNTHESIS #17).
├── di/
│   ├── compiler.py         # TWO-PHASE: reflect signature → immutable Dependant DAG →
│   │                       #   flat topological plan + dense interned-int cache slots
│   │                       #   (the moat made cheap, D01 §4.4 / SYNTHESIS steal #1).
│   ├── solve.py            # per-request execute-only of the compiled plan; crosses
│   │                       #   into Python only for the N user callables.
│   └── scopes.py           # app/router/route layering (Litestar superset, D03 §3.1).
├── validation.py           # per-field TypeAdapter compiled once → pydantic-core;
│                           #   single-pass validate_json(bytes) (NOT double-parse);
│                           #   the EXACT 422 wire-shape {"detail":[{type,loc,msg,
│                           #   input,ctx}]} (SYNTHESIS steal #7,#17).
├── responses.py            # Python typed wrappers over the Rust response classes;
│                           #   media-type defaults; response_model handling.
├── streaming.py            # @app.stream decorator; SSEResponse/AISSEResponse/
│                           #   ChunkedResponse Python facades; codec selection;
│                           #   mid-stream DI lifetime via the request-scoped exit
│                           #   stack (PEP-789 correct, D04 §1.3).
├── middleware.py           # two-tier: native/Rust(tower) chain + ASGI-compat escape
│                           #   hatch; NEVER BaseHTTPMiddleware/call_next (D03 §4.2).
├── security.py             # OAuth2PasswordBearer/APIKey/HTTPBearer/Security(scopes)
│                           #   surface (v0.x but stub surface in v0 for migration).
├── openapi.py              # OpenAPI 3.1 from the SAME type registry as validation/
│                           #   MCP (one representation; $ref dedup; in/out split);
│                           #   Swagger + Scalar + ReDoc HTML, served from const-tier.
├── testclient.py           # httpx-compatible TestClient(app) — in-process, drives the
│                           #   Rust core without a socket (SYNTHESIS table-stakes #11).
├── settings.py             # pydantic-settings integration; Settings DI provider,
│                           #   resolved ONCE at registration (D02 §5).
├── cli.py                  # `lancelot run|init|mcp-stdio`; in-process hot-reload
│                           #   (dev-only, hard-disabled non-TTY/production) (D02 §5).
├── llm/
│   ├── client.py           # LLMClient Python facade over crates client/*; chat/stream/
│   │                       #   open/relay/embed/embed_batch; Provider/Breaker config;
│   │                       #   structured(output_schema=) passthrough (D04 §4.6).
│   └── prices.py           # the pluggable price table → gen_ai.cost.usd (D04 §4.5).
├── mcp.py                  # @app.mcp.tool/.resource/.prompt registry (Robyn mcp.py
│                           #   shape, 5 gaps closed); lazy @property mount; shared
│                           #   tool registry with @app.agent (D04 §2.5).
├── agent.py                # @app.agent: AgentContext, ctx.run loop, AgentStep types
│                           #   (ModelDelta|ToolCall|ToolResult|Reasoning|Final),
│                           #   session=True SessionStore, durable=True checkpoint
│                           #   (D04 §3).
├── rag.py                  # @app.rag, @app.embeddings, @app.limit, app.gather,
│                           #   @app.embedding_cache; VectorStore ABC (D04 §6).
├── background.py           # background.spawn() Python facade over crates background.
├── observability.py        # @app.observe(capture_content,redact), @app.eval(sample);
│                           #   wires OTEL_* env; redaction hook (D04 §5.5).
└── _compat/
    ├── asgi.py             # mount_asgi() / app.asgi() Python glue over crates/asgi.
    └── lifespan.py         # merged async-CM chain; PEP-789 yield-teardown ordering.
```


---

## §C. The parallel-agent Work-Breakdown Structure

DESIGN. The build is decomposed into **46 units (U0–U45)** across **9 tracks**. Each unit has:
**files** (which §B paths it owns — no two concurrent units write the same file), an
**interface** (the contract other units depend on — frozen first so dependents can mock it),
**depends-on** (the edges), **milestone**, and **done-when** (a unit-local test gate).

The execution rule for the agent fleet: **freeze every interface in the WBS spine (U0–U5)
in a single first wave (interfaces only — empty trait/class stubs that compile), then dispatch
all downstream units in parallel against those frozen stubs.** A unit is "blocked" only by its
listed `depends-on`; everything else runs concurrently.

### §C.1 Track + unit index

```
Track 0  Bootstrap & FFI seam .......... U0  U1
Track 1  Server / runtime / supervisor . U2  U3  U4  U5  U6  U7
Track 2  Router & const-tier ........... U8  U9
Track 3  Request / response / codec .... U10 U11 U12 U13 U14
Track 4  DI / validation / OpenAPI ..... U15 U16 U17 U18 U19
Track 5  Middleware / security / ASGI .. U20 U21 U22 U23
Track 6  AI tier ....................... U24 U25 U26 U27 U28 U29 U30 U31
Track 7  Ops / cloud-native ............ U32 U33 U34 U35 U36 U37
Track 8  Python API surface / DX ....... U38 U39 U40 U41 U42
Track 9  Conformance / bench / CI / pkg  U43 U44 U45
```

### §C.2 The dependency-ordered unit list (the actual execution plan)

Ordering below is a valid topological sort: an agent can start a unit the moment its
`depends-on` units have **published their interface** (not necessarily finished). Units with
the same indent depth and no shared edge are fully parallel.

---

**U0 — Workspace + FFI-seam skeleton + the GIL wrapper.** *(spine root; blocks everything)*
- Files: `Cargo.toml`(workspace+crate), `pyproject.toml`, `build.rs`, `src/lib.rs` (empty `#[pymodule]`), `src/gil.rs`, `src/errors.rs`, `rust-toolchain.toml`, `.cargo/config.toml`, `lancelot/__init__.py` (import shim).
- Interface (FROZEN FIRST): `gil::without_gil<R>(f: impl FnOnce()->R) -> R`; `gil::with_gil<R>(f: impl FnOnce(Python)->R) -> R` (both take **non-async** closures by type — the compile-time invariant, D01 §2.2); `errors::to_http(PyErr)->(StatusCode,Bytes)`; `lib.rs` registers names; `#![deny(clippy::await_holding_lock, clippy::unwrap_used, clippy::expect_used, clippy::panic)]` on request-path mods.
- Depends-on: —
- Milestone: M0. Done-when: `maturin develop` builds; `import lancelot` works; `cargo clippy` clean; a `gil::with_gil` containing `.await` **fails to compile** (negative test).

**U1 — pyo3 conventions + auto-`.pyi` generator + ft-clean lints.** *(spine)*
- Files: `src/ffi/awaitables.rs`, a `xtask`/script `gen_pyi.py`, CI hook `tools/check_pyi.sh`.
- Interface: the 5 awaitables (`PyEmptyAwaitable…PyFutureAwaitable`, poll model, SYNTHESIS #15); a `#[pyclass(frozen)]`+interior-sync lint checklist (D01 §3.3a); `gen_pyi` reads the compiled module → emits `lancelot/_lancelot.pyi`; CI fails on drift.
- Depends-on: U0
- Milestone: M0. Done-when: awaitables unit-test green (no per-call Python Future alloc, measured); `.pyi` regenerates deterministically; `python3.14t` import smoke passes.

**U2 — Shared socket + tokio runtime builder + cgroup reader.** *(spine)*
- Files: `src/supervisor/shared_socket.rs`, `src/runtime/mod.rs`, `src/runtime/cgroup.rs`.
- Interface: `SocketHeld::bind(host,port)->SocketHeld` (SO_REUSEPORT+SO_REUSEADDR+TCP_NODELAY, listen(16384)); `SocketHeld::try_clone()`; `runtime::build(cfg)->tokio::Runtime` (worker_threads=clamp(cgroup,2,4), max_blocking_threads=512); `cgroup::cpus()->u32` (parses `cpu.max` "$MAX $PERIOD"|"max"|single, v1 fallback, D02 §2.4).
- Depends-on: U0
- Milestone: M0. Done-when: two processes bind the same port via SO_REUSEPORT; cgroup parser unit-tested against all three `cpu.max` forms incl. the VictoriaMetrics bug case.

**U3 — `FnInfo` ABI + arg-convert + dispatch (the GIL choreography).** *(spine; the perf core)*
- Files: `src/ffi/fn_info.rs`, `src/ffi/convert.rs`, `src/ffi/dispatch.rs`.
- Interface: `#[pyclass(frozen)] FnInfo{handler,is_async,n_params,params,deps,kind}` (Clone via py-clone); `dispatch::call_async(&FnInfo, args)` = `with_gil{call→coro→pyo3-async-runtimes into_future}` → **DROP GIL** → `.await` → `with_gil{marshal}`; `dispatch::call_sync` routes to `blocking::Runner`; `convert::build_args` = Rust-tuple vectorcall, `cast` not `extract` (D01 §2.3). `catch_unwind`/zero-unwrap at this seam (D01 §5.2).
- Depends-on: U0, U2
- Milestone: M0. Done-when: a fixed async + sync handler invoked from Rust returns correctly; GIL-hold profiled at single-digit µs ×3/req; `await_holding_lock` clean.

**U4 — LazyRequest + pooled body.** *(spine; the ~1–3 alloc mechanism)*
- Files: `src/request/lazy.rs`, `src/request/body.rs`.
- Interface: `#[pyclass(frozen)] LazyRequest` (raw hyper parts + unconsumed `Bytes`; `.headers/.query/.json()/.body()/.text()` materialize on access only); `request::from_hyper(parts,body)->LazyRequest`; streaming-body async-iter w/ pause-by-default backpressure.
- Depends-on: U0
- Milestone: M0. Done-when: handler reading 1 header alloc-profiles ≤ 3 framework allocs; body never copied unless `.json()/.text()` called.

**U5 — Response + typed JSON path + downcast fast-path.** *(spine)*
- Files: `src/response/mod.rs`, `src/response/json.rs`.
- Interface: `#[pyclass(frozen)] Response`; `response::serialize(py, obj, model)->Bytes`: typed → pydantic-core `dump_json` (1 pass); untyped/error/OpenAPI → simd-json in Rust; **never** `jsonable_encoder` (SYNTHESIS #6). `bytes/str/dict/Response` fast downcast via `cast`.
- Depends-on: U0
- Milestone: M0. Done-when: dict/Pydantic-model/str/bytes returns all serialize correctly; the 422 error body is byte-exact (cross-checked vs FastAPI fixture).

**U6 — hyper service + serve loop + tower stack.** *(spine; closes M0)*
- Files: `src/server/mod.rs`, `src/server/service.rs`.
- Interface: `server::serve(socket, router, rt)`; the `tower::Service` hot path: parse → `router.match` → const-tier short-circuit → `LazyRequest` → `dispatch` → `response::serialize` → write; hyper-util auto h1/h2.
- Depends-on: U0, U2, U3, U4, U5, U8
- Milestone: M0. Done-when: **the M0 vertical slice (§E) is green.**

**U7 — Supervisor + signals + graceful drain + config boot.** *(M3 spine)*
- Files: `src/supervisor/mod.rs`, `src/supervisor/signals.rs`, `src/server/graceful.rs`, `src/server/timeouts.rs`, `src/server/connlimit.rs`, `src/config/boot.rs`.
- Interface: `supervisor::run(cfg)` = validate config in Rust before fork (fail-fast non-zero, NEVER stdin) → fork N (cgroup count) | spawn N threads (ft) → poll+respawn (backoff cap) → SIGTERM: flip /readyz 503, stop accept, drain (`graceful_timeout`~25s), SSE finish-current, run shutdown lifespan, exit 0; SIGQUIT fast-drain; 2nd SIGTERM stop. header/body/total timeouts; per-worker conn cap.
- Depends-on: U2, U6
- Milestone: M3. Done-when: the 12-point stability checklist (D01 §5.7) is all green in an integration test (kill a worker → respawn; SIGTERM mid-request → request completes then exit; bad port → exit≠0 not hang).

---

**U8 — Router (matchit per-method, frozen `Arc<ImmutableRouter>`).** *(spine; M0)*
- Files: `src/router/mod.rs`, `src/router/params.rs`.
- Interface: `Router::add(method, template, RouteId)` (build-time only); `Router::freeze()->Arc<ImmutableRouter>` (arc-swap'd, ft-safe D01 §3.3a); `ImmutableRouter::match(method,path)->Option<(RouteId, PathParams)>`; lazy raw query split.
- Depends-on: U0
- Milestone: M0. Done-when: `/users/{id}` matches with param capture; 404 path is single-pass (no second scan — the FastAPI AVOID); hot-reload swaps the whole `Arc`.

**U9 — Const/static-response tier.** *(M2; the `/livez`-under-load substrate)*
- Files: `src/router/const_tier.rs`.
- Interface: `ConstTier::register(method, path, baked: Response)`; matched **before any Python**, zero GIL/alloc; trailing-slash normalized (fix Robyn bug); composes with pure-Rust middleware. Powers `/livez /readyz-static-part /openapi.json /docs` + user `@app.get(const=True)`.
- Depends-on: U8
- Milestone: M2. Done-when: `/livez` returns 200 with the Python pool fully saturated (the decisive D02 §2.1 test); ~10–50× latency vs a Python handler measured.

---

**U10 — SSE/Chunked Rust streaming pump (SPSC ring + keepalive + cancel).** *(M2; the headline)*
- Files: `src/response/stream.rs`, `src/codec/sse.rs`, `src/codec/jsonl.rs`.
- Interface: `#[pyclass(frozen)] SSEResponseRs/ChunkedResponseRs`; producer↔writer bounded SPSC ring (size-1 default, tunable); tokio timer-wheel keepalive (15s `: ping\n\n`, wraps the ring receive **not** the generator — PEP-789); per-chunk cancel checkpoint; boolean send-gate (no Future/token); first-chunk-flush-immediately + 25ms/2KiB coalesce; sync generators rejected.
- Depends-on: U3, U4, U5, U6
- Milestone: M2. Done-when: a 1000-token async-gen stream profiles ≤ 3 framework allocs/stream + ~1 GIL/flush; slow-client test → bounded RAM regardless of completion length.

**U11 — Client-disconnect → cancel → abort-upstream wiring.** *(M2; the money differentiator)*
- Files: `src/response/stream.rs` (the disconnect watcher half — coordinated with U10 via the frozen ring interface), `src/server/service.rs` (FIN/RST signal hook — coordinated with U6).
- Interface: Rust watches the downstream socket out-of-band; on FIN/RST → cancel the Python producer task → its `finally` unwinds the request-scoped exit stack → closes the in-flight `app.llm` upstream conn (abort). Default ON; `@app.stream(detach_on_disconnect=True)` opt-out.
- Depends-on: U10, U6, U28
- Milestone: M2. Done-when: integration test: close client mid-stream → upstream mock observes connection abort within one scheduler tick; token billing meter stops.

**U12 — OpenAI/Anthropic/Vercel-AI codecs (emit + parse).** *(M2)*
- Files: `src/codec/openai.rs`, `src/codec/anthropic.rs`, `src/codec/vercel_ai.rs`.
- Interface: `Codec::frame(normalized_delta)->Bytes` and `Codec::parse(bytes)->normalized_delta`; one normalized internal delta (`{text}`/ToolCallDelta/Usage), N wire formats; Anthropic split-usage handled; OpenAI `[DONE]`/`include_usage`; Vercel `v1` typed parts (D04 §1.2 KNOWN).
- Depends-on: U10
- Milestone: M2. Done-when: golden-fixture round-trip (record real OpenAI/Anthropic streams → emit byte-identical; parse → normalized matches).

**U13 — WebSockets (tokio-tungstenite, no actor system).** *(M1)*
- Files: `src/ws/mod.rs`.
- Interface: WS over hyper upgrade; bounded mpsc; raw-bytes **and** text path; matchit `:id` WS routing; DI but no response model (cheaper per-message).
- Depends-on: U6, U8, U3
- Milestone: M1. Done-when: echo WS test (text + binary) passes; `/ws/conversations/{id}` param routing works; bounded channel back-pressures a fast client (no OOM).

**U14 — Static files (correct: ETag/Last-Modified/Range/304/sendfile).** *(M1)*
- Files: a `src/static_files.rs` (own module — not in §B.1 list above; **add it**), wired via `tower-http::fs`.
- Interface: `StaticFiles::mount(prefix, dir)`; conditional requests, byte-range (no off-by-one), `sendfile(2)` via tower-http, path-traversal guard.
- Depends-on: U6, U8
- Milestone: M1. Done-when: Starlette-class static conformance fixtures pass (Range, 304, ETag); no Blazeio off-by-one.

---

**U15 — DI two-phase compiler.** *(M1; the moat)*
- Files: `lancelot/di/compiler.py`, `lancelot/di/solve.py`, `lancelot/di/scopes.py`.
- Interface: `compile_dependant(fn)->Plan` (reflect signature → immutable DAG → flat topological plan + dense interned-int cache slots, registration-time only); `solve(plan, request_scope)->kwargs` (execute-only; into Python only for the N user callables); yield-deps + exit-stack teardown ordering (PEP-789); `dependency_overrides`; app/router/route scoping.
- Depends-on: U3 (FnInfo), U16 (validation interface)
- Milestone: M1. Done-when: 3-level nested `Depends()` resolves; yield-dep teardown runs after response (and after stream end — checked in M2); override swaps in tests; no per-request signature introspection.

**U16 — Validation (pydantic-core seam, exact 422).** *(M1; spine for U15/U17)*
- Files: `lancelot/validation.py`, `lancelot/params.py`.
- Interface: per-field `TypeAdapter` compiled once → `validate_json(bytes)` **single-pass** (not double-parse); primitive-scalar params parsed in Rust, model params via pydantic-core; the **exact** 422 wire-shape `{"detail":[{type,loc,msg,input,ctx}]}`; `Query/Path/Body/Header/Cookie` markers + `Default()` sentinel (FastAPI byte-compat).
- Depends-on: U5 (Response/json interface), U3
- Milestone: M1. Done-when: 422 body byte-identical to FastAPI across the conformance error corpus; single-pass confirmed (no `json.loads` then `validate_python`).

**U17 — OpenAPI 3.1 + Swagger/Scalar/ReDoc.** *(M1)*
- Files: `lancelot/openapi.py`.
- Interface: build OpenAPI 3.1 once at boot from the **same** type registry as U16/U30 ($ref dedup, in/out split); serve `/openapi.json` + `/docs`(Swagger) + `/scalar` + `/redoc` HTML from the **const-tier** (U9).
- Depends-on: U16, U9
- Milestone: M1. Done-when: generated schema validates against OpenAPI 3.1 meta-schema; an existing FastAPI app's SDK-gen consumes it unchanged (conformance check).

**U18 — Routing/app surface (`App`, `APIRouter`, `include_router`, lifespan).** *(M1; spine for Python side)*
- Files: `lancelot/app.py`, `lancelot/routing.py`, `lancelot/_compat/lifespan.py`.
- Interface: `App()`, decorators `@app.get/post/...`, `@app.websocket`, `include_router(r, prefix=)` flatten-at-registration (FastAPI semantics), `@app.on_startup/@app.on_shutdown` + lifespan as merged async-CM chain (PEP-789 yield ordering); `App.start()` builds Rust `Server`, feeds one `FnInfo` per compiled route.
- Depends-on: U3, U8, U15, U16
- Milestone: M1. Done-when: a multi-router app boots; lifespan startup/shutdown runs in order; routes reach the Rust router.

**U19 — Settings (pydantic-settings DI provider).** *(M1)*
- Files: `lancelot/settings.py`.
- Interface: `Settings` base (pydantic-settings), a DI provider resolved **once at registration** (cached singleton); reads existing user `Settings`; `$PORT`/`LANCELOT_*` env contract documented.
- Depends-on: U15
- Milestone: M1. Done-when: a user's existing pydantic-settings class injects via `Depends(get_settings)`; missing required env → boot crash, not runtime 500.

---

**U20 — Two-tier middleware (Rust/tower chain + ASGI-compat escape hatch).** *(M1)*
- Files: `lancelot/middleware.py`, `src/server/service.rs` (the tower-layer wiring half — coordinated with U6 via frozen Service interface).
- Interface: native middleware = tower `Layer`s compiled into the Rust chain (CORS, auth-header, rate-limit, logging) — const-tier-compatible; ASGI-compat escape hatch runs existing Starlette/FastAPI middleware unmodified at the **app-callable boundary only**; **never** `BaseHTTPMiddleware`/`call_next`.
- Depends-on: U6, U18
- Milestone: M1. Done-when: a CORS preflight passes via the native chain; an unmodified ASGI middleware (e.g. Sentry-shaped) runs via the escape hatch.

**U21 — CORS (native, built-in).** *(M1)*
- Files: part of `lancelot/middleware.py` + `tower-http::cors` config.
- Interface: `app.add_cors(...)` → tower-http CORS layer; preflight handled in Rust/const-tier.
- Depends-on: U20
- Milestone: M1. Done-when: browser CORS conformance fixtures pass.

**U22 — ASGI compat: `mount_asgi()` + `app.asgi()` + `__rsgi__`.** *(M3 — the go-to-market)*
- Files: `src/asgi/inbound.rs`, `src/asgi/outbound.rs`, `lancelot/_compat/asgi.py`.
- Interface: `app.mount_asgi(prefix, asgi_app)` (mounted subtree pays the ASGI tax; native routes don't); `app.asgi()->ASGI3 callable` (HTTP+WS scope; lifespan/edge gaps documented); `__rsgi__` shim.
- Depends-on: U6, U18
- Milestone: M3. Done-when: an **unmodified** sample FastAPI app runs under `mount_asgi("/", fastapi_app)`; `uvicorn lancelot_app:asgi` serves Lancelot routes (lock-in-killer test).

**U23 — Security surface (OAuth2/APIKey/HTTPBearer/Security scopes).** *(v0 stub surface, M1; full M4)*
- Files: `lancelot/security.py`.
- Interface: `OAuth2PasswordBearer`, `APIKeyHeader`, `HTTPBearer`, `Security(scopes=)` — FastAPI-surface-compatible so migrants' imports resolve; auto OpenAPI security schemes (via U17).
- Depends-on: U15, U17
- Milestone: M1 (import-compatible surface) → M4 (full flows). Done-when: `from lancelot.security import OAuth2PasswordBearer` works and produces correct OpenAPI security scheme.

---

**U24 — `@app.stream` Python facade + mid-stream DI lifetime.** *(M2)*
- Files: `lancelot/streaming.py`, `lancelot/responses.py`.
- Interface: `@app.stream(buffer=1,keepalive=15.0,coalesce=("25ms","2KiB"),detach_on_disconnect=False,eager_deps=True,codec=None)`; `SSEResponse/AISSEResponse/ChunkedResponse` Python facades over U10/U12; the producer CM entered on the **request-scoped AsyncExitStack** (U15) so yield-deps stay open through the stream, close after (PEP-789).
- Depends-on: U10, U12, U15
- Milestone: M2. Done-when: `async def` + `yield` streams SSE; a yield-dep DB session is open during the stream and closed after the last token (the PEP-789 correctness test no other framework but FastAPI passes).

**U25 — `app.llm` transport pool.** *(M2)*
- Files: `src/client/pool.rs`, `lancelot/llm/client.py` (the pool-facade half).
- Interface: per-(scheme,host,port) pool; stale-detect-before-handout; least-in-flight; idle-reap < provider timeout; HTTP/2 default; verify=True default; DNS cache; TCP_NODELAY; `max_conns` per-provider (~32).
- Depends-on: U0, U2
- Milestone: M2. Done-when: 100 concurrent calls reuse a bounded pool; a server-idle-closed socket is detected and replaced before hand-out.

**U26 — `app.llm` policy: breaker + semaphore + POST-never-retried.** *(M2)*
- Files: `src/client/policy.rs`.
- Interface: POST-never-retried invariant (connection-reset-after-write is NOT retried); per-(provider,model) circuit breaker with half-open; per-provider concurrency semaphore → bounded wait → `503 Retry-After`; `Retry-After`/`x-ratelimit-*` parse.
- Depends-on: U25
- Milestone: M2. Done-when: a POST is never replayed (fault-injection test); breaker opens after K failures and half-opens after cooldown; semaphore sheds to 503 not unbounded queue.

**U27 — `app.llm.relay()` zero-copy proxy.** *(M2)*
- Files: `src/client/relay.rs`.
- Interface: `relay(upstream)->AISSEResponse`: same-codec = byte splice (1 copy total); cross-codec = normalize+reframe in Rust, still streaming/bounded; inherits U11 (disconnect→abort).
- Depends-on: U25, U10, U12, U11
- Milestone: M2. Done-when: relaying an OpenAI upstream to client is byte-identical with one copy; Anthropic→OpenAI re-frame streams correctly.

**U28 — Outbound abort hook (the seam U11 closes against).** *(M2)*
- Files: a thin `src/client/abort.rs` (own file) exposing `InFlight::abort()` consumed by U11.
- Interface: `InFlight` handle on every `app.llm` request; `.abort()` closes+resets the pooled connection (not returned half-read). This is the unit U11 wires the disconnect into.
- Depends-on: U25
- Milestone: M2. Done-when: `.abort()` on an in-flight stream closes the upstream socket and the pool slot is reset.

**U29 — MCP server (Streamable HTTP, 5 gaps closed).** *(M2)*
- Files: `src/mcp/server.rs`, `lancelot/mcp.py`.
- Interface: one Rust endpoint POST/GET/DELETE; `Mcp-Session-Id` (crypto UUID, TTL store); `MCP-Protocol-Version` negotiation (400 on unsupported); `Last-Event-ID` resumability **reusing the U10 ring + event-id cursor**; `Origin` validation; JSON-RPC dispatcher (`initialize/resources|tools|prompts`); `@app.mcp.tool/.resource/.prompt` lazy `@property` mount; Pydantic args → rich JSON-Schema **via the U16/U17 registry** (no drift); `lancelot mcp-stdio`.
- Depends-on: U10, U16, U17, U18
- Milestone: M2. Done-when: an MCP client (Claude-Desktop-shaped) lists+calls a tool over Streamable HTTP; session+resumability work; stdio mode works; all 5 Robyn gaps closed (negotiated version, rich schema, auth, structured output, streaming).

**U30 — `@app.agent` loop + AgentStep + session/durable.** *(M2 core loop; durable M4)*
- Files: `lancelot/agent.py`.
- Interface: `@app.agent(codec=,session=,durable=)`; `AgentContext.run(model,tools,messages)` = framework-owned LLM↔tool loop; `AgentStep = ModelDelta|ToolCall|ToolResult|Reasoning|Final`; **shared tool registry with U29** (register once, callable by remote MCP + in-process loop); termination (max_steps=10, budget, disconnect, ctx.stop); tool-failure→ToolResult(error)→fed back (not HTTP 500); `session=True`→pluggable `SessionStore` (Redis/SQLite) keyed by `gen_ai.conversation.id`; `durable=True`→step checkpoint+replay (resume cursor = SSE `id:`).
- Depends-on: U24, U25, U29
- Milestone: M2 (loop+session) / M4 (durable). Done-when: an agent runs a 2-tool loop streaming typed steps; disconnect cancels the loop + aborts upstream; durable mode resumes from checkpoint without re-billing completed steps.

**U31 — RAG/embeddings/concurrency primitives.** *(M4)*
- Files: `lancelot/rag.py`.
- Interface: `@app.rag`, `@app.embeddings` (OpenAI `/v1/embeddings`-compatible), `@app.limit(key,max)` (declarative semaphore, shares U26 admission), `app.gather(*coros,concurrency=)` (capped structured fan-out on the GIL-free await path), `@app.embedding_cache` (content-hash keyed, model-id in key); `VectorStore` ABC (pluggable).
- Depends-on: U25, U24, U30
- Milestone: M4. Done-when: streaming-RAG example (retrieving→sources→tokens) works end-to-end with retrieval+embeddings spans; `embed_batch` bounded fan-out preserves order.

---

**U32 — Blocking pool (bounded, GIL-detaching, wired into HTTP).** *(M1 — fixes the FastAPI cliff)*
- Files: `src/blocking/runner.rs`.
- Interface: 3-variant (Empty/Mono/Pool) runner; crossbeam **BOUNDED(10_000)**; `py.detach()` on `recv`; adaptive single-flight spawn; idle-timeout death; on full → `503 Retry-After` emitted by Rust **before** the Python frame; classified async/sync at registration (`FnInfo.is_async`) and routed here for sync — **actually wired into the HTTP path** (fixes Robyn's sync-bug).
- Depends-on: U3, U6
- Milestone: M1. Done-when: 50 concurrent sync handlers don't pin reactor threads; the 41st-equivalent overload returns 503+Retry-After (not a silent hang); queue depth in `/metrics`.

**U33 — Background task pool.** *(M1)*
- Files: `src/background/pool.rs`, `lancelot/background.py`.
- Interface: `background.spawn(fn,*a,**kw)` → detached Rust-owned pool (separate from request workers), bounded queue, configurable concurrency; request returns immediately; `await_in_request=True` opt-in for old semantics.
- Depends-on: U3, U6
- Milestone: M1. Done-when: a 5s background job does **not** delay the response (fixes the FastAPI connection-blocking bug); queue depth/in-flight/rejected in `/metrics`.

**U34 — Probes (`/livez /readyz /startupz`).** *(M3)*
- Files: a `src/observ/probes.rs` (own file) + `lancelot/observability.py` (the `@app.readiness_check` registration half).
- Interface: `/livez` = const-tier (U9), zero-GIL — passes even with the pool saturated; `/readyz` = registered async checks on a **reserved concurrency lane**, required-vs-optional dependency model, flips 503 on SIGTERM before draining; `/startupz` = 200 only after lifespan complete; paths configurable; `/livez` doubles as Docker HEALTHCHECK + LWA readiness.
- Depends-on: U9, U7, U18
- Milestone: M3. Done-when: `/livez` 200 under saturated pool; `/readyz` 503 when a required dep down (200 when only an optional dep down); `/startupz` 503 until startup done.

**U35 — Metrics + tracing + JSON logs.** *(M3)*
- Files: `src/observ/metrics.rs`, `src/observ/trace.rs`, `src/observ/logs.rs`.
- Interface: `/metrics` Prometheus (http_* RED + `lancelot_inflight`/`blocking_pool_*`/`workers`, route-template labels, AI-extended buckets to 30/60s), cheap scrape; OTLP `http/protobuf` default, SERVER span created in Rust at request entry (covers zero-GIL parse), W3C traceparent; JSON logs to stdout with trace_id/span_id/request_id, X-Request-ID accept|ULID; default-on/off policy table (D02 §4.4).
- Depends-on: U6, U7
- Milestone: M3. Done-when: `/metrics` scrapes with correct route-template labels; a request produces one SERVER span covering parse→handler→serialize; logs are JSON with correlation IDs; no-op when no OTLP endpoint.

**U36 — GenAI semantic-convention spans/metrics on `app.llm`.** *(M3 — the observability differentiator)*
- Files: `src/observ/genai.rs`, wired into `lancelot/llm/client.py`.
- Interface: every `app.llm` call → `{operation} {model}` CLIENT span (provider/model/params/finish_reasons/usage), `gen_ai.client.operation.duration` + `token.usage`(split input/output) + streaming `time_to_first_token`/`time_per_output_chunk` histograms (exact bucket boundaries from D04 §5.3); derived `gen_ai.cost.usd`; span stays open across the whole stream, closes on terminal chunk; content-capture OFF default + redaction hook; `OTEL_SEMCONV_STABILITY_OPT_IN` gate; convention version pinned in a constant.
- Depends-on: U35, U25, U30
- Milestone: M3. Done-when: a chat call emits a spec-compliant `chat gpt-…` span with correct token attrs from the terminal SSE chunk; cost metric appears; no prompt content captured unless opted in.

**U37 — `lancelot init` Docker + K8s scaffolds + cold-start.** *(M3)*
- Files: `lancelot/cli.py` (the `init` subcommand), all of `templates/`.
- Interface: `lancelot init --docker --k8s --lambda` writes the distroless/alpine/wolfi Dockerfiles, `.dockerignore` (incl `target/`,`.git/`), `docker-compose.yml`, K8s Deployment(3 probes+preStop sleep5+grace40+maxSurge1/maxUnavail0+securityContext+resources), PDB, HPA v2, KEDA ScaledObject, Lambda Dockerfile (LWA layer, `/livez` readiness), `.env.example`, `app/main.py`.
- Depends-on: U34 (probe paths must exist), U7
- Milestone: M3. Done-when: scaffolded distroless image builds < 60 MB, runs non-root, HEALTHCHECK green; cold-start bench (process spawn → first 200 on `/livez`) < 50 ms.

---

**U38 — Public API `__init__` + Python facades.** *(M1; the import surface)*
- Files: `lancelot/__init__.py` (full), `lancelot/responses.py` (full).
- Interface: every public name a FastAPI migrant imports (`App`/`Depends`/`Security`/`Query`/`Path`/`Body`/`Header`/`Cookie`/`status`/`Response`/`JSONResponse`/`SSEResponse`/`AISSEResponse`/`ChunkedResponse`/`WebSocket`/`BackgroundTasks`/`TestClient`/`HTTPException`) resolves with FastAPI-compatible signatures.
- Depends-on: U18, U16, U24, U33
- Milestone: M1. Done-when: a FastAPI app's `import` block runs unchanged against `lancelot` (alias `from lancelot import FastAPI`? — DESIGN: ship `lancelot.App` + a `FastAPI = App` compat alias module `lancelot.compat`).

**U39 — `TestClient`.** *(M1; gate-enabler — conformance needs it)*
- Files: `lancelot/testclient.py`.
- Interface: `TestClient(app)` httpx-compatible, **in-process** (drives the Rust core without a real socket — a loopback `tower::Service` call), supports the conformance harness; WS test support.
- Depends-on: U6, U18
- Milestone: M1. Done-when: `TestClient(app).get("/")` works; an existing FastAPI test-suite shape (`client.post(json=)`, `client.websocket_connect`) runs against Lancelot.

**U40 — CLI `run` + hot-reload.** *(M3)*
- Files: `lancelot/cli.py` (the `run` subcommand + reload).
- Interface: `lancelot run app.main:app --host --port`; bind `$PORT`; in-process hot-reload **hard-disabled** when non-TTY or `LANCELOT_ENV=production`; `lancelot mcp-stdio`.
- Depends-on: U7, U18, U29
- Milestone: M3. Done-when: `lancelot run` serves; reload works in dev TTY; reload refuses to start in production env (the prod-reload-outage guard).

**U41 — Examples.** *(M4 — proof + docs)*
- Files: `examples/{chat-sse,rag,mcp-server,agent,migrate-from-fastapi}`.
- Interface: runnable example per differentiator; `migrate-from-fastapi` shows `mount_asgi` then route-by-route port.
- Depends-on: U24, U29, U30, U31, U22
- Milestone: M4. Done-when: every example runs via `lancelot run`.

**U42 — Plugin protocols (v0.x surface stubs).** *(M4)*
- Files: a `lancelot/plugins.py`.
- Interface: `InitPlugin.on_app_init(AppConfig)`, `SerializationPlugin`/DTO, `DIPlugin`, `OpenAPISchemaPlugin`, two-tier `MiddlewarePlugin`, `LifecyclePlugin` — all **registration-time-only** (contribute to the compiled plan, never per-request Python on the hot path).
- Depends-on: U15, U16, U17, U20, U18
- Milestone: M4. Done-when: a sample `InitPlugin` adds a route + DI provider that compile into the Rust plan like first-party code.

---

**U43 — Conformance harness (the dogfood gate).** *(parallel from M0; the % gate)*
- Files: `conformance/corpus/*`, `conformance/runner.py`, `conformance/report.py`.
- Interface: a corpus of small FastAPI apps + recorded golden responses; `runner.py` runs each on FastAPI (reference) and on Lancelot (`TestClient`), diffs status/headers/body/`/openapi.json`/422-shape; `report.py` emits `pass% = matched / total`. **This % is the M1/M4 gate number.**
- Depends-on: U39 (needs TestClient)
- Milestone: M1 (≥90%) → M4 (≥99%). Done-when: harness runs in CI and prints the %; the gate is enforced (CI fails below threshold).

**U44 — Benchmarks (vs-FastAPI, cold-start, stream-1k, const-tier).** *(M4)*
- Files: `bench/*`.
- Interface: `vs_fastapi.py` (RPS + p50/p95/p99 on JSON echo + DB-paginated), `coldstart.py` (spawn→first 200, vs FastAPI+uvicorn), `stream_1k.py` (1000-token stream alloc/GIL profile + slow-client RAM bound), `const_tier.py` (const vs Python handler latency).
- Depends-on: U6, U9, U10, U37
- Milestone: M4. Done-when: all four produce numbers; cold-start < 50 ms vs FastAPI's 100–200 ms is reproduced and published.

**U45 — CI + wheel matrix + release.** *(parallel from M0; M4 gate)*
- Files: `.github/workflows/{ci.yml,wheels.yml,bench.yml}`, `tools/check_pyi.sh`.
- Interface: `ci.yml` = clippy(+the deny set) + cargo test + pytest + the U43 conformance gate + `python3.14t` smoke + `.pyi`-drift check; `wheels.yml` = maturin matrix → abi3-py311 ×(macOS-universal2, manylinux_2_28 x86_64/aarch64, musllinux, win-x64) **and** cp313t/cp314t ×same; `cargo auditable` for the SBOM; `bench.yml` nightly.
- Depends-on: U0 (and consumes U43 as a job)
- Milestone: M0 (CI skeleton) → M4 (full matrix green). Done-when: every wheel in the matrix builds and `pip install`s into the matching interpreter; the conformance gate blocks merge below threshold.

### §C.3 The dependency graph (edges, condensed)

```
U0 ─┬─ U1
    ├─ U2 ─┬─ U3 ──┬─ U6 ───────────────────────────────── (M0 closes here)
    │      │       ├─ U13  U32  U33
    │      └─ U25 ─┼─ U26  U28
    │              └─ U36
    ├─ U4 ─ U6
    ├─ U5 ─ U6
    └─ U8 ─┬─ U6
           └─ U9 ─ U17, U34
U6 ─┬─ U7 ─┬─ U22  U34  U35  U40
    ├─ U10 ─┬─ U11(+U6,U28)  U12  U24  U27(+U12,U11)
    ├─ U14  U20 ─ U21
    └─ U39 ─ U43 ─ U45
U16 ─┬─ U15 ─┬─ U18 ─┬─ U38  U19  U23(+U17)  U42
     │       │       └─ U30(+U24,U25,U29)
     └─ U17 ─ U29(+U10,U18) ─ U31
U25 ─ U36 ;  U30 ─ U31 ;  U34 ─ U37 ;  {U24,U29,U30,U31,U22} ─ U41 ;  {U6,U9,U10,U37} ─ U44
```

Spine (must be sequential-ish, everything waits on these interfaces): **U0 → U2 → U3 → U4/U5/U8 → U6** (M0). After U6's `tower::Service` interface is frozen, **Tracks 3–9 fan out fully in parallel** — that is where the "hours via massively parallel agents" leverage is: ~38 of 46 units are concurrently buildable post-M0 against frozen interfaces.


---

## §D. Milestone ordering & done-criteria (measurable gates)

Each milestone's gate is a **single command that prints PASS/FAIL** — no subjective "looks done".

### M0 — Vertical slice (the proof the architecture is real)
- **Units:** U0 U1 U2 U3 U4 U5 U6 U8 (+ U43/U45 skeletons running but not gating).
- **Gate command:** `make m0` runs: (a) `cargo clippy --workspace -- -D warnings` (incl. the deny set + `await_holding_lock`); (b) the negative compile test (a `with_gil{ .await }` fixture **must fail to compile**); (c) boots the §E app on one process, `curl -s :8000/echo -d '{"x":1}'` returns `{"x":1}` 200; (d) the alloc profiler on that request reports **≤ 5 framework allocations** (target 1–3; 5 is the M0 ceiling); (e) GIL-hold trace shows the GIL released across the handler `.await`.
- **Why this gate:** it proves the *entire thesis* in one slice — Rust accept, off-GIL lifecycle, lazy request, pydantic-core serialize, GIL-drop-across-await. Per DESIGN-DECISIONS fork #7: a pure-Python v0 proves none of these; the thin-but-real Rust core from day one is mandatory, so M0 *is* the first deliverable.

### M1 — Table-stakes (FastAPI migration MVP)
- **Units:** U13 U14 U15 U16 U17 U18 U19 U20 U21 U23(surface) U32 U33 U38 U39 + M0.
- **Gate command:** `make m1` → the **conformance harness ≥ 90 %** (U43), AND: 3-level `Depends()` resolves; 422 body byte-identical to FastAPI across the error corpus; `/openapi.json` validates vs OpenAPI 3.1 meta-schema and an existing FastAPI SDK-gen consumes it; 50 concurrent sync handlers → no reactor stall + overload returns `503 Retry-After`; `background.spawn` does not delay the response; WS echo (text+binary) + static-files Range/304 fixtures pass.
- **Done = "a FastAPI app migrates over a weekend and the tests pass"** (D03 §6 sequencing rule).

### M2 — AI tier (the thesis-proving differentiators)
- **Units:** U9 U10 U11 U12 U24 U25 U26 U27 U28 U29 U30(loop+session).
- **Gate command:** `make m2` → ALL of: const-tier `/livez` returns 200 with the Python pool fully saturated (the decisive D02 §2.1 test); a 1000-token stream profiles ≤ 3 framework allocs/stream and bounded RAM under a slow client; **client-disconnect mid-stream aborts the upstream LLM mock within one scheduler tick and stops the token meter** (the money differentiator — the single most important M2 assertion); `app.llm` POST is never replayed under fault injection; an MCP client lists+calls a tool over Streamable HTTP with session+resumability; an `@app.agent` 2-tool loop streams typed steps and a disconnect cancels the loop.
- **Done = the moat is demonstrable**, not described.

### M3 — Ops / cloud-native (production-deployable)
- **Units:** U7 U22 U34 U35 U36 U37 U40.
- **Gate command:** `make m3` → the **12-point stability checklist (D01 §5.7) all green** in an integration suite: graceful SIGTERM drain; worker respawn after kill; `panic=abort`+supervisor self-heal; header-receipt deadline rejects a slow-loris; bounded pool→503; end-to-end streaming backpressure; per-request contextvars isolation (tenant-bleed test); max-body-limit; cgroup-aware count (forks `floor(cpu.max)` not host cores); no `input()` on port conflict (exits ≠0, doesn't hang); no `BaseHTTPMiddleware`/sequential-BackgroundTasks/per-chunk-sync-hop; detach-across-await enforced; liveness≠readiness. PLUS: cold-start bench < 50 ms; scaffolded distroless image < 60 MB non-root HEALTHCHECK-green; an unmodified FastAPI app runs under `mount_asgi`; GenAI span is spec-compliant.
- **Done = it survives a rolling deploy and a bad input without dropping a request.**

### M4 — Conformance-green (ship)
- **Units:** U23(full) U30(durable) U31 U41 U42 U44 U45(full matrix).
- **Gate command:** `make m4` → **conformance harness ≥ 99 %**; the full wheel matrix (abi3-py311 + cp313t/cp314t × {macOS-universal2, manylinux_2_28 x86_64/aarch64, musllinux, win-x64}) all build and `pip install` into the matching interpreter; `bench/vs_fastapi.py` + `coldstart.py` produce published numbers; every `[R]` item in D02 §7 is checked; `python3.14t` full test suite green (free-threaded-clean proven).
- **Done = a startup `pip install lancelot`s, points its FastAPI app at it, and ships.**

### Conformance % progression (the gate's own ramp — DESIGN)
| Milestone | Conformance gate | What's measured |
|---|---|---|
| M0 | n/a (skeleton runs, not gating) | harness exists, runs, prints a number |
| M1 | **≥ 90 %** | routing, params, Depends, validation, 422, OpenAPI, middleware, lifespan, CORS, sync/async, TestClient, WS, static, settings, bg-tasks |
| M2 | ≥ 90 % held + AI assertions | + streaming/const correctness (AI tier doesn't regress table-stakes) |
| M3 | ≥ 95 % | + ops endpoints (`/livez|/readyz|/startupz|/metrics`), ASGI-mount parity |
| M4 | **≥ 99 %** | the long-tail FastAPI edge cases; the residual <1 % is documented intentional divergence |

The conformance % is the **objective truth** about "did we replicate FastAPI's wire behavior".
It is the single number the build is steered by — every milestone re-runs it; a regression
below the prior gate blocks merge (U45 enforces).

---

## §E. The M0 vertical-slice spec (the first thing the fleet builds)

DESIGN. M0 is one slice through every load-bearing layer, deliberately minimal, proving the
thesis. It is the integration target U0–U8 converge on.

### E.1 The slice (data flow, end to end)

```
TCP accept (socket2 SO_REUSEPORT fd, one process)                     U2
  → hyper-util auto h1/h2 serve_connection                            U6
  → tower::Service::call(Request<Incoming>)                           U6
  → ImmutableRouter.match("POST","/echo") → RouteId(0)                 U8
  → [const-tier checked first: miss]                                   U9 (stub: always miss in M0)
  → LazyRequest::from_hyper(parts, body)  (nothing materialized)       U4
  → dispatch::call_async(&FnInfo[0], (lazy_request,))                  U3
        with_gil { handler(lazy_request) → coroutine
                   pyo3_async_runtimes::tokio::into_future(coro) }     U3
        ── DROP GIL ── .await the future (asyncio loop steps coro) ──  gil.rs (U0)
        with_gil { ret = coro result;
                   bytes = response::serialize(py, ret, model=None) }  U5
  → write Response(200, application/json, bytes)                        U6
```

### E.2 The M0 application (the test fixture, `tests/m0_app.py`)

```python
from lancelot import App
app = App()

@app.post("/echo")
async def echo(req):                 # LazyRequest; .json() materializes body ON ACCESS
    body = await req.json()          # the ONLY place the body is parsed/copied
    return body                      # dict → pydantic-core/simd-json dump_json, 1 pass
```

### E.3 M0 acceptance (the exact `make m0` assertions — each maps to a unit)

1. `cargo clippy --workspace -- -D warnings` clean, **including** `await_holding_lock`, `unwrap_used`, `expect_used`, `panic` on request-path modules. *(U0)*
2. Negative compile test `tests/compile_fail/await_in_gil.rs` — `gil::with_gil(|py| async { x.await })` **must not compile** (the invariant is type-enforced, not convention). *(U0)*
3. `lancelot run tests.m0_app:app` binds `$PORT` (default 8000) on one process; logs `workers=1 (cgroup …)` JSON line. *(U2, U6, U7-min)*
4. `curl -s -XPOST :8000/echo -d '{"x":1,"y":"a"}'` → `200`, body `{"x":1,"y":"a"}`, `content-type: application/json`. *(U6, U4, U5, U8)*
5. Alloc profile (a `dhat`/`bytehound` Rust run + a Python `tracemalloc` delta) of one `/echo` request: **≤ 5 framework allocations** (target 1–3; the lazy proxy + zero-copy body + vectorcall args must hit this — SYNTHESIS line 136). *(U4, U3, U5)*
6. A `tracing` span dump shows the GIL acquired exactly ~3× (build-args, call, marshal) for single-digit µs each, and **released across the handler `.await`** (no `with_gil` span overlaps the await). *(U3, gil.rs)*
7. `import lancelot` under both `python3.13` and `python3.13t` (free-threaded) succeeds; `sys._is_gil_enabled()` asserted appropriately per build. *(U1)*

### E.4 What M0 deliberately omits (so it stays a slice, not a milestone)
No DI, no validation/422, no OpenAPI, no streaming, no middleware, no const-tier hit, no
supervisor/multiprocess, no AI layer, no TestClient. Those are M1+ and hang off M0's frozen
interfaces. M0 exists to **kill fork #7's risk**: it proves the thin-real-Rust-core path is
buildable and the alloc/GIL targets are real before the fleet scales out on it.

---

## §F. Test / CI / release pipeline

### §F.1 Unit & integration tests
- **Rust:** `cargo test` per crate — `gil.rs` (negative compile via `trybuild`), `router` (match/404-single-pass/freeze-swap), `cgroup` (3 `cpu.max` forms), `const_tier` (trailing-slash, under-saturation), `stream` (alloc/GIL profile, slow-client RAM bound), `client/policy` (POST-never-retried fault injection, breaker state machine), `codec` (golden round-trip).
- **Python:** `pytest` + `pytest-asyncio` — DI nesting/yield-teardown/overrides, 422 byte-shape, OpenAPI meta-schema, lifespan ordering, TestClient parity, WS, static-files conditional requests, `mount_asgi` runs an unmodified FastAPI app, `app.asgi()` runs under uvicorn.
- **Free-threaded:** the **entire** `pytest` suite also runs under `python3.14t` in CI from the first commit (D01 §3.2) — a regression there is a build break, not a follow-up.

### §F.2 The dogfood conformance harness (U43 — the gate)
- `conformance/corpus/` = N small FastAPI apps (each exercises one table-stakes feature: path/query/body params, nested Depends, response_model, 422 cases, include_router, CORS, lifespan, WS, static, SSE) + their **golden responses recorded by running them on real FastAPI**.
- `conformance/runner.py`: for each corpus app, run it on FastAPI (reference) and the same app code on Lancelot via `TestClient`; assert status, headers (normalized set), body bytes, `/openapi.json` (semantic-equal), 422 wire-shape (byte-equal).
- `conformance/report.py`: prints `CONFORMANCE: matched/total = NN.N%`. CI parses this; `make m1` requires ≥90, `make m4` ≥99; a drop below the **previous** milestone's floor fails the build (regression guard).
- This is the literal "Can I write the server code from this teardown?" test inverted: it proves Lancelot reproduces FastAPI's observable contract.

### §F.3 Benchmark vs FastAPI (U44)
- `bench/vs_fastapi.py`: `wrk`/`oha` against JSON-echo + a DB-paginated endpoint, Lancelot vs FastAPI+uvicorn — RPS + p50/p95/p99. (Honest framing: the win is alloc/cold-start/streaming, not raw RPS on a GIL-bound JSON echo — D01 §1.7a.)
- `bench/coldstart.py`: process spawn → first 200 on `/livez`, Lancelot vs FastAPI+uvicorn, on a cold container. **Target < 50 ms vs FastAPI's KNOWN 100–200 ms** (D02 §3.2) — the headline serverless number, published in the README.
- `bench/stream_1k.py`: 1000-token SSE — framework allocs/stream, GIL acquisitions/stream, RAM under a throttled client. Asserts ≤3 allocs + ~1 GIL/flush + bounded RAM.
- `bench/const_tier.py`: `/livez` const-tier vs an equivalent Python handler — asserts ~10–50× latency reduction.

### §F.4 Wheel build matrix (U45 `wheels.yml`)
```
abi3 matrix  (maturin --features abi3, abi3-py311):
  macOS-universal2 · manylinux_2_28-x86_64 · manylinux_2_28-aarch64 · musllinux_1_2-x86_64 · windows-x64
free-threaded matrix (no abi3 feature; built against python3.13t & python3.14t):
  same 5 platforms × {cp313t, cp314t}
+ maturin sdist (the only source fallback)
+ cargo auditable build  → CycloneDX Rust SBOM embedded in the .so (Trivy/Grype-readable)
+ gen_pyi → commit-check: CI fails if lancelot/_lancelot.pyi drifts from the built ABI
```
The musllinux wheel is a **hard requirement** (D02 §1.2 — Alpine is a startup reflex; a missing
musl wheel = silent Rust source-build = worst onboarding).

### §F.5 CI pipeline (`ci.yml`) — the per-PR gate, in order
1. `cargo fmt --check` + `cargo clippy --workspace -- -D warnings` (the deny set).
2. `trybuild` negative-compile tests (the await-in-GIL invariant).
3. `cargo test --workspace`.
4. `maturin develop` then `pytest` on `python3.13` **and** `python3.13t`.
5. `python -m conformance.runner && python -m conformance.report` → enforce the milestone %.
6. `.pyi`-drift check.
7. (nightly, `bench.yml`) the four benchmarks → publish numbers.

---

## §G. `lancelot init` scaffolding contents (U37 — the "provide, don't document" deliverable)

`lancelot init [--docker] [--k8s] [--lambda]` writes, verbatim from `templates/`, the
D02-specified production envelope so a startup with no platform team ships correctly by default:

- **`Dockerfile.distroless`** (default): two-stage, Debian-12-family build (glibc match), `uv sync --frozen`, runtime `gcr.io/distroless/python3-debian12:nonroot`, `USER 65532:65532`, exec-form ENTRYPOINT (`["python","-m","lancelot","run","app.main:app","--host","0.0.0.0","--port","8000"]`), `HEALTHCHECK` via bundled CPython against `/livez` (no curl/sh in distroless). Target < 60 MB.
- **`Dockerfile.alpine`** (the startup reflex): musllinux wheel path — exists *because* startups type `python:alpine`; without it they hit a Rust source build.
- **`Dockerfile.wolfi`** (enterprise/CVE-conscious): Chainguard base, rolling CVE patch.
- **`Dockerfile.lambda`**: adds the AWS Lambda Web Adapter layer (`COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter …`), `AWS_LWA_READINESS_CHECK_PATH=/livez` — **no code change** (LWA, not Mangum, D02 §3.3); the const-tier `/livez` does triple duty (K8s liveness + Docker HEALTHCHECK + LWA readiness).
- **`.dockerignore`**: includes `target/` (Lancelot-critical — a local Rust build is multi-GB), `.git/`, `.venv/`, `__pycache__/`, `tests/`, `*.md`, `.env*`.
- **`docker-compose.yml`**: app + a Redis (for `SessionStore`/durable-agent) + an OTLP collector, env-driven.
- **`k8s/deployment.yaml`**: 3 probes wired (startup `failureThreshold:30 periodSeconds:2`; liveness `/livez timeoutSeconds:2`; readiness `/readyz`), `preStop: sleep 5`, `terminationGracePeriodSeconds: 40` (> preStop + shutdown_timeout 25 + margin), `strategy maxSurge:1 maxUnavailable:0`, `securityContext` runAsNonRoot+`readOnlyRootFilesystem:true`+drop ALL caps, `resources.limits.cpu:"2"` with the §2.4 comment (Lancelot forks `floor(2)`=2, not host cores).
- **`k8s/pdb.yaml`**: `maxUnavailable: 1`.
- **`k8s/hpa.yaml`**: HPA v2 = CPU + the `lancelot_inflight_requests` custom metric.
- **`k8s/scaledobject.yaml`**: a KEDA `ScaledObject` (RPS + in-flight; scale-to-zero capable).
- **`app/main.py`** + **`app/settings.py`** + **`.env.example`**: a minimal working Lancelot app (a streaming chat route + a `/livez` already wired by the framework) and a pydantic-settings `Settings` with `.env.example` placeholders (`.env` gitignored).

The `lancelot init` output is itself a conformance artifact: `make m3` builds the scaffolded
distroless image and asserts < 60 MB, non-root, HEALTHCHECK-green, cold-start < 50 ms.

---

## §H. Parallel-agent dispatch protocol (how the fleet actually runs this)

DESIGN, applying the house Phase-1..4 batch workflow to a *build* (not an RE pass):

1. **Wave 0 (1 agent, serial — the spine interfaces):** U0 publishes `gil.rs`/`errors.rs`/workspace skeleton + **frozen empty interfaces for U2,U3,U4,U5,U6,U8** (trait/`#[pyclass]` stubs that compile and panic-todo). This is the only serial bottleneck; ≈30–60 min of work. Nothing else can start until the seam compiles.
2. **Wave 1 (≈6 agents, parallel — close M0):** U1,U2,U3,U4,U5,U8 against U0's frozen stubs; U6 integrates them. Gate: `make m0`.
3. **Wave 2 (≈12 agents, parallel — M1):** U13,U14,U15,U16,U17,U18,U19,U20,U21,U32,U33,U38,U39 + U43 corpus. All depend only on M0's frozen `tower::Service`/`FnInfo`/`LazyRequest`/`Response` interfaces. Gate: conformance ≥90 %.
4. **Wave 3 (≈11 agents, parallel — M2):** U9,U10,U11,U12,U24,U25,U26,U27,U28,U29,U30. Gate: the disconnect→abort + const-under-saturation + alloc/RAM assertions.
5. **Wave 4 (≈7 agents, parallel — M3):** U7,U22,U34,U35,U36,U37,U40. Gate: the 12-point checklist + cold-start + image size.
6. **Wave 5 (≈6 agents, parallel — M4):** U23-full,U30-durable,U31,U41,U42,U44,U45-full. Gate: conformance ≥99 % + wheel matrix.

**Collision rule (enforced):** no two concurrently-dispatched units write the same §B file.
Where a unit needs to touch a file another owns (U11/U20 touch `src/server/service.rs`,
U11/U24 touch `src/response/stream.rs`), the WBS routes the cross-cutting concern through a
**separate seam file** (`src/client/abort.rs` for U28, an explicit layer-registration hook in
`service.rs` owned by U6 that U11/U20 call) so the owning unit publishes the seam and the
consumer codes against it — never a shared edit.

**Verify-don't-trust (Phase 2):** every wave's gate is a `make` target that prints PASS/FAIL
from real assertions (alloc counts, byte-diffs, the conformance %); a self-reported "done" is
not accepted — the gate command is the truth. A failing unit is auto-relaunched (Phase 3) with
the specific failing assertion named, never a vague "go deeper".

The leverage: after Wave 1 (M0, ~6 agents, the only place the build is narrow), **38 of 46
units are concurrently buildable** against frozen interfaces — that is the structural property
that turns a framework this size into an hours-not-weeks build with a sufficiently large fleet.

*Generated 2026-05-19 from the Lancelot design corpus (`DESIGN-DECISIONS.md` + `SYNTHESIS.md` +
`research/design/01..04`, ~6,400 lines). Every crate/version/constant/path traces to those by
`§`. `Lancelot/` is gitignored.*
