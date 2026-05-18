# Lancelot v0 — Design Specification

**Date:** 2026-05-19 (repository model corrected & second mining addendum folded in 2026-05-19) · **Status:** for user review (brainstorming approval gate) · **Location:** `docs/specs/` in the project repo `github.com/LeventySeven/Lancelot.git` — **committed**. This supersedes the original "gitignored / never committed" project rule, which applied only while this work lived inside the `researchfms` repo; the project now has its own repository.

**Evidence base:** the 19,125-line research corpus in `Lancelot/research/` (23 source/design/deep dossiers + `SYNTHESIS.md` + `DESIGN-DECISIONS.md`) and `Lancelot/ULTIMATE-PLAN.md`. This spec is the authoritative, self-contained design; it cites the corpus rather than re-deriving it.

---

## 1. Summary, goals, non-goals

**Lancelot** is a Python web/AI backend framework with a Rust core: FastAPI's developer surface, byte-compatible where it touches the wire, running on a hyper+tokio+PyO3 core, with a first-class AI hot path (LLM streaming, agents, MCP) that ties the inbound HTTP stream and the outbound LLM call into one backpressured, cancellation-correct system.

**Positioning (locked):** universal — a *complete* drop-in replacement for **both FastAPI and Robyn**, for **any** web application, not an AI-niche framework. As good as FastAPI for general web/API work (byte-compatible surface, near-zero migration), faster than Robyn and more web-optimized for exactly what FastAPI targets, so that **revolutionary startups and bigtech corporations alike can migrate any application type off FastAPI** (or Robyn) onto Lancelot. The Agentic/AI backend — agentic systems, multi-agent systems, LLM streaming — is the **differentiated headline hot path no pure-Python framework can structurally match**, but it is the standout use case, not the only one: general web universality and the AI hot path are co-equal v0 requirements.

**Goals (v0):** (a) FastAPI-wire-compatible enough that FastAPI's own test suite passes against Lancelot unmodified; (b) measurably faster (≤3 framework allocs/req, GIL-free non-handler lifecycle, <50ms cold start); (c) stable — a 113-guard release gate + a ratcheting conformance CI invariant; (d) AI-native — streaming/agent/MCP/`app.llm` shipped in v0, not bolted on later; (e) cloud-native — probes, drain, OTel, scaffolding shipped in v0.

**Non-goals (v0):** native Rust JSON-Schema/validator (delegate to pydantic-core via FFI; revisit post-1.0); free-threaded-default (multiprocess-first, ft-clean, dual-build — flip later on 4 criteria); ASGI as a fast path (ASGI is the migration on-ramp only); being an agent *framework* (Lancelot *serves* agent backends, it is not LangGraph).

**Success = measured, not claimed:** v0 ships when M0–M3 gates (§8) are green and the conformance harness (§6) reports v0-subset 100% + full-suite ≥90%.

---

## 2. Architecture (locked — see `DESIGN-DECISIONS.md` for the evidence per fork)

**Core stack:** `pyo3 0.27` (written 0.28-clean, free-threaded-clean) · `hyper 1.x` + `hyper-util` + `http-body-util` + `tower 0.5`/`tower-http` · `tokio` multi-thread · `tokio-tungstenite` · `socket2` · `matchit 0.8` · `bytes`. Build: `lto=fat, codegen-units=1, opt-level=3, panic="unwind"` + FFI-seam `catch_unwind` + respawn supervisor. Not actix (throughput is GIL-capped; chose API stability + Granian-validated).

**Process model:** N OS processes, one CPython each, sharing one socket via `SO_REUSEPORT`+`SO_REUSEADDR`+`TCP_NODELAY`, `listen(16384)`; **cgroup-aware** worker count. Per process: one tokio multi-thread runtime for Rust I/O + one uvloop asyncio loop + a bounded, load-shedding, GIL-detaching `BlockingRunner` for sync handlers (sheds to `503 Retry-After`). Per-request `contextvars.Context`. SIGTERM → graceful drain.

