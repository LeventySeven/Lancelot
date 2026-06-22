# Google ADK — Mechanism-Level Teardown for Lancelot

**Source version:** `google-adk==2.0.0` (Round 2, 2026-05-30); also `1.33.0` (Round 1, 2026-05-17).
**Primary sources:** `teardowns/ADK_RE_NOTES/01_PYTHON_CORE.md`, `02_MULTI_AGENT.md`, `03_TOOLS.md`, `04_RUNTIME_STREAMING_SESSIONS.md`, `05_MODELS_EVAL_DEPLOY.md`, synthesis in `teardowns/GOOGLE_ADK.md` (7,255 lines of source-first RE).
**Honesty label:** KNOWN = directly from source with `file:line`; INFERRED = deduced; DESIGNED = gaps.

---

## 1. The core architectural primitive: async-generator event stream

ADK is not a graph, not a crew, not handoff. The primitive is:

> **Every agent is `async def run_async(ctx) -> AsyncGenerator[Event]`.** The whole multi-agent tree is nested `async for` over generators sharing one `InvocationContext`. There is no scheduler, no message bus.

Source: `GOOGLE_ADK.md` §0 (TL;DR), corroborated by `01_PYTHON_CORE.md` §3, `02_MULTI_AGENT.md` §0.

**The agent loop (`base_llm_flow.py:851`, v2.0.0):**
```python
while True:
    last_event = None
    async for event in self._run_one_step_async(invocation_context):
        last_event = event
        yield event
    if not last_event or last_event.is_final_response() or last_event.partial:
        break
```
One iteration = one LLM call + its tool calls. Termination = last event `is_final_response()`. No tool-iteration counter. Hard ceiling: `max_llm_calls=500` (`run_config.py:314`). Source: `01_PYTHON_CORE.md` §3.3, `GOOGLE_ADK.md` §R2.1.

**`is_final_response()` exactly (`event.py:83-98`, v2.0.0):**
```python
def is_final_response(self):
    if self.actions.skip_summarization or self.long_running_tool_ids:
        return True
    return (not self.get_function_calls()
            and not self.get_function_responses()
            and not self.partial
            and not self.has_trailing_code_execution_result())
```
This is the loop terminator. A function-response event has `get_function_responses()` set → NOT final → loop continues for the next model call. Source: `04_RUNTIME_STREAMING_SESSIONS.md` §8.2, `GOOGLE_ADK.md` §R2.1.

**Lancelot byte-path lens:** Lancelot's `@app.stream` is an async-gen → Rust R3 pump → SSE/bytes path. The ADK loop is pure Python; what Lancelot would adopt is the EVENT MODEL above the loop, not the pump. Lancelot's `lancelot.stream.TTFT` + backpressure + disconnect cancel already provide better transport than ADK's FastAPI/`adk api_server`.

---

## 2. The request-processor pipeline — the single most important design pattern

**SingleFlow pipeline (exact order, `single_flow.py:36-69`, v2.0.0):**
```
1. basic              — model + generate_content_config
2. auth_preprocessor  — inject OAuth/credentials
3. request_confirmation — handle adk_request_confirmation responses
4. instructions       — agent.instruction → system_instruction (or user content if static_instruction set)
5. identity           — "You are an agent. Your internal name is '{name}'."
6. compaction         — sliding-window summary replaces old events (before contents)
7. contents           — session events → conversation history (branch-filtered)
8. context_cache      — explicit/implicit context-cache config
9. interactions       — stateful Interactions API previous_interaction_id
10. _nl_planning       — PlanReActPlanner scaffold (after contents)
11. _code_execution    — code executor priming (after contents)
12. _output_schema     — inject set_model_response tool when output_schema + tools co-exist
[AutoFlow only]
13. agent_transfer     — inject transfer_to_agent enum-constrained tool + routing instructions
```

**AutoFlow = SingleFlow + ONE processor** (`auto_flow.py:44`): `self.request_processors += [agent_transfer.request_processor]`. That's the ONLY delta between single-agent and multi-agent. Source: `01_PYTHON_CORE.md` §3.2, `GOOGLE_ADK.md` §R2.2.

**Flow selection (`llm_agent.py:801-809`):** AutoFlow is the default unless `disallow_transfer_to_parent AND disallow_transfer_to_peers AND not sub_agents`.

