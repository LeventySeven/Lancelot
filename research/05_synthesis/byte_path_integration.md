# Byte-Path Integration — the definitive REUSE / EXTEND / BUILD-ON-TOP / SKIP brief

> **Layer:** integration / synthesis (the capstone that ties the 8 agent-layer briefs to Lancelot's existing byte path).
> **Altitude:** "just build agents, brutal." This brief decides, per agent-framework primitive, *what already exists vs. what an agent layer adds* — and sketches the module map for a hypothetical `lancelot.agents` package.
> **Date:** 2026-06-22
> **Status:** exploratory artifact, finished for completeness then parked. No build is authorized by this document; it is the map a future build would start from.
> **Cardinal rule honored throughout:** runtime dep is `pydantic>=2.7` only. Every provider SDK, `sqlite-vec`, `json5`/`json_repair`, Redis, Postgres driver is **lazy-imported** behind a `try/except` (the existing `lancelot.secrets` Vault/Infisical/boto3 pattern, `lancelot_serve_tools.md §7`). `import lancelot.agents` must never pull `httpx`, `opentelemetry`, `tiktoken`, etc.
> **Sources:** the 8 layer briefs in `research/05_synthesis/{loop,model,tools,context,memory,evals,streaming,durable}.md`; the 2 byte-path inventories `research/04_byte_path/{lancelot_model_io,lancelot_serve_tools}.md`; verified against direct reads of `lancelot/{llm,context,canonical,upstream_metrics,tool,app,stream,batch,net,secrets,tenant,ratelimit,webhook,telemetry,metrics}.py`.

---

## 0. The one-paragraph finding

**Lancelot has already built the entire transport / serialization / streaming / resilience half of an agent framework, and it is the half that pure-Python frameworks (Pydantic AI, LangGraph, Letta, Mastra) cannot match because it rides a GIL-released Rust byte path.** Across the ten canonical agent primitives, the split is decisive: the **plumbing is ~70% REUSE-as-is** (the outbound LLM call, streaming pump, cost/trace accounting, tool registration+validation, SSRF/secrets/rate/tenant infra, canonical-JSON cache key, batch coalescing, fanout, spawn_child), a **small slice is EXTEND** (add three ContextVars, enforce the two unenforced `ToolSpec` fields, add `agent.*` OTel histograms, add `ephemeral_1h/5m` cache fields), and the genuinely missing work is the **agent *brain* — BUILD** (the loop, the model-shape adapter, context assembly+compaction, typed memory, evals, the durable seam, the safety gate). The marquee structural fact, repeated in every brief: **an agent run and a prod run are the same code on the same byte path** — so the agent layer is *thin glue + state machines*, not a re-platforming. The byte path is the moat; the agent layer is the product surface on top of it.

The build target is a new package **`lancelot.agents`** (note: NOT `lancelot.memory_pool`, which is byte-OOM reservation, a different layer flagged in every memory-mentioning teardown — `memory.md §2`). It is ~3,000–4,000 LoC of pure-Python state-machine + adapter logic, zero new runtime deps.

---

## 1. The definitive table — every agent primitive, classified

Legend:
- **REUSE** = byte path provides it; agent layer calls it, builds nothing.
- **EXTEND** = byte path has the mechanism but it needs a small, additive widening (a field, a ContextVar, a histogram) — no rewrite.
- **BUILD-ON-TOP** = pure-Python composition of existing primitives; no new transport, no new dep.
- **BUILD** = genuinely missing; the agent layer's real work.
- **SKIP** = off-thesis / depreciating / overkill / user-side; do not build.

### 1.1 Loop (ReAct, StopReason, budget, stuck-detect)

| Sub-primitive | What Lancelot already provides | What the agent layer adds | Verdict | Cite |
|---|---|---|---|---|
| The model call inside the loop | `TransportPool.request()` — retry, **POST never retried**, Retry-After, budget cap, cancel, Idempotency-Key, traceparent | thread `request=req` for disconnect→cancel | **REUSE** | `lancelot_model_io.md §1.1`; `loop.md §11` |
| Cost/token budget reads | `cost_usd` / `tokens_in` / `tokens_out` ContextVars (accumulate automatically) | per-step delta = snapshot `cost_usd.get()` before/after (no new var) | **REUSE → BUILD-ON-TOP** | `lancelot_model_io.md §3.1`; `loop.md §3` |
| Error-branch selector | `classify_status` → `ok/quota/timeout/permanent/transient/upstream_5xx` (422→permanent = malformed tool call) | map taxonomy to retry/escalate/bail branches | **REUSE** | `lancelot_model_io.md §2.3`; `loop.md §7` |
| The `~150-line` recursion (assemble→call→dispatch→recurse) | — | the loop itself | **BUILD** | `loop.md §1` |
| `StopReason` typed enum (11-value intersection) | — | the termination taxonomy callers branch on | **BUILD** | `loop.md §2` |
| Multi-axis `Budget` + grace-call | reads existing ContextVars; Anthropic `task_budget` is one JSON field on the existing `request()` | the priority-ordered budget + graceful nudge | **BUILD** (constants) + **REUSE** (the call) | `loop.md §3`; `claude_code.md §7` |
| Stuck detector (counter-based: 4/3/3/6) | — | OpenHands thresholds, terminal `StopReason.stuck` | **BUILD** | `loop.md §4`; `autonomous_coding.md §1` |
| Within-turn tool dispatch (ordered, parallel-capable) | `TransportPool.fanout(first_n,deadline,cancel_laggards,concurrency)` for cross-model fan-out; `asyncio.gather` over `app.call_tool` for tools | re-sort completion-order → call-index order | **REUSE** (fanout) + **BUILD-ON-TOP** (re-sort) | `loop.md §7`; `lancelot_model_io.md §1.7` |
| Hook registry (the ONE extension seam) | `telemetry.set_sink`/`emit` for spans; metrics lazy-OTel pattern | the 5-hook typed registry (before/after model, before/after tool, on_finish) | **BUILD** | `loop.md §8`; `lancelot_serve_tools.md §5` |
| Tool-rule solver + mode-gate (both opt-in, off) | — | `InitToolRule`/`TerminalToolRule`; `planning_interval` | **BUILD (opt-in)** | `loop.md §9, §10` |
| Graph DSL for a linear loop / `while True` with no ceiling | — | — | **SKIP** | `loop.md §12`; `langgraph.md §3.2` |

### 1.2 Model adapter (translation, routing, failover)

| Sub-primitive | What Lancelot already provides | What the agent layer adds | Verdict | Cite |
|---|---|---|---|---|
| HTTP body bytes, auth, pool, TLS/mTLS | `TransportPool` (retry w/ **jitter present** `llm.py:481`, Retry-After, budget cap) | nothing | **REUSE** | `model.md §0`; `lancelot_model_io.md §1` |
| Token/cost/error taxonomy across 3 provider shapes | `upstream_metrics.{extract_tokens,calc_cost_usd,classify_status,detect_provider}` | use `classify_status` as the **failover branch selector** | **REUSE** | `model.md §2`; `upstream_metrics.py:156` |
| Message-shape translation (OpenAI-canonical → Anthropic `messages`+`system` / Gemini `contents`) | — | `ProviderProfile` dataclass + `transform_request/response` | **BUILD** | `model.md §1.1`; `hermes_gateway.md §1` |
| `init_model("provider:model")` string router | `secrets.get()` for keys; speaks HTTP directly (no SDK import — a Lancelot *advantage*) | string-split + lazy profile lookup | **BUILD** | `model.md §1.2`; `langchain.md:31` |
| Error-typed cross-model failover | `classify_status` taxonomy is the gate; `dumps_canonical` + session-affinity preserve cache across same-family hops | ~40 LoC profile-list walk (same-family first, return PRIMARY error) | **BUILD-ON-TOP** | `model.md §1.3`; `devin_warp.md §1` |
| `AuxiliaryClient` (cheap sidebar, separate cost bucket) | `TransportPool` + `upstream_metrics` `llm.class` label | thin client bound to `default_aux_model` w/ distinct cost label | **BUILD-ON-TOP** | `model.md §1.4`; `hermes_core.md §4` |
| Per-provider tool-schema sanitization (Anthropic nullable-strip / OpenAI strict) | `ToolSpec` emits raw `TypeAdapter.json_schema()` — **verified does NOT sanitize** (`tool.py:63,83`) | 2 pure functions in `sanitize_tool_schema` hook | **BUILD** | `model.md §1.5`; `openai_agents.md §10` |
| `ModelResponse` ordered-parts intermediate (+ normalized 7-value finish_reason) | pydantic-core serialize path | frozen dataclass `content: list[Text\|Reasoning\|ToolCall\|ToolResult]` | **BUILD** | `model.md §1.6`; `vercel_ai_sdk.md §2.2` |
| `FallbackModel`-as-resilience / shipped pricing table / semantic cache / Bifrost worker-pools / 22-reason enum | transport resilience + `dumps_canonical` + env rate table already cover these | — | **SKIP** | `model.md §3` |

### 1.3 Tools (registration → dispatch → repair → permission → result discipline)

| Sub-primitive | What Lancelot already provides | What the agent layer adds | Verdict | Cite |
|---|---|---|---|---|
| Tool registration + JSON-Schema | `ToolSpec` + `_build_tool_schema` (Pydantic `TypeAdapter`); `read_only`/`scopes` fields **exist but unenforced** | wire the existing fields | **REUSE → EXTEND** | `tools.md §2`; `tool.py:29-30` |
| Tool list for prompt / validated invocation | `app.tool_schemas()` (exact Anthropic/OpenAI/MCP wire) + `app.call_tool(name,/,**kwargs)` | wrap `call_tool` in the never-raises envelope | **REUSE** (substrate) | `tools.md §2`; `app.py:376,430` |
| `dispatch()` returning `{data,error,ok}` — **never raises** | `app.call_tool` currently **raises** `KeyError`/`ValidationError` — the wrong shape for a loop | the envelope (headline BUILD) | **BUILD** | `tools.md §1.1, §3.2` |
| 4-stage repair ladder + fuzzy-name >0.85 + repeated-call short-circuit | `dumps_canonical` for the call-fingerprint; `difflib`/`ast` are stdlib | the ladder (`json5`/`json_repair` lazy-optional) | **BUILD** | `tools.md §1.2`; `crewai.md §2` |
| Annotation bits → permission gate (`kind`/`destructive`/`idempotent` + allow/deny/ask) | `read_only`/`scopes` on `ToolSpec` (unenforced); `TenantConcurrencyLimiter`/`RateLimit`/SSRF are infra-level safety | add 4 fields + one `permit` hook | **EXTEND** (fields) + **BUILD** (gate) | `tools.md §1.3, §3.1`; `claude_agent_sdk.md` mech 3 |
| Bounded-parallel reads (5) + ordered + sequential writes | `TransportPool.fanout(concurrency=5)` for tool→HTTP | read/write split + re-sort to call-index | **REUSE** + **BUILD-ON-TOP** | `tools.md §1.4`; `composio.md §2` |
| `validate()`-before-side-effect | — | a `validate=` kwarg on action tools + executor branch | **BUILD** | `tools.md §1.5`; `vertical_agents.md §3` |
| `think` + `done` builtins (ship ~0 domain tools) | `@app.tool` registration | 2 trivial tools (~6 lines each) | **BUILD-ON-TOP** | `tools.md §1.6`; `nia.md §2` |
| Fetch/browse URL safety | `assert_safe_url` / `SSRFError` (CGNAT + IPv4-mapped-IPv6) | **MANDATORY** call before any user-URL tool | **REUSE** | `tools.md §2`; `net.py` |
| Result truncation + handle-and-fetch | 4 KB **warn** in `mcp.py` (does not truncate); scrub `str(exc)` (anti-pattern #3) | upgrade WARN→enforce + scrub | **EXTEND** | `tools.md §3.6`; `lancelot_serve_tools.md §2` |
| MCP server mount | `lancelot.mcp.mount` (auto-exposes `@app.tool`) | keep **opt-in**, not the default surface | **REUSE (opt-in)** | `tools.md §4`; `mcp.py` |
| BM25/dep-graph tool router / ReAct text-parse / code-action sandbox v1 / per-provider munge matrix / 30+ prebuilt tools | — | — | **SKIP** | `tools.md §4` |

### 1.4 Context engineering (assembly, compaction, inspect)

| Sub-primitive | What Lancelot already provides | What the agent layer adds | Verdict | Cite |
|---|---|---|---|---|
| Deterministic cache-stable JSON | `dumps_canonical` (sorted-key, `allow_nan=False`) — *the* KV-cache implementation primitive | use as `sha256(dumps_canonical({model,messages,tools}))` cache key | **REUSE** | `context.md §1`; `canonical.py:19` |
| Session→worker KV-cache affinity | `Response.set_session_affinity(key)` | nothing | **REUSE** | `context.md §1` |
| Cost/token accumulation | `cost_usd`/`tokens_in/out` ContextVars + `upstream_metrics` | feeds the budget gate | **REUSE** | `context.md §1` |
| Cache-token split (1h vs 5m) extraction | `telemetry.cost_event_from_openai_response` parses `cache_read_input_tokens` (3 shapes) | add `ephemeral_1h`/`ephemeral_5m` fields | **EXTEND** | `context.md §1`; `claude_code.md §11` |
| `ContextBundle` — structured, append-only, cache-aware assembly | — | the core BUILD (dataclass + `.render()`→`dumps_canonical` + `DYNAMIC_BOUNDARY` split) | **BUILD** | `context.md §B1`; `manus.md §1.5` |
| Compaction: observation-masking (52% cost, +2.6% solve) then LLM-summary fallback | summary call rides `TransportPool.request()` to a cheap model | the masking dict-op + convergent constants (0.75 trigger, 0.30 evict, circuit-breaker 3) | **BUILD** | `context.md §B2`; `agentic_deep.md §2.2` |
| `agent.inspect()` / `ContextUsage` (honesty primitive) | post-call token counts free from `upstream_metrics`; char/4 estimate (no tiktoken) | per-category introspection of the *rendered* prompt + `@app.get('/agent/context')` | **BUILD** | `context.md §B3`; `claude_code.md §3` |
| `conversation_id` (cross-turn identity) | `request_id`/`agent_id` exist; **no `conversation_id`** | one ContextVar w/ `"-"` sentinel | **EXTEND** | `context.md §6`; `lancelot_model_io.md §3.1` |
| String-concat prompt builders / semantic cache / KG augmentation / tiktoken dep | `@app.batch` already gives ~16× for textual dups | — | **SKIP** | `context.md §3` |

### 1.5 Memory (typed tiers, retrieval, temporal)

| Sub-primitive | What Lancelot already provides | What the agent layer adds | Verdict | Cite |
|---|---|---|---|---|
| Dedup-hash + cache-stable embedding input | `dumps_canonical` | feeds the dedup + embed input | **REUSE** | `memory.md §2`; `memory_systems.md:348` |
| Parallel embed / ANN+BM25 calls | `TransportPool.fanout` (no GIL re-acquire, vs ThreadPoolExecutor) | the parallel embed pattern | **REUSE** | `memory.md §2`; `agno.md:87` |
| Coalesce high-volume embed/extract | `@app.batch(size_fn,max_size)` (token-budget flush) | the embed-coalescing config | **REUSE** | `memory.md §2` |
| Scope keys / per-cred secret access | `agent_id`+`tenant_id` ContextVars; `secrets.get()` (lazy SDK, SIGHUP) | the lazy-import template for `sqlite-vec` | **REUSE** | `memory.md §2`; `lancelot_model_io.md §3` |
| 4-tier typed store (scratchpad/episodic/fact/procedural) | — | one SQLite table, `tier` enum column | **BUILD** | `memory.md §1.1`; `transcripts_dlai.md:212` |
| Memory-as-tools (`remember`/`recall`/`recall_as_of`) | `ToolSpec`/`tool_schemas`/`call_tool` validate on input+output | 3 tools on the existing substrate | **BUILD-ON-TOP** | `memory.md §1.2`; `letta.md:21` |
| `MemoryBackend` Protocol + `SqliteVecBackend` default | stdlib `sqlite3`; `sqlite-vec` lazy-loaded on first `add` | 4-method Protocol + sqlite default | **BUILD** | `memory.md §1.3` |
| Composite retrieval (`0.5·vec+0.3·recency+0.2·importance`) + RRF k=60 (opt-in) | — | float-math scorer + `rrf()` utility | **BUILD** | `memory.md §1.4`; `memory_vector.md:25` |
| Temporal facts: ADD-only + tombstone (`valid_at`/`invalid_at`); bi-temporal opt-in | — | 4 bi-temporal columns always present; LLM arbitration gated | **BUILD** | `memory.md §1.5`; `memory_systems.md:51` |
| Virtual-context page-table (`<memory_metadata>`) + memory-freeze invariant | `dumps_canonical` stabilizes the frozen snapshot bytes | `page_table()` str-replace + anti-injection wrapper | **BUILD-ON-TOP** | `memory.md §1.6`; `hermes_core.md:110` |
| Consolidation hook (background, frequency+rate-gated, cheap model) | `@app.lifetime_task` daemon; cheap-model call accrues cost automatically | the gated `reflect()` pass | **REUSE** (daemon) + **BUILD** (pass) | `memory.md §1.8`; `letta.md:121` |
| `lancelot.memory_pool` (byte-OOM) | exists — **NOT agent memory**; new module must be `lancelot.agents.memory` | — | **REUSE (different layer)** | `memory.md §2` |
| Memory-class hierarchy / mandatory Postgres+pgvector / graph-DB-by-default / GBNF / hard-delete / eager consolidation | — | — | **SKIP** | `memory.md §3` |

### 1.6 Evals (the wedge — eval == run)

| Sub-primitive | What Lancelot already provides | What the agent layer adds | Verdict | Cite |
|---|---|---|---|---|
| Native traces for trajectory evals | `traceparent` auto-inject + copy-on-Task ContextVars (complete sub-call tree, no sampler) | nothing — eval==run | **REUSE** | `evals.md §3`; `lancelot_model_io.md §1.8` |
| Cost/per-step rate per eval | `cost_usd` + `lancelot.upstream.{requests,duration,ttft,cost}` histograms | plot score-vs-cost (already emitted) | **REUSE** | `evals.md §3`; `lancelot_model_io.md §2.5` |
| Streamed-call token correctness | `_scan_last_data_frame` + `track_usage=True` (solves Weave's accumulator anti-pattern) | nothing | **REUSE** | `evals.md §1`; `lancelot_model_io.md §1.3` |
| Tool input/output validation metric | `ToolSpec._validator` + auto `output_schema` | tool-call-validity check (zero new code) | **REUSE** | `evals.md §3` |
| Judge LLM calls / pass^k trials | `TransportPool.request()` / `fanout(first_n,concurrency)` | judge is just another pool call | **REUSE** | `evals.md §3` |
| `eval.unit` (trajectory IN_ORDER, `failure_score=0.0`, concurrent, 0.999 gate) | `asyncio.gather`; `classify_status` transport verifier | deterministic CI harness | **BUILD** | `evals.md §2.1`; `google_adk.md §12` |
| `eval.judge` (categorical rails, orthogonal panel, 4 axes, `align()`≥0.90) | judge call via TransportPool | the panel + calibration step | **BUILD** | `evals.md §2.2`; `transcripts_dlai.md:60` |
| `define_outcome` (5 verdicts, **required** `max_iterations`) | — | bounded generate→grade→revise | **BUILD** | `evals.md §2.3`; `deep_research.md §2` |
| `@app.tool(verify=...)` verifier hook | auto `output_schema`; verify-fail maps to `classify_status`→permanent | a post-call predicate (~20 LoC) | **BUILD-ON-TOP** | `evals.md §2.4`; `bad_guide_ai_research.md §2` |
| `lancelot.context.force_eval` toggle | the same per-request `finally` seam | one ContextVar (~5 LoC) | **EXTEND** | `evals.md §2.5` |
| SaaS observability platform / ROUGE-BERTScore / numeric-Likert judges / self-referential loops / anything in hot path / DSPy optimizer / data-viewer UI | `set_sink` hook lets operators point Langfuse/Phoenix at it | — | **SKIP** | `evals.md §4` |

### 1.7 Streaming (agent-event protocol)

| Sub-primitive | What Lancelot already provides | What the agent layer adds | Verdict | Cite |
|---|---|---|---|---|
| The pump (async-gen → bounded-mpsc → hyper, GIL-released, backpressure, disconnect→cancel) | **R3 pump** (`stream.py` + `src/stream.rs`), `LANCELOT_STREAM_BUFFER=256` | nothing — *the marquee, faster than TS stacks* | **REUSE** | `streaming.md §2.1`; `lancelot_serve_tools.md §1` |
| SSE framing + CR/LF/NUL strip | `Event`/`sse_adapter`/`_clean_field`; `[DONE]` on clean exit, **no sentinel on exception** | reuse `_clean_field` verbatim on every event field | **REUSE** | `streaming.md §2.1` |
| `@app.stream` knobs (sse / defer_status / max_stream_silence / max_stream_lifetime / error_frame) | all present | agent endpoint sets `sse=True, error_frame=True, defer_status=True` | **REUSE** | `streaming.md §2.1` |
| TTFT metric (the AI-UX number) | `lancelot.http.stream.ttft` on first non-keepalive chunk | nothing — already the exactly-right metric | **REUSE** | `streaming.md §1.1` |
| Upstream stream read + usage scan + idle guard | `TransportPool.stream(track_usage=True)`/`relay()`, `idle_guarded_chunks` | normalizer translates provider SSE → Lancelot events | **REUSE** | `streaming.md §2.1` |
| Normalized event taxonomy (~10–12 typed parts, true-delta, reasoning channel) | — | `lancelot.agents.events` frozen dataclasses → `Event` | **BUILD** | `streaming.md §2.2.1`; `vercel_ai_sdk.md §2.4` |
| Stream normalizer (5 production fixes + terminal guarantee) | `sse_adapter` handles the single-generator `[DONE]` case | ~150-LoC provider-quirk pass | **BUILD** | `streaming.md §2.2.2` |
| Cooperative cancel (`athrow`) | R3 disconnect→cancel; `cancel_on_disconnect` | ~20-LoC `athrow(RunCancelled)` wrapper | **BUILD-ON-TOP** | `streaming.md §2.2.4`; `letta.md §10` |
| Resumability (`seq_id` field now, Redis backend later) | in-memory default needs nothing new | `seq_id` on every part + `StreamStore` Protocol | **BUILD-ON-TOP** (seam) / **SKIP v1** (Redis) | `streaming.md §2.2.5` |
| 86-chunk/27-chunk taxonomies / GBNF / per-chunk validation / smooth-stream / mandatory Redis / per-chunk cross-thread bridge | — | — | **SKIP** | `streaming.md §2.3` |

### 1.8 Durable (event-sourced checkpoint / resume / HITL)

| Sub-primitive | What Lancelot already provides | What the agent layer adds | Verdict | Cite |
|---|---|---|---|---|
| The LLM call inside `step()` | `TransportPool.request` (idempotency-key stable across retries) | nothing | **REUSE** | `durable.md §3`; `lancelot_model_io.md §1.5` |
| Budget feed for `max_budget_usd` | `cost_usd` ContextVar (accumulates across sub-calls) + `X-Lancelot-Cost-USD` | the gate `if cost_usd.get() > cap` | **REUSE** | `durable.md §B5` |
| `attach` live stream (backpressure/keepalive/TTFT) | R3 pump + `@app.stream` + `sse_adapter` | `seq_id`-cursor replay glue | **REUSE** | `durable.md §B4` |
| Cancel that runs the agent's `finally` | `cancel_on_disconnect` + Closed-channel upstream cancel | pair with `generator.athrow` | **REUSE** + **BUILD-ON-TOP** | `durable.md §B4`; `letta.md §10` |
| HITL typed-object schemas | `@app.tool`→`ToolSpec` auto JSON schema (`model_json_schema()`) | reuse for the human-fills form | **REUSE** | `durable.md §B3` |
| Snapshot serialization | pydantic-core `to_json` (explicitly preferred over JsonPlus/Msgpack) | nothing | **REUSE** | `durable.md §3`; `langgraph.md §8` |
| HITL callback to user URL (signed, SSRF-safe) | `webhook.sign`/`verify` + `assert_safe_url` | nothing | **REUSE** | `durable.md §3` |
| `submit` 202 + background daemon | `@app.lifetime_task` (drain-bounded) | the submit handler | **REUSE** | `durable.md §B4` |
| `Checkpointer` Protocol + event-log shape | stdlib `sqlite3` (SqliteCheckpointer = zero new dep) | 2-method Protocol; default `InMemoryCheckpointer` | **BUILD** | `durable.md §B1` |
| `@step` bracket — side-effect skip on resume (the heart) | — | record-once / return-on-replay | **BUILD** | `durable.md §B2` |
| `ctx.hitl.request` + `await_condition` (checkpoint-BEFORE-pause) | typed schemas from `@app.tool`; `to_json` snapshot | the durable-promise machinery | **BUILD** | `durable.md §B3`; `transcripts_stanford_palantir.md §7` |
| `StopReason` + budget as typed termination | reads `cost_usd`; graceful-budget threshold | shared enum (ONE with the loop layer) | **BUILD** (shared) | `durable.md §B5` |
| Graph DSL / VM-snapshot / mandatory Postgres / Inngest-Temporal coupling / `MAX+1` ordering / chat-box HITL / cross-language durability v1 | stdlib-sqlite + in-memory cover 95% | — | **SKIP** | `durable.md §4` |

### 1.9 Safety / permissions (cross-cutting — emerges from loop + tools)

| Sub-primitive | What Lancelot already provides | What the agent layer adds | Verdict | Cite |
|---|---|---|---|---|
| Infra-level safety (conn cap, slow-loris, body cap, per-route timeout) | the whole resilience suite (`lancelot_serve_tools.md §8-11`) | nothing | **REUSE** | `lancelot_serve_tools.md` |
| Inbound admission control | `RateLimit` + `TenantConcurrencyLimiter` (per-tenant concurrency, queue_wait_ms) | per-tenant tool-call budget rides `tenant_id` | **REUSE** + **BUILD-ON-TOP** | `tools.md §2` |
| SSRF / secret access / signed callbacks | `assert_safe_url`, `secrets.get()`, `webhook.sign/verify` | mandatory calls in fetch/cred/callback tools | **REUSE** | `tools.md §2` |
| Tool permission gate (allow/deny/ask) | `ToolSpec.read_only`/`scopes` (unenforced) | the `permit` hook reading the annotation bits | **EXTEND** + **BUILD** | `tools.md §1.3` |
| `@app.tool(verify=...)` post-call guard | auto `output_schema` | the predicate | **BUILD-ON-TOP** | `evals.md §2.4` |
| Prompt-injection: mode-switch on developer-role only | — | the mode-gate invariant (reject user-role switch) | **BUILD** | `loop.md §10`; `codex.md §10` |
| Naive keyword injection guardrail / 5-tier autonomy v1 / `defer` 4th permission value + multi-hook chain | — | — | **SKIP** | `tools.md §4`; `loop.md §12` |

### 1.10 Orchestration (sub-agents, fan-out)

| Sub-primitive | What Lancelot already provides | What the agent layer adds | Verdict | Cite |
|---|---|---|---|---|
| Sub-agent context isolation | `TransportPool.spawn_child(parent, session_affinity)` — snapshots traceparent/request_id/agent_id at construction (survives ContextVar reset) | the sub-agent dispatch glue | **REUSE** | `loop.md §11`; `lancelot_model_io.md §1.8` |
| Parallel sub-agent / panel-of-models | `TransportPool.fanout(first_n, deadline, cancel_laggards, concurrency)` — completion-order, partial-failure shape | first-N-wins pattern | **REUSE** | `model.md §2`; `lancelot_model_io.md §1.7` |
| Anti-ping-pong handoff detection | — | `repetitive_handoff_detection_window` (orchestration-layer stuck-detect) | **BUILD (if multi-agent)** | `loop.md §4`; `strands.md §8` |
| Multi-agent shared mutable context | — | — | **SKIP** (Cognition "don't build multi-agents"; single-trace via spawn_child is the default) | `loop.md §12`; `bad_guide_build_positioning.md §10` |

---

## 2. The headline split, summarized

**REUSE (build nothing — ~70% of the plumbing):** the outbound LLM call + retry/Retry-After/budget/cancel/idempotency/traceparent (`TransportPool.request`), the streaming pump + SSE framing + TTFT (`stream.py`/`src/stream.rs` + `Event`/`sse_adapter`/`_clean_field`), cost/token/trace ContextVars + 6 `upstream.*` histograms, the error taxonomy (`classify_status`), tool registration+validation+wire-schema (`ToolSpec`/`tool_schemas`/`call_tool`), the cache-key primitive (`dumps_canonical`), batch coalescing (`@app.batch`), within-turn + cross-model fan-out (`fanout`), sub-agent isolation (`spawn_child`), SSRF (`assert_safe_url`), secrets (`secrets.get` + SIGHUP), inbound admission (`RateLimit`/`TenantConcurrencyLimiter`), signed callbacks (`webhook`), supervised daemons (`lifetime_task`), the telemetry sink. **Eval gets this for free — the wedge: an eval run and a prod run are the same byte path.**

**EXTEND (small additive widening — no rewrite):** enforce the existing `ToolSpec.read_only`/`scopes` + add `kind`/`destructive`/`idempotent`/`validate`/`max_result_bytes` fields; add 1 ContextVar `conversation_id` + 1 ContextVar `force_eval`; add `ephemeral_1h`/`ephemeral_5m` to the cache-token parser; upgrade the 4 KB MCP **warn** to enforce+scrub; add `lancelot.agent.{loop_iterations,tool_calls,context_tokens}` histograms via the existing lazy-OTel pattern.

**BUILD (the agent brain — the real work, pure-Python, zero new deps):** the loop (`StopReason`, `Budget`+grace, stuck-detector, hook registry, tool-rule/mode-gate opt-ins); the model adapter (`ProviderProfile`, `init_model`, error-typed failover, `AuxiliaryClient`, schema sanitization, `ModelResponse`); the tool dispatch discipline (`dispatch()` never-raises, repair ladder, permission gate, `think`/`done`); context engineering (`ContextBundle`, observation-masking + LLM-summary compaction, `inspect()`); typed memory (4-tier store, memory-as-tools, `MemoryBackend` Protocol, composite scoring, temporal facts); evals (`eval.unit`/`eval.judge`/`define_outcome`/`@app.tool(verify=)`); the durable seam (`Checkpointer` Protocol, `@step` bracket, `ctx.hitl.request`/`await_condition`); the streaming event taxonomy + normalizer.

**SKIP (off-thesis / depreciating / overkill — stop and check this list before building):** graph DSL for a linear loop, `while True` with no ceiling, shipped pricing table, semantic cache, `FallbackModel`-as-resilience, BM25/dep-graph tool router, ReAct text-parse path, code-action sandbox (v1), per-provider schema-munge matrix, 30+ prebuilt tools, MCP-as-default, string-concat prompt builders, KG context augmentation, tiktoken dep, memory-class hierarchy, mandatory Postgres+pgvector, graph-DB-by-default memory, GBNF, hard-delete-on-supersede, SaaS observability platform, ROUGE/BERTScore, numeric-Likert judges, self-referential eval loops, DSPy optimizer, 86/27-chunk stream taxonomies, smooth-stream, mandatory Redis, VM-snapshot durability, chat-box HITL, multi-agent shared mutable context, and (Tier-D, the hard rule) **any per-chunk cross-thread bridge that regresses the R3 pump** (the 16–26× cliff).

---

## 3. Module map — the hypothetical `lancelot.agents` package

A new top-level package, sibling to `lancelot.llm`/`lancelot.tool`/`lancelot.stream`. Naming note: **`agents`** (plural) avoids collision with the per-file `agent` references in the briefs and reads as the package; the public surface re-exports `Agent` (singular). Every module is pure-Python on the existing byte path; lazy-imports listed per module. LoC estimates are from the per-layer briefs.

```
lancelot/agents/
│
├── __init__.py            # public surface: Agent, run, StopReason, Budget, init_model,
│                          #   ContextBundle, Memory, eval, hooks. Lazy re-exports only —
│                          #   `import lancelot.agents` pulls pydantic + stdlib, nothing else.
│
├── loop.py                # BUILD ~200 LoC. The <200-line async ReAct recursion.
│                          #   StopReason(str,Enum) [11-value], Budget(max_turns=20 +
│                          #   cost/token, priority turns→cost→tokens, grace-call), the
│                          #   while-loop with budget GATE-A / stuck GATE-B / resume GATE-C.
│                          #   REUSES TransportPool/context/stream verbatim. (loop.md)
│
├── stuck.py               # BUILD ~40 LoC. StuckThresholds(4/3/3/6), counter-based detector,
│                          #   terminal StopReason.stuck. (loop.md §4)
│
├── hooks.py               # BUILD ~80 LoC. The ONE extension seam: before/after_model,
│                          #   before/after_tool (mutable Decision: swap/rewrite/cancel),
│                          #   on_finish (reject+reinject). Wires telemetry.set_sink +
│                          #   the agent.* histograms. The permission gate plugs in here.
│                          #   (loop.md §8)
│
├── rules.py               # BUILD ~120 LoC, OPT-IN. InitToolRule + TerminalToolRule solver;
│                          #   planning_interval mode-gate; developer-role-only mode switch.
│                          #   Default None (off). (loop.md §9-10)
│
├── model/
│   ├── __init__.py        # init_model("provider:model"), resolve_model, register_profile.
│   ├── profile.py         # BUILD ~150 LoC. ProviderProfile frozen dataclass + OMIT sentinel;
│   │                      #   transform_request/response, build_api_kwargs_extras split,
│   │                      #   sanitize_tool_schema hook. Top-5 profiles. (model.md §1.1)
│   ├── client.py          # BUILD ~120 LoC. ModelClient over TransportPool.request/stream;
│   │                      #   complete()/stream(); AuxiliaryClient (separate cost bucket).
│   ├── failover.py        # BUILD-ON-TOP ~40 LoC. Profile-list walk gated on classify_status;
│   │                      #   same-family-first, return PRIMARY error. (model.md §1.3)
│   ├── schema.py          # BUILD ~80 LoC. strip_nullable_unions (Anthropic),
│   │                      #   ensure_strict_json_schema (OpenAI). Pure fns. (model.md §1.5)
│   └── response.py        # BUILD ~60 LoC. ModelResponse(content:list[Text|Reasoning|
│                          #   ToolCall|ToolResult], finish_reason 7-value, usage, raw).
│                          #   *Single source of truth — loop + streaming import it.*
│   # lazy: nothing (speaks HTTP via TransportPool — no provider SDK import; a Lancelot edge)
│
├── tools.py               # BUILD ~250 LoC. dispatch() → {data,error,ok} NEVER raises;
│                          #   4-stage repair ladder (json5/json_repair LAZY-OPTIONAL);
│                          #   fuzzy-name >0.85 (difflib, stdlib); repeated-call short-circuit
│                          #   (dumps_canonical fingerprint); dispatch_batch (reads@5 ordered,
│                          #   writes serial); ToolSession (permit hook + dedup sets);
│                          #   think/done builtins; handle-and-fetch truncation.
│                          #   EXTENDS ToolSpec with kind/destructive/idempotent/validate.
│                          #   REUSES assert_safe_url, fanout(concurrency=5). (tools.md §3)
│                          #   # lazy: json5, json_repair (graceful degrade to stdlib ladder)
│
├── context_engine.py      # BUILD ~300 LoC. ContextBundle (3-block append-only, DYNAMIC_BOUNDARY,
│                          #   .render()→dumps_canonical, .to_cache_key()); compact()
│                          #   (observation-masking dict-op THEN LLM-summary via TransportPool
│                          #   to a cheap model, circuit-breaker=3); inspect()→ContextUsage;
│                          #   @app.get('/agent/context'). char/4 estimate (NO tiktoken).
│                          #   (context.md §B1-B3,B5)
│
├── memory.py              # BUILD ~400 LoC. Memory (4-tier SQLite table, tier enum);
│                          #   remember/recall/recall_as_of tools (install_tools(app));
│                          #   MemoryBackend Protocol + SqliteVecBackend default;
│                          #   composite scorer + rrf(k=60) util; bi-temporal columns +
│                          #   tombstone (arbitration opt-in); page_table(); consolidate()
│                          #   via @app.lifetime_task. NAME IS lancelot.agents.memory —
│                          #   NOT lancelot.memory_pool (byte-OOM, different layer).
│                          #   (memory.md §1)  # lazy: sqlite-vec (on first add), embed SDKs
│
├── stream/
│   ├── events.py          # BUILD ~120 LoC. ~12 frozen-dataclass event parts (run.start/finish,
│   │                      #   message/reasoning/tool .start/delta/end, error, status, ping);
│   │                      #   true-delta; deterministic id = base_id+type_index; seq_id on
│   │                      #   every part; .to_sse()→Event. (streaming.md §2.2.1)
│   ├── normalize.py       # BUILD ~150 LoC. The 5 provider-quirk fixes (tool-calls-win,
│   │                      #   empty-first-chunk, finish_reason:length→targeted error, blank-
│   │                      #   strip, tolerant partial-parse) + terminal-[DONE] guarantee.
│   │                      #   *Could fold into ProviderProfile — see open Q.* (streaming.md §2.2.2)
│   ├── cancel.py          # BUILD-ON-TOP ~40 LoC. athrow(RunCancelled) wrapper; distinguish
│   │                      #   from CancelledError (disconnect). (streaming.md §2.2.4)
│   └── store.py           # BUILD-ON-TOP ~60 LoC. StreamStore Protocol + InMemoryStreamStore
│                          #   (bounded ring + TTL). Redis-Streams impl DEFERRED (SKIP v1).
│   # REUSES R3 pump + sse_adapter + @app.stream knobs + TTFT verbatim. (streaming.md §2.1)
│
├── durable.py             # BUILD ~600-900 LoC. Checkpointer Protocol (append/load +
│                          #   optional load_pending/cancel); InMemoryCheckpointer default;
│                          #   SqliteCheckpointer (stdlib sqlite3, ZERO new dep);
│                          #   @step bracket (record-once / return-on-replay); ctx.hitl.request
│                          #   + await_condition (checkpoint-BEFORE-pause, det. id via
│                          #   hashlib); submit/attach/cancel on the byte path (202 +
│                          #   @app.stream seq_id replay + athrow). Snapshot = pydantic-core
│                          #   to_json. SHARES StopReason + budget gate with loop.py.
│                          #   (durable.md §B1-B6)  # lazy: psycopg (PostgresCheckpointer only)
│
├── eval/
│   ├── unit.py            # BUILD ~150 LoC. Case, trajectory(EXACT|IN_ORDER|ANY_ORDER, default
│   │                      #   IN_ORDER), failure_score=0.0 (crash≠abort), concurrency=8,
│   │                      #   pass_rate≥0.999 gate. REUSES ToolSpec._validator, asyncio.gather.
│   ├── judge.py           # BUILD ~150 LoC. Judge (categorical rails ["pass","fail"], samples=1,
│   │                      #   panel=orthogonal-not-merged); 4 built-in axes (intent/relevance/
│   │                      #   groundedness/tool-accuracy); align(labeled)≥0.90. Judge call via
│   │                      #   TransportPool. (evals.md §2.2)
│   ├── outcome.py         # BUILD ~80 LoC. define_outcome (5 verdicts, REQUIRED max_iterations,
│   │                      #   unresolvable→log+STOP). (evals.md §2.3)
│   └── verify.py          # BUILD-ON-TOP ~25 LoC. @app.tool(verify=...) post-call predicate →
│                          #   ToolVerifyError; force_eval ContextVar. (evals.md §2.4-2.5)
│   # lazy: sqlite-vec (only if a future case-store wants semantic retrieval)
│
└── _context_vars.py       # EXTEND ~10 LoC. conversation_id + force_eval ContextVars
                           #   (added to lancelot.context, "-"/False sentinels, matching the
                           #   existing request_id pattern). (context.md §6, evals.md §2.5)
```

**Total: ~3,000–4,000 LoC of pure-Python state-machine + adapter logic. Zero new runtime deps.** Every heavy import (`json5`, `json_repair`, `sqlite-vec`, embedding SDKs, `psycopg`, Redis) is lazy behind a `try/except` that degrades gracefully — exactly the `lancelot.secrets` template (`lancelot_serve_tools.md §7`).

### 3.1 Cross-module shared contracts (decide once, not per-module)

These are the seams the briefs repeatedly flag as needing a single owner:

1. **ONE `StopReason` enum + ONE budget gate**, shared by `loop.py` and `durable.py` (`durable.md §6` open-Q 6, `loop.md §14`). Define in `loop.py`; `durable.py` imports it. Do not ship two.
2. **`ModelResponse` ordered-parts type** is the seam between `model/response.py`, `loop.py`, and `stream/events.py` (`model.md §6` open-Q 2). Define in `model/response.py`; everyone imports it.
3. **`conversation_id` ContextVar** is the cross-turn identity that `context_engine.py`, `memory.py`, and `durable.py` all key on (`context.md §6`). One var in `lancelot.context`.
4. **The normalizer↔ProviderProfile fold** — `stream/normalize.py`'s per-provider quirks and `model/profile.py`'s `transform_*` are the same provider-specific code; the briefs recommend folding the stream normalizer INTO `ProviderProfile` so transport + message-shape + stream-shape translation are one unit (`streaming.md §6` open-Q 3). Decide at build time.
5. **`@step` bracket is *inside* the loop**, not a parallel engine — `durable=True` flips the checkpointer behind the same loop path, byte-identical when off (`durable.md §0`). The loop and durable seams are one machine with two modes.
6. **Multi-worker gaps are documented, not solved** in v1 — the SQLite memory store, the `InMemoryCheckpointer`, the MCP session set, and the `InMemoryStreamStore` all share the single-worker-default / shared-backend-for-scale boundary (`durable.md §6` open-Q 3, `memory.md §6` open-Q 3, `lancelot_serve_tools.md §3`). The Protocols make the shared backend a swap; don't build it until it's a real requirement.

### 3.2 The end-to-end shape (what a user touches)

```python
import lancelot
from lancelot.agents import Agent, StopReason, Budget, ContextBundle, Memory, eval
from lancelot.agents.model import init_model

app = lancelot.App()

@app.tool(read_only=True)                      # REUSE: ToolSpec + auto schema (kind="lookup" inferred)
def search(query: str) -> list[str]: ...

@app.tool(kind="action", destructive=True, idempotent=False,
          validate=lambda a: _preflight(a),    # EXTEND: new annotation bits + validate-before-side-effect
          verify=lambda r, ctx: r.ok)          # BUILD-ON-TOP: post-call verifier hook
def issue_refund(order_id: str) -> Receipt: ...

model = init_model("anthropic:claude-sonnet-4",                 # BUILD: string router
                   fallback=["anthropic:claude-haiku-4",         # BUILD: error-typed, same-family-first
                             "google:gemini-flash"])
mem   = Memory(scope=("agent_id", "tenant_id"), embed=...)       # BUILD: 4-tier; sqlite-vec lazy
mem.install_tools(app)                                            # BUILD-ON-TOP: remember/recall as tools

agent = Agent(
    model=model,                                # REUSE TransportPool underneath
    tools=app,                                  # REUSE tool_schemas() + call_tool()
    budget=Budget(max_turns=20, max_cost_usd=0.50),   # REUSE cost_usd ContextVar; BUILD the gate
    context=ContextBundle(system="...", tools=app.tool_schemas(), max_context_tokens=180_000),
)
agent.on(hooks.before_tool, permission_gate)    # BUILD: the safety seam (allow/deny/ask)

# unary — eval == run: this same handler is the eval target
result = await agent.run("Refund order 123.")
assert result.stop_reason is StopReason.end_turn
print(result.output, result.cost_usd, result.turns)   # cost_usd from the byte-path ContextVar

# streaming — REUSE the R3 pump + TTFT + sse_adapter byte-identically
@app.stream("/agent", sse=True, error_frame=True, defer_status=True, cancel_on_disconnect=True)
async def stream_agent(req):
    async for part in agent.stream(req.query):  # yields normalized lancelot.agents.stream.events
        yield part

# durable — same loop, durable=True flips the checkpointer (byte-identical off)
@lancelot.agents.durable.durable_agent(checkpointer=SqliteCheckpointer("runs.db"), max_budget_usd=1.0)
async def support(ctx, task: str):
    plan = await ctx.step("plan", lambda: ctx.llm(messages=[...]))   # recorded once, skipped on replay
    ok: Approval = await ctx.hitl.request(Approval)                  # checkpoint-BEFORE-pause → HTTP 202
    ...
app.mount_durable(support, path="/support")     # submit/attach/cancel ride the byte path

# evals — deterministic CI gate, runs the SAME prod handler over the SAME byte path
report = await eval.unit(cases, run=agent.run, trajectory_mode="in_order", concurrency=8)
assert report.pass_rate >= 0.999
```

---

## 4. Why this integration is "brutal" (the altitude check)

- **It builds the brain, not the body.** ~70% of an agent framework's surface is plumbing Lancelot already shipped and load-tested under adversarial load; the agent layer is ~3.5k LoC of pure-Python state machines and adapters on top. The briefs are unanimous: the loop is 5–10 lines (`loop.md §0`); evals share the prod byte path (`evals.md §1`); durable execution's transport/streaming half is done (`durable.md §7`).
- **It honors the cardinal rule exactly.** `import lancelot.agents` pulls pydantic + stdlib. `json5`/`json_repair`/`sqlite-vec`/embedding SDKs/`psycopg`/Redis are all lazy behind graceful-degrade `try/except` — the proven `lancelot.secrets` pattern. No `httpx`/`opentelemetry`/`tiktoken` ever enters the import graph.
- **It refuses the over-builds, by name, with citations.** Graph DSLs, semantic cache, memory-class hierarchies, SaaS eval platforms, 86-chunk stream taxonomies, mandatory Postgres/Redis, multi-agent shared state, and (the Tier-D hard line) any regression of the R3 pump to per-chunk cross-thread bridging. The SKIP column is as load-bearing as the BUILD column.
- **It leads with the validated wins.** The streaming story is "the GIL-released R3 pump that beats Starlette, now speaking one clean agent-event protocol." The eval story is "eval == run, same transport, same cost meter, same traces — for free." The cost story is "`dumps_canonical` + session-affinity + observation-masking, the #1 free lever." These are structural advantages pure-Python frameworks cannot replicate — which is the entire reason an agent layer *for Lancelot* is worth sketching at all.

---

## 5. Open questions carried up from the layer briefs (for a future build phase)

1. **Package name** — `lancelot.agents` (this brief) vs `lancelot.agent`. Recommend plural package, singular `Agent` export; never `lancelot.memory_pool` for agent memory.
2. **`api_mode` count at v1** — OpenAI-compat + Anthropic-native (the two shapes that differ on the wire), add Gemini-native on demand (`model.md §6`).
3. **Where `compact()` runs** — inline when over budget (you can't proceed), background otherwise; the loop decides the trigger, `context_engine` provides both entry points (`context.md §6`).
4. **Default embedding provider** — require `EmbeddingDefinition` from the user (no hidden model dep), per cardinal rule (`memory.md §6`).
5. **Case store for evals** — a thin sqlite-only store is in-thesis (plumbing); semantic case retrieval stays lazy `sqlite-vec` (`evals.md §7`).
6. **Resumability backend** — ship the `seq_id` + `StreamStore`/`Checkpointer` Protocols day-1; defer Redis-Streams behind a real background-run requirement (`streaming.md §1.7`, `durable.md §B4`).
7. **The 6 cross-module shared contracts in §3.1** — decide each once, at the start of any build, to avoid two `StopReason` enums / two budget gates / two `ModelResponse` types.

---

*This is the capstone of the Lancelot agent-framework research harvest: the lens (`00_LENS.md`), the byte-path inventory (`04_byte_path/`), the 8 layer briefs (`05_synthesis/`), and the raw corpus (`02_core/`, `03_teardowns/`) all reduce to this single decision table and module map. Exploratory artifact, finished for completeness, parked. Every REUSE/EXTEND row is verified against direct reads of `lancelot/` source; every BUILD/SKIP row cites the layer brief and the primary teardown beneath it.*
