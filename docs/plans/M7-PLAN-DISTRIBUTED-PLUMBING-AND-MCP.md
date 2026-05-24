# Lancelot M7 Plan — Distributed Plumbing & MCP (transcript-derived)

**STATUS: 📋 PLAN (not started).** Predecessor M6 plan committed at `aa2d245`, also not yet executed. M7 executes AFTER M6 (M5 → M6 → M7).

**Date:** 2026-05-24
**Anchor rule (unchanged):** *no bullshit, no useless logic.* Every M7 item is **directly traceable to a verbatim pattern in `researchfms/core/TRANSCRIPTS_YC_ROOT_ACCESS.md`** — extracted via 4 parallel agents (Batches A/B/C/D) on 24 transcript sections covering scale, observability, AI gateways, and data/edge/security.

## Why M7 exists — the transcript signal

The YC Root Access transcripts were read systematically. **Every M7 item below cites the exact section that motivates it.** Three convergent signals across 24 sections:

1. **Outbound LLM transport primitives are missing.** Giga's sub-agent outbound calls, Parahelp's N-trace fanout, Chroma's 10-30 parallel-probe query plans, Letter AI + Mnemonic's MCP server adapters — all need a Lancelot-level primitive for *outbound* LLM calls fired *during* an in-flight request, with `traceparent` + cost + session-affinity propagated. Lancelot's existing `TransportPool` (M2 Tier-C) is the natural seam; M7 extends it.

2. **Distributed observability across the LLM-call boundary is the marquee.** Sim.ai monetizes on AI compute consumption (needs per-request cost telemetry). Commure runs "cybersecurity-framed" continuous monitoring on payer-API calls (per-provider RED + error taxonomy). Conductor's "head of engineering" surface needs fleet-rollup. Hightouch's per-customer fan-out forces cardinality-bounded labels. HappyRobot proves observability is only useful if it captures adverse paths (cancel/timeout/4xx). M7 wires the full picture.

3. **Multi-region + secrets + identity is the deploy story M6 doesn't ship.** Infisical's stateless-app + external-secrets-store pattern maps 1:1 to Lancelot's multiprocess `SO_REUSEPORT` model extended to N regions. Agent-identity as a 3rd actor class (alongside human + machine) needs a `context.agent_id` ContextVar. Veriff's per-jurisdiction routing gives the config shape.

**M7's critical correction to the prior framing:** Batch C's deep-read of Parahelp / Magic Patterns / Giga explicitly **rejected** the "build a multi-AI-gateway abstraction" idea (Vercel AI Gateway / Portkey / OpenRouter). Every customer-validated LLM app uses a single model family; model-stack churn is paid in eval/calibration *upstream of transport*. A framework gateway would solve a non-bottleneck and violate the cardinal rule. **This is now Tier-D for Lancelot, documented below.**

## Headline claim M7 must enable

> "Lancelot ships outbound LLM transport primitives (`TransportPool.spawn_child` + `fanout` + LLM-aware OTel attrs + MCP-server adapter) that close the distributed-trace loop end-to-end through the LLM call; distributed observability spans regions + tenants + adverse paths with cost-per-request and queue-wait as first-class metrics; multi-region deploy is documented + tested across Render + AWS + Fly.io with rotate-aware secrets + agent identity in context; and the Rust hot path is hardened with 14 patterns extracted from 8 production-Rust projects (test buffer=1; `select_biased!` graceful shutdown; per-tenant memory pool; `intern!` hot-path attr lookups; etc.). Every primitive is traceable to either a YC Root Access transcript or a Rust-project teardown."

Four sub-claims (outbound primitives + MCP adapter; distributed observability; multi-region plumbing + secrets; Rust hot-path discipline) — each backed by a transcript-or-teardown citation and a reproducible artifact in the repo.

## Honest state of the framework (updated for M7)