**Packaging:** maturin dual-build — `abi3-py311` (1 wheel/platform) + `cp313t/cp314t` free-threaded wheels; macOS-universal2 + manylinux_2_28 + musllinux + win-x64; `maturin sdist` fallback; auto-generated `.pyi`.

**Request lifecycle (the hot path):**
```
Rust accept/TLS/parse (single-pass resumable, header-receipt deadline)
 → matchit route match
 → const-tier short-circuit (zero Python, zero alloc) ─┐ if const route → return
 → admission-time disconnect check (drop dead client BEFORE GIL acquire)
 → LazyRequest proxy (1 alloc; it IS the shared mutable request — a per-phase
       rebuild breaks the middleware/contextvars contract, deep/04)
 → with_gil { bind args from compiled DI plan; call handler → coroutine }
 → DROP GIL ───────── await handler (GIL-free; outbound LLM I/O here) ─────────
 → with_gil { pydantic-core validate_json / dump_json — one Rust pass }
 → Rust response writer (boolean backpressure; no Future per token)
```
Invariant (type-enforced): **no `with_gil` scope may contain `.await`** — clippy `await_holding_lock` deny-set + a negative compile test (a `with_gil{ .await }` fixture must fail to compile).

---

## 3. Components (each: purpose · interface · dependencies)

Bounded units; full 46-unit WBS + dependency edges in `research/deep/09-ultimate-buildable-wbs.md`.

**Rust crate (`Lancelot/src/`):**
- `gil` — the `with_gil`-without-`.await` invariant wrapper. Iface: `attach`/`detach` helpers. Deps: pyo3.
- `socket` — SO_REUSEPORT bind, cgroup CPU count, tokio builder. Iface: `serve(addr, workers)`.
- `ffi` (FnInfo ABI + conversions) — the one Python↔Rust contract: opaque callable + arity + compiled plan handle; lazy `Request` `#[pyclass]` over hyper `Parts`+`OnceCell`; response extraction (5-tier fast dispatch); exception→response mapping *in Rust* (byte-exact 422). Iface: `FnInfo`, `Request`, `respond()`. Deps: pyo3, hyper, http.
- `router` — matchit per-method + const-tier (pre-baked `CachedResponse`). Iface: `match(method,path) -> Route|Const`. Deps: matchit.
- `service` — the `tower::Service` (the frozen seam everything layers on). Deps: hyper-util, tower.
- `stream` — Rust SSE/JSONL codec (tagged union `{Reasoning|Content|ToolCallDelta}`), timer-wheel keepalive (spec-valid SSE comment), boolean backpressure, disconnect watcher → cancel. Iface: `SseSink`, `ChunkedSink`.
- `blocking` — bounded GIL-detaching sync-handler pool. `supervisor` — drain + respawn + `catch_unwind`. `ws` — tokio-tungstenite bridge.

**Python package (`Lancelot/lancelot/`):**
- `app` — `Lancelot()` app object, `app.state`, lifespan (ordered, fail-fast, `{**child,**parent}` merge), `mount_asgi()`, `app.asgi()`.
- `routing` — decorators, `include_router`/route groups, trailing-slash-redirect-as-second-scan.
- `params`+`di` — registration-time signature→immutable DAG→flat topological plan + interned-int cache slots; yield-dep teardown (request vs function scope; PEP-789).
- `validation` — pydantic-core FFI seam (pinned version); 422 wire-shape.
- `openapi` — OpenAPI 3.1 via pydantic `GenerateJsonSchema` over FFI (byte-identical); Swagger/ReDoc/Scalar.
- `responses`+`streaming` — `JSONResponse`, `StreamingResponse`, `SSEResponse`, `AISSEResponse(codec=)`.
- `security` — OAuth2/HTTPBasic/Bearer/APIKey (always 401 + `WWW-Authenticate`; scopes plumbed not enforced).
- `testclient` — `httpx.Client` over a shared FFI `process_one(bytes)` (drives the real Rust pipeline; cannot drift) + `app.asgi()`+Starlette fallback; `TestResponse` IS `httpx.Response`.
- `llm` — pooled client (HTTP/2, `verify=True`, POST-never-retried, failure-**rate** circuit breaker [cooldown 5s, allowed_fails 3, fallback-depth 5, max-retries 2], 429-aware retry, cache-warm sticky `(session,model)` routing, per-key rotation, `.relay()` zero-copy proxy + cancellation-moat).
- `mcp` — `@app.mcp.tool/.resource/.prompt`, Streamable HTTP (2025-06-18), session/resumability.
- `agent` — `@app.agent(on_concurrent=reject|interrupt|rollback|enqueue)`; framework-owned step boundary; `DurableStore` ABC (per-task pending-writes, content-addressed state, deterministic interrupt id); in-proc tool dispatch on shared registry.
- `observability` — Prometheus `/metrics` + OTLP + JSON logs default-on; OTel GenAI spans + the provider→canonical attribute normalization table.
- `cli` — `lancelot init` (copytree-from-wheel: distroless + K8s probes + lockfile + `render.yaml`/`Procfile`/`railpack.json` with drain-timeout coherence) · `lancelot run` (canonical entrypoint).

