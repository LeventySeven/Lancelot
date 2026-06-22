# RE Startups Map — Competitive/Primitive Map for Lancelot

Source files: `core/14_EXTERNAL_AGENTIC_PRODUCTS_RE.md`, `core/15_TOP_AGENTIC_STARTUPS_RE.md`, `core/16_MISSING_TOP_AGENTIC_STARTUPS_RE.md`, `core/17_NEXT_TOP_AGENTIC_INFRA_STARTUPS_RE.md`

Date: 2026-06-22. Freshness of source: 2026-04-26.

---

## 1. Primitive Map: What Wins, What Is Commoditized, White Space

### 1.1 The Loop Itself Is Commoditized

Every major framework — LangGraph, Strands, Pydantic AI, OpenHands, Mastra, CrewAI, LlamaIndex — ships roughly the same 5-10-line ReAct/function-call loop. The loop is not where anyone competes. [14, Executive Ranking]

The product is the harness around the loop:
- event-sourced state machine (OpenHands)
- durable workflow checkpoints (LangGraph, Dust via Temporal)
- HITL/approval as first-class status (Mastra suspend, OpenHands WAITING_FOR_CONFIRMATION)
- stuck detection before retry proliferation (OpenHands: 4 repeated action/observation, 3 action/error, 3 monologue, 6 alternating pattern — scans last 20 events) [14, section 2]
- tool call normalization for malformed JSON from production LLMs [14, section 2]

### 1.2 The Gateway Layer Is Mandatory Infrastructure

Portkey Gateway (17, section 1) is the convergence point. Every agent call should route through a control plane that owns: routing mode (`single/loadbalance/fallback/conditional`), retry/Retry-After handling, before/after hooks, guardrails with 246-status option, provider transforms. The gateway is NOT optional decoration — without it, fallback, caching, and guardrails are re-implemented per-agent. **Lancelot's `TransportPool` is this layer for outbound LLM calls** — the routing modes and Retry-After handling in Portkey are byte-path analogues that `TransportPool` already owns.

Key Portkey constants (KNOWN from source commit `351692fd`):
- weighted load balance: `Math.random() * totalWeight`, iterate subtracting weights [17, section 1]
- timeout synthetic 408: `{"error":{"message":"Request exceeded the timeout...","type":"timeout_error",...}}`
- retry-after: convert seconds→ms, bail if exceeds `MAX_RETRY_LIMIT_MS` [17, section 1]
- conditional routing: operators `$eq/$ne/$gt/$gte/$lt/$lte/$in/$nin/$regex/$and/$or`; **only two-level key paths** like `metadata.user_id` — not arbitrarily deep [17, section 1]
- guardrail failure returns HTTP 246 (not 4xx), so calling code can differentiate content policy from hard errors

### 1.3 Memory Write Path Matters More Than Read Path

Mem0 (16, section 2) is the cleanest open evidence for production agent memory. Critical mechanism:

**Write path** (KNOWN from `mem0/memory/main.py`, commit `bd9d27ff`):
- integer-alias existing memory UUIDs before showing to LLM (anti-hallucination tactic) [16, section 2.3]
- ADD-only extraction (`ADDITIVE_EXTRACTION_PROMPT`) — no UPDATE/DELETE from LLM [16, section 2.6]
- dedup via `hashlib.md5(text.encode()).hexdigest()` [16, section 2.3]
- entity link threshold: `score >= 0.95` to merge into existing entity [16, section 2.3]

**Read/search path** (KNOWN):
- `internal_limit = max(limit * 4, 60)` — oversample by 4× before scoring [16, section 2.4]
- BM25 sigmoid normalization by query length: `<=3 terms`: midpoint 5.0, steepness 0.7; up to `midpoint 12.0, steepness 0.5` for long queries [16, section 2.4]
- entity boost: first 8 entities only, `top_k=500` entity store search, match threshold 0.5, `ENTITY_BOOST_WEIGHT = 0.5` [16, section 2.4]
- combined score: `min((semantic + bm25 + entity_boost) / max_possible, 1.0)` where max_possible = 2.5 with all signals [16, section 2.4]

