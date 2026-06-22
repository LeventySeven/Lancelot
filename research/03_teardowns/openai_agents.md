# OpenAI Agents SDK + Swarm + Operator — Mechanism-Level Teardown

**Sources read:** `researchfms/teardowns/OPENAI_AGENTS_SDK.md` (3 rounds, v0.17.2 + v0.17.4), `researchfms/teardowns/OPENAI_SWARM.md` (2 rounds, commit `6af0b4caf3`), `researchfms/teardowns/OPENAI_OPERATOR.md` (Tier-1A, `openai-cua-sample-app` `responses-loop.ts` 917 LOC).

---

## 1. The Handoff Primitive — Wire Level

**Swarm (the fossil, `core.py:71-87`):**
```python
case Agent() as agent:
    return Result(value=json.dumps({"assistant": agent.name}), agent=agent)
```
That is the entire handoff. A Python tool returns an `Agent` instance; the run loop checks `partial_response.agent` and reassigns `active_agent`. LLM-visible tool-message content: `{"assistant": "Agent B"}`. The receiving agent gets the full history, no filter.

**Agents SDK v0.17.4 (`handoffs/__init__.py:222-333`):**
```python
def handoff(
    agent: Agent[TContext],
    tool_name_override: str | None = None,
    input_type: type[THandoffInput] | None = None,     # Pydantic schema for the handoff payload
    input_filter: Callable[[HandoffInputData], HandoffInputData] | None = None,  # history shaping
    on_handoff: Callable | None = None,                # side-effect callback
    nest_handoff_history: bool | None = None,          # summary-wrap old transcript
    is_enabled: bool | Callable = True,                # per-turn gate
) -> Handoff:
```

Tool name derives as `transform_string_function_style(f"transfer_to_{agent.name}")` — `"Refunds Agent"` → `"transfer_to_refunds_agent"`. The target agent is stored via `weakref.ref(agent)` to break circular retain; the strong reference lives in the closure of the invocation function only. `ensure_strict_json_schema()` is called unconditionally on the input schema (even on `{}` if no `input_type`).

Wire-level content is identical to Swarm: `{"assistant": "agent_name"}` via `Handoff.get_transfer_message(agent)`. Everything else is production wrapper.

**Lancelot byte-path implication:** `TransportPool.spawn_child` and `fanout` are byte-path analogues for sub-agent I/O but NOT the same concept. Handoff is control-transfer at the loop level — unrelated to transport. Lancelot lacks the loop, the Agent dataclass, and the handoff dispatch; these are the build target.

---

## 2. `HandoffInputData` — the three-tier history split

`handoffs/__init__.py:41-83`:
```python
@dataclass(frozen=True)
class HandoffInputData:
    input_history: str | tuple[TResponseInputItem, ...]      # what was passed to Runner.run()
    pre_handoff_items: tuple[RunItem, ...]                   # items before current agent's turn
    new_items: tuple[RunItem, ...]                           # items in current turn incl. handoff trigger
    run_context: RunContextWrapper[Any] | None = None
    input_items: tuple[RunItem, ...] | None = None           # override: what next agent actually receives
```

`input_filter(data) -> HandoffInputData` is the caller's hook. Patterns:
- Drop everything except original user query: `data.clone(pre_handoff_items=(), new_items=())`
- Compress old history: `data.clone(input_history=summary_message, pre_handoff_items=())`
- Pass a shaped set: `data.clone(input_items=relevant_subset)`

**Why it matters for Lancelot:** context engineering at handoff time is the production differentiator. The naive design (always pass full history) is Swarm; the disciplined design (filter to what the next agent needs) is what production systems require and what the Agents SDK calls out explicitly as a named primitive. Lancelot must expose an equivalent — the context assembly problem at agent-boundary time.

---

## 3. Guardrails Shape (copy verbatim)