**Why this pattern matters for Lancelot:** processors are ordered, named, composable, and mutate a mutable `LlmRequest` in place. Each can also yield `Event`s (e.g. state-delta events from the planner). This is the correct shape for a "context engineering as a primitive" layer — assemble the LLM request through a named, extensible pipeline rather than a monolithic function. Each processor: `async def run_async(ctx, llm_request) -> AsyncGenerator[Event]`.

**Lancelot byte-path lens:** nothing in Lancelot currently does this. The `lancelot.context` ContextVars hold request-scoped state, but there is no pipeline that assembles `LlmRequest`. This is a genuine gap and the pattern to copy.

---

## 3. The Event + EventActions schema — the side-channel

**`Event` extends `LlmResponse`** (so it has `content`, `partial`, `finish_reason`, `error_code`, `usage_metadata`, transcription fields). Added fields (`event.py:47-74`):

| Field | Type | Role |
|---|---|---|
| `invocation_id` | `str` | format `e-<uuid>` |
| `author` | `str` | `'user'` or agent name |
| `actions` | `EventActions` | the coordination side-channel |
| `long_running_tool_ids` | `Optional[set[str]]` | forces `is_final_response()=True` |
| `branch` | `Optional[str]` | dot-path `agent1.agent2.agent3` for parallel isolation |
| `id` | `str` | `uuid4()` |
| `timestamp` | `float` | `time.time()` |

**`EventActions` (`event_actions.py:51-115`) — every coordination signal in one struct:**

| Field | Role |
|---|---|
| `skip_summarization` | don't summarize this tool result; forces final response |
| `state_delta` | mutations to session state (prefix-routed at append time) |
| `artifact_delta` | `{filename: version}` |
| `transfer_to_agent` | set by `transfer_to_agent` tool → flow routes to target agent |
| `escalate` | `LoopAgent` exit signal |
| `requested_auth_configs` | tool-requested OAuth (HITL auth) |
| `requested_tool_confirmations` | tool-requested user confirmation (HITL) |
| `compaction` | `{start_ts, end_ts, compacted_content}` |
| `end_of_agent` / `agent_state` | resumability checkpoints (v2) |
| `rewind_before_invocation_id` | undo marker |
| `render_ui_widgets` | UI iframe payloads |

Source: `01_PYTHON_CORE.md` §5.2, `04_RUNTIME_STREAMING_SESSIONS.md` §8.3, `GOOGLE_ADK.md` §R2.4.

**The key invariant (KNOWN, `runners.py:954`):** partial events are yielded to the caller but NEVER persisted to the session. Only `event.partial is not True` events are appended. `partial=None` and `partial=False` are both non-partial.

**Lancelot byte-path lens:** Lancelot's `lancelot.context` ContextVars partially cover this (cost_usd, tokens_in/out, agent_id, tenant_id, request_id). But there is no `EventActions`-style struct, no `state_delta`, no `escalate`, no HITL fields. The ADK pattern of attaching a side-channel struct to every yielded event is the right design for a durable agent layer. Lancelot's `@app.stream` yields bytes/str/SSE Events — it can be extended to yield typed `Event` objects.

---

## 4. State management: prefix-partitioned persistence

**`extract_state_delta` (`_session_util.py:37-50`) — the exact algorithm:**
```python
deltas = {"app": {}, "user": {}, "session": {}}
for key in state:
    if key.startswith("app:"):     deltas["app"][key[4:]]  = state[key]
    elif key.startswith("user:"):  deltas["user"][key[5:]] = state[key]
    elif not key.startswith("temp:"):  deltas["session"][key] = state[key]
    # temp: silently dropped — never persisted
```

| Prefix | Scope | Persisted to |
|---|---|---|
| `app:` | cross-session per app | `app_states` table |
| `user:` | cross-session per user | `user_states` table |
| (none) | this session | `sessions.state` column |
| `temp:` | this invocation only | in-memory only, stripped before write |

On `get_session`, app/user state is re-prefixed and merged into one flat dict so the agent sees `session.state["app:foo"]` normally.

**`temp:` pattern:** `output_key="temp:result"` in a `SequentialAgent` step → the next step reads `state["temp:result"]` but it never hits the DB. Zero-copy inter-step data flow.

Source: `04_RUNTIME_STREAMING_SESSIONS.md` §5, `GOOGLE_ADK.md` §R2.6, `01_PYTHON_CORE.md` §R3.1.

