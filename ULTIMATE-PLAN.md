# Lancelot — The Ultimate Plan

> Synthesis of the full 19,125-line research corpus (23 dossiers + SYNTHESIS + DESIGN-DECISIONS).
> This is the capstone: positioning, locked architecture, v0 scope, the parallel build, and how
> "instant + stable as FastAPI + faster" is *measured*, not claimed.
> Lives in its own repo `github.com/LeventySeven/Lancelot.git` (`researchfms/` reference dump gitignored). Status: **awaiting user approval to begin the build.**

---

## 1. What Lancelot is (locked positioning)

**The only Python web framework where unchanged FastAPI-style code runs on a Rust core, *and* the inbound HTTP stream and the outbound LLM call are one backpressured, cancellation-correct, MCP-exposing system.** Near-zero migration cost; an AI hot path no pure-Python framework or bare Rust server can structurally match.

**Universal mandate (locked):** Lancelot is positioned as a *complete* drop-in replacement for **both FastAPI and Robyn**, for **any** application type — as good as FastAPI for general web/API work, faster than Robyn and more web-optimized for exactly what FastAPI targets, so that **revolutionary startups and bigtech corporations alike can migrate any application off FastAPI/Robyn**. The Agentic/AI backend (agentic & multi-agent systems, LLM streaming) is the differentiated headline hot path — the standout use case, not the only one; general web universality and the AI hot path are co-equal v0 requirements.

Two architectural lines decide the market (`design/03 §5`): Rust-core vs pure-Python, and framework vs bare-server. Only **Robyn** sits on the right side of both — and it fumbled the two things that matter (no `Depends()`, catastrophic per-token streaming). That gap is Lancelot's lane. Strategy: **be FastAPI's wire surface byte-compatible, on a Rust core** — Litestar proved that superior design alone loses to FastAPI's gravity.

How "instant + stable + faster" is made real and *provable*:

- **Stable** = Lancelot dogfoots against **FastAPI's own 581-file / 74k-line test suite, unmodified**. FastAPI's `TestClient` is a pure in-process ASGI driver → runs against Lancelot via `app.asgi()` with a ~30-line `conftest` symbol swap, zero edits to any test file (`deep/08`). "Stable like FastAPI" becomes a ratcheting CI invariant, not a slogan.
- **Faster** = the entire non-handler request lifecycle is GIL-free Rust; GIL held µs ×3/req, dropped across every `.await`; ~1–3 framework allocs/req (vs Robyn ~30–66, FastAPI ~10–15); a const-tier serving zero-Python; sub-50ms cold start (vs FastAPI+uvicorn's 100–200ms). Proven by the Blazeio `tracemalloc` bench harness reused verbatim (`deep/06`).
- **Instant** = built in hours by a fleet of parallel coding agents against a 46-unit WBS with frozen interface seams (`deep/09`).

---

## 2. Architecture (every fork evidence-locked — see `DESIGN-DECISIONS.md`)

- **Core stack:** `pyo3 0.27` (0.28-clean) · `hyper 1.x` + `hyper-util` + `http-body-util` + `tower 0.5`/`tower-http` · `tokio` multi-thread · `tokio-tungstenite` · `socket2` · `matchit 0.8`. **Not actix** (throughput is GIL-capped; chose API stability + Granian-validated). Build `lto=fat, codegen-units=1, opt-level=3, panic="unwind"` + `catch_unwind` at the FFI seam + respawn supervisor.
- **No-GIL:** multiprocess-first, **free-threaded-*clean*** (every `#[pyclass]` thread-safe by construction now — retrofitting later is a full rewrite), **dual-build** wheels `abi3-py311` + `cp313t/cp314t`. Flip the default to free-threaded only on 4 measurable criteria.
- **Process model:** N OS processes via `SO_REUSEPORT`+`SO_REUSEADDR`+`TCP_NODELAY`, `listen(16384)`, **cgroup-aware count** (fixes Robyn's `os.cpu_count()` meltdown); per-process tokio runtime + a **bounded, load-shedding GIL-detaching blocking pool** for sync handlers (503 `Retry-After`); per-request `contextvars.Context`; graceful SIGTERM drain.
- **Request lifecycle:** Rust accept/TLS/parse (single-pass resumable, header-receipt deadline) → `matchit` match → **const-tier short-circuit before any Python** → **lazy `Request` proxy that IS the shared mutable request** (1 alloc; a naive per-phase rebuild breaks the middleware/contextvars contract — `deep/04`) → `with_gil`{bind args from compiled plan, call handler} → **drop GIL across the entire `.await`** (clippy + a negative-compile-test enforce the invariant) → `with_gil`{pydantic-core `validate_json`/`dump_json`, one Rust pass} → Rust writer, boolean backpressure, no Future/token.
- **Ergonomics (the FastAPI moat made cheap):** two-phase DI — registration reflects signature → immutable DAG → flat topological plan + dense interned-int cache slots; per-request executes the plan, into Python only for the N user callables. Validators/serializers/OpenAPI compiled once from one type representation. **Validation delegated to pydantic-core via FFI** (pin the version) — 422 + OpenAPI schemas are byte-from-pydantic; reimplementing them in Rust drifts on every pydantic bump.
- **AI-native layer:** `@app.stream`/`AISSEResponse(codec="openai"|"anthropic"|"vercel_ai")` · pooled `app.llm` (HTTP/2, `verify=True`, POST-never-retried, circuit-breaker, **`app.llm.relay()`** = Blazeio's 2-task relay topology + the cancellation-moat, ~40 lines on the pass-1 Session pool) · `@app.mcp.tool/.resource/.prompt` (Streamable HTTP, 2025-06-18 spec) · `@app.agent` loop · `background.spawn()` (detached — fixes FastAPI's connection-blocking bug) · `output_schema=` structured passthrough · OTel GenAI spans default-on. **The moat: client-disconnect → cancel response task → `upstream.abort()` → PEP-789 yield-dep teardown** — write `yield token`, get "stop paying for tokens nobody reads" + "don't OOM on slow clients" free.

---

## 3. v0 scope — the non-negotiable surface

**Table-stakes (no adoption without ALL, byte-compatible where it touches the wire — `deep/01-03`):** typed path/query/header/cookie/body/form/file params with FastAPI's exact coercion + the 5 nastiest edge cases (`str|None`-no-default is *required*; `validation_alias` precedence; 422 `input`/`ctx` shape varies by call site; body-embed rules; yield-dep teardown timing) · Pydantic v2 validation + **exact `{"detail":[{type,loc,msg,input,ctx}]}` 422 wire-shape** (233 test files assert it) · `Depends()`/`Security()`/sub-deps/yield-teardown/`dependency_overrides` · auto **OpenAPI 3.1** (byte-identical, via pydantic `GenerateJsonSchema` over FFI) + Swagger/ReDoc/Scalar · `include_router`/route groups · trailing-slash-redirect-as-second-scan (307, query preserved) · two-tier middleware (Rust/ASGI-pure + ASGI-compat escape hatch, never `BaseHTTPMiddleware`/`call_next`) · full `lifespan`+`app.state` (ordered, fail-fast, `{**child,**parent}` merge) · CORS · OAuth2/HTTPBasic/Bearer/APIKey (always 401 + `WWW-Authenticate`; scopes plumbed not enforced) · sync+async handlers (sync bounded, no 40-cliff) · `TestClient` (= `httpx.Response`, drives the real Rust pipeline via shared FFI `process_one`, + `app.asgi()` fallback) · WebSockets · static files (ETag/Range/304/sendfile) · pydantic-settings-compatible config · **real detached background tasks**.

**+ the 2 thesis-proving differentiators in v0:** the AI streaming tier (Rust SSE codec, per-token zero-alloc backpressure, mid-stream DI lifetime, disconnect→cancel→upstream-abort) and the const/static-response tier (doubles as `/livez`).

**Ops surface in v0 (`design/02`):** `/livez` `/readyz` `/startupz` (const-tier) · SIGTERM graceful drain · cgroup-aware workers · Prometheus `/metrics` + OTLP + JSON logs default-on · OTel GenAI spans on `app.llm` · `lancelot init` (copytree-from-wheel; distroless + K8s probes + lockfile templates) · bind `$PORT`, zero-config boot, fail-fast config · musllinux abi3 wheel + <60MB distroless image · sub-50ms measured cold start.

**Stability release-gate = 113 numbered guards (`deep/07`), merge-blocking.** The 5 bugs all three frameworks share that Lancelot fixes by construction: (1) no disconnect→upstream-abort; (2) no per-route/total/header/idle timeout coverage; (3) unbounded hot-path queues; (4) streaming memory/teardown not structurally bounded; (5) no crash/abort self-healing. A Rust-speed core *adds* failure surfaces (FFI-panic = process kill; GIL-across-await = interpreter freeze; `#[pyclass]` data race under free-threading) — the gate proves Lancelot pays that risk down *below* the pure-Python baseline, not just to parity.

---

## 4. The build — 46-unit WBS, parallel, milestone-gated (`deep/09`)

**Thin serial spine, then a wide parallel fan-out:**

```
Wave 0 (1 agent, ~30–60 min — the only narrow point: freezes interface seams)
  U0 workspace + gil.rs invariant wrapper → U2 SO_REUSEPORT socket + tokio + cgroup
  → U3 FnInfo ABI + GIL choreography → U4/U5/U8 LazyRequest / Response+pydantic-core / matchit router
  → U6 hyper tower::Service  ══════════ closes M0 (vertical slice) ══════════
Waves 1–5 (≈6/12/11/7/6 agents in parallel against frozen stubs; collision-free via dedicated seam files)
  DI compiler · validation/422 · OpenAPI · streaming pump · disconnect→abort · provider codecs
  · app.llm pool/policy/relay · MCP · agent loop · RAG · blocking pool · background pool
  · probes · metrics/trace/logs · GenAI spans · lancelot init · TestClient · ASGI compat
  · conformance harness · static files · wheel matrix · …
```

**Milestone gates (each a measurable merge gate):**

| M | Gate (objective, automated) |
|---|---|
| **M0** | Vertical slice: socket→hyper→tower→matchit→LazyRequest→GIL-choreo→pydantic `dump_json`→200. 7 assertions incl. **negative compile test** (`with_gil{ .await }` must fail to compile), ≤5 allocs/req, GIL released across `.await`, imports under py3.13 *and* py3.13t. Retires fork-#7 risk first. |
| **M1** | Table-stakes green: FastAPI conformance **v0-subset 100%** (~180–220 files) + **full ≥90%**; byte-exact 422; stability gates 1–6. |
| **M2** | AI tier: streaming + disconnect→abort + `app.llm.relay()` + MCP + agent loop; conformance ≥90% held; the moat proven by a token-billing-stops-on-disconnect test. |
| **M3** | Ops/cloud: probes, drain, OTel, `lancelot init`, dual-build wheels, <60MB image, <50ms cold start; conformance ≥95%. |
| **M4** | Release: full conformance ≥99% (every intentional divergence in `known_divergence.toml` as `xfail(strict=True)`), 113 stability guards green, benchmark vs FastAPI published. |

Regression guard: dropping below the prior milestone's conformance floor blocks merge (the ratchet). Conformance % is the single objective steering metric.

---

## 5. Execution model

Maps the proven house Phase-1..4 parallel-agent workflow onto a *build*: Wave-0 agent freezes the spine + interface stubs; subsequent waves dispatch parallel implementation agents each owning one WBS unit writing to non-overlapping files behind frozen seams; each unit ships with its tests; the conformance harness + stability gate run in CI as the objective truth. Same incremental-write discipline, same verify-don't-trust, same auto-relaunch-on-thin.

Build proceeds in the project repo (`github.com/LeventySeven/Lancelot.git`; build artifacts gitignored, no nested repo): repo-root `lancelot/` (Python pkg), `src/` (Rust crate), `tests/` + `conformance/` (the dogfood harness), `docs/`, `Cargo.toml`/`pyproject.toml` (maturin dual-build).

---

## 6. Immediate next steps (pending your approval)

1. **Write the formal design spec** → `Lancelot/docs/specs/2026-05-19-lancelot-v0-design.md` (this plan, formalized + self-reviewed).
2. **You review the spec.**
3. On approval → **writing-plans skill** produces the detailed implementation plan from the WBS.
4. **Build M0 first** (the vertical slice) — proves the thin-real-Rust-core path end-to-end, then fan out Waves 1–5 to hit M1→M4.

---

## 7. Mining addendum — confirmed deltas (wider-repo scan, anti-inflation)

Selective scan of `teardowns/`+`products/`+`core/` (574 lines of findings, ruthlessly filtered per user instruction). **Verdict: zero structural changes — the plan's moat, architecture, and WBS are confirmed correct by independent sources (vLLM's abort flow, OpenRouter's cancellation matrix, LangGraph's durability model all match the plan).** 17 small, concrete, reimplementable refinements fold in (no redesign, no scope inflation):

**`app.llm` decision-math the plan lacked (`external/01`):** failure-**rate** circuit breaker not failure-count — adopt LiteLLM constants (cooldown 5s, allowed_fails 3, fallback-depth 5 [also *bounds* the previously-unbounded failover], max-retries 2; never trip on 400/403/422) · 429-aware retry (honor `retry-after`, 60s total budget; POST-never-retried unchanged) · **cache-warm sticky routing** `(session,model)→last provider/key` (net-new; real money on multi-turn streaming) · per-key rotation within provider before provider-fallback on 429 · TTFT-normalized latency formula · 2 SSE-codec correctness fixes (keepalive = spec-valid SSE *comment* line; mid-stream error = `finish_reason:"error"` chunk at HTTP 200) · document the "abort can't stop billing on Bedrock/Groq/Google" caveat.

**Streaming/serving (`external/02`):** admission-time disconnect check (drop dead clients *before* GIL acquire, not only mid-stream) · `stream_options.include_usage` gates the terminal usage chunk (else silent SDK incompat) · codec internal model = tagged union `{Reasoning|Content|ToolCallDelta}` — **fold before Wave-1 freezes the codec seam** · `/livez`=200 even when pool-saturated, 503 only if un-booted (prevents K8s-liveness-kills-under-load).

**Agent durability precision (`external/03`, into `design/04 §3.3-3.4`):** per-task pending-writes table (mid-step crash must not re-bill finished tool calls) · content-addressed state blobs (kills O(steps²) history re-serialization) · deterministic interrupt id = `hash(conversation_id, step_index, tool_call_id)` (fixes HITL double-prompt) · `@app.agent(on_concurrent=reject|interrupt|rollback|enqueue)` + per-`conversation_id` run lease · in-proc tool dispatch = direct JSON-RPC on the shared registry (never loopback transport); only non-partial step events checkpointed.

**Deploy/DX (`external/04`):** `lancelot init` scaffolds `render.yaml maxShutdownDelaySeconds = timeout+10` + boot-warn on PaaS drain-timeout mismatch (else the in-flight-drop bug silently returns) · `lancelot run` canonical + scaffold `Procfile`/`railpack.json` (zero-config Railway must boot) · **Laminar-style provider→canonical OTel attribute normalization table** (collapse openai/anthropic/langchain/vercel-ai conflicting `gen_ai.*` keys to one canonical set — ~1-screen map, strengthens the observability-first thesis at ~zero cost). Optional v1+: `/llms.txt` + per-route `.md` doc twins.

These are tracked as spec line-items; none move a milestone gate.

---

*The only thing between here and a building framework is your go-ahead. Compiled 2026-05-19.*