**Byte-path implication:** Lancelot has no agent memory layer. `lancelot.context.ContextVars` (`cost_usd`, `tokens_in`, `tokens_out`, `agent_id`, `queue_wait_ms`) are request-scoped transport metadata, not long-term memory. The memory substrate is user-side. The Mem0 pattern — additive extraction, entity-linked hybrid search — is the reference design for the memory layer Lancelot would need to build if it owned agent memory. Currently out of scope (user side per the byte-path mission), but Mem0's MCP interface (`add_memories`, `search_memory`) is directly usable via `lancelot.mcp.mount`.

### 1.4 Tool Auth Is Not A Schema Problem

Arcade (16, section 3) is the strongest evidence that production tool calling is an auth, schema, transport, AND error problem — not just JSON schema generation.

Key mechanism (KNOWN from `arcade_core/schema.py`, `arcade_tdk/tool.py`, commit `70515e33`):
- `TOOL_NAME_SEPARATOR` = `"."` from env `ARCADE_TOOL_NAME_SEPARATOR` [16, section 3.2]
- `ToolRuntimeError` has: `message`, `developer_message`, `stacktrace`, `can_retry`, `status_code`, `retry_delay`, `kind`, `extra_data` — **agent-facing message is separate from developer log** [16, section 3.4, 3.6]
- Worker auth: `ARCADE_WORKER_SECRET` env; if auth enabled and no secret, startup raises [16, section 3.7]
- MCP output schema: primitive/list outputs are wrapped as `{result: ...}`; `additionalProperties: false` [16, section 3.8]
- Tool execution: Pydantic input validation errors report only field path + reason + error type — **never includes offending input values** [16, section 3.6]
- Client disconnect during tool invocation → HTTP 499 [16, section 3.7]

**Byte-path implication:** Lancelot's `@app.tool` + `lancelot.mcp.mount` provides tool schema auto-derivation and MCP mounting. Arcade's contribution is the auth layer (OAuth per-provider) and the structured error taxonomy (`can_retry`, `developer_message` vs `agent_message`). The error taxonomy is directly applicable — Lancelot's tool error handling should separate agent-facing from developer-facing errors. The HTTP 499 on client disconnect is already handled by Lancelot's C2 cancellation.

### 1.5 Event Log Is The Durable Agent State

OpenHands (14, section 2) is the cleanest open evidence. State is rebuilt from events, not from a hidden transcript:

- explicit statuses: `IDLE/RUNNING/PAUSED/WAITING_FOR_CONFIRMATION/FINISHED/ERROR/STUCK/DELETING` [14, section 2]
- `max_iterations` default 500 per run [14, section 2]
- stuck detector: scans max 20 events after latest user message; thresholds: repeated action/observation = 4, action/error loop = 3, monologue = 3, alternating pattern = 6 [14, section 2]
- tool call normalization: control-char sanitization for JSON, trailing garbage removal, alias normalization (`bash/command/execute/execute_bash → terminal`), typo normalization for `security_risk` [14, section 2]

SQL schema (DESIGNED composite from 14/15 cross-product): 
```sql
agent_runs(id, thread_id, status, max_iterations, created_at, updated_at)
agent_events(id, run_id, seq, event_type, source, payload jsonb, trace_id, span_id, created_at)
tool_calls(id, run_id, tool_name, args jsonb, status, approval_required, approved, resume_label, result jsonb, error, created_at)
checkpoints(id, thread_id, namespace, checkpoint_key, state jsonb, created_at)
```
[14, Cross-Product Architecture section]

**Byte-path implication:** Lancelot's `lancelot.context` ContextVars (`request_id`, `agent_id`, `trace_id`) are the transport-seam equivalent — they propagate per-request context into every OTel span and the `traceparent` header (W3C). The durable event store itself is user-side. But Lancelot's `lancelot.stream.ttft` + RED metrics + `X-Lancelot-Cost-USD` already provide the per-request observability primitives that feed into a Langfuse/Phoenix trace store.

### 1.6 Tool Output Offload Is Mandatory At Scale

