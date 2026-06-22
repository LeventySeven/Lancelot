# Low-Code / Visual Agent Builders — RE Distillation
## Sources: DIFY.md · LANGFLOW.md · OKIBI.md

> Focus: The graph-as-product trap — what these builders bury, why visual graphs anti-scale, and what Lancelot must NOT be.
> All citations are file+section relative to `/Users/seventyleven/Desktop/researchfms/teardowns/`.

---

## The core thesis (what the three sources converge on)

All three systems — Dify (open-source workflow graph), Langflow (visual flow builder → deployed REST), Okibi (NL → DAG → code) — make the **graph the product**. The result in every case:

1. The graph engine becomes the bottleneck, not the byte path.
2. Every non-trivial production requirement (streaming backpressure, disconnect cancellation, per-tenant rate limits, durable pause/resume) requires bolting onto the graph — and it shows in the seams.
3. The surface area explodes. Dify's `graphon` package is now an external dependency managing BSP worker-pool execution, VariablePool, and node types. Langflow's `ServiceManager` is 300+ lines of hand-rolled DI just to reach services from within graph nodes that run off the event loop. Okibi is pure scaffolding: NL → DAG JSON → TypeScript templates — the "agent" is visual chrome over generated code.

**Lancelot must not build the graph layer.** The byte path IS the product. The graph, if any, belongs on the user side.

---

## Anti-Pattern 1: The graph engine swallows the byte path (Dify)

**Source:** `DIFY.md §1, §CE-1`

Dify externalised its graph engine into `graphon` (`workflow_entry.py:29-36`, `graphon==0.4.0`). What stays in Dify's own repo: a `DifyNodeFactory` (`node_factory.py:11-46`) that injects Dify-specific bindings (the Jinja2 renderer, SSRF proxy, TokenBufferMemory, plugin resolver) into each `graphon` node. The generic execution — IfElse, Iteration, Loop, ParameterExtractor, VariableAggregator, TemplateTransform — all moved into the package.

The tell: `Iteration` nodes with `parallel_nums=10` (default, `iteration_node.py:85-86`) use a `ThreadPoolExecutor` (`max_workers=min(parallel_nums, len)`, `:257-259`) — a sync thread pool running inside an asyncio app. This is the GIL-across-threadpool smell. Each parallel iteration spins a nested `GraphEngine` (`_create_graph_engine`, `:231`). Streaming in this context means events bubble through multiple engine layers before reaching the HTTP response.

**Lancelot byte-path implication:** `TransportPool.fanout(coros, first_n, deadline, cancel_laggards, concurrency)` already owns the parallel-fan-out primitive at the transport layer, GIL-released, without a graph engine. The Iteration node pattern is what you get when the graph IS the product and fan-out must be expressed as a node rather than a transport call.

---

## Anti-Pattern 2: Custom DI to escape the request context (Langflow)

**Source:** `LANGFLOW.md §1.5, R2.1, R2.2`

Langflow's defining architectural problem: FastAPI's `Depends` is per-request and event-loop bound. Graph nodes run in `ThreadPoolExecutor` threads (sync), the lifespan runs before any request exists, and background tasks run without a request object. So `Depends` cannot serve these contexts.

The solution: a global `ServiceManager` (`lfx:services/manager.py:43-309`, KNOWN verbatim) with `get_service(ServiceType, default_factory)` reachable from any context. It has:
- Per-service `KeyedMemoryLockManager` locks (not one global lock — that would serialize all first-touches)
- Two resolution paths: legacy factory + type-hint introspection via `infer_service_types(factory.create, globalns=all_service_classes)` (`factory.py:42-63`)
- Plugin discovery from entry-points + TOML
- Circular-dependency detection (direct only — transitive cycles recurse to stack overflow)
- The fragile part: `param_type.__name__.upper().replace("SERVICE", "_SERVICE")` maps `DatabaseService → DATABASE_SERVICE` — rename a class and the mapping silently breaks (`factory.py:54`)

**What this means for Lancelot:** the `lancelot.context` ContextVars (`cost_usd`, `agent_id`, `tenant_id`, `request_id`, `trace_id`) are already the right primitive for passing cross-request state without a service registry. The ASGI seam sets them in middleware, and any coroutine inherits them. Langflow needed a process-global registry because graph nodes run in sync thread pools off the event loop. Lancelot's async-first design avoids this problem by design — but if agent tools ever run in sync threads (`run_in_executor`), ContextVar propagation requires explicit copying (ContextVars do NOT propagate through `run_in_executor` by default — they must be snapshotted and restored).

