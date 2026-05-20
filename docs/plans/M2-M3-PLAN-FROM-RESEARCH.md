# Lancelot M2/M3 Plan — Distilled from researchfms/ Teardowns

**Date:** 2026-05-20
**Source:** 5 parallel research agents (Robyn / FastAPI / Blazeio / core/ / teardowns/) — see git log for invocations.
**Anchor rule (user-restated):** *no bullshit, no useless logic.* Every item below is something at least one teardown calls a measurable win OR a wire-correctness gap; nothing speculative.

## Positioning (locked, do not drift)

Lancelot is a **backend framework**, like FastAPI / Robyn — it optimizes HTTP transport / serialization / streaming for AI / cloud / web / startups. **AI logic lives on the user side** (LangGraph, agent loops, vector stores, prompt orchestration). We do not own the agent. We own the byte path.

## Tier-A — wire correctness & cheap latency wins (≤1 day each)

These are the no-brainers: low effort, ≥2 independent sources confirm the gain or correctness.

| # | Item | Effort | Source | Why |
|---|------|--------|--------|-----|
| A1 | `TCP_QUICKACK` (Linux only, graceful fallback) on the hyper listener | 30 min | Blazeio `socket_ops.py:23`, teardowns table | Eliminates 40 ms delayed-ACK on pre-4.5 kernels. Two lines of Rust. |
| A2 | `TCP_FASTOPEN(5)` + `SO_REUSEPORT` knobs on the listener | 1 h | Blazeio + Robyn `shared_socket.rs:24` | 0-RTT for repeat clients, kernel-level multi-worker LB. Opt-in flags. |
| A3 | JSON max-nesting-depth = 128 in Pydantic body parse | 30 min | Robyn `request.rs:271`, teardowns table | Stops nested-bomb DoS. Configurable. |
| A4 | `/livez` + `/readyz` auto-registered (FastAPI doesn't) | 1 h | core 02_STRUCTURE 200-230 | K8s table-stakes. /livez = process alive; /readyz = lifespan-started AND in-flight queue under threshold. |
| A5 | SSE auto-emit `X-Accel-Buffering: no` + keepalive comment every 15 s on idle | 1 h | FastAPI `routing.py:590`, Robyn SSE, teardowns 2.2 | Survives nginx/CDN buffering; prevents proxy 60-s timeouts on slow LLMs. |
| A6 | `stream_interval` knob on `@app.stream` to batch N tokens per SSE event | 2 h | vLLM `VLLM.md:774` | Real CPU saving on token-by-token streams; default 1 (unbuffered) keeps current behavior. |
| A7 | Verify `Content-Length` is OMITTED on chunked / SSE; assert no body on 204/205/304/1xx with a wire-byte test | 1 h | RFC 7230 / 7232; teardowns 6 | Easy regression-guard; cheap. |

## Tier-B — observability & resilience (1-3 days each, table-stakes for cloud)

| # | Item | Effort | Source | Why |
|---|------|--------|--------|-----|
| B1 | Graceful shutdown: SIGTERM → stop accept, drain in-flight (bounded), exit. Surface `app.shutting_down` flag for /readyz. | 2 d | core 02_STRUCTURE 200-230, Robyn anti-pattern | Robyn hard-kills (loses streams). Lancelot must do better — that's the differentiator on the cloud-native axis. |
| B2 | OTel RED auto-emit: per-route counter (req_total), error counter, histogram (req_duration_seconds). Existing `gen_ai.cost` event stays. | 2 d | core 06; teardowns Phase-3 | We already ship `gen_ai.cost`. Add framework-level RED so the user gets dashboards out of the box. |
| B3 | Per-request ContextVar propagation across middleware phases (Py 3.11+, fallback on 3.10) | 1 d | Robyn `asyncio.rs:14-49` | Trace-ID / user-ID / span-ID must reach handler + middleware + logs cleanly. We don't currently isolate. |
| B4 | Connection limits + 503 (Retry-After) when over threshold (we already 503 on PoolFull — extend to a global connection cap) | 1 d | Blazeio backpressure, core 04_UNDER_THE_HOOD 350-400 | Slow-loris defense already partially in. Close the loop with a global cap. |
| B5 | `header_read_timeout` is shipped (R6). Add **per-route timeout** (decorator kwarg `timeout=30.0`) → handler is cancelled past deadline; client gets 504. | 1 d | core 04 | Robyn doesn't have per-route timeouts; FastAPI doesn't. Pure differentiator. |

## Tier-C — AI-specific primitives (3-7 days each)

These are the things that justify the "AI-optimized backend" pitch. Keep AI **logic** out — we ship transport for it.

| # | Item | Effort | Source | Why |
|---|------|--------|--------|-----|
| C1 | KV-cache-friendly response helpers: deterministic JSON key ordering (sorted) + session-affinity header (`X-Session-Affinity`) routed via sticky load-balancer hash. NOT a cache — a hint. | 3 d | core 06_AGENTIC + Manus + Claude-Code teardowns | AI startups burn money on cache misses. Helping the user keep cache hits ≠ implementing caching. |
| C2 | Streaming-cancellation propagation: when `request.cancelled` flips (client disconnect, already shipped), the user's async-gen sees it on the next `await` AND we cancel any in-flight `lancelot.llm.TransportPool` calls bound to this request. | 2 d | core 06; M2-P4 already lays foundation | Stops $$ on un-watched tokens. We have `request.cancelled`; this finishes the wiring. |
| C3 | `@app.tool` polish: auto-derive `output_schema` from return-type annotation (we already do `input_schema`); validate Anthropic/OpenAI/Google tool-call shapes round-trip cleanly. | 2 d | core 08_STARTUP_AI + teardowns 4.1 | Tools are the I/O contract for agents — Lancelot owning this contract is a moat. |
| C4 | Dynamic writer dispatch in `@app.stream`: at first chunk, bind `write` to `write_sse` / `write_chunked` / `write_raw` based on response content-type — remove per-chunk branching. | 1 d | Blazeio `server_protocol_extratools.py:306-341` | 1–3 % throughput win on streaming, code gets clearer. |
| C5 | `@app.batch` decorator for prefix-aware batching of LLM requests (group requests with the same system prompt for one upstream call). NOT inference; just request grouping. | 5 d | core 06_AGENTIC + TGI radix-prefix teardown | Tier-C bet: this is what AI startups would actually pay for, and FastAPI has no equivalent. |

## Tier-D — explicitly skip (the anti-bullshit list)

These are tempting; teardowns flagged them as not-worth-it for our positioning.

- **Custom JSON serializer (orjson/ujson)**: Pydantic v2 `TypeAdapter.dump_json` is already Rust-core fast. FastAPI's own ledger says deprecated. We stay on Pydantic.
- **Callback / webhook surface in OpenAPI**: rare, adds complexity. Defer.
- **Dependency-overrides per-request rebuild**: useful only for tests. Keep our simple form.
- **Built-in CLI (`lancelot run …`)**: FastAPI delegates to fastapi-cli — separate package. Users invoke uvicorn / hypercorn / our own runner. Not core.
- **Replacing AI logic** (agents, vector stores, prompt templates): user-side. We never grow this surface. *(this is the user's rule, restated for future Claude sessions)*
- **Robyn-style hard-kill shutdown**: explicit anti-goal — see B1.
- **Per-chunk Pydantic validation in streaming**: kill any path that does this; streaming is raw-bytes + lightweight typing only.

## Verified-already-shipped (do not re-implement)

These came up as "consider stealing" in the agents' reports but Lancelot already has equivalent or better:

- Const-tier routing (R1, Python-side; we are ALREADY where Robyn's ConstRouter is)
- `catch_unwind` FFI panic isolation (R7) — Robyn doesn't have this
- `header_read_timeout(15s)` + TokioTimer (R6) — slow-loris defense
- Pydantic v2 `TypeAdapter.dump_json` response path (no custom JSON)
- 422 byte-exact validation-error shape (90 % FastAPI conformance verified)
- `@app.stream` SSE + `@app.tool` schema + MCP server (M2-P5)
- `lancelot.gather` + `lancelot.llm.TransportPool` + OTel `gen_ai.cost` (M2-P1..P4)
- `Request`/`BackgroundTasks`/`WebSocket`/`Response`/`SecurityScopes` magic-inject (M1-EXIT this session)
- AsyncExitStack request-vs-function scope ordering (M1-P2 B4)

## Sequencing recommendation

1. **Land Tier-A first** (1 sprint, 7 items, ~1 week). They're all guard-rails / cheap latency. Tag `m2-tierA`.
2. **Tier-B next** (1.5 sprints). B1 (graceful shutdown) blocks production-grade claims; do it first within tier.
3. **Tier-C is the M3 narrative**. C5 (`@app.batch`) is the marquee.
4. Tier-D is a *don't-do* list — link to it from CLAUDE.md so future agents don't burn cycles on it.

## Definition of "no bullshit"

Every item above:
- has a file_path:line_number citation in at least one teardown OR is a verified-this-session gap, AND
- changes the *byte path* or the *operator experience*, not the AI logic, AND
- is small enough (per-tier budget) that a single developer can land it without ceremony.

If a future item fails any of those three, it does not belong on the Lancelot roadmap.

---

**Cross-references (for future sessions):**
- Per-agent full reports: see git log around 2026-05-20.
- Lancelot M0 status: [[lancelot-m0-status]]
- Lancelot M1 progress: [[lancelot-m1-progress]]
- Tagged at 90 %: `m1-exit-90`.
