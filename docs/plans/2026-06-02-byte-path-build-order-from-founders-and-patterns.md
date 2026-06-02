# Lancelot — honest build order from two cross-validated workflows (2026-06-02)

Two multi-agent workflows, each adversarially reviewed, then cross-checked against each other.
Both ran against `/Users/seventyleven/Desktop/researchfms` and Lancelot's actual source.

- **Workflow A — `founder-switch-verdict`** (22 agents): 10 FastAPI-founder personas (Polar, Bitcart,
  LangServe, Langflow, LitServe, R2R, Letta, Hayhooks, Prefect, FastAPI-Users) each judged, then an
  adversarial FastAPI-loyalist skeptic refuted weak reasons, then synthesis + honesty audit.
- **Workflow B — `pattern-mine`** (11 agents): 4 FastAPI clusters + 4 Rust clusters
  (DataFusion/Polars/LakeSail/LanceDB/Turbopuffer/Zed/Tauri/Ripgrep/Robyn/Blazeio) mined, deduped per
  track, then mapped to Lancelot as build / adapt / confirm / reject.

Scripts: `docs/plans/wf-founder-switch-verdict.mjs`, `docs/plans/wf-pattern-mine.mjs`.
Raw output: `tasks/wftnrombp.output`, `tasks/w9ohnw5y0.output`.

---

## The headline finding (both workflows agree)

**10/10 founders would NOT switch** — founder self-assessment AND the skeptic. The recurring, verified
reason: **their bottleneck is never the HTTP framework** (it's Postgres / the LLM upstream / a GPU worker
behind a queue / Argon2). Lancelot's marquee wins optimize a path that isn't their cost center, and two
structural facts (verified in Lancelot source) cap the value for *incumbents*:

1. The big Rust wins live on a server real DI apps can't use — `const_routes` degrade to ordinary routes
   under uvicorn (`app.py:1077`); native `run()` still does no DI/body-parsing (`deploy.md:32`).
2. TTFT only fires inside Lancelot's own stream seam (`asgi.py:1082`); a user's `StreamingResponse` /
   `sse_starlette` bypasses it entirely.

**Strategic consequence:** stop pitching migrations to welded-in incumbents. Lead with streaming hardening
+ `@app.batch` to **greenfield AI byte-path builders only**. Make "drop-in" actually drop in by closing the
compat gaps below.

---

## Build status (this session)

- [x] **T1 #1 `max_stream_lifetime`** — DONE. `routing.py` field + `@app.stream` kwarg + `asgi.py` watchdog
  (absolute wall-clock cap, emits terminal `event: reconnect` frame, runs gen `finally`). TDD; real-server
  test `tests/test_m19_stream_lifetime.py` (3 tests). Gates green: full suite 584 passed, conformance 978.
- [x] **T1 #2 const-tier auth guardrail** — DONE. `ConstRouteAuthBypassWarning` + `_audit_const_route_middleware_bypass(app, strict=)` in `app.py`, wired into `run()` (warn default, `LANCELOT_CONST_STRICT` → raise). TDD; `tests/test_m19_const_auth_guardrail.py` (5 tests). Gates: full suite 589, conformance 978.
- [x] **T1 #3 TTFT-on-any-body** — DONE. `_ttft_send_wrapper`/`_record_ttft_now` in `asgi.py`, applied at all 3 streamed-response emit points (custom-route delegation, normal ASGI-callable delegation, `body_iterator` loop). Fires on `text/event-stream` or first `more_body=True` chunk; never on unary. Honest "first-framework-byte" semantics. TDD; `tests/test_m19_ttft_any_stream.py` (3 tests). Gates: full suite 592, conformance 978.
- [x] **T1 #4 bytes-in-flight admission** — DONE (safe variant). `LANCELOT_MAX_INFLIGHT_BYTES` (default 0=off) in `src/service.rs`: aggregate in-flight request-body budget, charged at admission (Content-Length capped at `max_body`), **load-sheds with 503+Retry-After** (deadlock-free) rather than LanceDB's queue-and-wait min-in-flight admission; forward-progress guarantee (always admit when nothing in flight). RAII `InflightGuard`. **Design note:** the full min-in-flight *waiting* admission was deliberately NOT built — reject/shed is the correct HTTP-edge behavior and avoids the deadlock subtlety; default-off so the hot path is byte-identical. TDD; real-server adversarial test `tests/test_m19_inflight_bytes.py` (2 tests). Gates: clippy clean, cargo test 17, conformance 978, **full suite 594 passed**.