**Concrete copy:** the `session_scope` family's `except HTTPException: rollback-silent / except Exception: log+rollback` split (`lfx:services/deps.py:194-270`, KNOWN verbatim) is a directly transplantable pattern for any endpoint-level DB session. The read-only variant (`session_scope_readonly()`) that skips commit avoids write locks on read paths — a real Postgres throughput win at high concurrency.

---

## Anti-Pattern 3: Three divergent streaming formats in one codebase (Langflow)

**Source:** `LANGFLOW.md §1.7, §1.8, §1.9, §4.3`

Langflow has **three streaming styles** for what is conceptually one thing (stream agent output):
1. **Flow-build** (`api/build.py:210-213`): `media_type="application/x-ndjson"`, `{"event": <type>, "data": <jsonable>}\n\n` framing, `EventManager` queue
2. **Deployed-flow run** (`api/v1/endpoints.py:517-521`): `media_type="text/event-stream"`, same JSON-object-per-line framing
3. **Per-vertex classic SSE** (`api/v1/chat.py:616`): real `event:\ndata:\n\n` framing via `StreamData.__str__`

This is the documented result of historical drift across teams. A client must handle all three. The teardown notes this explicitly as an anti-pattern (`LANGFLOW.md §1.11`).

**Lancelot byte-path implication:** `@app.stream` + `lancelot.http.stream.ttft` already picks one wire format. The lesson is to pick it once and hold it. Lancelot's SSE path (`event: data\ndata: <bytes>\n\n` + `[DONE]`) should not grow a second framing style for "agent output" vs "tool output."

---

## Anti-Pattern 4: The graph IS the HITL mechanism — pause/resume complexity explodes (Dify)

**Source:** `DIFY.md §3, §9`

Dify's `agent_v2` node supports durable pause/resume for human-in-the-loop (`agent_node.py:173, 258`). The session snapshot is keyed `(workflow_run_id, node_id, agent_config_snapshot_id)` (`session_store.py:40`), stored to the DB. On resume, `load_active_snapshot` re-enters the agent backend session. Separately, workflow-level HITL (`human_input_policy.py`) adds `PauseReasonType.HUMAN_INPUT_REQUIRED` with a `form_token` + `expiration_time` credential so the workflow engine can pause and resume at the graph level.

The result: two overlapping HITL mechanisms (agent-level snapshot resume + graph-level form pause), each with its own session/snapshot model, each requiring DB persistence. The teardown explicitly calls this out as the "biggest architectural advance over core/14" (`DIFY.md §3`) — meaning it took a complete architectural revision to add it, not an incremental feature.

**Lancelot byte-path implication:** durable pause/resume (the "durable seam" in the lens) is not in scope for Lancelot's byte path, and Dify's implementation shows why it shouldn't be bolted onto a transport layer. It requires DB-backed snapshot storage + credential-gated resume URLs + multi-layer session management. This belongs on the user side (LangGraph checkpoints, durable workflow engines). The byte path's contribution is making the HITL HTTP round-trip fast and reliable, not owning the checkpoint.

---

## Anti-Pattern 5: The "visual graph = product" failure mode (Okibi)

**Source:** `OKIBI.md §1, §7, §16, §19`

Okibi's bet: "Lovable for agents." Natural language → DAG → deployed TypeScript. The teardown's verdict: **Wrapper (with potential)** (`OKIBI.md §19`). The actual primitive: LLM generates follow-up questions (standard chat completion) → LLM generates TypeScript code from conversation → code references external APIs via service nodes (standard API orchestration) → code deployed to their EC2.

The graph (`flowchart: {nodes: FlowchartNode[], edges: FlowchartEdge[], ultimate_goal: string}`) is a **visualization** of the generated code's structure, not an execution primitive. The execution model is entirely server-side black box (`/api/run`, `/api/deploy`, `/api/agents` — all 307 when unauthenticated, `OKIBI.md §5`). The teardown notes: "The question is whether 'agent' is different enough from 'app' to warrant a new primitive. If agents are just apps with LLM calls, then no."

The product differentiates against Zapier/Make/Lindy by criticizing "visual flow-based, slow, requires flowchart configuration" — and then builds a visual flowchart as the output. This is the graph-as-product trap restated: the founders correctly identified the UX problem (visual builders anti-scale) but shipped the same visual representation as the artifact, not the underlying agent runtime.

**What Okibi reveals about the market:** the real primitive customers want is "describe agent in English, get running agent" — not the graph. The graph is only valuable as a debugging/auditing view, not as the configuration surface. Lancelot's surface (`@app.tool`, `TransportPool`, `@app.batch`, `@app.stream`) IS that: configuration by code, with the byte path as the primitive.

---

## Anti-Pattern 6: ReAct over-engineering the stop sequence (Dify)

**Source:** `DIFY.md §2, §2.1`