```python
@dataclass
class InputGuardrail[TContext]:
    guardrail_function: Callable[[RunContextWrapper, Agent, str | list], MaybeAwaitable[GuardrailFunctionOutput]]
    name: str | None = None
    run_in_parallel: bool = True   # concurrent with model call, default True

@dataclass
class OutputGuardrail[TContext]:
    guardrail_function: Callable[[RunContextWrapper, Agent, Any], MaybeAwaitable[GuardrailFunctionOutput]]
    name: str | None = None

@dataclass
class GuardrailFunctionOutput:
    output_info: Any              # diagnostic payload — arbitrary
    tripwire_triggered: bool      # True → raise exception, abort run
```

Tripwire exceptions: `InputGuardrailTripwireTriggered`, `OutputGuardrailTripwireTriggered`, `ToolInputGuardrailTripwireTriggered`, `ToolOutputGuardrailTripwireTriggered`. All carry the triggering `GuardrailFunctionOutput`.

**Execution timing** (`run_internal/run_loop.py`):
- Sequential guardrails (`run_in_parallel=False`): run BEFORE model call. Trip → no model call happens.
- Parallel guardrails (`run_in_parallel=True`, the default): run concurrently with model. Trip → model response discarded.
- Output guardrails: run after `NextStepFinalOutput`. Trip → output discarded.
- Tool-scoped guardrails: around each individual tool call.

**Diagnostic payload `output_info` persists in `RunResult`** even when no tripwire fires:
```python
# RunResult carries:
input_guardrail_results: list[InputGuardrailResult]
output_guardrail_results: list[OutputGuardrailResult]
```

**Lancelot byte-path implication:** Lancelot has `@app.middleware` and per-route `timeout=` for HTTP-level guardrails. Agent-level guardrails (pre/post-LLM-call, with `output_info` diagnostics) are NOT in the byte path — these belong in the agent loop layer. The shape above is worth copying directly.

---

## 4. The Run Loop — NextStep State Machine

`run_internal/run_loop.py:671-889` (v0.17.4). The explicit turn-result discriminated union — do not implement as a flat if-else chain:

```python
# run_steps.py:154-169
class NextStepHandoff(new_agent: Agent)             # LLM called transfer_to_X — switch agent, loop
class NextStepFinalOutput(output: Any)              # no tool calls or stop_at_tool matched — run output guardrails, return
class NextStepRunAgain                              # tools ran, feed results back, loop
class NextStepInterruption(interruptions: list[ToolApprovalItem])  # needs human approval — break, serialize, return
```

```python
# max_turns default: 10 (run_config.py:33)
DEFAULT_MAX_TURNS = 10

# loop skeleton (run_loop.py:671):
current_turn = 0
while True:
    # (turn=0 only) input guardrails
    turn_result = await run_single_turn(...)       # model + tools → SingleStepResult
    match turn_result.next_step:
        case NextStepInterruption: break           # HITL — break and return resumable state
        case NextStepHandoff(new_agent): current_agent = new_agent; continue
        case NextStepFinalOutput: break            # run output guardrails, return RunResult
        case NextStepRunAgain: ...                 # continue
    current_turn += 1
    if current_turn > max_turns: raise MaxTurnsExceeded
```

**`tool_use_behavior` — the termination policy knob (`agent.py:345-365`):**
- `"run_llm_again"` (default) — tools run, results fed back, model decides.
- `"stop_on_first_tool"` — first tool result IS the final output.
- `StopAtTools({"stop_at_tool_names": ["X", ...]})` — named tool result IS the final output.
- Callable `(ctx, list[FunctionToolResult]) -> ToolsToFinalOutputResult(is_final_output, final_output)` — custom policy.

**`reset_tool_choice = True` default (`agent.py:367`):** after any tool fires, resets `tool_choice` to `"auto"`. Prevents model from getting stuck in repeated tool-call loops.

---

## 5. RunState — Durable Pause/Resume Boundary (the HITL primitive)

`run_state.py` (3,305 LOC, v0.17.4). `CURRENT_SCHEMA_VERSION = "1.10"`. The schema-versioned, JSON-serializable run snapshot:

