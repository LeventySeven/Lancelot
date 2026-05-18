# Lancelot — Design Decisions (forks resolved by evidence)

> Consolidates the 9 source-level teardowns + `SYNTHESIS.md` + the 4 design-research dossiers
> (`research/design/01..04`, 2,883 lines, ~165 external sources). Every fork that was "open" in
> `SYNTHESIS §4.8` is now **decided with evidence**. This is the conclusions layer; the approved
> spec (post-review) lives in `docs/specs/`.
>
> Status legend: **DECIDED** (evidence-backed, locked) · **DEFERRED-WITH-CRITERIA** (decision rule written, re-evaluated on a trigger) · **OPEN** (needs the user).

---

## 1. The resolved forks

| # | Fork (was open) | **Decision** | Why (evidence) |
|---|---|---|---|
| 1 | Rust HTTP engine: hyper vs actix vs axum vs ntex | **DECIDED: `hyper 1.x` + `hyper-util` + `http-body-util` + `tower 0.5`/`tower-http` + `tokio` multi-thread + `tokio-tungstenite` + `socket2` + `matchit 0.8`.** actix = schedule-hedge spike only; axum = reference/2nd fallback. | `design/01 §1,§7`. Throughput is GIL-capped for a Python framework (hyper-vs-actix ≈ 10% lever on the ~10% of request time that is Rust) → decide on hyper's 3-yr semver stability + owned-runtime control + tower ecosystem, not benchmarks. Granian (nearest competitor) independently chose hyper+tokio. |
| 2 | Free-threaded (no-GIL) Python — nogil-first? | **DECIDED: multiprocess-first, free-threaded-*clean*, dual-build** (`abi3-py311` 1-wheel/platform + `cp313t`/`cp314t` free-threaded wheels). NOT nogil-first. | `design/01 §3`. nogil's 8× is CPU-bound only; Lancelot's headline workload is IO-bound (GIL already released across the await) → ~0 gain; +5–10% single-thread tax, +15–20% mem, ~50% of top deps lack ft wheels (Feb 2026), an unported C-ext silently re-enables GIL. But designing ft-clean now is ~free; retrofitting later is a full `#[pyclass]` rewrite. |
| 2b | When to flip default to free-threaded | **DEFERRED-WITH-CRITERIA**: flip only when *all 4* hold — (a) dep closure ft-safe, (b) <5% single-thread penalty, (c) PEP 803 `abi3t` shipped, (d) ft is the CPython default. Re-evaluate each CPython release. | `design/01 §3` |
| 3 | ASGI: core protocol or compat shim or none? | **DECIDED: dual-tier.** Native Rust `FunctionInfo`-style ABI = the *only* fast path. Plus `mount_asgi(prefix, asgi_app)` (run unmodified FastAPI/Starlette under Lancelot — migration *in*) and `app.asgi()` (expose Lancelot as ASGI 3.0 — deploy *out*, free Lambda/Modal). Middleware native/Rust only — never `BaseHTTPMiddleware`/`call_next`. | `design/02 §6`. ASGI-as-core re-imposes the exact per-request Python tax the Rust core exists to delete; ASGI-as-onramp is the entire go-to-market (nobody migrates off FastAPI without it). Proven Granian/RSGI model. |
| 4 | How far does DI/validation move into Rust? | **DECIDED for v0: two-phase compiler is *ours* (Rust-assisted plan), validation *delegated to pydantic-core via FFI*.** Native Rust validator = post-v1 research track (`SYNTHESIS` steal-tier, kept open). | `SYNTHESIS §2 #6-7`, `design/03 §2`. Table-stakes demands byte-exact FastAPI 422 + Pydantic v2 + `Depends()`; pydantic-core is already Rust and ecosystem-canonical. Single-pass `validate_json(bytes)` (no double-parse) is the free win. |
| 5 | Const/static-response tier — v0 or v2? | **DECIDED: v0.** It doubles as the substrate for `/livez` (a saturated Python pool must still pass liveness) — one mechanism, three platforms (K8s probe + Docker HEALTHCHECK + AWS LWA). | `design/02 §2,§7`, `SYNTHESIS §1 steal #5` |
| 6 | v0 feature scope | **DECIDED: full table-stakes set + 2 differentiators** (AI streaming tier + const tier). Anything less = not adoptable; the 2 differentiators prove the thesis. | `design/03 §2,§6` |
| 7 | **v0 build path: Python-first vs Rust-core-from-day-1** | **OPEN — needs the user.** Evidence strongly favors a *thin-but-real Rust core from day 1* (see §4) because every differentiator (sub-50ms cold start, const-tier `/livez`, dual-tier ASGI, the disconnect→abort AI moat) structurally requires the Rust core to exist — a pure-Python v0 proves none of them. Trade-off is speed-to-first-working-thing. | `design/01`, `design/02`, `design/04` all converge here |

---

## 2. Positioning (locked)

> **Lancelot is the only Python web framework where unchanged FastAPI-style code runs on a Rust core, *and* the inbound HTTP stream and the outbound LLM call are one backpressured, cancellation-correct, MCP-exposing system — near-zero migration cost, an AI hot path no pure-Python framework or bare Rust server can structurally match.**

Two architectural lines decide the market (`design/03 §5`): (1) Rust-core vs pure-Python, (2) framework vs bare-server. Only **Robyn** is on the right side of both — and it fumbled the two things that matter (no `Depends()`, catastrophic per-token streaming). That gap is Lancelot's lane.

The strategy is **FastAPI-wire-compatibility on a Rust core**, not "better framework design" — Litestar proves superior design alone loses to FastAPI's gravity/ecosystem/hiring pool (`design/03 §3.1`).