**Lancelot byte-path lens:** Lancelot has no equivalent. `lancelot.context` ContextVars are per-request, not durable. The prefix-scoped state model is durable agent memory at the right granularity. Copy the algorithm verbatim — it is elegant and complete.

---

## 5. Session persistence: the SQL V1 schema

**4 tables, V1 schema (`sessions/schemas/v1.py`), the current/default:**

```sql
sessions (
    app_name VARCHAR(128), user_id VARCHAR(128), id VARCHAR(128) PK,
    state JSON, create_time DATETIME(fsp=6), update_time DATETIME(fsp=6)
)

events (
    id VARCHAR(128), app_name VARCHAR(128), user_id VARCHAR(128), session_id VARCHAR(128) PK,
    invocation_id VARCHAR(256), timestamp DATETIME(fsp=6),
    event_data JSON,  -- entire Event.model_dump(exclude_none=True, mode='json')
    FK → sessions CASCADE,
    INDEX (app_name, user_id, session_id, timestamp DESC)
)

app_states (app_name VARCHAR(128) PK, state JSON)
user_states (app_name VARCHAR(128), user_id VARCHAR(128) PK, state JSON)
```

**Concurrency:** per-session `asyncio.Lock` + optimistic `_storage_update_marker` (= `update_time.isoformat(microseconds)`). Source: `04_RUNTIME_STREAMING_SESSIONS.md` §4.4.

**V1 vs V0:** V1 stores the whole event as a JSON blob (`event_data`). V0 was columnar + pickled `actions`. V1 is simpler and is the default. Never use pickle for actions.

**Sizing constants (`schemas/shared.py:25-26`):** `DEFAULT_MAX_KEY_LENGTH=128`, `DEFAULT_MAX_VARCHAR_LENGTH=256`. MySQL `DATETIME(fsp=6)` for microsecond precision.

**Lancelot byte-path lens:** Lancelot has no session persistence layer — `lancelot.secrets` is credentials, not agent state. The 4-table schema is the correct minimal durable store for an agent framework. This is a pure implementation target.

---

## 6. The Runner: sole persister contract

**The only rule:** agents yield events; the `Runner._exec_with_plugin` (`runners.py:834-966`) is the ONLY layer that calls `session_service.append_event`. Agents never touch storage.

**`run_async` signature (keyword-only):**
```python
async def run_async(self, *, user_id, session_id,
    invocation_id=None,     # set to RESUME
    new_message=None,
    state_delta=None,
    run_config=None) -> AsyncGenerator[Event]
```

**`_find_agent_to_run` logic (`runners.py:1153-1205`):**
1. If last event is a function_response → return the agent that authored the matching function_call.
2. Else: most recent non-user, non-state event whose author is transferable up to root → that sub-agent.
3. Fallback: root_agent.

This is how a multi-turn conversation in a transferred state resumes at the right agent.

**Resumability:** `invocation_id` param + `ResumabilityConfig` on the `App`. A paused long-running tool or HITL flow stores `agent_state` checkpoints in `EventActions.agent_state`; resume picks up at the checkpointed index. Source: `04_RUNTIME_STREAMING_SESSIONS.md` §1.4, `01_PYTHON_CORE.md` §5.3.

**Lancelot byte-path lens:** Lancelot's `Runner` equivalent is the `@app.route` handler + ASGI seam. The ADK "agent yields, runner persists" contract is the right design for a durable agent layer — decouple the agent logic from storage entirely.

---

## 7. Multi-agent: three distinct mechanisms (the critical distinction)

| Mechanism | How | Same session? | Control returns? |
|---|---|---|---|
| **SequentialAgent** | `for sub_agent in sub_agents: yield from sub_agent.run_async(ctx)` | yes (same ctx) | deterministic |
| **ParallelAgent** | `asyncio.TaskGroup` + `asyncio.Queue` + per-event `asyncio.Event` resume signal | yes (branched ctx, isolated history) | yes (merge) |
| **LoopAgent** | `while (times_looped < max_iterations) and not escalate` | yes | escalate signal |
| **LLM transfer** | model calls `transfer_to_agent(enum)` → `EventActions.transfer_to_agent` set → flow runs target agent's `run_async` inline | **yes (same session)** | **NO — control never returns this invocation** |
| **AgentTool** | wrap agent as tool; spins fresh `Runner + InMemorySessionService`; returns string | **NO (isolated)** | **YES — always returns** |