**What it captures:** `_current_turn`, `_current_agent`/`_starting_agent` (by stable identity key, not pickled), `_original_input`, `_model_responses: list[ModelResponse]`, `_context` (approvals + usage), `_generated_items + _session_items`, `_max_turns` (nullable since 1.10), `_conversation_id + _previous_response_id`, `_generated_prompt_cache_key`, `_reasoning_item_id_policy: Literal["preserve","omit"]`, all four guardrail-result lists, `_current_step: NextStepInterruption`, `_last_processed_response: ProcessedResponse` (the pending turn's full model output — needed to resume), `_tool_use_tracker_snapshot`, `_trace_state`, `_sandbox`.

**The HITL cycle:**
1. Run → a tool with `needs_approval=True` produces `ToolApprovalItem` instead of executing → `NextStepInterruption` → loop breaks → `RunResult._current_step = interruptions`.
2. `state.get_interruptions()` → `list[ToolApprovalItem]`.
3. `state.approve(item, always_approve=False)` or `state.reject(item, rejection_message=...)`.
4. `state.to_json()` → persist anywhere (DB, queue). Resume in any process.
5. `Runner.run(agent, input=state)` — `is_resumed_state` flag → `resolve_interrupted_turn()` re-applies approve/reject keyed by `(tool_name, call_id)`, executes approved tools (rejected ones return `rejection_message` as tool result), continues loop.

Approval key is `(tool_name, call_id)`. `always_approve=True` whitelists ALL future calls to that tool for the run's lifetime.

**`SUPPORTED_SCHEMA_VERSIONS = frozenset(...)` gates deserialization** — incompatible schema snapshots are rejected, not silently migrated. The TypeScript SDK has the same contract at schema `"1.12"`, demonstrating this is a cross-language interop primitive.

**Lancelot byte-path implication:** `lancelot.context` ContextVars are per-request byte-path state, not agent turn-state. RunState is fundamentally different — it's a mid-flight agent loop snapshot. The durability is new build target. Lancelot's ASGI seam could be the persistence layer (POST → serialize `RunState` → return 202 + run ID → client polls or webhooks when approved), but the RunState itself is agent-layer work.

---

## 6. Session Protocol (copy this shape)

`memory/session.py:14`:
```python
class Session(Protocol):
    async def get_items(limit: int | None = None) -> list[TResponseInputItem]
    async def add_items(items: list[TResponseInputItem]) -> None
    async def pop_item() -> TResponseInputItem | None      # undo last
    async def clear_session() -> None
```

SQLite implementation (`sqlite_session.py:147-165`) — two tables:
```sql
CREATE TABLE agent_sessions (session_id TEXT PRIMARY KEY, ...)
CREATE TABLE agent_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    message_data TEXT NOT NULL,    -- JSON
    FOREIGN KEY (session_id) REFERENCES agent_sessions(session_id) ON DELETE CASCADE)
CREATE INDEX idx_agent_messages_session_id ON agent_messages (session_id, id)
```
`get_items(limit)` reads last N by `id`; `pop_item` deletes max `id`. `ON DELETE CASCADE` makes `clear_session` a single DELETE.

Sessions and server-managed Responses API conversations are **mutually exclusive** (`session_persistence_enabled = session is not None and server_conversation_tracker is None`, `run.py:571`). History auto-loaded via `prepare_input_with_session()` (`run.py:527`) before first model call; new items written back after each turn.

**Lancelot byte-path implication:** `lancelot.context` and `lancelot.tenant` are request-scoped; no cross-request session primitive exists. The `Session` protocol above is the build target for agent memory persistence. The `pop_item` undo is non-obvious and worth including.

---

## 7. Lifecycle Hooks (12 callbacks)

`lifecycle.py`. Two parallel classes:

**`RunHooks` — fired for every agent in the run:**
- `on_agent_start(ctx, agent)` — fires on initial agent + every handoff.
- `on_agent_end(ctx, agent, output)`
- `on_handoff(ctx, from_agent, to_agent)`
- `on_tool_start(ctx, agent, tool)` — `tool` carries `tool_call_id` + `tool_arguments`.
- `on_tool_end(ctx, agent, tool, result)`
- `on_llm_start(ctx, agent, system_prompt, input_items)` — exposes the EXACT resolved system prompt.
- `on_llm_end(ctx, agent, response)`

**`AgentHooks` — per-agent scoped variant.** `on_handoff(ctx, agent, source)` fires when THIS agent is the TARGET of a handoff (vs `RunHooks.on_handoff` which fires on the source side).

Execution order per turn: `on_agent_start` → `on_llm_start` → (LLM) → `on_llm_end` → per tool: `on_tool_start` → (tool) → `on_tool_end` → if handoff: `on_handoff` → next turn or `on_agent_end`.

`on_llm_start` including `system_prompt: str | None` is the production hook for prompt injection auditing and per-turn dynamic system-prompt inspection.

---

## 8. Agent-as-Tool vs Handoff — Two Distinct Primitives

**Handoff** (`Agent.handoffs`): full control transfer. Receiving agent owns the conversation. History passed through `input_filter`. One agent is active at a time. Use for routing workflows (triage → specialist).

**Agent-as-tool** (`agent.as_tool(...)` → `FunctionTool`): sub-agent invoked synchronously, returns a string to the parent. Parent stays in control. Use for delegation-with-return (orchestrator → worker → result back). Full `max_turns`, `hooks`, `session`, `needs_approval`, `StructuredToolInputBuilder` (parent builds typed Pydantic input for sub-agent).

Most frameworks conflate these. The explicit distinction is the right design.

**Lancelot byte-path implication:** `TransportPool.spawn_child` / `fanout` handle the byte-path side of sub-agent I/O (HTTP requests with traceparent/agent_id propagation). The agent-loop semantics (who owns the conversation, what history flows) are different concepts and belong in the agent layer on top.

---

## 9. Operator / Responses API — Computer-Use Loop Constants

From `responses-loop.ts:98-99, 731-735, 826-828` (OpenAI's canonical reference implementation):

| Constant | Value | Meaning |
|---|---|---|
| `defaultInteractionDelayMs` | `120` | Pause between browser actions |
| `toolExecutionTimeoutMs` | `20_000` | Hard timeout for `exec_js` |
| `parallel_tool_calls` | `false` | Strict serial for CUA |
| `reasoning.effort` | `"low"` (sample) | Per-turn hidden CoT budget |
| `truncation` | `"auto"` | Server manages context window |
| `console.log` maxStringLength | `2_000` | Per-arg truncation cap in code-mode sandbox |
| `formatActionBatchDetail` cap | `2_000` | Payload truncation before event log |

The **`display()` helper** in code-mode sandbox (`responses-loop.ts:707-713`) — the model calls `display(base64Image)` from inside `exec_js` to push an `input_image` content part into the next turn's input. This is how vision feedback works without requiring a screenshot-and-send action.

**`pending_safety_checks`** on every `computer_call` — the model (or a server-side classifier) emits `[{code, message}]` before risky actions. The runtime must surface them and block until acknowledged. Codes include `pii_disclosure`, `financial_action`, `email_send`, `social_media_post`, `account_creation`, `malicious_instructions`. The sample throws; production implements an approval UI.

**Lancelot byte-path implication:** Lancelot has `lancelot.webhook` (Standard Webhooks sign/verify) and `lancelot.net` SSRF guard. The `pending_safety_checks` pattern maps cleanly onto a Lancelot webhook-driven approval gate: emit a signed webhook event when a safety check is raised, wait for HMAC-verified confirmation before proceeding. This is byte-path work Lancelot can own. The computer-use loop itself is agent logic.

---

## 10. Tool Schema — `ensure_strict_json_schema` (copy this function)

`strict_schema.py`. OpenAI strict mode requires:
- All optional fields → NOT `required: false`, instead included as required with `T | null` type.
- All objects → `additionalProperties: false`.
- No `anyOf`/`oneOf`/`allOf` of mixed nullable/non-nullable.
- No `$ref` without inlining (via `jsonref`).

This is called unconditionally on every `FunctionTool` schema AND on every `Handoff` input schema. The exact rules are non-obvious and painful to derive from OpenAI's docs — **lift the function as-is** into Lancelot's tool layer.

Swarm's `function_to_json` (`util.py:31-87`) is the anti-pattern: `Optional[int]` → `"string"` (the type_map lookup misses `Union[int, None]`), `List[str]` → `"string"` (same), no nested objects, no enums. Do not copy it.

**Lancelot byte-path implication:** `lancelot.tool` (`@app.tool → ToolSpec w/ auto JSON schema + auto output_schema`) generates schemas via Pydantic. Whether it runs `ensure_strict_json_schema` is worth checking — if not, it should.

---

## 11. Tracing Constants

`tracing/processors.py:34`:
```python
_OPENAI_TRACING_INGEST_ENDPOINT = "https://api.openai.com/v1/traces/ingest"
_OPENAI_TRACING_MAX_FIELD_BYTES = 100_000     # per-field truncation cap
max_batch_size = 128                          # spans per HTTP POST
schedule_delay = 5.0                          # seconds between flushes
export_trigger_ratio = 0.7                    # flush when queue hits 70% capacity
# Retry: max_retries=3, base_delay=1.0s (exponential: 1s, 2s, 4s)
```

14 span types: `AgentSpanData`, `TaskSpanData`, `TurnSpanData`, `FunctionSpanData`, `GenerationSpanData`, `ResponseSpanData`, `HandoffSpanData`, `CustomSpanData`, `GuardrailSpanData`, `TranscriptionSpanData`, `SpeechSpanData`, `SpeechGroupSpanData`, `MCPListToolsSpanData` (13 concrete + abstract base).

`GuardrailSpanData` carries `triggered: bool` — the tripwire result is observable in the trace.

`SpeechSpanData.first_content_at: str | None` — the voice TTFT equivalent (time to first TTS byte). Lancelot already has `lancelot.http.stream.ttft`; for voice it's the same concept at the audio layer.

**Lancelot byte-path implication:** Lancelot's OTel histogram infrastructure (`lancelot.upstream.*`, RED metrics, TTFT) is the transport layer. Agent-level spans (AgentSpanData, TurnSpanData, HandoffSpanData, GuardrailSpanData) are a new OTel span hierarchy above the byte-path histograms. They share the same W3C `traceparent` via `lancelot.context.trace_id`.

---

## 12. The Handoff Prompt (verbatim, `extensions/handoff_prompt.py`)

```python
RECOMMENDED_PROMPT_PREFIX = (
    "# System context\n"
    "You are part of a multi-agent system called the Agents SDK, designed to make agent "
    "coordination and execution easy. Agents uses two primary abstraction: **Agents** and "
    "**Handoffs**. An agent encompasses instructions and tools and can hand off a "
    "conversation to another agent when appropriate. "
    "Handoffs are achieved by calling a handoff function, generally named "
    "`transfer_to_<agent_name>`. Transfers between agents are handled seamlessly in the background;"
    " do not mention or draw attention to these transfers in your conversation with the user.\n"
)
```

Note the shipped typo: "two primary abstraction" (should be plural). Used via `prompt_with_handoff_instructions(prompt: str) -> str` which prepends this. The instruction "do not mention or draw attention to these transfers" is the key production insight — without it models describe handoffs to users.

---

## 13. Stream Events (misspelling note)

`stream_events.py`. `RunItemStreamEvent.name` Literal values include:
- `"handoff_occured"` — **misspelled, kept for compat** (comment: "This is misspelled, but we can't change it because that would be a breaking change."). The correct spelling `occurred` was never shipped.

The three event types: `RawResponsesStreamEvent` (raw Responses API SSE chunks), `AgentUpdatedStreamEvent` (current_agent changed), `RunItemStreamEvent` (11 sub-events including the misspelled handoff one).

---

## 14. AVOID — Anti-Patterns from These Sources

**Swarm `function_to_json`:** `Optional[int]` → `"string"`. Never derive tool schemas from `inspect.signature` without Pydantic. The wart propagated into every downstream system that copied Swarm naively.

**Swarm `context_variables` injection via `func.__code__.co_varnames`:** includes ALL local variable names, not just parameters. A local variable named `context_variables` in the function body would trigger injection. The correct check is `inspect.signature(func).parameters`. Don't copy this.

**Swarm streaming missing `and active_agent` in while condition (`run_and_stream`, line 154):** the blocking `run` terminates when `active_agent is None`; the streaming variant does not. This is a latent bug in Swarm. Test both paths independently.

**Swarm `context_variables` mutation via nested mutable objects:** `copy.deepcopy` copies the dict but callers mutating nested lists/dicts inside tools may still see unexpected behavior. Typed per-agent context (Agents SDK's `Agent[TContext]` generic) is the fix.

**Responses API `parallel_tool_calls=False` for CUA** — correct for computer-use (serial action execution), wrong for multi-tool agent workflows. Don't cargo-cult this into a general agent loop.

**`reasoning.effort = "low"` as default** — the sample chooses `"low"` for demo scenarios. Production Operator likely uses `"medium"` or `"high"` for hard tasks. Hardcoding `"low"` in an agent framework is a performance trap.

**`verbosity="low"` for all GPT-5 defaults** — `models/default_models.py` sets this for all models because `"minimal"` is not supported by hosted tools (FileSearch, ImageGeneration). Note: `"minimal"` is not the same as `"low"`. If Lancelot's model adapter exposes verbosity, don't map `"low"` and `"minimal"` as equivalent.

---

## 15. GPT-5 Reasoning Effort Table (11 patterns, `models/default_models.py:50-64`)

```python
# date-suffix optional: (?:-\d{4}-\d{2}-\d{2})?
gpt-5            → "low"
gpt-5.1          → "none"
gpt-5.2          → "none"
gpt-5.2-pro      → "medium"
gpt-5.2-codex    → "low"
gpt-5.3-codex    → "none"
gpt-5.4          → "none"
gpt-5.4-pro      → "medium"
gpt-5.4-mini     → "none"
gpt-5.4-nano     → "none"
gpt-5.5          → "none"
# chat-latest aliases (gpt-5-chat-latest, etc.) → NO reasoning param at all
```

`gpt-5.4-mini-2025-08-15` matches `gpt-5.4-mini` via the date suffix regex. A Lancelot model adapter that wants to set sane reasoning defaults should adopt this table or a provider-neutral equivalent.

---

## Honest Hype Flags

**Reasoning effort "low"/"medium"/"high"** — these are vendor-specific calibration values that will churn as OpenAI redefines model capability per generation. The pattern (configurable per-turn effort dial) is durable; the exact values are not.

**`previous_response_id` / Responses API server-side state** — this is OpenAI-specific. Anthropic, Gemini, and local models don't have it; they still require client-managed message arrays. Don't build your agent loop assuming server-side state management. The Chat Completions path (full history resent, `Converter` translates items) is the portable design.

**14 span types + `api.openai.com/v1/traces/ingest`** — the endpoint is OpenAI-specific. The span taxonomy (`AgentSpanData`, `TurnSpanData`, `HandoffSpanData`, `GuardrailSpanData`) is durable and worth copying; the vendor endpoint is not.

**`RunState` schema versioning at `"1.12"` in TypeScript vs `"1.10"` in Python** — the schemas are ahead of each other and not currently interop-compatible across languages. Cross-language agent durability is aspiration, not production reality.