Dify's CoT/ReAct runner (`cot_agent_runner.py:64`, KNOWN) appends `"Observation"` to the model's stop sequences automatically. This is not optional — the stop sequence IS the ReAct control loop mechanism (the model halts after proposing an action, Dify injects the tool result, next iteration). The model is called **with `tools=[]`** — native function calling is disabled; the model must emit a JSON `Action` blob matching the verbatim prompt template (`template.py:1`).

The last-iteration safety valve (`cot_agent_runner.py:108`): strip `_prompt_messages_tools = []` on `iteration_step == max_iteration_steps` to force a final answer. If the model still tries a non-final tool call at the last iteration, raise `AgentMaxIterationError` (`cot_agent_runner.py:178`). The cap is `min(app_config.agent.max_iteration, 99) + 1`.

**What this is:** a working production ReAct loop with production-validated stop constants. The verbatim template (`template.py:1`) and the `max_iteration = min(N, 99) + 1` cap are directly usable by Lancelot's agent loop. The key insight: **the stop sequence mechanism is the loop control**, not function calling. For models that handle native tool calls poorly (or for strict token-accounting reasons), this is the fallback.

**Lancelot byte-path implication:** Lancelot lacks the agent loop entirely (per the lens). The Dify CoT runner is a reference implementation of the 5-10 line durable loop. The byte path (`TransportPool.request`, `stream`) is what the loop calls for each LLM invocation — that seam is already clean.

---

## Durable primitives (copyable — not hype, persists past model clock)

### 1. `DisconnectHandlerStreamingResponse` — 22-line drop-in for streaming cleanup

**Source:** `LANGFLOW.md §1.7, R2.5`

`api/disconnect.py:1-32` (KNOWN, verbatim). Subclasses `StreamingResponse`, overrides `listen_for_disconnect(receive)`. When `message["type"] == "http.disconnect"` fires (Starlette already races body against this), invokes `self.on_disconnect()` callback — which in Langflow's case cancels the build `asyncio.Task` + finalizes the `EventManager`. Supports both sync and async callbacks (`if asyncio.iscoroutine(coro): await coro`).

**Lancelot byte-path implication:** Lancelot already has client-disconnect → upstream cancel (C2) in `@app.stream`. This is the orthogonal case: a **long-running non-streaming handler** (an agent turn that buffers output before responding) that should abort if the client disconnects. Lancelot's `cancel_on_disconnect` (M15, flag on route decorator) is the right place — this confirms the mechanism: a `is_disconnected()` poll race is the correct approach when not using streaming.

### 2. Three-mode event delivery (stream | direct | poll) off a single job queue

**Source:** `LANGFLOW.md R2.5, §1.7`

`get_flow_events_response` (`api/build.py:119-214`, KNOWN verbatim). Same `asyncio.Queue` backing all three delivery modes:
- `STREAMING`/`DIRECT`: live `DisconnectHandlerStreamingResponse` draining the queue
- `POLLING` (default): drain all currently available events non-blocking (`while not main_queue.empty()`), block on ONE if empty, return joined NDJSON

The job outlives any single request. The client reattaches by re-calling the events endpoint with the `job_id`. This is the correct shape for agent turns behind flaky HTTP (proxies with idle timeouts, browser page reloads).

**Lancelot byte-path implication:** `@app.stream` owns the SSE/streaming path. But for agent turns that might exceed proxy timeouts, the job-queue + poll-mode pattern is what you add. The Lancelot primitive would be: POST `/agent/run` → `{job_id}`, GET `/agent/run/{job_id}/events` → SSE or poll. The byte path (`@app.stream`, `lancelot.http.stream.ttft`) serves the streaming leg; a small coordinator (asyncio.Queue + task handle) enables the poll leg. This is not in scope for the byte path itself but is the right shape for agent turn delivery.

### 3. `EventManager.send_event` — cross-thread queue enqueue

**Source:** `LANGFLOW.md §1.8`

`lfx:events/event_manager.py:71-99` (KNOWN). `send_event` detects whether the caller is on the event loop or a sync thread: if in-loop, `queue.put_nowait(item)`; if off-loop (threadpool executor), `loop.call_soon_threadsafe(queue.put_nowait, item)`. `__getattr__` returns `self.events.get(name, self.noop)` — unregistered `on_*` calls are harmless no-ops.

**Lancelot byte-path implication:** if Lancelot agent tools ever call back to the HTTP response from a sync thread (`run_in_executor`), this `call_soon_threadsafe` pattern is mandatory. Lancelot's current streaming pump (`src/stream.rs`) is already on-loop; this matters for synchronous tool implementations that emit progress events.

### 4. `session_scope` — `HTTPException` vs real-error rollback split

**Source:** `LANGFLOW.md R2.3`