**Transfer vs AgentTool is the single most important architectural point.** Use transfer when you want the sub-agent to take over (no result needed); use AgentTool when you need a string result and want to continue. Source: `02_MULTI_AGENT.md` §0, §3, §4.

**ParallelAgent backpressure (`parallel_agent.py:51-86`):** NOT `asyncio.gather` of full runs. Each sub-agent produces one event, puts `(event, resume_signal)`, blocks on `resume_signal.wait()`. Consumer yields the event, calls `resume_signal.set()`. Event ordering = completion order (non-deterministic). State clobbering risk: all branches share the same `session.state` (model_copy is shallow). Use distinct `output_key` per branch.

**LoopAgent termination (`loop_agent.py:69-124`):**
1. `max_iterations` reached.
2. Any event with `event.actions.escalate=True` (set by `exit_loop` tool or a custom tool that sets `tool_context.actions.escalate=True`).
3. Pause (resumable + long-running tool).

`ctx.reset_sub_agent_states(self.name)` between iterations: clears per-agent checkpoints so each loop pass starts fresh, but `session.state` accumulates across passes.

**Lancelot byte-path lens:** `TransportPool.fanout(coros, first_n, deadline, cancel_laggards)` is the byte-path analogue of `ParallelAgent`. `spawn_child(parent_request, session_affinity)` is the byte-path analogue of `AgentTool`'s isolated Runner. These already exist. What Lancelot lacks is the `SequentialAgent`/`LoopAgent`/transfer orchestration primitives operating on Python agents (not HTTP sub-calls).

---

## 8. Tool dispatch: parallel execution + 6-step callback pipeline

**Parallel execution (`functions.py:429-463`):**
```python
tasks = [asyncio.create_task(_execute_single_function_call_async(...)) for fc in filtered_calls]
results = await asyncio.gather(*tasks)
merged_event = merge_parallel_function_response_events(results)
```
All tool calls from one model turn run concurrently. Results merged into one event.

**6-step callback pipeline per tool call (`functions.py:550-620`):**
1. plugin `before_tool_callback` → non-None = skip real tool
2. agent `canonical_before_tool_callbacks` list (first-wins) → non-None = skip real tool
3. `tool.run_async(args, tool_context)` → on exception: `on_tool_error` callbacks or re-raise
4. plugin `after_tool_callback` → non-None = replace result
5. agent `canonical_after_tool_callbacks` list (first-wins) → non-None = replace result
6. Use the altered result if any callback returned non-None

This is the guardrail / audit / mock seam for tools.

**Function-call ID scheme:** `generate_client_function_call_id()` → `f'adk-{uuid}'` (`functions.py:180-181`). Stripped before sending to the model (models don't need them), re-added by ADK for tracking. Invocation IDs: `e-<uuid>`.

**Mandatory-arg error recovery (`function_tool.py:186-189`):** if any mandatory arg is missing, FunctionTool returns (does NOT raise): `{'error': "Invoking `<name>()` failed as the following mandatory input parameters are not present:\n<names>\nYou could retry calling this tool, but it is IMPORTANT for you to provide all the mandatory parameters."}`. The model self-corrects on the next turn.

**Lancelot byte-path lens:** `lancelot.tool` (`@app.tool` → `ToolSpec` w/ auto JSON schema + `output_schema`) covers tool declaration. Lancelot has no parallel tool dispatch or 6-step callback pipeline. The callback pipeline is the extension seam for guardrails — copy it.

---

## 9. Schema generation: default is the MANUAL type parser (not Pydantic)

**`FeatureName.JSON_SCHEMA_FOR_FUNC_DECL` is `WIP, default_on=False`** (`_feature_registry.py:137-139`). The DEFAULT path in production is `_parse_schema_from_parameter` — a manual Python-type-to-JSON-schema translator, NOT `model_json_schema()`.

**Type mapping (default path, `_function_parameter_parse_util.py:35-47`):**

| Python | JSON Schema type |
|---|---|
| `str` | `STRING` |
| `int` | `INTEGER` |
| `float` | `NUMBER` |
| `bool` | `BOOLEAN` |
| `list` | `ARRAY` |
| `dict` | `OBJECT` |
| `None` | `NULL` |
| `Any` | `TYPE_UNSPECIFIED` |
| `Enum` subclass | `STRING` + `enum=[e.value for e in ...]` |
| `pydantic.BaseModel` subclass | `OBJECT` + recursive `properties` |
| `Literal[...]` | `STRING` + `enum=[...]` (strings only) |
| `list[T]` | `ARRAY` + recursive `items` |
| `Optional[T]` / `Union[T, None]` | collapses to T + `nullable=True` |

**Caveat:** top-level `Union` that doesn't collapse (multi-type non-Optional) fails on Gemini API but works on Vertex AI (`_raise_if_schema_unsupported`, `:160-166`).

**Context param stripping:** `find_context_parameter(func)` detects by type annotation (`is Context`), falls back to literal name `'tool_context'`. The function is rebuilt without that param — the model never sees it. At call time it's re-injected.

**Lancelot byte-path lens:** `lancelot.tool` uses Pydantic `model_json_schema()` for schema generation (the opt-in ADK path). For Lancelot's agent layer, the Pydantic path is fine (it's the better one). The KNOWN production gotcha is: if you ship `Union[str, int]` annotations, Gemini API rejects them; gate the annotation or collapse to `str`.