| Surface | Post-M6 status | M7 changes |
|---|---|---|
| `TransportPool` (M2 Tier-C) | Pool with cancel propagation, traceparent forward, KV-cache helpers | **T1.1 spawn_child + T1.2 fanout + T1.3 LLM-aware OTel attrs** |
| MCP exposure | M6 T3.2 ships docs-MCP server | **T1.4 adds `lancelot.mcp` adapter that auto-mounts `@app.tool` routes as MCP tools** |
| OTel metrics | RED + TTFT + in-flight (M4) | **T2.1 cost ContextVar + T2.2 per-upstream RED + T2.5 adverse-path emit + T2.4 cardinality controls** |
| Multi-region deploy | M6 ships single VPS-pair recipe | **T3.1 Render multi-region + AWS multi-AZ + Fly.io recipes** |
| Secrets | env-var only | **T3.2 `lancelot.secrets` lazy provider (env/Vault/Infisical/AWS-SM + rotate-on-SIGHUP)** |
| Identity in context | request_id + trace_id (M4 B3) | **T3.3 agent_id ContextVar (Infisical's 3rd actor class)** |
| mTLS | not supported | **T3.4 toggle on TransportPool + rotate-on-SIGHUP** |
| Multi-region session-affinity | `Response.set_session_affinity` exists; multi-region behavior undocumented | **T2.6 verification recipe + cross-region test** |

## M7 items — every row cites the transcript pattern that motivates it

### Track T1 — Outbound LLM transport primitives (sub-agent + fanout + observability + MCP)

**Goal:** Close the distributed-trace + cost loop end-to-end through outbound LLM calls fired during an in-flight request. Make MCP a first-class transport adapter.

| # | Item | Transcript citation | Done = |
|---|------|---|--------|
| T1.1 | **`TransportPool.spawn_child(parent_ctx)`** — sub-agent outbound primitive that forwards `traceparent` + `context.request_id` + `Response.session_affinity` + `context.agent_id` into LLM calls made *while* the parent request is in-flight. The current TransportPool is one-shot; M7 makes it nestable. | Giga (lines 9358–9852): "a new protocol where you can make outbound calls from within a single agent — basically like a sub-agent…Both calls are 'live' simultaneously". Parahelp (lines 7798–8292): N-trace fanout pattern. | New API + regression test: parent request issues child via `pool.spawn_child(parent)`, traceparent observed downstream propagates parent's trace-id with new span-id. (Coding ~3-4 h.) |
| T1.2 | **`TransportPool.fanout([req1, req2, …], cancel_laggards=True, deadline=…)`** — parallel-probe primitive with bounded concurrency + first-N-of-M semantics + cancel-laggards on first N successes. | Chroma/Huber (lines 14052–14207): "an LLM generates a 'query plan' that creates 10–30 distinct search probes simultaneously across structured and unstructured data sources". | New API + regression test: 10 fanout calls; first 5 success → remaining 5 cancelled within ~10ms. (Coding ~3-4 h.) |
| T1.3 | **LLM-aware OTel attrs on `TransportPool`** — emit `llm.provider`, `llm.model`, `llm.tokens.input`, `llm.tokens.output`, `llm.cost_usd`, `llm.first_token_ms` on every outbound LLM call. Cost computed from a user-supplied rate table (Lancelot doesn't ship rates). | Giga: "products which are extremely token intensive and that's where you need to optimize for per token cost". Sizabi (lines 2763–3053): "logs are all you need — AI agents can now read log lines at scale". | New metric + attrs visible in OTel collector under TransportPool spans; user-supplied rate table loaded from env or config. (Coding ~2-3 h.) |
| T1.4 | **`lancelot.mcp` adapter** — auto-mount existing `@app.tool` routes as an MCP server endpoint (e.g. `/mcp`), reusing the already-derived `output_schema` (M2 Tier-C). Pure HTTP↔MCP transcoding; zero AI logic. | Letter AI (lines 4156–4279): "Letter AI MCP servers, allowing customers' agents…to query Letter's content". Mnemonic (lines 3053–3349): "MCP integration that allows Cursor, Claude Code, or other coding agents to call Momentic as an external tool during an active development session". | `lancelot.mcp.mount(app)` returns an MCP server handle; smoke test: Claude Code calls a Lancelot `@app.tool` over MCP and gets the response. (Coding ~3-4 h.) |
| T1.5 | **MCP-tool minimal-return discipline doc** — per Dex Horthy: "MCP tools that return big blobs of JSON flood your context window with nonsense." Document the design rule in `docs/operator/mcp-guidelines.md`. Lancelot's MCP adapter (T1.4) defaults to schema-strict, no extra-field bloat. | Dex Horthy / HumanLayer (already-read M6 context, lines 13900–14052): "MCP tools that return big blobs of JSON, that's going to flood your context window". | Doc committed; T1.4 adapter has a config knob for max-response-bytes + a default that warns above 4 KB. (Writing ~1 h.) |

**Done = (T1):** spawn_child + fanout + LLM-aware OTel attrs all shipped with regression tests + smoke verified; MCP adapter functional + minimal-return doc committed.

---

### Track T2 — Distributed observability (cost + per-upstream + fleet + cardinality + adverse paths)

**Goal:** Multi-tenant + multi-region cost/health attribution as a first-class framework primitive, separate from AI-logic dashboards. The framework instruments; the user builds the dashboard.

| # | Item | Transcript citation | Done = |
|---|------|---|--------|
| T2.1 | **`lancelot.cost` ContextVar + per-request cost trailer** — tokens-in/out + upstream provider $/request accumulator, propagated like `request_id`. Emitted as `X-Lancelot-Cost-USD` response trailer and `lancelot.http.cost.usd` metric. Lets users build usage-based billing on top (Sim.ai pattern) without Lancelot touching AI logic. | Sim.ai (lines 9056–9358): "Sim monetizes on AI compute consumption rather than a per-seat or per-workspace subscription". | New ContextVar + trailer + metric; smoke: a handler that does one upstream LLM call sees `X-Lancelot-Cost-USD` populated. (Coding ~2 h.) |
| T2.2 | **Per-upstream-provider RED + structured error taxonomy** — extend `TransportPool` to emit `lancelot.upstream.{provider,model}.{rate,errors,duration,ttft}` with structured error class (`transient`, `permanent`, `timeout`, `quota`, `upstream_5xx`). Gives operators an AI-gateway dashboard with no AI-side dep. | Commure (lines 9852–10356): "payer APIs just work 80% of the time"; "I almost see it as like a cybersecurity problem. Every denial is something broke…We have to debug what went wrong". | Metrics emitted per provider; smoke: kill upstream → `lancelot.upstream.{provider}.errors{class=upstream_5xx}` increments. (Coding ~2-3 h.) |
| T2.3 | **Fleet-rollup `/fleet/status` aggregator + region-tagged OTel resource** — per-process emits `region` + `worker_id` in OTel `resource.attributes` at startup (env: `LANCELOT_REGION`, `LANCELOT_WORKER_ID`). `/fleet/status` endpoint surfaces N-worker health as one stream. | Conductor (lines 1886–2167): "head of engineering layer aggregates status across all running agents, surfaces exceptions and decisions that need human input". | `LANCELOT_REGION=eu-west-1` baked into OTel resource; `/fleet/status` returns per-region health for the multi-process fleet. (Coding ~2 h.) |
| T2.4 | **Cardinality-bounded per-tenant metric labels** — `tenant_id` label is bounded (configurable `LANCELOT_TENANT_LABEL_MAX=1000`; over-cap → bucket to `_overflow_`). Prevents metric explosion at high tenant count. | Hightouch (lines 15494–15774): per-customer propensity scoring at scale; the canonical OTel anti-pattern is unbounded tenant labels. | Config knob + test: 2000 tenants → 1001 series emitted (1000 + overflow bucket). (Coding ~1-2 h.) |
| T2.5 | **Adverse-path observability completeness** — RED + TTFT + traceparent + request-id must emit on cancel / timeout / 5xx / abort, not just success. Audit existing emit points; close gaps. | HappyRobot (lines 13176–13466): "every offer is captured… every rejection is recorded alongside its reason if stated". | Audit checklist + regression test: client disconnect mid-request → metric still emits with `status=499` + duration measured at disconnect. (Coding ~1-2 h.) |
| T2.6 | **Multi-region session-affinity verification** — `Response.set_session_affinity` (M2 Tier-C) is KV-cache-aware-routing today; M7 ships a multi-region deploy.md sister doc + real-server test proving the same affinity key lands on the same worker across a multi-region fleet. | HappyRobot: "If agent A is getting a strong offer… at the same moment agent B is talking to carrier Y, agent B knows in real time" (cross-agent shared state requires affinity to survive). | Test + doc; deploy recipe with N regional fleets + nginx/HAProxy ip_hash by affinity header. (Coding ~2 h + bench ~1 h.) |

**Done = (T2):** All 6 items shipped; smoke tests green for cost trailer, per-upstream RED, fleet aggregator, cardinality cap, adverse-path emit, and multi-region session affinity.

---

### Track T3 — Multi-region plumbing + secrets + identity

**Goal:** Make the multi-region deploy that M6 doesn't yet ship a documented + tested path, with rotate-aware secrets and an agent-identity ContextVar (Infisical's 3rd actor model).

| # | Item | Transcript citation | Done = |
|---|------|---|--------|
| T3.1 | **Multi-region deploy recipes** — extend M6's `benchmarks/m5/exit_gate/` with three new recipes: (a) Render multi-region (`render.yaml` multi-region service), (b) AWS multi-AZ via ECS or EC2 ASG, (c) Fly.io multi-region. Each: deploy script + nginx/proxy config + smoke + 1-hour cross-region soak. | Infisical (lines 14963–15111): "Infisical is actually stateless. You can replicate across because all the containers are stateless… state lives in a backing external datastore" — the stateless-app + external-state pattern enables trivial multi-region. Veriff (lines 7297–7798) implies global multi-region by definition. | Three deploy recipe directories under `benchmarks/m5/exit_gate/multi_region/{render,aws,fly}/`; each smokes + 1h soak green; results in `benchmarks/results/M7/multi-region/`. (Coding ~4-6 h; wall clock ~3 h × 3 deploys.) |
| T3.2 | **`lancelot.secrets` lazy provider** — pluggable backends: env (default), Vault, Infisical, AWS Secrets Manager. Rotate-on-SIGHUP semantics: re-read secrets without restart. Audit hook fires on every secret access. | Infisical: "injected at startup (not hardcoded); rotated without service downtime; auditable". | New module + 4 backends + smoke test for SIGHUP rotation; runtime-dep stays pydantic-only (each backend lazy-loaded). (Coding ~4-5 h.) |
| T3.3 | **Agent-identity ContextVar** — `lancelot.context.agent_id` ContextVar alongside `request_id` + `trace_id`. Populated from configurable header (default `X-Agent-Id`). Emitted in logs + OTel attrs. | Infisical: "AI agent, which also needs access to resources and even needs to be able to trust and talk to each other" (3rd actor type alongside humans + machines). | New ContextVar + header parsing + test that asserts isolation across 200 concurrent agent requests. (Coding ~1-2 h.) |
| T3.4 | **mTLS toggle on `TransportPool`** — cert-path + key-path config on outbound calls; rotate-on-SIGHUP for cert paths; clear error on cert mismatch. Defaults off (opt-in for service-mesh deploys). | Infisical: "Infisical PKI — certificate management… Infisical KMS — encryption-as-a-service". | Config + test: TransportPool with mTLS configured calls a self-signed-mTLS upstream successfully; SIGHUP rotates cert without dropping in-flight requests. (Coding ~2-3 h.) |
| T3.5 | **Region-aware route registration** — `@app.get("/api/x", region="eu-west-1")` annotation; route resolver respects current `LANCELOT_REGION` to prefer local-region handlers. Failover to nearest if local is down. | Veriff: "always use the best tool available for a given jurisdiction… not a single global flow that would produce degraded accuracy". | Annotation + resolver + regression test: 2 routes registered with different regions; resolver picks correctly based on env. (Coding ~2-3 h.) |

**Done = (T3):** 3 multi-region deploy recipes (Render / AWS / Fly.io) with passing smoke + 1h cross-region soak; secrets provider with 4 backends + SIGHUP rotation; agent-identity ContextVar + mTLS toggle + region-aware routing all functional.

---

### Track T4 — Rust hot-path discipline (added 2026-05-24; sourced from 8 Rust-project teardowns)

**Goal:** apply Rust patterns extracted from 8 Rust-project teardowns (Polars / Lakesail / Ripgrep / Turbopuffer / Datafusion / LanceDB / Tauri / Zed) that directly inform the Lancelot byte-path. **Full pattern catalog with `file:line` citations:** [`docs/peer-source-recheck/M7-RUST-PATTERNS.md`](../peer-source-recheck/M7-RUST-PATTERNS.md).

| # | Item | Source citation | Done = |
|---|------|---|--------|
| T4.1 | **`cfg(test)` buffer-size = 1 on every bounded channel.** Force backpressure code path under every unit test. Zed ships this exact pattern: `INCOMING_BUFFER_SIZE = 256` in prod, `1` under `cfg(test, feature = "test-support")`. Would have caught 4 of M4's broken-under-load features. Apply to `LANCELOT_STREAM_BUFFER`, every internal mpsc Lancelot creates. | ZED.md:6157-6165 | Existing channels audited; new channels have `#[cfg(test)] const BUFFER: usize = 1;`. (Coding ~1-2 h.) |
| T4.2 | **`select_biased!` + `util::defer` Drop-guard for graceful shutdown.** Replace `serve()`'s ad-hoc `select!` with `tokio::select! { biased; shutdown_signal => …, accept_loop => … }` so SIGTERM-priority is compile-time-guaranteed. Wrap per-connection IO future in `scopeguard::defer!` that drops pending oneshot senders → all awaiters get `Cancelled` immediately. M4 V1.4 was the 3rd broken feature; this makes the invariant impossible to forget. | ZED.md:4826-4861 + 10994-11030 | `serve()` uses `biased;` ordering; per-conn IO futures wrap state in `scopeguard::defer!`; regression test exercises the SIGTERM-priority path. (Coding ~2-3 h.) |
| T4.3 | **Asymmetric backpressure on T7.x distributed channels.** Outgoing = unbounded (app code never blocks); incoming = bounded (peers feel backpressure). Pair with a write-timeout (`LANCELOT_WRITE_TIMEOUT_MS`) so unbounded outgoing doesn't OOM on partial network partition. | ZED.md:4803-4817 | New T1.1 `spawn_child` + T1.4 MCP channels use the pattern; write-timeout env knob shipped. (Coding ~1-2 h.) |
| T4.4 | **`ForegroundWork` dispatch queue for `lancelot.mcp` adapter (T1.4 implementation pattern).** Send-bg tasks post `ForegroundWork` enums onto an `mpsc::UnboundedSender`; one foreground task drains onto the asyncio loop with GIL. Matches Zed's ACP `acp.rs` and the R3 on-loop `_drain` design exactly (one bridge crossing per stream, not per call). | ZED.md:10283 (acp.rs:620-699) | T1.4 (`lancelot.mcp`) implemented this way; not a separate item, but the design constraint must be honored. (Constraint; no additional coding beyond T1.4.) |
| T4.5 | **Triple-channel RPC model for T1.1 `spawn_child` distributed primitive.** Outgoing unbounded mpsc + response oneshot for unary + stream mpsc for server-streaming, all keyed by request id. Direct model for any M7+ cross-node primitive. | ZED.md:4729-4772 (peer.rs) | T1.1 `spawn_child` implementation follows this shape; design constraint. (No additional coding; integrated into T1.1.) |
| T4.6 | **Per-tenant fair `MemoryPool` + named-consumer OOM error.** `MemoryPool` trait + RAII `MemoryReservation` (Drop calls `free()`); on `ResourcesExhausted` the error names the top consumer ("OOM caused by route=/v1/embed (tenant=acme, 380MB)"). Pairs with T2.4 cardinality cap. | DATAFUSION.md:2058-2265 | New `lancelot.memory_pool` module + `TrackConsumersPool` wrapper; integrates with M7 multi-tenant story; failing-tenant OOM names route + tenant. (Coding ~4-6 h.) |
| T4.7 | **`tokio::task::coop::consume_budget` in CPU-heavy Rust paths called from handlers.** Any handler-callable Rust function that loops over MB-size payloads (canonical-JSON, large response framing) calls `tokio::task::coop::consume_budget` once per N iterations to surrender the executor mid-batch. Avoids holding the tokio worker for many ms on big payloads. | DATAFUSION.md:1301-1318 | `src/serialize.rs::to_json_bytes` (and any new bulk-iteration Rust path) calls `consume_budget` per 1k iterations; benchmark shows no regression on small payloads + improved fairness on large. (Coding ~1-2 h.) |
| T4.8 | **`queue_wait_ms` metric + per-tenant concurrency cap.** Emit `lancelot.http.queue_wait_ms` (total_in_handler − pure_handler_compute) alongside the existing duration histogram. Add `LANCELOT_TENANT_CONCURRENCY` per-tenant semaphore. The missing third number after RED + TTFT for multi-tenant AI backends. | TURBOPUFFER.md:6680-6712 | New metric in `lancelot/metrics.py`; per-tenant semaphore in middleware; T2.4 cardinality cap extends to this label. (Coding ~2-3 h.) |
| T4.9 | **`x-should-retry: true/false` server-controlled retry header.** Lets handlers tell SDKs "this 500 is permanent" or "this 4xx is transient lock-conflict, retry me." Pairs with `@app.batch` to surface upstream-LLM backpressure to clients without thundering-herd retries on real outages. | TURBOPUFFER.md:858-906 + 6733-6770 | New `Response.set_retry_hint(should_retry: bool)` helper; emit header; document in operator runbook + agents.md. (Coding ~1-2 h.) |
| T4.10 | **Levenshtein "did you mean?" on 404 — lazy in `fmt::Display`.** Iterate registered matchit paths inside the 404 error's Display impl; suggest closest match. Pays nothing on caught errors; massive dev-UX win at near-zero size. | LANCEDB.md:4076-4108 | `src/dispatch.rs` 404 body suggests closest route name when path doesn't match; `<50` LOC; smoke test shows the suggestion. (Coding ~1 h.) |
| T4.11 | **KeyboardInterrupt-aware 100ms poll in blocking `_native.run()`.** Periodically `Python::with_gil(\|py\| py.check_signals())` and abort the runtime on `Err`. Today Ctrl+C in dev relies on tokio's signal handler reaching us; adding the explicit GIL re-check makes dev feel identical to FastAPI/uvicorn. | LANCEDB.md:2569-2607 (executor.rs:88-115) | Native `run()` loop polls `check_signals` every 100ms; Ctrl+C in `python -c "lancelot.run(...)"` exits within 100ms. (Coding ~30 min.) |
| T4.12 | **`snafu` + `#[snafu(implicit)] location: Location` at the Rust↔Python boundary.** Replace ad-hoc `PyValueError::new_err("...")` sites with a typed `LancelotError` enum carrying auto-populated `location`. Operators get file:line on every traceback; pairs with W3C `traceparent` propagation. | LANCEDB.md:4023-4067 | New `src/error.rs` with `LancelotError` enum (snafu-derive); existing `PyValueError::new_err` sites migrated; tests assert `location` appears in error str. (Coding ~3-4 h.) |
| T4.13 | **`intern!(py, "name")` Python-string interning for hot attr lookups.** Cache PyString objects for `__anext__`, `body`, handler-call names → attribute lookup becomes pointer-equality instead of string compare. Microbench in `dispatch.rs` should show measurable improvement at high RPS. | LAKESAIL.md:854 | Hot path attr lookups in `src/dispatch.rs` + `src/ffi/request.rs` use `intern!`; criterion bench shows ≥5% improvement on /echo at c=50. (Coding ~1-2 h.) |
| T4.14 | **Per-worker `Pool<Vec<u8>>` scratch buffer for header staging.** Backed by `thread_local!` per tokio worker. Defers F3 "alloc past routing" to the handler path — eliminates per-request `Vec` allocs in header build-up. | RIPGREP.md:247 + 494 | New `src/scratch.rs` with `thread_local! Pool<Vec<u8>>`; response header writing borrows + returns the buffer; criterion bench shows ≥3% on the /echo path. (Coding ~2-3 h.) |

**Done = (T4):** All 14 items shipped with regression tests; criterion benches for T4.13 + T4.14 show measurable improvement; LancelotError migration completes ≥80% of existing PyValueError sites. The 4 patterns that integrate with other tracks (T4.3 / T4.4 / T4.5 / T4.6) gate the corresponding T1/T2 items.

**Estimation (T4 add to M7 total):** ~20-30 h coding (most items are small/medium; T4.6 + T4.12 are the larger ones); ~30 min wall clock for criterion benches. Brings M7 total to ~56-75 h coding (was 36-45 h pre-T4).

---

## Explicitly NOT in M7 (Tier-D — every reject is transcript- or teardown-sourced)

These were CONSIDERED for M7 (or implied by earlier framing) but the source reads explicitly reject them. **Cardinal rule: AI logic stays user-side; byte path only; no useless logic.**

| Tier-D item | Why NOT | Transcript / teardown citation |
|---|---|---|
| Custom work-stealing async executor (replace tokio) | Polars ships one; we'd give up hyper integration + add a maintenance burden for no measured win | POLARS.md:36 + 645-723 |
| Crossbeam-deque LIFO for parallel request dispatch | Right primitive for filesystem walks (Ripgrep), wrong for request dispatch (tokio's mpsc/JoinSet is the match) | RIPGREP.md:706-723 |
| Arrow C Data Interface for zero-copy Python↔Rust | Lancelot payloads are JSON/text bytes via pydantic-core/orjson, not Arrow batches; adding arrow dep violates pydantic-only runtime rule | LANCEDB.md:2609-2624 |
| `std::sync::LazyLock` for env-driven config touching Python | GIL × OnceLock deadlock memory ([[lancelot-gil-oncelock-deadlock]]); only safe when zero py-touch; use `PyOnceLock` everywhere Python-adjacent | LANCEDB.md:3607-3611 |
| zstd-compressed binary protocol inside `_native` | Compression is the proxy/CDN's job; CLAUDE.md Tier-D already says don't reimplement response compression | ZED.md:4663-4703 |
| Priority-tagged spawn (e.g. `Priority::RealtimeAudio`) | Out-of-scope for HTTP framework; tokio's single multi-thread + `spawn_blocking` is the right tool | ZED.md:6105 |
| Custom URL-scheme transport (`ipc://localhost/cmd`) | Browser/webview-specific; HTTP is already Lancelot's transport | TAURI.md:65-100 |
| Pluggable async-runtime indirection (tokio swappable) | Lancelot is tokio-only by design; the runtime-swap layer = useless abstraction with no user demand | TAURI.md:859-877 |
| Capability-based ACL pre-dispatch | Lancelot's auth = user-side middleware; building an ACL = AI-logic-boundary violation + reinventing FastAPI dependencies | TAURI.md:280-310 |
| Per-session HMAC anti-CSRF token in framework | Browser-side problem; HTTP framework's CSRF surface is cookie SameSite + same-origin policy | TAURI.md:1490-1502 |
| SaaS control plane (multi-tenancy + ACLs + billing) | Lancelot is a framework, not a SaaS — defer to user-side. T4.6 ships the per-tenant memory primitives without the control plane | LANCEDB.md:2921-2964 |
| S3 byte-range fetches with SSTable metadata | AI logic / storage layer; not the HTTP byte path | TURBOPUFFER.md:160-171 |
| Multi-AI-gateway abstraction (Vercel AI Gateway / Portkey / OpenRouter front) | Every customer-validated LLM app uses a single model family; switching cost is calibration upstream of transport. (Original M7 framing had this; transcripts rejected it.) | Parahelp, Magic Patterns, Giga (see YC RA transcript ranges in plan) |
| Framework-side judge / sampling-N-traces orchestration | AI logic; user-side. T1.1 spawn_child + T1.2 fanout are the byte-path primitives user code uses. | Parahelp |
| Durable workflow / job-queue (snapshot/restore, retry, idempotency at framework level) | Orchestration = user-side. Trigger.dev / Hatchet / Temporal own this category. | Parahelp, Trigger.dev |
| Voice multi-channel (per-speaker isolated channels on one HTTP/2 connection) | We're http1-only by design (M3 hardening); multi-channel is an upstream/voice-stack concern | David AI (YC RA) |
| Framework-level AI-eval pipeline | AI logic; user-side. CoCounsel's per-prompt eval lives in product code; Lancelot exposes the metrics they need (T2.x). | Jake / CoCounsel |
| Customer-supplied plain-text NL-config that the framework interprets via LLM | Same: AI logic. Reducto's product, not its framework. | Reducto |
| Multi-tier agent orchestration ("AI Worker" / "Frontal" / etc.) | Same: orchestration is user-side | HappyRobot |
| LLM-classification taxonomy / domain engines | Same: AI-application pattern | Numeral / Sphere |

The pattern: **Lancelot's role is to make the byte path under each of these AI patterns fast, observable, and reliable** — not to absorb the AI logic into the framework. Both the YC Root Access transcripts AND the 8 Rust-project teardowns make this distinction visible at section-level.

## How each M7 item is validated

Every item ships with: (1) a **real harness** (subprocess + HTTP for transport primitives; real deploy for multi-region), (2) the metric / behavior it must hit, (3) a regression test exercising the path the bug would live on, (4) raw evidence in `benchmarks/results/M7/` where applicable, (5) all gates green at each chunk commit (`pytest tests/ -q` + `make m1-conformance` ≥ 974 + `cargo clippy --release --all-targets -- -D warnings`).

## Sequencing

1. **T1.4 `lancelot.mcp` adapter + T1.5 minimal-return doc** can start immediately — independent of TransportPool changes. T4.4 (ForegroundWork dispatch queue) is the implementation constraint.
2. **T1.1 spawn_child → T1.2 fanout → T1.3 LLM-aware OTel attrs** — chain (each depends on TransportPool internals from previous). T4.5 (triple-channel RPC) constrains T1.1; T4.3 (asymmetric backpressure) constrains both.
3. **T2.1 cost ContextVar + T2.3 fleet rollup + T2.4 cardinality cap + T2.5 adverse-path emit** — independent, parallel-safe.
4. **T2.2 per-upstream RED** — gates on T1.3 (LLM-aware OTel attrs).
5. **T2.6 multi-region session-affinity verification** — gates on T3.1 (at least one multi-region recipe deployed).
6. **T3.2 `lancelot.secrets` + T3.3 agent-identity ContextVar + T3.4 mTLS + T3.5 region-aware routing** — independent, parallel-safe.
7. **T3.1 multi-region deploy recipes** — gates on T3.2 (secrets needed for cross-region credential rotation) + T3.5 (routing). Render first (M5/M6 precedent), AWS multi-AZ, Fly.io.
8. **Track T4 items**:
   - T4.1 (`cfg(test)` buffer=1), T4.10 (Levenshtein 404), T4.11 (Ctrl+C poll), T4.13 (`intern!`), T4.14 (scratch buffer) — all parallel-safe; pure local hot-path wins. Can land anytime in the M7 sequence.
   - T4.2 (`select_biased!` + Drop-guard) — gates the existing `serve()` graceful-drain audit; land BEFORE any new multi-region deploy lands to keep graceful drain robust.
   - T4.6 (per-tenant `MemoryPool`) — gates T2.4 (cardinality cap) extension to memory dimension.
   - T4.7 (`consume_budget`) + T4.12 (snafu errors) — independent.
   - T4.8 (`queue_wait_ms`) + T4.9 (`x-should-retry`) — independent.
9. **M7 exit tag** — all T1/T2/T3/T4 items shipped with smoke + soak green; doc committed; ANALYSIS.md showing the distributed-trace + cost picture for the multi-region deploy.

## Estimation summary

| Track | Coding (agentic) | Wall-clock-bound | Notes |
|---|---|---|---|
| T1 outbound primitives + MCP | ~12-15 h | ~30 min smoke | TransportPool changes are the biggest unknown |
| T2 distributed observability | ~10-12 h | ~30 min smoke | All 6 items relatively bounded |
| T3 multi-region + secrets + identity | ~14-18 h coding | ~9-12 h wall clock (3 deploys × 3h each incl. provisioning) | Multi-region deploys are the wall-clock long pole |
| T4 Rust hot-path discipline | ~20-30 h coding | ~30 min criterion bench | 4 items (T4.4/T4.5/T4.3/T4.6) integrate into other tracks; rest are independent |
| **Total** | **~56-75 h coding** | **~10-13 h wall-clock (with parallel deploys)** | Larger than M6 (~16-26h); ~5-7 days sequential, ~3-4 days parallel |

## Definition of "no bullshit" (unchanged, restated)

Every item: cites a transcript pattern that motivates it; ships a byte-path primitive (never AI logic); has a metric / artifact it must produce; is exercised under real load or real deploy. The "rejected" Tier-D items above are also transcript-sourced — every reject quotes the section that proves Lancelot shouldn't ship it.

## Adoption flywheel (continuation of M6 T3 work)

M6 ships agents.md + MCP docs + code-first README. M7 extends the LLM-recommendable surface:

- T1.4 + T1.5 make Lancelot's `@app.tool` routes auto-discoverable by Cursor / Claude Code via MCP — matches the Trigger.dev "two users now" pattern + the Mnemonic + Letter AI MCP integrations.
- T3.2 `lancelot.secrets` removes a friction point in agent-built code: agents recommending Lancelot can suggest a secrets backend that Just Works instead of telling users to wire env vars manually.
- T2.1 cost ContextVar makes agent-built billing-on-top trivial — Sim.ai's "monetize on AI compute consumption" becomes a one-liner on Lancelot.

---

**Cross-references:**
- **Rust patterns catalog (added 2026-05-24):** [`docs/peer-source-recheck/M7-RUST-PATTERNS.md`](../peer-source-recheck/M7-RUST-PATTERNS.md) — full citations for the 14 Track-T4 ADOPT items + 14 VALIDATEs + 12 AVOIDs extracted via 4 parallel agents from 8 Rust-project teardowns (Polars, Lakesail, Ripgrep, Turbopuffer, Datafusion, LanceDB, Tauri, Zed; ~78k source lines).

- M5 plan (committed): `docs/plans/M5-PLAN-PROVE-VS-PEERS-AND-ADOPTABLE.md`
- M6 plan (committed, unchanged by M7 work): `docs/plans/M6-PLAN-SPEED-STABILITY-ADOPTION.md`
- M5 lab-body results: `benchmarks/results/M5/ANALYSIS.md`
- M5 exit-gate scaffolding (extended in T3.1): `benchmarks/m5/exit_gate/`
- M5 peer source recheck: `docs/peer-source-recheck/M5-peers.md`
- YC Root Access transcripts (source of every M7 item): `researchfms/core/TRANSCRIPTS_YC_ROOT_ACCESS.md`
- Honest perf envelope + Tier-D do-not-do list: `CLAUDE.md`
- Estimation discipline: [[agentic-coding-timing]]