Dust (15, section 3) exposes exact thresholds (KNOWN from monorepo source, commit `98533b9d`):
- text blocks over **20KB** → offload to file [15, section 3]
- image blocks over **2MB** → offload [15, section 3]
- resource blocks over **20MB** → offload [15, section 3]
- remote tool hard limits: 2MB text, 2MB image, 20MB resource, **50MB total result array rejection** [15, section 3]
- snippet length fed back to model: **8000 chars** [15, section 3]
- tool output processing concurrency: **10** [15, section 3]

Context pruning mechanism: previous interaction tool results pruned with placeholder `<dust_system>This function result is no longer available.</dust_system>`; current interaction progressively pruned oldest-to-newest; pruned token estimates 20 (prev) / 40 (current) [15, section 3].

**Byte-path implication:** Lancelot's `lancelot.mcp.mount` already warns on tool responses >4KB (the minimal-return discipline from M7). Dust's thresholds are the production-validated limits for the offload boundary. The 8KB WARN threshold in Lancelot's MCP is already below Dust's 20KB text offload threshold — appropriate for MCP but may need adjustment for direct tool responses.

### 1.7 Structured Output Parsing: BAML's Jsonish Parser

BAML (17, section 2) is the only source in this corpus that exposes a real structured-output repair-and-coercion mechanism (KNOWN from `bex_sap` source, commit `786aa508`):

The parse chain: `raw text → jsonish::parse(allow_markdown=true) → TyResolvedRef::coerce(ctx, target_ty, parsed)` [17, section 2]

Coercion scoring: flag-based additive scoring; `DefaultButHadUnparseableValue` penalizes 2; incompleteness has no penalty (intentional for streaming partials). Flags include: `ObjectFromFixedJson`, `JsonToString`, `ExtraKey`, `DefaultFromInProgress`, string-to-int/bool/null/char/float, non-integer number to int [17, section 2].

Generated companion functions per LLM function: `render_prompt`, `build_request`, `parse` — **LLM functions compile to three generated operations**, not a single prompt string [17, section 2].

**Byte-path implication:** Lancelot's `@app.tool` auto-derives `output_schema` (M7) and `lancelot.canonical.dumps_canonical` provides stable JSON. BAML's repair/coercion layer sits between the model response and the user's typed result — this is the model adapter translation layer Lancelot explicitly lacks (per CLAUDE.md "what Lancelot lacks"). The jsonish parser is the highest-signal mechanism in this category.

### 1.8 Observability Is A Queue System, Not A Logging Table

Langfuse (17, section 4) exposes exact BullMQ production tuning (KNOWN from `worker/src/app.ts`, commit `9cfaa757`):
- Trace delete + eval execution queues: `lockDuration: 60000`, `stalledInterval: 120000`, `maxStalledCount: 3`
- Default BullMQ lock duration causes high-CPU wait sensitivity — these overrides are production fixes [17, section 4]
- Storage split: Postgres (metadata), ClickHouse (analytics/traces), Redis (queues), S3/MinIO (large payloads) [17, section 4]

Queue topology (KNOWN): `TraceUpsertQueue` (sharded) → `CreateEvalQueue` → `EvalExecutionQueue` (sharded) → `LLMAsJudgeExecutionQueue` (sharded) → `CoreDataS3ExportQueue` → `DeadLetterRetryQueue` + `OtelIngestionQueue` + `EventPropagationQueue` [17, section 4].

Phoenix (17, section 5): OTLP/gRPC span ingest → `BulkInserter` → `SpanCostCalculator` daemon → `ExperimentRunner`; Prometheus metric `SPAN_QUEUE_REJECTIONS` for backpressure visibility [17, section 5].

**Byte-path implication:** Lancelot's OTel histograms (`lancelot.upstream.*`, `lancelot.http.stream.ttft`, `lancelot.http.queue_wait_ms`, `lancelot.http.cost.usd`) are the producers. Langfuse/Phoenix are the consumers. Lancelot already propagates `traceparent` (W3C) into `TransportPool` calls, so Langfuse trace trees would automatically link HTTP-layer spans to LLM-call spans without additional instrumentation.

### 1.9 Sub-Agent Isolation and Bounded Concurrency

Dust Deep Dive (15, section 3, KNOWN): max concurrent sub-agent tasks = **6**; sub-agents get self-contained atomic tasks and **no prior context** unless explicitly needed; primary agent owns plan, synthesis, final answer [15, section 3].

