# Agno — Mechanism-Level Teardown for Lancelot

**Source:** `researchfms/teardowns/AGNO.md` — RE of `agno==2.6.9` @ commit `1444c9b`, `libs/agno/agno/`
**Date:** 2026-06-22
**Focus:** Performance-claimed agent framework. Loop architecture, memory, HITL, structured output, multi-agent delegation — with byte-path lens applied throughout.

---

## TLDR

Agno's headline claim ("~3 µs instantiation, ~6.5 KiB memory") is real and the mechanism is visible: `Agent` is a `@dataclass(init=False)` whose `__init__` does nothing but attribute assignment; every expensive operation (DB connect, tool resolution, MCP handshake, thread pool creation) is deferred to first `run()`. This is an architectural choice, not a benchmark trick. The loop lives in `Model.response()` at `models/base.py:697`, not in `Agent`. The perf claim is real for what it measures but the first `run()` pays all the deferred cost.

---

## 1. The Loop — exact mechanism

**Source: `models/base.py:697-858`** (KNOWN, file:line from teardown)

The canonical ReAct loop is a method of `Model`, not of `Agent`. `Agent.run()` calls `call_model_with_fallback(...)` exactly once; that enters `Model.response()` which owns the `while True:`.

```python
# models/base.py:697-858 (faithful pseudocode)
while True:
    # (a) mid-loop compression if token threshold crossed
    if compression_manager.should_compress(messages): compression_manager.compress(messages)

    # (b) one provider call
    assistant_message = call_provider(messages, tool_schema, tool_choice)
    messages.append(assistant_message)

    # (c) no tool calls → done
    if not assistant_message.tool_calls: break                      # :858

    # (d) run tools, accumulate results
    results = run_function_calls(calls, limit=tool_call_limit)
    append_tool_messages(messages, results)

    # (e) break conditions (checked after running tools, not before)
    if any(r.stop_after_tool_call for r in results): break          # :832
    if any(r.requires_confirmation for r in results): break         # :836  HITL
    if any(r.external_execution_required for r in results): break   # :840  HITL
    if any(r.requires_user_input for r in results): break           # :844  HITL
    if run_response.requirements and any(not req.is_resolved()):break# :850  member-agent HITL propagation
    continue
```

**Critical: there is NO `max_turns` on the inner loop.** The only cap is `tool_call_limit` (default `None` = unlimited, `agent/agent.py:169`). Contrast: OpenAI Agents SDK defaults to `max_turns=10`. Agno relies on the model stopping or `stop_after_tool_call`. **Design implication for Lancelot:** add a hard turn ceiling — an unlimited `while True` driven only by a tool cap can spin if the model emits non-tool, non-final content.

**Tool-call limit is cumulative, not per-turn** (`models/base.py:2310-2328`): the count passes in as `current_function_call_count` each iteration, so `tool_call_limit=20` means 20 total calls across the whole run, not 20 per loop turn. Over-limit calls receive a synthetic error result (`create_tool_call_limit_error_result`) and the model is expected to wrap up on the next turn.

**Four copies of the loop exist** (`models/base.py`): `response()` (sync buffered, `:697`), `response_stream()` (sync streaming, `:1366`), `aresponse()` (async buffered, `:879`), `aresponse_stream()` (async streaming, `:1645`). They are kept in lockstep by hand — a maintenance burden.

---

## 2. Zero-work constructor — the perf claim's actual mechanism

**Source: `agent/agent.py:376` + `agent/_init.py` + `agent/agent.py:714-734`** (KNOWN)

`Agent` is `@dataclass(init=False)` with a hand-written `__init__` that does pure attribute assignment (~300 fields). Deferred to first `run()`:

| Deferred operation | Deferred to |
|---|---|
| DB connection | `_run.py:407` `read_or_create_session` |
| Tool resolution + Toolkit expansion | `_run.py:453` `get_tools` + `determine_tools_for_model` |
| MCP tool initialization | `_run.py`, `_mcp_tools_initialized_on_run=[]` at `:694` |
| Callable tool factory invocation | Same as above |
| Thread pool creation | `background_executor` `@property` at `:714`, first access only |
| Learning machine init | `learning_machine` `@property` at `:725` |
| `get_models()` | Only normalizes string→`Model` instance at `:707`, no network |