---

## 4. Data flow

- **Request → JSON:** §2 lifecycle. DI plan executes in Rust, into Python only for the N user callables. One pydantic-core pass out.
- **Streaming (LLM):** handler `yield`s → Rust `SseSink` frames per codec → boolean backpressure to socket → client-disconnect watcher cancels the response task → `upstream.abort()` → PEP-789 yield-dep teardown closes the LLM client. Slow client → stalled handler → stalled upstream read → bounded RAM (structural, not coded).
- **Agent run:** framework loop persists only non-partial step events to `DurableStore`; SSE `id:` is the single resume cursor; reconnect replays from the last checkpoint; deterministic interrupt ids prevent HITL double-prompt.
- **Migration in/out:** `mount_asgi(prefix, fastapi_app)` runs unmodified FastAPI under Lancelot; `app.asgi()` exposes Lancelot as ASGI 3.0 (Lambda/Modal/tooling). ASGI is never the fast path.

---

## 5. Error handling & stability

Release gate = 113 numbered guards (`research/deep/07`), merge-blocking. The 5 bugs all three reference frameworks share, fixed by construction: (1) no disconnect→upstream-abort; (2) no per-route/total/header/idle timeout coverage; (3) unbounded hot-path queues; (4) streaming memory/teardown not structurally bounded; (5) no crash/abort self-healing. Rust-core-specific surfaces explicitly guarded: FFI-panic (= `catch_unwind` + supervisor, never process kill), GIL-across-await (= type-enforced invariant), `#[pyclass]` data race (= free-threaded-clean by construction). Exception→response mapping lives in Rust at the seam, byte-exact 422 from pydantic-core.

---

## 6. Testing & conformance (how "stable like FastAPI" is *proven*)

- **Dogfood lane (the linchpin, `research/deep/08`):** FastAPI's `TestClient` is a pure in-process ASGI driver → a ~30-line `conftest.py` rebinds `fastapi.testclient.TestClient` to a Lancelot ASGI-bridge; **581 FastAPI test files run unmodified** from a pinned vendored read-only checkout. Comparison is **string-compare on serialized JSON** (not dict-equality — wire order matters). Pin the identical pydantic-core version so `type`/`msg`/`ctx` strings match byte-for-byte.
- **Real-socket lane:** Robyn's integration tests reused (one line swapped) to exercise the Rust I/O path (parser, SSE, drain, SO_REUSEPORT) the in-process lane can't reach.
- **CI ratchet:** four lane jobs emit `parity.json`; each lane's floor = its best-ever default-branch value (parity can only rise). v0 = v0-subset 100% + full ≥90%; 1.0 = full 100% with every intentional divergence in `known_divergence.toml` as `xfail(strict=True)`.
- **Perf gate:** Blazeio's `tracemalloc` bench harness reused verbatim to assert ≤3–5 framework allocs/req and benchmark vs FastAPI.
- Per-unit tests ship with each WBS unit.