Mastra (14, section 3, KNOWN): default tool concurrency = **10**; forced to **1** when any tool requires approval or suspension [14, section 3].

**Byte-path implication:** `TransportPool.fanout(coros, first_n=, deadline=, cancel_laggards=True, concurrency=)` is the byte-path equivalent for sub-LLM-call fan-out. For sub-agent orchestration (where each "sub-agent" is a full agent loop, not just an LLM call), the 6 cap is the production-validated default to document in any Lancelot agent layer.

### 1.10 The White Space: What No Framework Owns

After mapping all 24 products:

**White space #1 — Stream-first observability with TTFT on every shape:** Every agent framework emits traces, but TTFT (time-to-first-chunk, the AI-latency metric) is absent from all framework-level trace data in this corpus. Lancelot already has `lancelot.http.stream.ttft` — this is a genuine differentiator on the byte path.

**White space #2 — Backpressure-aware batch coalescing (`@app.batch`):** No analogue in any product in this corpus. Composio batches multi-tool calls (`COMPOSIO_MULTI_EXECUTE_TOOL`) but not prefix-aware request grouping to a rate-limited LLM backend. Lancelot's ~12× throughput win is unchallenged.

**White space #3 — Canonical/KV-cache-stable serialization as a first-class primitive:** `lancelot.canonical.dumps_canonical` is referenced nowhere in these 4 documents. BAML's `render_prompt` produces stable templates, but output-side canonical JSON for prompt caching is absent. Lancelot owns this.

**White space #4 — Byte-path-level cost accumulation across sub-calls (`X-Lancelot-Cost-USD`, `cost_usd` ContextVar):** Langfuse/Phoenix track cost at the trace level (post-hoc). Lancelot accumulates cost in-band in the response trailer, including child `spawn_child` calls. No framework does this at the transport layer.

**Commoditized / skip:**
- Basic retry + exponential backoff: every product has this. TransportPool already has it with Retry-After.
- OpenAI-compatible `/v1/chat/completions` gateway routing: Portkey, Helicone, LiteLLM all do this. Not Lancelot's lane.
- LLM-as-judge eval runners: Langfuse, Phoenix, Braintrust. User-side.
- Vector store backends: Mem0 supports 30+ stores. Not Lancelot's layer.

---

## 2. Specific Mechanism-Level Findings

### 2.1 Production Loop Constants (compile table)

| Framework | Max iterations | Retry attempts | Retry initial delay | Retry max delay |
|---|---|---|---|---|
| OpenHands | 500 per run | 1 (tools non-idempotent) / 15 (interrupt retry) | — | — |
| Strands | — | 6 | 4s | 240s |
| Dust (Temporal) | 64 steps | 5 (model only), 1 (tools) | coefficient 1 | — |
| LlamaIndex | 20 | — | — | — |
| Haystack | 100 | — | — | — |
| Dify | min(max, 99) + 1 | — | — | — |
| Mastra routing | 1 (or caller-specified) | — | — | — |
| Google CUA | 10 | 5 | 1s, exp backoff | — |

Sources: [14, section 5, Patterns Worth Copying]; [15, section 1, Dust]; [17, section 1, Portkey]

The Strands retry `(max_attempts=6, initial_delay=4, max_delay=240)` [14, section 5] is the most-cited production-tested constant in this corpus. Lancelot's `TransportPool` already implements Retry-After honoring; the 6/4/240 constants are worth documenting as the default retry profile.

### 2.2 Approval/HITL Semantics (converged pattern)

All five top frameworks converge on the same shape:
- HITL is a **named status**, not an exception path
- approval metadata is stored **inside the assistant message** (Mastra: `metadata[toolCallId]`) or as a DB row with `resume_label`
- when approval or suspension can happen, tool execution concurrency is forced to **1** (Mastra KNOWN)
- resume via `Command(resume=...)` (LangGraph) / `resume(toolCallId, {approved: bool})` (Mastra)

[14, sections 3, 4; 15, section 1 (Dust); 16, section 6 (Relevance AI)]

### 2.3 Firecrawl Interaction Agent Constants (KNOWN)