**Lancelot byte-path lens:** the zero-work constructor is the right design pattern for the agent layer riding Lancelot. Since `TransportPool` is already instantiated at server init, the agent object itself should be equally cheap to construct per-request. The deferred cost model matches Lancelot's `@dataclass`-style handler pattern.

---

## 3. Background concurrency during the model call

**Source: `agent/_managers.py`, `agent/_run.py:492-509`, `agent/agent.py:716-718`** (KNOWN)

Three side-effect pipelines are submitted to `ThreadPoolExecutor(max_workers=3)` named `"agno-bg"` while the model call is in flight (step 6 of the 16-step pipeline), then joined at step 14:

1. `start_memory_future` (`:492`) → MemoryManager LLM call
2. `start_learning_future` (`:500`) → LearningMachine extraction
3. `start_cultural_knowledge_future` (`:509`) → CultureManager extraction

Each takes `existing_future` to avoid double-submit on retry. `wait_for_open_threads` is also called on the HITL-pause early-return path (`_run.py:560-563`) so background work is never orphaned when a run pauses.

**Lancelot byte-path lens:** this is the correct pattern for Lancelot's agent layer. Memory extraction should run concurrently with (not after) the model call. Lancelot's `TransportPool.fanout` (`first_n`, `deadline`, `cancel_laggards`) is the async analog — use it instead of a raw `ThreadPoolExecutor` so the work runs on the asyncio loop (no thread-pool overhead, no GIL re-acquire latency for I/O-bound LLM calls).

---

## 4. Agentic memory — the MemoryManager pattern

**Source: `memory/manager.py:967-1049`** (KNOWN)

The memory system is **agentic**: a `MemoryManager` (its own `Model` instance) decides what to remember by calling `add_memory`/`update_memory`/`delete_memory`/`clear_memory` as tools. This is not a semantic-similarity lookup; it is a full LLM call per run when `enable_agentic_memory` is set.

**MemoryManager system prompt (verbatim, `memory/manager.py:991-1034`):**
```
You are a Memory Manager that is responsible for managing information and preferences about the user.
You will be provided with a criteria for memories to capture in the <memories_to_capture> section
and a list of existing memories in the <existing_memories> section.

## When to add or update memories
- Decide if a memory needs to be added, updated, or deleted based on the user's message OR if no changes are needed.
- If the user's message meets the criteria … and that information is not already captured … you should capture it.
...
<existing_memories>
ID: {id}
Memory: {memory}
...
</existing_memories>
```

**Memory search prompt (verbatim, `memory/manager.py:682-692`):**
```
Your task is to search through user memories and return the IDs of the memories that are related to the query.
<user_memories>
ID: {memory_id}
Memory: {memory}
Topics: {topics}
...
</user_memories>
REMEMBER: Only return the IDs of the memories that are related to the query.
```

Storage schema (`db/postgres/schemas.py:29-39`): `MEMORY_TABLE_SCHEMA` = `memory_id` (PK), `memory` (JSONB), `user_id` (indexed), `agent_id`/`team_id`, `topics` (JSONB), `created_at`/`updated_at` (indexed).

Session storage (`db/postgres/schemas.py:11-27`): `SESSION_TABLE_SCHEMA` = `session_id` (PK), `session_type` (indexed), `runs` is a JSONB array **inline on the session row** (not a separate table), `summary` JSONB, timestamps as BigInteger epoch.

**Note on memory manager cost:** when `update_memory_on_run=True`, every user turn issues one background LLM call to the MemoryManager. This is a meaningful cost multiplier. The §9.2 search prompt uses ID-returning structured output — cheap to parse, but still one extra call per retrieval.

**Lancelot byte-path lens:** Lancelot's `MemoryPool(total_bytes)` is byte-OOM management, not agent memory. The agentic memory pattern (LLM-as-editor of a memory store via tools) is something Lancelot's agent layer needs to build. `lancelot.upstream_metrics` already handles cost attribution for the background MemoryManager call; the `cost_usd` ContextVar would accumulate it automatically if memory extraction is dispatched via `TransportPool`.