---

## 7. v0 scope

**In (table-stakes, byte-compatible on the wire — `research/deep/01-03`):** typed path/query/header/cookie/body/form/file params (incl. the 5 nastiest edge cases); Pydantic v2 validation + exact 422 wire-shape; `Depends()`/`Security()`/sub-deps/yield-teardown/`dependency_overrides`; OpenAPI 3.1 byte-identical + Swagger/ReDoc/Scalar; `include_router`/groups; trailing-slash-redirect; two-tier middleware (Rust/ASGI-pure + ASGI-compat escape hatch, never `BaseHTTPMiddleware`); `lifespan`+`app.state`; CORS; OAuth2/Basic/Bearer/APIKey; sync+async handlers (bounded); `TestClient`; WebSockets; static files (ETag/Range/304/sendfile); pydantic-settings config; detached background tasks. **+ differentiators:** AI streaming tier + const tier. **+ ops:** 3 probes, SIGTERM drain, cgroup workers, Prometheus/OTLP/JSON+GenAI spans, `lancelot init`/`run`, dual-build wheels, <60MB image, <50ms cold start.

**Out (v0):** native Rust validator/JSON-Schema; free-threaded default; HTTP/3; non-essential templating; the AI-gateway control-plane surface (routing DSLs etc.).

---

## 8. Build plan & milestone gates

Thin serial spine (Wave 0, 1 agent freezes interface seams) → 38/46 units fan out parallel (Waves 1–5, ≈6/12/11/7/6 agents, collision-free via dedicated seam files). Gates (each an automated merge gate; conformance % is the single steering metric; the ratchet blocks regressions):

| M | Gate |
|---|---|
| M0 | Vertical slice green: 7 assertions incl. negative-compile-test, ≤5 allocs/req, GIL released across `.await`, imports under py3.13 *and* py3.13t. Retires the build-path risk first. |
| M1 | Table-stakes: conformance v0-subset 100% + full ≥90%; byte-exact 422; stability gates 1–6. |
| M2 | AI tier: streaming + disconnect→abort + `app.llm` + MCP + agent loop; conformance ≥90% held; token-billing-stops-on-disconnect test passes. |
| M3 | Ops/cloud: probes, drain, OTel, `lancelot init`, dual-build wheels, <60MB, <50ms cold start; conformance ≥95%. |
| M4 | Release: full conformance ≥99%; 113 stability guards green; benchmark vs FastAPI published. |

The 17 first-addendum deltas (`ULTIMATE-PLAN §7`) and the 11 second-addendum deltas (§11) are spec line-items; none moves a gate. F3 (codec tagged-union) must land before Wave-1 freezes the codec seam; the §11 Cluster-A `app.llm` transport-lifecycle items must land before Wave-1 freezes the `app.llm` pool seam.

---

## 9. Risks & open items

- **Byte-exact OpenAPI/422 drift** on pydantic-core upgrades → mitigated by pinning pydantic-core and delegating schema/validation via FFI (never reimplement in Rust).
- **Lazy-request correctness** — must be the shared mutable request object, not a per-phase rebuild (deep/04 hazard) → enforced by an explicit middleware/contextvars conformance test.
- **Free-threaded ecosystem readiness** — deferred-with-criteria; dual-build means no bet is forced now.
- **`abort()` can't stop billing on Bedrock/Groq/Google** — documented caveat, not a bug.
- No other open design questions; all 7 forks are evidence-locked (`DESIGN-DECISIONS.md`).

---

## 10. Spec self-review

Placeholder scan: none (no TBD/TODO). Internal consistency: architecture (§2) ↔ components (§3) ↔ scope (§7) ↔ gates (§8) aligned; **both** mining addenda (§7 first, §11 second) are reconciled into §3's existing components and flagged in §8 — no new component, no scope growth, none moves a gate. Scope: single coherent v0 with explicit out-list — focused enough for one implementation plan (the WBS already decomposes it). Ambiguity: the one reimplementation-ambiguous behavior (lazy request identity) is made explicit in §9 with an enforcing test. **No issues found requiring rework.**