---

## 10. BIDI/Live streaming: `LiveRequestQueue` → send/receive demux

**`LiveRequestQueue` (`live_request_queue.py`) — the input bus:** unbounded `asyncio.Queue()` with typed `LiveRequest` payloads:
- `content: Content` — text turn
- `blob: Blob` — realtime audio/video chunk
- `activity_start/end` — manual VAD markers
- `close: bool` — shutdown signal

**Two concurrent tasks in `run_live`:**
1. `_send_to_model`: pulls from `LiveRequestQueue`, routes `blob`→`send_realtime`, `content`→`send_content`, `close`→`llm_connection.close()`.
2. `_receive_from_model`: demuxes `GeminiLlmConnection.receive()` into partial text / transcription / tool_call / `turn_complete` / `interrupted` / `go_away` / `session_resumption_update` events. Tool calls: Gemini 3.1 yields immediately (deadlock avoidance); other models buffer until `turn_complete`.

**Reconnect loop (`base_llm_flow.py:635-666`):** on WS close codes `1000` or `1006`, if `live_session_resumption_handle` exists and `attempt <= DEFAULT_MAX_RECONNECT_ATTEMPTS` (5), reconnect with `transparent=True` resumption. Any other code re-raises.

**Transcription ordering buffer (`runners.py:889-952`):** partial transcription events arrive after the function-call/response that precedes them. ADK buffers non-partial function-call/response events while transcribing, flushes them after the non-partial transcription arrives. This is a correctness fix for multi-agent live, not just cosmetic ordering.

**Live API versions:** Vertex AI → `v1beta1`; AI Studio API key → `v1alpha` (`google_llm.py:366-375`). Transparent session resumption = Vertex-only (raises `ValueError` on GEMINI_API backend).

**Lancelot byte-path lens:** Lancelot's R3 streaming pump (`@app.stream`, Rust mpsc, GIL-dropped per chunk, backpressure) is the SSE output path. For BIDI/live, Lancelot has no `run_live` equivalent. The `LiveRequestQueue` + two-task send/receive split is the correct architecture for a WS bidi handler. Lancelot's WS conformance layer (`test_websocket` green) is the transport substrate — `run_live` would sit above it.

---

## 11. Context compaction: sliding-window summary

**`EventsCompactionConfig`** on the `App`. Compaction runs AFTER each invocation completes (never mid-invocation, `runners.py:617-629`). The `compaction.request_processor` (6th in the pipeline, BEFORE `contents`) marks compacted events with `EventCompaction{start_timestamp, end_timestamp, compacted_content}`. The `contents` processor skips raw events inside compacted time ranges and substitutes the summary.

**`include_contents='none'` mode:** walk backward to the latest user message or other-agent reply starting the current turn, then `_get_contents` on only `events[i:]`. For agents that don't need full history — reduces token usage.

**Lancelot byte-path lens:** Lancelot has no context compaction. The `lancelot.canonical.dumps_canonical` covers prompt-cache stability (byte-stable prefix) but not context length management. Compaction is an agent-layer concern; the byte path is agnostic. Flag as a future agent primitive.