**TIER 1 COMPLETE** (4/4). All gated green: full suite 594 passed / 3 skipped, conformance 978/987, clippy -D warnings clean, cargo test 17. Wheel rebuilt (maturin --release). Not yet committed (on `main`).
- [x] **T2 #5 runtime/dynamic-surface conformance matrix** — DONE. Probed the 5 highest-coupling dynamic surfaces empirically, then pinned each with a characterization test (`tests/test_m19_runtime_surface_matrix.py`, 5 tests). 4 PROVEN, 1 documented divergence (below). No router change made — fixing the divergence would risk the const-tier hot path + live-replace semantics for a narrow edge case (no-bullshit rule), so it is documented, not patched.

### Runtime/dynamic-surface conformance matrix

| # | Surface (named by) | Result | Note |
|---|---|---|---|
| 1 | custom `APIRoute.get_route_handler` + `route_class` (Polar 8-mixin) | **PROVEN** | custom handler is invoked; `openapi_extra` mutation in `__init__` works |
| 2 | runtime route registration `add_route_live` (Hayhooks) | **PROVEN** | route added after first serve is dispatched with full DI plan (raw `router.add` is NOT the supported path — it skips plan compilation → 422) |
| 3 | dynamic `pydantic.create_model` body annotation (LitServe) | **PROVEN** | resolved + validated at registration (valid→200, invalid→422) |
| 4 | duplicate same-path/method resolution (LangServe first-wins) | **DIVERGENT** | Lancelot static routes are **last-registered-wins** (const-tier dict overwrite); FastAPI is **first-wins**. Dynamic `/param/` routes DO iterate first-wins. Workaround: distinct paths or parameterized routes. Documented, not patched. |
| 5 | mounted sub-app lifespan (Hayhooks) | **PROVEN** | startup AND shutdown run (M15 fix confirmed) |