---

## 3. The non-negotiable v0 surface (table-stakes — no adoption without ALL)

From `design/03 §2.2`, byte-compatible with FastAPI where it touches the wire:

Typed path/query/body params · Pydantic v2 request+response validation · `Depends()` DI with yield-teardown + `dependency_overrides` · auto **OpenAPI 3.1** + Swagger/Scalar/ReDoc · `include_router`/route groups · two-tier middleware (Rust/ASGI-pure + ASGI-compat escape hatch) · lifespan startup/shutdown · CORS · sync+async handlers (sync bounded, **no 40-thread cliff**, `503 Retry-After`) · JSON + **exact FastAPI 422 wire-shape** · `TestClient` · WebSockets · correct static files (ETag/Range/304/sendfile) · pydantic-settings-compatible config · **real detached background tasks** (fixes FastAPI's connection-blocking bug).

**+ the 2 thesis-proving differentiators in v0:** the AI streaming tier (`AISSEResponse(codec="openai")`, Rust SSE codec, per-token zero-alloc backpressure, mid-stream DI lifetime, disconnect→cancel→upstream-abort) and the const/static-response tier.

**Ops surface that ships with v0** (`design/02 §7`): `/livez` `/readyz` `/startupz` (const-tier) · SIGTERM graceful drain in the Rust supervisor · cgroup-aware worker count · Prometheus `/metrics` + OTLP + JSON logs default-on · OTel GenAI spans on the built-in LLM client · `lancelot init` scaffolding Docker + K8s manifests · bind `$PORT` + zero-config boot + fail-fast config validation · musllinux abi3 wheel + <60MB distroless image · sub-50ms measured cold start.

---

## 4. Recommended v0 architecture (the synthesis of all 13 dossiers)

**Core stack:** `pyo3 0.27` floor / 0.28-clean · `hyper 1.x` + `tower` + `tokio` multi-thread (`worker_threads=clamp(cgroup_cpus,2,4)`) · `matchit 0.8` router · `tokio-tungstenite` WS · uvloop per worker · build `lto=fat, codegen-units=1, panic=abort, opt-level=3` + worker supervisor + FFI `catch_unwind`.

**Process/concurrency:** N OS processes via `SO_REUSEPORT`+`SO_REUSEADDR`+`TCP_NODELAY`, `listen(16384)`, cgroup-aware count; per process: tokio runtime for Rust I/O + bounded GIL-detaching `BlockingRunner` for sync handlers (load-sheds to 503); per-request `contextvars.Context`; graceful drain + respawn supervisor.

**Request lifecycle:** accept/TLS/parse in Rust (single-pass resumable parser, header-receipt deadline) → `matchit` route match → **const-tier short-circuit before any Python** → **lazy `Request` proxy** (nothing materialized until touched) → `with_gil` bind args from the compiled plan + call handler → **drop GIL across the entire `.await`** (the invariant; clippy-enforced) → `with_gil` pydantic-core `validate_json`/`dump_json` (one Rust pass) → Rust writer with boolean backpressure (no Future/token). Target ~1–3 framework allocs/req (Robyn ~30–66, FastAPI ~10–15).

**Ergonomics (the FastAPI moat made cheap):** two-phase DI — registration reflects signature → immutable DAG → flat topological plan + dense cache slots (interned-int keys); per-request executes the plan, crossing into Python only for the N user callables. Validators/serializers/OpenAPI compiled once from one type representation. Byte-preserve: 422 shape, `Annotated`/`Depends`/`Security`, yield-dep + exit-stack teardown (PEP-789 streaming fix — LLM client stays open through the stream, closes after).

**AI-native layer** (`design/04 §7`): `@app.stream`/`AISSEResponse(codec=)` · pooled `app.llm` (HTTP/2, `verify=True`, POST-never-retried, circuit-breaker, `.relay()` zero-copy proxy, auto token/cost/OTel, multi-provider failover) · `@app.mcp.tool/.resource/.prompt` (Streamable HTTP, 2025-06-18 spec) · `@app.agent` loop · `background.spawn()` · `output_schema=` structured passthrough · OTel GenAI conventions default-on. **The moat: framework ties client-disconnect → upstream-LLM-abort with end-to-end backpressure** — the developer writes `yield token` and gets "stop paying for tokens nobody reads" + "don't OOM on slow clients" for free.

**Packaging:** maturin dual-build — `abi3-py311` (1 wheel/platform) + `cp313t/cp314t` ft wheels; macOS-universal2 + manylinux_2_28 + musllinux + win-x64; `maturin sdist` fallback; auto-generated `.pyi`; single version source.

---

## 5. Anti-patterns locked out (each fixing a specific KNOWN defect)

O(N) regex routing · 40-thread sync cliff · `jsonable_encoder` on hot path · `BaseHTTPMiddleware`/`call_next` · sequential connection-blocking BackgroundTasks · double-validate-on-output (FastAPI) · eager total request marshalling · per-token GIL+spawn_blocking streaming · unbounded blocking queue · no graceful shutdown · `os.cpu_count()` ignoring cgroups · `input()` on port conflict · drifted hand-written `.pyi` (Robyn) · O(n·k) header re-scan / slow-loris · exception-as-primary-response · no `sendfile` / weak static · `ssl.CERT_NONE` client default (Blazeio). Full checklist = release gate (`design/01 §5.7`).

---

## 6. Open item for the user

Only **fork #7** (v0 build path) needs a human call. Everything else is evidence-locked. Recommendation and the two viable paths are presented for approval next.

*Compiled 2026-05-19. `Lancelot/` is gitignored.*