---

## 12. The eval framework: tool trajectory + LLM-as-judge

**Default eval config:** `{"tool_trajectory_avg_score": 1.0, "response_match_score": 0.8}` (`eval_config.py:160-162`). `NUM_RUNS=2` (`agent_evaluator.py:59`).

**Metric 1 — `tool_trajectory_avg_score` (trajectory match):** EXACT / IN_ORDER / ANY_ORDER. Default = EXACT: `len(actual)==len(expected)` AND every zipped `(name, args)` pair matches exactly. Source: `05_MODELS_EVAL_DEPLOY.md` §3.2.

**Metric 2 — `response_match_score` (ROUGE-1):** pure-Python F-measure (`use_stemmer=True`). Range [0,1]. Threshold default 0.8.

**Metric 3 — `final_response_match_v2` (LLM-as-judge):** 5 judge calls per invocation (default), majority vote, valid=1.0/invalid=0.0, `num_valid/num_evaluated` as final score. Judge model = `gemini-2.5-flash` (via `LLMRegistry` — the same model abstraction, so judge can be any provider). Source: `05_MODELS_EVAL_DEPLOY.md` §3.2.

**Eval set format:** `.evalset.json` files, Pydantic `EvalSet` → `EvalCase` → `Invocation{user_content, final_response, intermediate_data{tool_uses, tool_responses}}`.

**Lancelot byte-path lens:** Lancelot has no eval framework. The trajectory-match algorithm (EXACT/IN_ORDER/ANY_ORDER) is the most durable metric — model-agnostic, deterministic, cheap. Build this first. ROUGE-1 + LLM-as-judge are secondary. The eval data schema (goldset with tool trajectories) is directly reusable.

---

## 13. Model abstraction: `BaseLlm` + `LLMRegistry`

**`BaseLlm` contract (`base_llm.py:32-205`):**
```python
async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False) -> AsyncGenerator[LlmResponse]
def connect(self, llm_request) -> BaseLlmConnection  # live/bidi
```

**Non-streaming:** exactly one `LlmResponse(partial=False)`. **Streaming:** N chunks with `partial=True`, then a final `partial=False` aggregated response identical to non-streaming output.

**`LLMRegistry` (`registry.py:90-141`):** `@lru_cache(maxsize=32)`, `re.compile(regex).fullmatch(model)`. Lazy import on first match. Model string → class routing table (source: `01_PYTHON_CORE.md` §3.6 / `GOOGLE_ADK.md` §R3.4):

| Regex | Class |
|---|---|
| `gemini-.*`, `model-optimizer-.*`, `projects/.+/...` | `Gemini` (google-genai SDK) |
| `claude-3-.*`, `claude-.*-4.*` | `AnthropicLlm` (direct) |
| `openai/.*`, `azure/.*`, `groq/.*`, `anthropic/.*`, `bedrock/.*`, `ollama/.*`, `mistral/.*`, `deepseek/.*`, etc. | `LiteLlm` (~2,503-line translator) |

**The `LiteLlm` translator is the most important model artifact:** genai ↔ OpenAI message format bidirectional translation, thought-signature round-tripping, Anthropic thinking-block reconstruction, tool-result healing (inserts placeholder for unanswered tool calls), streaming chunk reassembly with litellm 1.81 workaround, structured output enforced via `_enforce_strict_openai_schema` (sets `additionalProperties:false`, all props required). Source: `05_MODELS_EVAL_DEPLOY.md` §1.5.

**ADK behavioral fingerprint strings (observable on the wire):**
- Empty contents → injected user turn: `"Handle the requests as specified in the System Instruction."` (`base_llm.py:168`)
- Last turn not user-role → `"Continue processing previous requests as instructed. Exit or provide a summary if no more outputs are needed."` (`base_llm.py:187`)
- Identity line in system: `You are an agent. Your internal name is "{name}".` (`identity.py:37`)
- Function call IDs: `adk-<uuid>`; invocation IDs: `e-<uuid>`.

**Lancelot byte-path lens:** `TransportPool` handles HTTP transport to model endpoints. Lancelot has `lancelot.upstream_metrics` for provider detection and token extraction. What Lancelot lacks is the **message-shape translation layer** (genai/OpenAI/Anthropic dialects) and `LlmRequest`/`LlmResponse` as provider-neutral intermediates. The `LiteLlm` translator is an existing open-source artifact that could be wrapped rather than rebuilt. Copy the `BaseLlm` interface contract for Lancelot's model adapter layer.