From `apps/api/src/lib/scrape-interact/browser-agent.ts` (commit `5caac5e3`):
- `MAX_STEPS = 25`
- `SNAPSHOT_TIMEOUT = 15`
- `SNAPSHOT_MAX_CHARS = 40000`
- Model: `gemini-2.5-flash`, `temperature: 0`
- One exposed tool: `browser` (CLI command surface)

[15, section 2]

The browser agent uses `temperature: 0` for deterministic browser actions — contrast with Stagehand's `temperature: 1` for creative multi-step reasoning [15, section 1]. These are intentional: narrow closed-domain action (0) vs open-ended exploration (1).

### 2.4 Dust Temporal Workflow Constants (KNOWN)

- Queue name: `agent-loop-queue-v2`
- Model activity start-to-close timeout: **10 minutes**
- Model activity heartbeat: **60 seconds**
- Tool activity heartbeat: **60 seconds**
- `DEFAULT_MCP_REQUEST_TIMEOUT_MS = 3 * 60 * 1000` (3 min)
- `RUN_AGENT_CALL_TOOL_TIMEOUT_MS = 10 * 60 * 1000` (10 min)
- `RUN_MODEL_MAX_RETRIES = 5`, backoff coefficient 1
- Non-idempotent tools: `maximumAttempts: 1` — tools are NOT auto-retried
- Retryable tool retry: `RETRY_ON_INTERRUPT_MAX_ATTEMPTS = 15`
- Context token budget: `model.contextSize - model.generationTokensCount`
- Token batch flush interval: **100ms** default, max buffer age 60s

[15, section 3]

### 2.5 Exa Research Event Schema (KNOWN from SDK, commit `fe36f41d`)

Event types expose the architecture for any research-agent trace log:
`research-definition → plan-definition → plan-operation (think/search/crawl) → plan-output (tasks OR stop) → task-definition → task-operation → task-output → research-output`

Research output includes cost accounting: pages, searches, reasoning tokens, total cost dollars.
Polling: `pollUntilFinished` default interval 1000ms, timeout `10 * 60 * 1000 ms`, max consecutive failures 5.

[16, section 4]

**Byte-path implication:** Lancelot's SSE streaming (`@app.stream`) is the delivery mechanism for research-agent event traces. The event type taxonomy above maps cleanly to Lancelot SSE events. No new primitives needed — the user emits these event types through the existing streaming surface.

### 2.6 MCP Transport Production Details

Dust MCP (KNOWN from monorepo):
- SDK heartbeat: **5 minutes**; CLI heartbeat: **15 minutes**; reconnect delay: **5 seconds**
- SSE carry `eventId` + `Last-Event-ID` for reconnect
- Register via `POST mcp/register`, heartbeat via `POST mcp/heartbeat`

Arcade MCP (KNOWN from `arcade_mcp_server`):
- Server name validation: alphanumeric or underscore, no leading/consecutive underscores, must end alphanumeric
- Session IDs must be visible ASCII
- Transport: `stdio` or `http` (streamable); `Mcp-Session-Id`, `MCP-Protocol-Version`, `Last-Event-ID` headers

Stagehand MCP (KNOWN):
- `connectToMCPServer` paginates `listTools(cursor)`
- Tool resolution converts JSON Schema to Zod
- Client identity: `{name: "Stagehand", version: "1.0.0"}`
- MCP tool names **overwrite** native tools on conflict (footgun — Arcade makes conflicts explicit)

[15, section 1; 16, section 3.8; 15, section 1 Stagehand]

**Byte-path implication:** Lancelot's `lancelot.mcp.mount` provides MCP server mount. The Dust heartbeat constants (5/15 min) and reconnect delay (5s) are relevant for Lancelot MCP clients connecting upstream. The Arcade name-collision guard (explicit conflict vs Stagehand's silent overwrite) is an API ergonomics improvement worth adopting in `lancelot.mcp`.

### 2.7 CrewAI Memory Scoring Constants (KNOWN)