---

## 11. Second mining addendum — `researchfms/` deep-scan deltas (2026-05-19)

Independent 4-domain parallel re-scan of the full `researchfms/` corpus, anti-inflation enforced. `products/` returned **zero** net-new (confirming §7's scan was complete there); `ai_roadmap/` and `shownmedia/` were inspected and dismissed as irrelevant noise. 11 concrete, reimplementable deltas survived: each ≤1 spec line, each lands inside an **already-scoped** §3 component, and **none moves a milestone gate**. Sequencing: Cluster A must land before Wave-1 freezes the `app.llm` pool seam.

**Cluster A — `app.llm` transport lifecycle (the one real coverage gap: the disconnect→abort moat covers a dead *client*, not a stalled *provider*). Into §3 `llm`:**
- **A1.** Split the pool into a unary client (read/write timeouts) and a **separate streaming client with zeroed read-timeout + bounded max-connection-duration** — else long reasoning / extended-thinking streams die at the unary timeout. `teardowns/BIFROST.md`
- **A2.** **Idle-timeout reader on the upstream LLM stream** (close cleanly on zero bytes for N s) — detects a hung provider mid-generation; a distinct axis from the inbound idle-timeout and the client-disconnect moat. `teardowns/BIFROST.md`
- **A3.** **Max-total-stream-duration wall-clock cap** on `app.llm`/`AISSEResponse` — catches trickle-forever streams an idle-timeout never trips. `teardowns/LITELLM.md`
- **A4.** **TCP keepalive on the `app.llm` dialer** (idle 10s / interval 5s / count 3); treat `ECONNRESET`/`EPIPE` on a pooled connection as stale keep-alive → drop the connection before the (idempotent-only) retry. `teardowns/BIFROST.md`, `core/06`

**Cluster B — agent-serving correctness/billing (at Lancelot's seam — `app.agent`/`app.llm`/`background`; Lancelot serves, does not run, the loop). Into §3 `agent`/`llm`:**
- **B1.** Persist the inbound turn to `DurableStore` **before** the first `app.llm` upstream call (not only at the first non-partial checkpoint) — a crash between accept and first checkpoint otherwise silently loses the turn. `core/06`
- **B2.** On HITL/approval resume, convert approved/rejected/auto tool results into input items and continue — **do NOT re-invoke the model** that produced the pending call (re-sampling = a duplicate paid generation; protects the billing moat). `core/12`
- **B3.** A `background.spawn()`-originated `app.llm` call **bails immediately on 529/overloaded** (no waiting client ⇒ do not burn money/connections). `core/06`

**Standalone — one line each:**
- **S1.** **`/metrics` binds to an admin port/interface distinct from `$PORT`** (or is auth-gated). Default-on `/metrics` on the public app socket is an unauthenticated endpoint/model-inventory leak — the §7/spec "Prometheus `/metrics` default-on" decision silently *creates* this; a Rust-speed core ships the leak faster. Into §3 `observability`/`cli`. `re_methods/RE_SERVER_SIDE.md` §16
- **S2.** **`set_ulimit()` (raise `RLIMIT_NOFILE`) at process boot** — `listen(16384)` is moot if fds cap at the default 1024; a load-only failure invisible in tests. Into §3 `socket`/`cli`. `teardowns/VLLM.md`
- **S3.** **OTel GenAI content-capture is opt-in; by default store content externally and put only an attribute reference on the span** — makes "GenAI spans default-on" safe (PII + span-size blowup on long contexts). Into §3 `observability`. `core/13`

Self-review of this addendum: all 11 reconcile into existing §3 components — no new component, no scope growth, no gate movement; S1 additionally closes an information-disclosure hole the default-on `/metrics` decision opened.

---

*Next on approval: invoke the implementation-planning skill → derive the step-by-step plan from the WBS → build M0 (the vertical slice) → fan out Waves 1–5 to M1→M4. This spec is committed to the project repo (`github.com/LeventySeven/Lancelot.git`).*