---

## 14. MCP integration: consumer-side, session pooling, transport crash safety

**Three transports:** stdio (`timeout=5.0s`), SSE (`sse_read_timeout=300s`), Streamable HTTP.

**Session pool key (`mcp_session_manager.py:268-293`):** stdio → `'stdio_session'`; SSE/HTTP → `'session_' + md5(json.dumps(merged_headers, sort_keys=True))`. Distinct auth headers get distinct pooled sessions.

**Transport crash detection (`session_context.py:156-209`):** `_run_guarded` races the tool-call coroutine against the background session task with `asyncio.wait(FIRST_COMPLETED)`. If the session task finishes first (transport crash, e.g. 403 from model armor mid-call) → cancel the coroutine immediately → `ConnectionError`. Without this, a crashed SSE transport hangs the tool call for up to 300s.

**`_MCP_GRACEFUL_ERROR_HANDLING` is `EXPERIMENTAL, default_on=False`** (`_feature_registry.py:140-141`). When OFF, transport crashes bubble as raw exceptions. When ON, `McpTool.run_async` catches `McpError` and any `Exception` → returns `{'error': '...'}` so the agent loop survives.

**ADK as MCP server:** `adk_to_mcp_tool_type(tool)` (`conversion_utils.py:27-60`) converts any `BaseTool` to `mcp.types.Tool`. No turnkey server CLI — user code must wire the `mcp` SDK server and dispatch to `tool.run_async`. Source: `03_TOOLS.md` §3.6.

**Lancelot byte-path lens:** `lancelot.mcp.mount(app, path='/mcp')` is a server-side MCP mount. Lancelot has no MCP client/consumer layer. The session pool + transport crash detection from ADK is the correct pattern for a resilient outbound MCP client. Flag as a future `lancelot.mcp.client` primitive.

---

## 15. Exact constants (KNOWN, all `file:line`)

| Constant | Value | Source |
|---|---|---|
| `max_llm_calls` hard ceiling | 500 | `run_config.py:314` |
| Default model | `gemini-2.5-flash` | `google_llm.py:115` |
| `DEFAULT_MAX_RECONNECT_ATTEMPTS` (live) | 5 | `base_llm_flow.py:71` |
| `DEFAULT_TRANSFER_AGENT_DELAY` | 1.0s | `base_llm_flow.py:68` |
| `DEFAULT_TASK_COMPLETION_DELAY` | 1.0s | `base_llm_flow.py:69` |
| Plugin close timeout | 5.0s | `runners.py:160` |
| Toolset close timeout | 10.0s | `runners.py:1598` |
| MCP connect timeout | 5.0s | `mcp_session_manager.py:100` |
| MCP `sse_read_timeout` | 300s | `mcp_session_manager.py:103` |
| OpenAPI/Gemini tool-name cap | 60 chars | `operation_parser.py:224` / `rest_api_tool.py:149` |
| DB VARCHAR PKs | 128 / 256 | `schemas/shared.py:25-26` |
| MySQL DATETIME precision | `fsp=6` | `schemas/shared.py:65-66` |
| Tool thread pool max_workers | 4 | `run_config.py:47-51` |
| Live WS recoverable close codes | 1000, 1006 | `base_llm_flow.py:651` |
| WS close: origin not allowed | 1008 | `adk_web_server.py:2090` |
| WS close: session not found | 1002 | `adk_web_server.py:2102` |
| WS close: internal error | 1011 | `adk_web_server.py:2165` |
| `LiveRequestQueue` | unbounded `asyncio.Queue()` | `live_request_queue.py:64` |
| Event ID format | `uuid4()` string | `platform/uuid.py:23` |
| Invocation ID format | `e-<uuid>` | `invocation_context.py:420` |
| Function call ID prefix | `adk-` | `functions.py:57` |
| HITL confirmation call name | `adk_request_confirmation` | `functions.py:59` |
| HITL credential call name | `adk_request_credential` | `functions.py:58` |
| `LoopAgent.max_iterations` default | `None` (unbounded) | `loop_agent.py:75` |
| LLM-as-judge default samples | 5 | `eval_metrics.py:93` |
| Judge model default | `gemini-2.5-flash` | `eval_metrics.py:77` |
| Code executor error retries | 2 | `base_code_executor.py:59` |
| Max direct memories per batch | 5 | `vertex_ai_memory_bank_service.py:104` |
| Artifact first version | 0, +1 per save | `base_artifact_service.py:123` |
| ADK progressive SSE env flag | `ADK_ENABLE_PROGRESSIVE_SSE_STREAMING` (default ON) | `run_config.py:136` |
| MCP graceful errors env flag | `ADK_ENABLE_MCP_GRACEFUL_ERROR_HANDLING` (default OFF) | `_feature_registry.py:140` |