**TIER 2 COMPLETE** (#5 matrix + #6 BaseHTTPMiddleware). Full suite + conformance green.
- [x] **T2 #6 BaseHTTPMiddleware compat** — DONE.
  - **#6a (the real founder blocker — already worked, now official):** empirically verified `add_middleware(BaseHTTPMiddleware-subclass)` already streams + injects headers + propagates `request.state`→`scope['state']` to the Lancelot handler (the dishka/tenant/CSRF per-request-scoping pattern). Removed the misleading "NEVER BaseHTTPMiddleware" docstrings; documented; regression tests `tests/test_m19_basehttp_middleware.py` (3).
  - **#6b (full streaming adapter — your pick):** rewrote `HTTPMiddlewareAdapter` from synchronous full-buffer capture to a **pure-asyncio streaming task-group model** (no anyio dep). Fixes the real bug it had: a corrupted DUPLICATED content-type (`text/event-stream, text/plain` / `application/json, text/plain`) on every response + full-buffering of streams. Lazy inner-task start preserves short-circuit semantics; **exception transparency** (Starlette `app_exc` parity) re-raises inner exceptions after the response. TDD; `tests/test_m19_http_middleware_streaming.py` (4). The rewrite first regressed 10 yield-dep conformance tests (the exact risk the ceiling note warned of) — root-caused to swallowed re-raise, fixed, conformance back to 978. `test_background_tasks` remains the documented ceiling failure (streaming alone doesn't flip it).

## TIER 1 — BUILD (net-new, byte-path, in-mission, cross-validated)

1. **Per-route `max_stream_lifetime` wall-clock cap + terminal `{type:reconnect}` sentinel.**
   *(Pattern-mine HIGH; from Polar 10-min cap + Letta. Reinforces founder streaming-resilience theme.)*
   Lancelot has `max_stream_silence` (idle watchdog, `routing.py:65` + `asgi.py:1043`) but **no absolute
   lifetime cap** — a stream trickling one byte every `(silence-1)s` never trips idle and lives forever
   (unbounded mpsc + upstream client + ContextVars). Mirror the silence plumbing; emit the existing
   `done_sentinel`/`error_frame` on breach so the gen `finally`/`aclose` runs. Default `None` (off).
   **Risk: low.** Subprocess load test required (cardinal rule 2).

2. **Const-tier auth guardrail — warn or refuse when a `const_route` shadows a path with auth/allowlist
   middleware.** *(Founder #3 — verified security footgun: Prefect `/health` Basic-auth gate, Letta
   `CheckPasswordMiddleware`.)* Today the prebaked tier silently strips the auth check → a regression, not a
   win. Tiny defensive change; do before any further const-tier promotion.

3. **TTFT attaches to ANY streamed body at the ASGI send boundary (incl. broker/queue-wait).**
   *(Founder #4, demanded by LangServe/Langflow/R2R/LitServe.)* Hook the first body event for
   `text/event-stream` or any chunked response, regardless of who built the `Response`. Ship as **honest
   observability** ("wall-clock to first framework byte", NOT "true LLM TTFT"). Carry the caveat that
   sync handlers pushed to `run_in_threadpool` can't be cancelled.

4. **Two-budget inbound backpressure: bytes-in-flight admission budget alongside the count-only conn cap.**
   *(Pattern-mine HIGH; from LanceDB.)* `src/service.rs:349` admits on a count-only semaphore (1024) with no
   byte budget → 1024 conns each buffering a large body = unbounded RAM. Add `LANCELOT_MAX_INFLIGHT_BYTES`
   (AtomicI64, charged at body-read, released on complete) with LanceDB's min-in-flight admission rule to
   avoid thrash/deadlock. Pairs with `LANCELOT_MAX_BODY`. **Risk: medium-high** (hot Rust admission path;
   GIL×OnceLock / hold-guard-across-await hazards). **Bench-gate**: if count cap + `MAX_BODY` already bound
   RAM acceptably under a large-body flood, downgrade to a doc'd knob instead of shipping complexity.

## TIER 2 — DE-RISK / POSITIONING (highest founder leverage; mostly verification + docs)

5. **Runtime/dynamic-surface conformance matrix + targeted tests** (the honesty audit's #1).
   The *most-cited actual blocker* for the highest-value targets: custom `APIRoute.get_route_handler`
   timing/ordering (Polar 8-mixin, Prefect response-scoped stack), runtime `app.routes` mutation + OpenAPI
   cache invalidation + `app.setup()` (Hayhooks), first-registered-wins resolution (LangServe), dynamic
   `create_model` annotation (LitServe), makefun runtime-signature Security (FastAPI-Users, Polar). The 0.4%
   parity gap is exactly where their load-bearing core lives. Publish proven-vs-aspirational per surface.

6. **BaseHTTPMiddleware compat — real adapter, or bounded shim + codemod + docs.** *(Founder #2; named by
   Bitcart/R2R/Langflow/Prefect; also pattern-mine's #1 documented conformance gap.)* `add_middleware`
   rejects `BaseHTTPMiddleware`/`call_next` (`app.py:237`). **Necessary-but-not-sufficient** — each of those
   4 also has an independent blocker, so don't sell it as "the conversion." Be brutal about what a bounded
   shim does NOT support (streaming `call_next`).

7. **Re-scope the pitch (positioning, not code):** lead with streaming *hardening* (bounded backpressure +
   graceful-drain-on-SIGTERM, **never** the 1.3× number) and `@app.batch` **only** for net-new LLM-gateway /
   read-heavy-RAG-proxy builders. Stop pitching to stateful/billing/per-tenant or DB/LLM/GPU-bound apps.

## TIER 3 — CHEAP HARDENING / ADAPTS (low risk; do opportunistically)

- **mimalloc as `#[global_allocator]` for the cdylib** — one line; **bench-gate on the oha harness**, drop
  if neutral (GIL-serialized single loop may make it a wash). *(LakeSail.)*
- **Opt-in inbound transient-exception → 503 `Retry-After` classifier** (user supplies the predicate; core
  stays pydantic-only). Reuses `exceptions.py:151-168`. *(Prefect.)*
- **Reusable/bounded gzip encode buffer** (`clear()` + `shrink_to(cap)`) + **`cfg(test)` gzip level** to
  exercise the off-loop codepath without level-9 CPU. *(Zed.)*
- **Truncate(~2KB)+NUL-strip any captured upstream body** in `upstream_metrics` (extend existing CRLF/NUL
  discipline). *(Polar.)*
- **Slot-based de-correlated retry backoff** in `TransportPool` (`llm.py:230/305` jitter-exp *synchronizes*
  competing retriers — the thundering-herd shape of `@app.batch` fanning to one 429'd LLM). *(LanceDB.)*
- **Opt-in out-of-band liveness watchdog** → flip `/readyz` to 503 on a user-supplied stuck-backend
  predicate (once a 200 stream line is sent, a dead backend can't become a 5xx). *(vLLM.)*
- **In-band `Server-Timing` header** surfacing already-computed `queue_wait_ms` + duration (flag-gated, not
  on every response by default). *(Turbopuffer.)*
- Lower: method-gated RED metrics (exclude OPTIONS/HEAD); SSE `seq_id` cursor on the keepalive ping;
  per-event client-drain (slow-consumer) metric; multi-range `multipart/byteranges` static with per-response
  cap; scope-rewrite legacy-prefix→canonical seam + `X-Deprecation-Notice`.
- **Speculative, build only on demonstrated demand:** coalescing 1-slot mailbox as an opt-in `@app.stream`
  mode (merge adjacent token frames under slow client) — never replace the default block-the-producer pump.

### Rust new-surface discipline checklist (NOT builds — guardrails for any future per-request/multi-task code)
JoinSet abort-on-drop · Drop-guard cleanup (not early-return) · `Arc<OnceCell<T>>` not `OnceCell<Arc<T>>`
(spawn_child connection-storm) · asymmetric channels (never block own producer) · Acquire/Release counters +
cache-line caution · cooperative-yield/off-loop-hop for in-Rust CPU loops · drop guard before `.await` ·
keep pydantic-core `to_json` as bytes through FFI (no re-parse double-encode) · audit hyper header-size
limit as both silent-truncation gotcha and inbound DoS knob · per-worker scratch buffer only behind a
profile.

---

## ROUND 2 (2026-06-02, post-M19) — deeper recheck + broader mine

Re-ran both workflows with richer context (M19 state + impl-file pointers), a 14-founder panel (+Dispatch,
OPAL, BentoML, Full-Stack-Template), a **code-grounded verifier** stage, and 10 mining clusters incl. deep
second-pass. Scripts: `wf-founder-recheck-v2.mjs`, `wf-pattern-mine-v2.mjs`. Outputs: `tasks/wd78wbkdl.output`,
`tasks/w1lu0ypr1.output`.

**Founder verdict movement: 0/14** — all still would-not-switch (M19 sound but lands on surfaces incumbents
can't reach; framework is never their bottleneck). The value is what the code-verifier caught — **3 real
bugs, all independently reproduced**:

| Bug | Reality | Impact | Fix |
|---|---|---|---|
| **OpenAPI nested-Security drop** | a `Security()` nested in a `Depends()` → `op.security=None` AND `securitySchemes=None` (`di.py` collects only direct security; `openapi.py:301` reads top-level only) | `/docs` Authorize button gone; **breaks fastapi-users + Polar Speakeasy SDK** | recurse `sub.plan.security` (template exists at `openapi.py:172-182`) |
| **Duplicate static path = last-wins** | `routing.py:742` dict overwrite vs FastAPI first-wins; not escaped under uvicorn | **500s LangServe's default `add_routes`**; latent for Prefect | first-wins (insert-if-absent) OR loud startup warning |
| **`request.state.x` AttributeError** | `request.state` returns a plain dict; `State.__getattr__` is wired only to `app.state` | my M19 "fixed #1 blocker" claim is a **relabel** — only dict-subscript works | return a dict-AND-attribute `State` wrapper |

Plus a doc defect: `lancelot/websockets.py` docstring advertises a Rust `src/ws/mod.rs` that does not exist.

**Audited next-build order (all code-verified, in-mission, Tier-D-safe, toward parity):** (1) OpenAPI security
flattening; (2) duplicate-static-path first-wins/warning; (3) `request.state` attribute parity.

**New byte-path build candidates from round-2 mining (verified file:lines):**
- HIGH: inbound **Idempotency-Key replay cache** (only outbound exists today); **outbound gzip on TransportPool
  request bodies** (only response gzip shipped); **per-request bypass/429-throttle for `LANCELOT_MAX_INFLIGHT_BYTES`**
  (M19 shipped all-or-nothing shed, no escape hatch).
- MED: **FD soft-limit raise (`RLIMIT_NOFILE`)** completing the conn-cap story; **`X-Forwarded-Prefix`→root_path**;
  inbound **transient-exc→503 classifier** (user predicate); **magic-byte MIME sniff** for static; **emit 499**
  through metrics on a won disconnect race (`metrics.py:149` anticipates it but it's never produced).
- ADAPT (high leverage, tiny): **honor inbound `x-should-retry`** in the outbound retry classifier (`llm.py:471`);
  **readiness-callback hook** → `/readyz` 503 (5-project converge); **name the responsible knob** in the 503 shed body.

**Completeness gaps (still un-mined after 2 rounds — next targets):** WebSocket byte-path depth (backpressure,
ping/pong, close-codes, permessage-deflate), HTTP/2-3 as an evidenced decision, TLS/connection-setup path,
streaming multipart upload ingest, gRPC/protobuf relay, cookie/CSRF edges, conditional-GET/ETag/304 for dynamic
responses, TransportPool connection lifecycle, non-Python/Rust corpora (Go/Node/Envoy), the OTel/Prometheus
export transport.

## CONFIRMED BULLSHIT / DO-NOT-BUILD (both workflows + Tier-D)

**Non-reasons (don't pitch as switch reasons):** tiny-handler throughput (loses to raw Starlette+uvloop;
never the bottleneck) · large-payload serialization as a *broad* pitch (sub-4KB payloads + orjson shops make
it lateral-to-regressive) · TransportPool/upstream_metrics/cost as a "freebie" (only works through
`TransportPool.request()`; routing their LiteLLM/LangChain through it crosses the AI boundary) ·
`lancelot.webhook` (hard part is the durable delivery worker, off the request path) · MCP mount / canonical
/ session-affinity / memory_pool / secrets / pagination / ratelimit (like-for-like replacements; ratelimit
is *weaker* than their Redis-shared limiter).

**Reject as builds (Tier-D / not byte-path):** per-chunk spawn_blocking streaming bridge (the 16–26×
regression already re-architected away) · OpenAI-compat model→handler indirection / model gateway / model
routing · adaptive SLA-aware batch window / dynamic model-call batching (worker-side, user logic) · custom
JSON serializer / orjson re-derivation · cross-worker pub/sub + leader election (external bus = user-side) ·
Arrow IPC / pickle-binary serde / spill machinery (no HTTP analogue; pickle on edge = RCE) · runtime route
registration + OpenAPI cache invalidation (fights matchit/const-tier) · ASGI-over-network-transport bridge ·
app-instance caching keyed on settings hash · boot-time app-secret validation (user-side).

**Confirms existing (validated choices, no action):** const tier · GIL-released-across-await · PyOnceLock ·
HTTP/1.1-only rejecting h2c · retry-hint headers · bounded fanout w/ JoinSet · await_capacity fire-on-full ·
`cfg(test)` buffer=1 · memory_pool named-OOM · SO_REUSEPORT multiprocess (Linux-only LB) · graceful drain
(no hard-kill) · BlockingPool sync offload + 503 valve · eager-first-chunk `defer_status` · client-disconnect
→ upstream cancel + `finally`/`aclose` · `cancel_on_disconnect` dual-task race · never-gzip-streaming · SSE
error→ProblemDetail + done_sentinel · route-template metric labels · raw-ASGI middleware discipline ·
correlation-id + W3C traceparent · in-flight gauge decremented post-body · /livez+/readyz+/fleet/status ·
json depth cap · registration-time arity/DI/TypeAdapter precompute · declined Last-Event-ID replay.