```python
score = 0.5 * semantic_score + 0.3 * (0.5 ** (age_days / 30.0)) + 0.2 * record.importance
```
Constants: semantic weight 0.5, recency weight 0.3, importance weight 0.2; recency half-life 30 days; consolidation threshold 0.85; consolidation limit 5; high confidence threshold 0.8; complex query threshold 0.7; recall oversample factor 2; batch dedup threshold 0.98.
[14, section 9]

**Byte-path implication:** User-side memory layer. These constants are the reference implementation if the user builds memory on top of Lancelot.

### 2.8 Portkey Conditional Router — Two-Level Path Limit

KNOWN gotcha: conditional routing only resolves two-level paths:
```ts
value = context[parts[0]]?.[parts[1]];  // e.g. metadata.user_id — NOT metadata.a.b.c
```
This is a known limitation. An agent routing config that passes `metadata.a.b.c` silently returns undefined. Document in Lancelot if a conditional routing layer is built.
[17, section 1]

### 2.9 Vellum Stable IDs from Class Names

KNOWN from `workflows/base.py` (commit `eed0189`):
```python
workflow_class.__id__ = uuid4_from_hash(workflow_class.__qualname__)
workflow_class.__output_ids__ = {
  ref.name: uuid4_from_hash(f"{workflow_class.__id__}|id|{ref.name}")
  for ref in workflow_class.Outputs
}
```
This makes code/UI sync deterministic — same class name always produces the same UUID. Directly copyable for any workflow graph layer.
[17, section 3]

---

## 3. Anti-Patterns Found (Confirmed Across ≥2 Sources)

1. **Single in-memory chat array as only state** — never the source of truth; event log is the primitive. [14, Anti-Patterns]
2. **Parallel tool execution when tools mutate shared resources without resource locks** — OpenHands and Mastra both enforce sequential when approval/suspension possible. [14, section 2, 3]
3. **RAG as a replacement for agent memory** — RAG retrieves documents; memory stores run-specific facts, decisions, preferences. Separate primitives. [14, Anti-Patterns]
4. **Model streaming directly into business logic before normalization** — Strands normalizes provider quirks (blank text around toolUse removal, invalid tool name replacement, JSON repair) before the loop sees results. [14, section 5]
5. **Max-iteration check without stuck detection** — max iterations alone lets the agent spin on an error loop. Add stuck detector first. [14, section 2, Anti-Patterns]
6. **One giant manager prompt for every product** — runtime state machines (Dify/Mastra/OpenHands) matter more than role prompts. [14, Anti-Patterns]
7. **MCP tool names silently overwriting native tools** (Stagehand bug) — make conflicts explicit at registration. [15, section 1]
8. **Exposing raw SaaS API exception messages to the LLM** — Arcade separates `message` (agent-facing, stable, terse) from `developer_message` (full fidelity for logs). [16, section 3.4]

---

## 4. Byte-Path Lens Summary

| Finding | Lancelot already has it | Lancelot changes/adds | Out of scope (user-side) |
|---|---|---|---|
| Gateway routing (fallback/loadbalance/conditional/retry) | `TransportPool` covers outbound LLM calls | Retry-After already honoring; conditional routing could add 2-level path routing if needed | — |
| TTFT metric | `lancelot.http.stream.ttft` | — | — |
| Batch coalescing | `@app.batch` (~12× win, no analogue elsewhere) | — | — |
| Canonical/KV-cache JSON | `lancelot.canonical.dumps_canonical` | — | — |
| Cost accumulation in-band | `cost_usd` ContextVar + `X-Lancelot-Cost-USD` trailer | — | — |
| Tool schema auto-derivation + MCP mount | `@app.tool` + `lancelot.mcp.mount` | MCP name-collision guard (Arcade pattern worth adding) | — |
| Structured output repair/coercion | None | BAML jsonish parser (`render_prompt/build_request/parse` triad) would be the model adapter layer | — |
| Agent memory (Mem0 write/search) | None (request-scoped ContextVars only) | — | User owns (Mem0 MCP tools usable via `lancelot.mcp.mount`) |
| Event-sourced agent state machine | None | — | User owns (OpenHands/Dust patterns) |
| HITL/approval status | None | — | User owns (Mastra/OpenHands patterns) |
| Stuck detection | None | — | User owns (OpenHands thresholds) |
| Eval runner (Langfuse/Phoenix/Braintrust) | OTel histograms as producers | — | User owns trace consumers |
| Tool auth (OAuth, Arcade pattern) | `lancelot.secrets` (5 backends) | Arcade's agent/dev error split is worth adopting in tool error handling | User owns OAuth flow |
| Durable workflow (Temporal/LangGraph checkpoints) | Graceful drain + SIGTERM handling | — | User owns workflow layer |
| Sub-agent fan-out | `TransportPool.fanout` (byte path) | — | User owns agent orchestration |