---

## 5. HITL loop — the exact pause/resume mechanism

**Source: `models/base.py:836-844`, `agent/agent.py:1498-1560`** (KNOWN)

**Pause:** the loop breaks (`:836`, `:840`, `:844`) when any tool execution in the just-completed batch has `requires_confirmation`, `external_execution_required`, or `requires_user_input` set on its `ToolExecution` record. The run returns a *paused* `RunOutput` carrying the pending `ToolExecution`s (which contain the model's requested arguments but no result yet).

**Resume** via `continue_run(run_id, session_id, updated_tools: List[ToolExecution])` (`agent/agent.py:1498`): the caller resolves each `ToolExecution` (sets `confirmed=True`, fills `result` for external, fills `user_input_schema` values), passes the list to `continue_run`, which reloads the paused run, **splices the resolved results back into message history, and re-enters the same `Model.response()` loop**. Same `run_id` is preserved — this is re-entry, not a new run. Async pair: `acontinue_run` (`:1574`).

**Team HITL propagation:** member-agent pauses propagate up via `run_response.requirements` (the `models/base.py:850` break) so a team pauses when any member pauses. `broadcast` (`adelegate_task_to_members`) propagates pauses after all members complete (`:1395-1400`).

**Lancelot byte-path lens:** the HITL state machine is what Lancelot's "durable seam" needs. The event constants from §6 (`RunPaused`, `run_paused="RunPaused"`) are the wire values. `continue_run` is the re-entry primitive. Lancelot doesn't have this yet; the pattern to copy is: loop breaks → persist paused `RunOutput` (with pending `ToolExecution` objects) → caller resolves → `continue_run(updated_tools=...)` → re-enter loop.

---

## 6. Streaming event schema — exact wire values

**Source: `run/agent.py:146-191`** (KNOWN)

Agno streams typed `RunOutputEvent` objects. Full event enum (wire values):

| Phase | Event → wire value |
|---|---|
| Lifecycle | `run_started="RunStarted"`, `run_content="RunContent"`, `run_content_completed="RunContentCompleted"`, `run_completed="RunCompleted"`, `run_error="RunError"`, `run_cancelled="RunCancelled"`, `run_paused="RunPaused"` |
| Tools | `tool_call_started="ToolCallStarted"`, `tool_call_completed="ToolCallCompleted"`, `tool_call_error="ToolCallError"` |
| Reasoning | `reasoning_started`, `reasoning_step="ReasoningStep"`, `reasoning_content_delta="ReasoningContentDelta"`, `reasoning_completed` |
| Memory | `memory_update_started/completed` |
| Model | `model_request_started/completed` |
| Compression | `compression_started/completed` |

`content_type` default is `"str"` (`run/agent.py:250`). AgentOS streams these over **WebSocket** (`os/app.py:42` `get_websocket_router`) — SSE is NOT the default transport for the full event stream (only for simpler deployments).

**Lancelot byte-path lens:** Lancelot's `@app.stream` (R3 pump, SSE via `lancelot.stream`) is the right transport. The agent event vocabulary above is what needs to be defined in Lancelot's agent layer. Copy the exact wire values — clients will expect them.

---

## 7. Structured output decision tree

**Source: `agent/_response.py:876-916`** (KNOWN)

Given `output_schema`, the runtime selects the `response_format` sent to the provider via a capability cascade:

```
if output_schema is None:
    return None                                    # free text
elif model.supports_native_structured_outputs:
    if (not use_json_mode) or structured_outputs:
        return output_schema                       # native: pass Pydantic class directly
    else:
        return {"type":"json_object"}
elif model.supports_json_schema_outputs:
    if use_json_mode or (not structured_outputs):
        return {"type":"json_schema",
                "json_schema":{"name": Schema.__name__,
                               "schema": Schema.model_json_schema()}}
    else:
        return None
else:
    return {"type":"json_object"}                  # last resort + system prompt injection
```

After the loop, `convert_response_to_structured_format` (`:923`) parses the raw JSON into the Pydantic instance. Same `output_schema=MyModel` yields different wire formats per provider. **A replica MUST implement this cascade or it will send invalid `response_format` to providers that don't support native structured output.**

**Three additional structured-output mechanisms** (`_run.py:543-548`):
- `parser_model` — a second (often cheaper) model call dedicated to reformatting the free-text answer into the schema. Decouples reasoning quality from formatting reliability.
- `output_model` — a different model generates the final user-facing answer (think: reason with a slow/expensive model, answer with a fast one).
- `followups` — generates suggested next questions (verbatim prompt: *"Generate exactly {num_suggestions} follow-up suggestions"*, `_response.py:1728-1747`).

**Total model call budget per run (worst case):** `[reasoning agent ≤10 steps] + [N main-loop turns] + [parser_model] + [output_model] + [followups] + [background: memory + learning + culture]` = 6+ distinct LLM calls for one user turn.

---

## 8. Multi-agent delegation — Team modes

**Source: `team/mode.py:12-23`, `team/_default_tools.py:440-1419`, `team/_tools.py:251-280`** (KNOWN)

Four team modes (all are first-class, not 3+footnote):

| Mode | Mechanism |
|---|---|
| `coordinate` | Leader picks member, authors task, synthesizes — `delegate_task_to_member(member_id, task)` |
| `route` | `respond_directly=True` (`team/team.py:108`) — member's `RunOutput` returned verbatim; output schema pushed down onto the member |
| `broadcast` | `delegate_to_all_members=True` (`:110`) — tool name `delegate_task_to_members(task)`, fan-out, `asyncio.Queue` fan-in |
| `tasks` | `team/_task_tools.py` + shared task list in `session_state` via `load_task_list()`; NO delegation tool — leader manipulates a task list |

**Multi-agent = leader's tool loop where one tool is "run another agent."** `delegate_task_to_member` is a Python function (wraps a full `agent.run()`) registered as a `Function` tool via `Function.from_callable`. The leader's LLM picks the `member_id` and authors the `task` string. Child events are yielded upward (with `parent_run_id` set) but terminal `RunOutput` objects are swallowed; only the content becomes the tool result (`:651-653`).

**Team context injection** (`:505`, `:511-512`): leader folds in prior member interactions as context string; `add_team_history_to_members` optionally injects `session.get_team_history_context(num_runs=team.num_team_history_runs)`.

**Lancelot byte-path lens:** `TransportPool.fanout(first_n, deadline, cancel_laggards, concurrency)` is the byte-path analog to `broadcast`. `TransportPool.spawn_child(parent_request, session_affinity)` is the sub-agent call substrate. The delegation tool pattern (register `run_subagent(member_id, task)` as a tool) is what Lancelot's Team-equivalent needs — Lancelot already has the byte-path primitives; the agent layer needs to wrap them.

---

## 9. Three-layer retry stack

**Source: `models/base.py:228-275`, `models/fallback.py`, `agent/_run.py:394`** (KNOWN)

Three retry layers, strictly nested:

1. **Invoke-retry** (`_invoke_with_retry`, `models/base.py:228`): per raw API call. `retries=0` default (`:172`) — **no model retry unless configured**. Delay = `delay_between_retries * 2**attempt` if `exponential_backoff` (`:176,190`), else flat 1s (`:174`).
2. **Retry-with-guidance** (`:258-272`): the clever bit — on a `RetryableModelProviderError` (e.g. malformed tool args, schema violation), instead of retrying blindly, appends a corrective `role="user", temporary=True` message telling the model what went wrong, capped at `retry_with_guidance_limit=1` (`:181`). The `temporary=True` flag strips the guidance from persisted history.
3. **Fallback-model walk** (`models/fallback.py`): on failure, walks `fallback_models: List[Model|str]` + `fallback_config` (error-specific routing). Wraps the whole inner loop per model.
4. **Run-retry** (`agent/_run.py:394`): outer `for attempt in range(agent.retries + 1)` — re-runs the full 16-step pipeline on exception.

Stack: invoke-retry ⊂ fallback-walk ⊂ run-retry.

**Lancelot byte-path lens:** `TransportPool` already handles transport-level retry with exp-backoff + Retry-After honoring. The **retry-with-guidance** pattern (inject a corrective user turn on schema violation before retrying) is NOT in Lancelot and is worth copying — it meaningfully improves structured-output reliability without extra prompting overhead.

---

## 10. Tool system mechanics

**Source: `tools/function.py:278-311`, `tools/decorator.py`, `tools/toolkit.py:12-33`** (KNOWN)

**Schema extraction** — framework-injected params (`agent`, `team`, `run_context`, `images`, `videos`, `audios`, `files`) are stripped from the JSON schema the model sees (`function.py:290-305`) then injected at call time. So `def my_tool(query: str, agent: Agent)` exposes `{query}` to the model only.

**`@tool` flags driving the loop's break conditions:**
- `stop_after_tool_call` → breaks the inner loop (`:832`) — the `final_answer` pattern
- `requires_confirmation` → HITL pause (`:836`)
- `requires_user_input` → HITL pause (`:844`)
- `external_execution` → HITL pause (`:840`)
- `tool_hooks: List[Callable]` → middleware chain around every call

**`Toolkit` bundle policy** (`tools/toolkit.py`):
- `auto_register=True` (`:33`) — wraps all public methods automatically
- `include_tools`/`exclude_tools` (`:24-25`) — allow/deny filter
- `requires_confirmation_tools: list[str]` (`:26`) — HITL at bundle level
- `cache_results=True` (`:30`) — in-memory memoization
- `add_instructions` (`:23`) — injects usage guidance into system prompt

**Lancelot byte-path lens:** Lancelot's `@app.tool` (`ToolSpec` + auto JSON schema + `output_schema`) covers the schema-extraction half. The HITL flags (`stop_after_tool_call`, `requires_confirmation`, `requires_user_input`) and tool middleware (`tool_hooks`) are NOT in Lancelot's tool surface yet and are needed for the agent layer.

---

## 11. Workflow engine — the deterministic DAG (Coverage Expansion E1)

**Source: `workflow/workflow.py`, `workflow/loop.py:75`, `workflow/router.py:52-74`, `workflow/cel.py`** (KNOWN)

Five composable primitives: `Step` (agent | team | callable), `Steps` (sequential chain), `Parallel` (fan-out), `Condition` (CEL or callable predicate), `Loop` (bounded, `max_iterations=3` default), `Router` (dynamic dispatch by CEL string or callable or HITL).

**Loop bounds are explicit and default-safe:** `workflow/loop.py:75` `max_iterations: int = 3`. Contrast the inner agent loop which has no cap.

**CEL expressions** — `Condition` and `Router` accept a serializable CEL string (e.g. `'previous_step_outputs.classifier.contains("billing") ? "Billing" : "Support"'`). CEL is the key serialization angle: `to_dict` stores `end_condition` as a string → workflows are JSON-round-trippable; agents/teams with code closures are not.

**`StepInput` fields accessible from CEL:** `input`, `previous_step_content`, `previous_step_outputs` (name→content map), `additional_data`, `session_state`. So branching logic can read accumulated step results.

**Lancelot byte-path lens:** `@app.batch` + `TransportPool.fanout` cover the parallel-fan-out case. The Workflow DAG (especially the CEL-serializable router/condition) is relevant if Lancelot's agent layer wants to support "serialize a workflow to JSON and restore it" — this is a durable-execution pattern. No existing Lancelot primitive covers it.

---

## 12. Guardrails — exact patterns

**Source: `guardrails/`, `guardrails/pii.py:37-42`, `guardrails/prompt_injection.py:17-32`** (KNOWN)

**`PromptInjectionGuardrail`** — substring match (naive `any(keyword in input.lower())`): `"ignore previous instructions"`, `"ignore your instructions"`, `"disregard guidelines"`, `"system prompt"`, `"jailbreak"`, `"ignore safeguards"` — full list at `prompt_injection.py:17-32`. Raises `InputCheckError`. Easily bypassed via rewording; a replica should upgrade to a classifier.

**`PIIDetectionGuardrail`** — regex patterns (all on by default):
- SSN: `\b\d{3}-\d{2}-\d{4}\b`
- Credit card: `\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b`
- Email: `\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b`
- `mask_pii=True` masks instead of raising; `custom_patterns` extends.

**`OpenAIModerationGuardrail`** — calls OpenAI moderation endpoint; `raise_for_categories` controls which flagged categories abort.

Guardrails run as `pre_hooks` (`agent/agent.py:183`) — `BaseGuardrail.check(run_input)` before the model is called; `InputCheckError` aborts.

**Lancelot byte-path lens:** the pre-hook pattern is the right integration point. Lancelot's `lancelot.net` SSRF guard is an analogous "check before dispatch." The PII regex set is copy-worthy for a safety layer; the prompt-injection keyword list is too naive to copy as-is (use a classifier model via a TransportPool call).

---

## 13. Knowledge retrieval — exact hybrid score formula

**Source: `vectordb/pgvector/pgvector.py:1067-1219`** (KNOWN)

Hybrid search score (computed in SQL):
```
# Text component
ts_vector = to_tsvector(content_language, content)
ts_query  = to_tsquery(query) | websearch_to_tsquery(query)  # prefix_match appends ':*' per token
raw_text_rank = ts_rank_cd(ts_vector, ts_query)              # returns 0.0–0.1 range
text_rank = raw_text_rank / (raw_text_rank + 0.1)            # squash to 0..1, magic k=0.1

# Vector component, per distance metric:
# cosine:            vector_score = max(0.0, 1 - cosine_distance)
# l2:                vector_score = 1 / (1 + l2_distance)
# max_inner_product: vector_score = max(0.0, min(1.0, (inner_product + 1)/2))

# Blend
hybrid_score = (vector_score_weight * vector_score) + ((1 - vector_score_weight) * text_rank)
# vector_score_weight default = 0.5 (pgvector.py:62)
```

Reranking applied after retrieval in all three search types (`vector`, `keyword`, `hybrid`). Default reranker models: CohereReranker `"rerank-multilingual-v3.0"` / SentenceTransformerReranker `"BAAI/bge-reranker-v2-m3"`. Reranking is best-effort — errors return original un-reranked list.

**`max_results` default = 10** (`knowledge/knowledge.py:49`).

---

## 14. Reasoning control-flow — two distinct paths

**Source: `reasoning/manager.py:1012-1055`, `reasoning/manager.py:790-899`** (KNOWN)

Branch on `is_native_reasoning_model(model)` (`:1042`):

**Native path** (when model id signals native reasoning — e.g. DeepSeek `reasoner/r1/v4/v3.1`, Anthropic `Claude` + `thinking` attribute set, OpenAI `o4/o3/o1`): pull the native reasoning trace out of the response, wrap as one `ReasoningStep`, append to `run_messages`. No loop — the provider does the thinking.

**Manual CoT path** (when NOT native): a `while next_action == CONTINUE and step_count < max_steps` loop (`manager.py:830`), where each iteration runs a *complete* reasoning agent (`output_schema=ReasoningSteps`, the prompt from §9.3). The last step's `next_action` drives continuation: `CONTINUE | VALIDATE | FINAL_ANSWER | RESET`. `get_next_action` (`reasoning/helpers.py:31-39`) defensively coerces bad/unknown values to `FINAL_ANSWER`. Accumulated `reasoning_messages` are spliced into the main run's message list before the main model call.

**Constants:** `reasoning_min_steps=1`, `reasoning_max_steps=10` (`agent/agent.py:194-195`). These bound the manual CoT loop; native path has no iteration (single extraction).

**`ReasoningTools` alternative** (`tools/reasoning.py:10`) — cheaper: `think(title, thought, confidence=0.8)` and `analyze(title, result, next_action, confidence=0.8)` tools that write into `session_state["reasoning_steps"][run_id]` and return the accumulated step list each call. No separate reasoning agent — reasoning IS the tool loop. `confidence` default `0.8`.

---

## 15. AgentOS — FastAPI control plane

**Source: `os/app.py:215`, `os/app.py:42,80,121,135,169,273`** (KNOWN)

`AgentOS` is a FastAPI app that composes `db_lifespan` (DB pool), `scheduler_lifespan`, and MCP lifespan (`:80`) into a combined lifespan. Runs stream over **WebSocket**, not SSE. `run_hooks_in_background=True` (`:273`) executes pre/post hooks as FastAPI background tasks.

**Lancelot byte-path lens:** Lancelot IS the framework AgentOS is built on. AgentOS validates that "agents exposed as HTTP+WS via a fast HTTP framework" is the right shape. The lifespan composition pattern (DB + scheduler + MCP) maps to Lancelot's `@app.on_event("startup")` / lifespan context.

---

## 16. Exact constants table

| Constant | Value | Source |
|---|---|---|
| Inner agent loop cap | **NONE** (`while True`) | `models/base.py:697` |
| `tool_call_limit` default | `None` (unlimited) | `agent/agent.py:169` |
| Retry attempts (run-level) | `agent.retries + 1` | `agent/_run.py:394` |
| `reasoning_min_steps` | `1` | `agent/agent.py:194` |
| `reasoning_max_steps` | `10` | `agent/agent.py:195` |
| Background executor workers | `3` (`"agno-bg"`) | `agent/agent.py:716-718` |
| `cache_session` | `False` | `agent/agent.py:98` |
| `references_format` | `"json"` | `agent/agent.py:156` |
| `ReasoningStep.confidence` default | `0.8` | `tools/reasoning.py:51` |
| Reasoning native detection (DeepSeek) | id contains `reasoner`, `r1`, `v4`, `v3.1`, `v3.2` | `reasoning/deepseek.py:29` |
| Reasoning native detection (OpenAI) | class + id contains `o4`\|`o3`\|`o1`\|`5.1`\|`5.2` | `reasoning/openai.py:14-27` |
| Model `retries` default | `0` | `models/base.py:172` |
| `retry_with_guidance_limit` | `1` | `models/base.py:181` |
| `exponential_backoff` default | `False` | `models/base.py:176` |
| Hybrid search `vector_score_weight` | `0.5` | `pgvector.py:62` |
| Hybrid text norm constant `k` | `0.1` (`x/(x+0.1)`) | `pgvector.py:1120` |
| Default `max_results` (knowledge) | `10` | `knowledge/knowledge.py:49` |
| Chunk size (fixed/recursive/semantic/agentic) | `5000` | `chunking/fixed.py:10`, etc. |
| Semantic `similarity_threshold` | `0.5` | `chunking/semantic.py:74` |
| Workflow `Loop.max_iterations` | `3` (bounded default!) | `workflow/loop.py:75` |
| DB backends | 17 | `db/` |
| CohereReranker default model | `"rerank-multilingual-v3.0"` | `reranker/cohere.py:14` |
| SentenceTransformerReranker model | `"BAAI/bge-reranker-v2-m3"` | `reranker/sentence_transformer.py:14` |

---

## 17. Anti-patterns from Agno (what to avoid)

1. **No `max_turns` on the inner loop.** The Agno `while True` at `models/base.py:697` has no turn ceiling — only `tool_call_limit` and the model stopping. A stuck model + no tool cap = infinite loop. Always add a hard `max_turns` ceiling in Lancelot's loop.

2. **Four manually-synchronized loop copies.** Agno maintains sync-buffered, sync-stream, async-buffered, async-stream as four separate code copies. This is a maintenance burden and a bug surface (one divergence already fixed in M19b conformance). Lancelot should have one loop definition; streaming should be a transport-layer concern (R3 pump handles this).

3. **`ThreadPoolExecutor` for background LLM calls.** Running the MemoryManager on a thread pool adds GIL-acquire overhead per result. The correct Lancelot pattern is `TransportPool.fanout` (async, no thread boundary for I/O-bound calls).

4. **Naive keyword guardrail.** The `PromptInjectionGuardrail` substring list is easily bypassed. Don't copy it as-is; use a classifier model call.

5. **Memory storage as JSONB inline on session row.** `runs` stored as a JSONB array on the session row does not scale to long sessions (one DB row grows unboundedly). Lancelot's durable seam should use a separate runs table with a foreign key.

6. **Agentic chunking (one LLM call per chunk boundary).** `knowledge/chunking/agentic.py` issues one model call per chunk to ask where to split. Expensive at indexing time, not a hot path, but a cost trap for large knowledge bases.