---

## 16. Avoid: anti-patterns surfaced by the teardown

1. **`asyncio.gather` for parallel sub-agents.** ADK uses `asyncio.TaskGroup` + per-event `asyncio.Event` resume signal for backpressure. `gather` would flood the downstream consumer. Source: `02_MULTI_AGENT.md` §1.2.

2. **Storing partials in the session.** Partial events must NEVER be appended to storage — they are streaming intermediates. Only `partial is not True` events are durable. Source: `04_RUNTIME_STREAMING_SESSIONS.md` §2.3.

3. **V0-style pickled EventActions.** V0 pickled `actions` — this is a migration footgun (changing fields breaks deserialization). V1 JSON blob is the correct approach. Source: `04_RUNTIME_STREAMING_SESSIONS.md` §4.4.

4. **Unbounded `LiveRequestQueue` without consumer-side backpressure.** ADK's queue is unbounded. For high-rate audio, this is a known memory risk. Lancelot's bounded mpsc channel model is the correct design for live audio pipelines.

5. **`google_search` mixed with other tools on Gemini 1.x.** Hard error: "Google search tool cannot be used with other tools in Gemini 1.x." (`google_search_tool.py:60-89`). Do not co-register grounding tools with function tools for Gemini 1.x.

6. **Relying on `tool_trajectory_avg_score=1.0` as the only eval metric.** Trajectory match (EXACT by default) fails if the model finds a valid alternative path (different tool order, different but correct args). Use IN_ORDER or ANY_ORDER for less brittle evals; combine with `response_match_score` or LLM-as-judge.

7. **Setting `global_instruction` on LlmAgent.** Deprecated in v1.33+; replaced by `GlobalInstructionPlugin`. `LlmAgent.canonical_global_instruction` emits a `DeprecationWarning`. Source: `01_PYTHON_CORE.md` §2.2.

8. **Wrapping the `SessionContext._run` task in `wait_for` for the AnyIO CancelScope.** ADK explicitly does NOT wrap the transport entry in `wait_for` when graceful error handling is enabled, because it creates a nested task that breaks AnyIO's `CancelScope`. The connect timeout is enforced by the outer manager's `wait_for` instead. Source: `03_TOOLS.md` §3.3.

---

## 17. Deprecation / churn watch

- `global_instruction` field on `LlmAgent` — deprecated, use `GlobalInstructionPlugin`.
- `MCPTool`/`MCPToolset` names — deprecated, use `McpTool`/`McpToolset`.
- `session=` parameter on `runner.run_live()` — deprecated, use `user_id`+`session_id`.
- `plugins=` kwarg on `Runner.__init__` — deprecated, use `App.plugins`.
- `save_live_audio` on `RunConfig` — deprecated alias for `save_live_blob`.
- `google.adk.agents.workflow.*` (`WorkflowAgent`, `FunctionNode`, `ParallelWorker`, `JoinNode`, `START`) — imported by some `adk-samples` but **does NOT exist in v1.33.0 or v2.0.0**. Unreleased preview graph-DAG layer. Do not implement.
- `BuiltInCodeExecutor` is Gemini-only (`is_gemini_eap_or_2_or_above` guard). Using it with other models raises `ValueError`.
- `LiteLlm.generate_content_async` has a `_warn_gemini_via_litellm` warning: using `openai/gemini-*` routes through LiteLLM → worse performance. Use `Gemini(model='...')` directly for Gemini models.
- LiteLlm streaming with litellm 1.81+ has a chunk-finalization quirk: `finish_reason="stop"` can appear on partial chunks; only finalize on a stop-only chunk (ADK workaround at `:2395-2422`). This is a depreciating tactical hack tied to a specific litellm version.