---

## 5. Depreciating vs Durable

**Durable (stable across 3+ model generations):**
- Event-sourced state machine + typed statuses
- Stuck detection with explicit thresholds
- Tool call normalization (production models always malform JSON sometimes)
- Approval/HITL as first-class status
- Memory write path (additive, entity-linked)
- Output offload thresholds (Dust's 20KB/2MB/20MB/50MB)
- Max iteration + graceful last-iteration tool removal (Dify)
- Retry-After honoring
- Static prompt / dynamic context separation for KV-cache

**Depreciating (churns on ~1-2yr model clock):**
- Specific model IDs in tool/voice configs (Retell's model enum with `gpt-5.4-nano`, `claude-4.6-sonnet` etc. — these churn)
- Native function calling LLM detection (models converge; ReAct fallback becomes vestigial)
- Per-provider stream normalization quirks (Strands blank-text-around-toolUse removal etc.) — fade as provider APIs stabilize
- FARA training set details (Qwen2.5-VL-7B base, 145k trajectories) — specific to a model version
- Tavily/Nia embedding model choices (`embed-english-v3.0`, `zembed-1`) — replaced by next embedding generation
- Specific LLM-rerank model (`gpt-4o-mini` at 0.0 temperature, max 100 tokens) — model-dependent

---

## 6. API Shape Notes Worth Stealing

**Gumloop async agent contract** (KNOWN from npm `gumloop@1.0.2` + docs):
```
POST /start_agent {gummie_id, message, user_id, project_id, interaction_id?}
→ {interaction_id, status: "processing"}
GET /agent_status/{interaction_id}?user_id=&project_id=
→ {state: ASYNC_PROCESSING|COMPLETED|FAILED, messages?, response?}
```
Continuation: pass previous `interaction_id`; previous must be in terminal state. Sanitized message parts: `{id, role, timestamp, parts, content}`. Part types: text, tool_invocation (with toolCallId/toolName/toolCallState/result/args), file, error.
[17, section 10]

This is the right shape for an async agent endpoint. Lancelot's SSE streaming (`@app.stream`) is the streaming alternative. Both patterns are valid — polling for long async jobs, streaming for interactive.

**Vellum line-delimited JSON stream** (KNOWN):
`POST v1/workflow-deployments/{id}/execute-stream` returns newline-delimited JSON events. Bad lines swallowed. Error taxonomy: 400 BadRequestError, 404 NotFoundError, 500 InternalServerError.
[17, section 3]

**Dust tool policy shape** (KNOWN):
```ts
type ToolConfig = {
  stake: "high" | "medium" | "low" | "never_ask";
  retryPolicy: "no_retry" | "retry_on_interrupt";
  outputLimits: { textBytes: 20480, imageBytes: 2097152, resourceBytes: 20971520, totalBytes: 52428800 };
  timeoutMs: number;
}
```
[15, Pattern 2]

**Braintrust queue** (KNOWN from `js/src/queue.ts`, commit `9be62fc`):
`DEFAULT_QUEUE_SIZE = 15000`; bounded mode drops NEW items when full (source code), not oldest (comment is wrong). WeakMap instrumentation plugin pattern: `{span, startTime}` keyed by event object.
[17, section 6]

---

*Sources: `core/14_EXTERNAL_AGENTIC_PRODUCTS_RE.md`, `core/15_TOP_AGENTIC_STARTUPS_RE.md`, `core/16_MISSING_TOP_AGENTIC_STARTUPS_RE.md`, `core/17_NEXT_TOP_AGENTIC_INFRA_STARTUPS_RE.md`. All KNOWN claims are from pinned source commits; INFERRED/DESIGNED claims are labeled accordingly.*