`lfx:services/deps.py:194-270` (KNOWN verbatim). `except HTTPException: rollback silently, re-raise` (no log spam for every 404/403). `except Exception: log at error level, rollback, re-raise`. Guards rollback with `if session.is_active: with suppress(InvalidRequestError)` — SQLAlchemy may have already rolled back, calling `.rollback()` again raises `InvalidRequestError`.

Not a byte-path primitive. Transplantable to any FastAPI + SQLAlchemy app in the agent layer.

### 5. Hybrid retrieval score-threshold forcing (Dify)

**Source:** `DIFY.md §5.2`

For `HYBRID_SEARCH`, the embedding-time `score_threshold` is **forced to 0.0** (`retrieval_service.py:320`, KNOWN). Reason (code comment citing GitHub #35233): embedding similarity scores are not comparable to fused/reranked scores — applying the user threshold at vector-retrieval time would drop valid high-quality chunks. The threshold is applied AFTER fusion: `vector_weight·cosine + keyword_weight·bm25`, sort desc, apply threshold to the fused score.

Not a byte-path primitive. Relevant to any RAG retrieval the agent layer builds. The constant: force embedding threshold to 0 for hybrid, score after fusion.

### 6. Plugin model daemon — providers as RPCs, no SDK in core (Dify)

**Source:** `DIFY.md §CE-4`

`api/core/plugin/impl/model.py` (KNOWN). Every model provider is a plugin dispatched to a daemon via HTTP `plugin/{tenant_id}/dispatch/model/<op>`. `PluginModelClient` has no provider SDKs — `fetch_model_providers`, `get_model_schema`, `validate_provider_credentials`, `invoke_llm` are all RPCs. The daemon runs each provider's code in a sandboxed process.

**Lancelot byte-path implication:** `lancelot.llm.TransportPool` is already the transport primitive. The plugin daemon pattern is what you add when you need runtime-pluggable providers (install Bedrock without restarting). For the startup/AI-infra use case, `TransportPool` with env-configured endpoints is sufficient. The daemon adds operational complexity (separate process, RPC latency) only justified when third-party provider plugins must be isolated.

---

## Tactical hacks (depreciating, ~1-2yr model clock)

These are things these frameworks do that are tied to current model limitations:

| Hack | Source | Why it depreciates |
|---|---|---|
| ReAct stop-sequence at `"Observation"` (CoT runner) | `DIFY.md §2` | Current models increasingly support native function calling; the stop-sequence JSON-blob approach is a workaround for models that don't. Native FC is cleaner when available. |
| `max_iteration = min(N, 99) + 1` hard cap | `DIFY.md §2, §7` | The cap exists because current models get stuck in loops. Better stuck-detection (repeated tool + same input, confidence drop) is more principled, but the constant is still useful as a safety net. |
| ParameterExtractor `reasoning_mode` function_call vs prompt fallback | `DIFY.md §CE-1` | As model structured-output support matures, the `"prompt"` fallback (`_extract_complete_json_response`) becomes dead code. |
| Okibi's multiple-choice follow-up questions for agent spec | `OKIBI.md §7` | A workaround for models that hallucinate scope without clarification. As models improve at interpreting ambiguous NL, the 5-question refinement loop shrinks. |

---

## What Lancelot must NOT be (the direct answer to the focus question)

1. **Do not build a graph DSL or graph execution engine.** Dify's `graphon` package + Langflow's `ServiceManager` are the tax that visual graph products pay. They solve problems (fan-out, branch, loop, variable aggregation) that `TransportPool.fanout`, plain Python `if/else`, and `asyncio.gather` already solve on the user side. Adding a graph layer to Lancelot would make the byte path a second-class citizen behind the graph runtime.

2. **Do not make the visual graph a configuration surface.** Okibi's verdict: the graph is valuable as a debugging/auditing view but not as the configuration surface. Lancelot's configuration surfaces are `@app.tool`, `@app.stream`, `@app.batch`, `@app.route` — pure Python, no visual layer needed.

3. **Do not multiplex streaming formats.** Langflow has three streaming formats in one codebase because the graph grew multiple output surfaces independently. Hold the one format (`text/event-stream`, `data: <bytes>\n\n`, `[DONE]`) and refuse to grow a second.

4. **Do not bolt HITL/durable-pause onto the byte path.** Dify's `agent_v2` pause/resume is 3002 lines across 13 files. It's correct for Dify (the graph IS the product). It's Tier-D for Lancelot (user-side orchestration concern).

5. **Do not own agent strategies as plugins.** Dify's plugin agent strategy system (`"{plugin_id}/{name}"` resolution, `AgentMessageTransformer` 8-type event dispatch) is 600+ lines and replaces direct Python function calls. The byte path's job is fast transport; the strategy is 5-10 lines of Python the user writes.
