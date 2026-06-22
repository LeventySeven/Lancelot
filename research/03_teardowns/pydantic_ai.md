# Pydantic AI — Lancelot harvest note

Source: `researchfms/teardowns/PYDANTIC_AI.md` — full read, commit `cff2825fb5534ee4f0ecea6a79ceb0a91c87160c` (2026-05-30).

---

## 1. The core primitive to copy: typed-return-annotation graph engine

The agent loop is not a `while True:` — it is a three-node typed state machine where **the `-> BaseNode | End[T]` return annotation on each node's `run()` method IS the adjacency list**, read at runtime by `get_type_hints()` (`basenode.py:104-136`). A node with no return annotation raises `GraphSetupError` at startup. This is compile-time-enforced topology.

The cycle is:
```
UserPromptNode → ModelRequestNode ⇄ CallToolsNode → End(FinalResult)
```
(`_agent_graph.py:2086` — `build_agent_graph()`)

**Why copy this for Lancelot:** it is the one structural primitive (not a constant or API shape) that makes durability, HITL, and re-entrant stepwise execution fall out of the engine rather than being bolted on. Every other framework (`while True: call_model; call_tools`) requires a bespoke checkpoint subsystem to get the same properties.

The generic driver (`graph.py:664-767`):
```python
async def next(self, node):
    await persistence.snapshot_node_if_new(node.snapshot_id(), state, node)
    async with persistence.record_run(node.snapshot_id()):
        next_node = await node.run(GraphRunContext(state, deps))
    if isinstance(next_node, End): await persistence.snapshot_end(state, next_node)
    else:                          await persistence.snapshot_node(state, next_node)
    return next_node
```
Key: **snapshot before AND after every node transition**, not only at user-visible boundaries. State is a plain Pydantic dataclass — round-trips through JSON freely.

Public entrypoint `Agent.iter()` yields the `GraphRun` so user code can step node-by-node, inspect/mutate `next_node`, and call `run.next(node)` externally. `Agent.run()` is a thin drain loop over that. The loop is genuinely external and re-entrant.

---

## 2. Two independent retry budgets (copy verbatim)

**Per-tool retries:** `ToolManager.default_max_retries = 1` (`tool_manager.py:92`). Carried across steps by `for_run_step` (`tool_manager.py:120-145`): `retries = {failed_tool: prev_count + 1}`. Raises `UnexpectedModelBehavior(f'Tool {name!r} exceeded max retries count of {max_retries}')` at the limit.

**Output-validation retries:** `GraphAgentState.output_retries_used`, incremented by `consume_output_retry(max_output_retries)` (`_agent_graph.py:158-175`). Raises `UnexpectedModelBehavior(f'Exceeded maximum output retries ({max_output_retries})')` at the limit.

These are separate because a tool loop-failure and a structured-output parse failure are different failure modes with different budgets. Conflating them into one counter means a flaky tool eats your output retry budget. **Lancelot must implement both.**

The retry payload is NOT a fixed string. `_wrap_error_as_retry` (`tool_manager.py:183-191`) wraps a Pydantic `ValidationError` into a `RetryPromptPart` whose content is `error.errors(include_url=False, include_context=False)` — the structured Pydantic error dict IS the corrective prompt. This is the self-healing tool-args loop.

Special case: `check_incomplete_tool_call` (`tool_manager.py:142-156`) — if `finish_reason == 'length'` and the last tool call's args fail JSON-parse (mid-stream truncation), raise a targeted `UnexpectedModelBehavior` telling the user to raise `max_tokens`, rather than burning retries. **Do not skip this; missing it silently burns retries on a configuration problem.**

---

## 3. Tool priority rule: tool calls win over text (copy verbatim)

`CallToolsNode._run_stream` (`_agent_graph.py:1252`): when a response contains BOTH text AND tool calls (Anthropic does this — the text is usually a preamble), execute the tools and **discard the text this turn**. The discarded text is recoverable via `_recover_text_from_message_history` if the next response is empty.

The exact branch (`_agent_graph.py:1201-1210`):
```python
if tool_calls:
    async for event in self._handle_tool_calls(ctx, tool_calls): yield event
    return
# else: try to interpret text as output
```

Missing this means your agent gets confused by Anthropic preambles and retries the model unnecessarily.

---

## 4. The "nudge" retry string (copy verbatim)

When the model returns a response that is neither a valid output nor a tool call, synthesise a `RetryPromptPart` with content (`_agent_graph.py:1234`):
```python
f'Please {" or ".join(alternatives)}.'
```
where `alternatives` is built from the schema: subset of `{'return text', 'call a tool', 'include your response in a tool call', 'return an image'}`. This is the literal string sent back to the model. No fancy engineering. The nudge is minimal and schema-driven.

---

## 5. Parallel tool execution with deterministic history ordering

`_call_tools` (`_agent_graph.py:1818-1921`) runs function tools in three modes:
- `'sequential'` — if ANY called tool has `tool_def.sequential=True`, forces the whole batch sequential.
- `'parallel'` (default) — `asyncio.wait(FIRST_COMPLETED)` loop, events emitted as tasks complete.
- `'parallel_ordered_events'` — run parallel, emit events in call-index order.

Mode is decided by `get_parallel_execution_mode` (`tool_manager.py:155-168`): `'sequential'` if any sequential tool, else the value of `_parallel_execution_mode_ctx_var` (default `'parallel'`).

**Results are always appended to history in `sorted(call_index)` order regardless of completion order** (`_agent_graph.py:1916`). Parallel execution, deterministic transcript. This is the key invariant — missing it makes replays non-deterministic.

---

## 6. End-strategy semantics (three modes, exact behaviour)

`EndStrategy = Literal['early', 'graceful', 'exhaustive']` (default `'early'`, `agent/__init__.py:285`):
- **`'early'`**: first valid output tool → remaining output tools skipped + remaining function tools get stub return `'Tool not executed - a final result was already processed.'`
- **`'graceful'`**: remaining output tools skipped, function tools run to completion.
- **`'exhaustive'`**: all output + function tools run; first valid output is the final result.

The stubs sent back to the model for skipped tools are verbatim strings from `_agent_graph.py:1627`/`:1714`. The model sees them as tool returns; they explain why the tool did not run.

---

## 7. Output modes and the output-tool trick (three modes)

`OutputMode = Literal['text','tool','native','prompted','tool_or_text','image','auto']` (`output.py:42`). The structured subset: `tool` / `native` / `prompted`.

**`tool` (default):** output type becomes a tool named `'final_result'` with description `'The final response which ends this conversation'` (`_output.py:72-73`). Model "returns" structured output by calling that tool; args validated against schema. This is `ModelProfile.default_structured_output_mode = 'tool'` (`profiles/__init__.py:95`).

**`native`:** uses provider-native structured output (OpenAI `response_format`, Gemini `responseSchema`). Schema sent on request; response body IS the JSON. Do not also inject the prompted template unless `native_output_requires_schema_in_instructions=True` (default `False`, `profiles/__init__.py:107`).

**`prompted` (fallback for models with no tool-calling or native JSON):** inject verbatim template into instructions (`profiles/__init__.py:97-105`):
```
Always respond with a JSON object that's compatible with this schema:

{schema}

Don't include any text or Markdown fencing before or after.
```
`{schema}` is replaced by `json.dumps(object_def.json_schema)` (`_output.py:717-729`). Template is overridable per `ModelProfile`.

After any final result is produced, send back a synthetic ack: `'Final result processed.'` as a `ToolReturnPart` (`_agent_graph.py:1523`). The model must see this to know the tool completed.

---

## 8. Schema generation from Python signature (the hidden `function_schema` machinery)

`_function_schema.py:103-281` — the mechanism behind "just write a function":
1. Build `pydantic._internal.GenerateSchema` with `ConfigDict(title=function.__name__, use_attribute_docstrings=True)`.
2. Walk `signature(function)` + `get_type_hints(original_func, include_extras=True)`.
3. If first param is annotated `RunContext[...]`, strip it from the JSON schema (detected by `_is_call_ctx`).
4. Parse docstring via `griffe` (Google/NumPy/Sphinx auto-detected) — injects `arg: description` into JSON schema property `description`. `use_attribute_docstrings=True` also picks up Pydantic field attribute docstrings.
5. Build a `pydantic-core` `SchemaValidator` once (used at call time, not re-built per request).
6. Emit `json_schema` (sent to model) from same schema via `schema_generator().generate(schema)`.

**Single-arg unwrapping**: if the tool takes exactly one model-like argument, the tool's JSON schema IS the model's schema (no envelope). `single_arg_keys` records top-level keys for disambiguation at validate time.

**Return schema**: `TypeAdapter(return_type).json_schema(mode='serialization')` computed eagerly, falling back to `{}` with a warning on unsupported types. Used when `include_return_schemas=True` on the toolset.

For Lancelot: `@app.tool` already auto-derives JSON schema + `output_schema`. The question is whether to replicate the docstring→description injection — it is cheap and materially improves model tool-use. Copy the `griffe` approach or equivalent.

---

## 9. Streaming constants (debounce + partial validation)

- **`debounce_by: float | None = 0.1`** (`result.py:76,105,137,533`) — universal streaming constant. Groups raw model deltas into 100 ms temporal windows before delivery. `None` = per-token. This is not a suggestion; it materially reduces overhead on high-throughput token streams.
- **Partial-validation mode**: `allow_partial='trailing-strings'` (`tool_manager.py:234`) — pydantic-core's flag that allows incomplete JSON to validate. An incomplete `{"name": "Ada", "age":` validates to `{name: 'Ada'}` mid-stream. This is what makes streaming structured-output preview possible without buffering the full response.
- **Event taxonomy** (wire names, `messages.py:2699-2933`): `part_start` / `part_delta` / `part_end` / `final_result`. Delta payload discriminated by `part_delta_kind`: `'text'`, `'thinking'`, `'tool_call'`. Each delta has `.apply(part)` to incrementally build the full part.

---

## 10. Durability / HITL — the graph persistence contract

`BaseStatePersistence` (`persistence/__init__.py:106`):
```python
async def snapshot_node(state, next_node)
async def snapshot_node_if_new(snapshot_id, state, next_node)  # idempotent, dedup on resume
async def snapshot_end(state, end)
async def record_run(snapshot_id) -> AsyncContextManager       # status: created→running→success/error
async def load_next() -> NodeSnapshot | None                   # next 'created' → 'pending'
async def load_all() -> list[Snapshot]
```

`SnapshotStatus = Literal['created','pending','running','success','error']` (`persistence/__init__.py:32`). The five-state lifecycle maps to workflow-engine states.

Three shipped implementations: `SimpleStatePersistence` (one in-memory snapshot — enough for next step), `FullStatePersistence` (full history, deep-copied), `FileStatePersistence` (JSON-file-backed, survives process restart).

HITL resume path (`UserPromptNode._handle_deferred_tool_results`, `_agent_graph.py:355-400`): finds the last `ModelResponse` with tool calls, maps provided results by `tool_call_id`, marks already-executed calls `'skip'`, **jumps straight to `CallToolsNode`**. This bypasses the model entirely for approved/denied deferred calls — the human-in-the-loop loop is a literal graph edge.

`ToolApproved.override_args` lets the human edit args before execution (`_agent_graph.py:1679`). This is the approval gate: the human sees the proposed call, edits args if needed, and the framework re-executes with the modified args.

---

## 11. `RunContext` — the per-call object (field contract)

`_run_context.py:34`. The fields every tool and hook sees (copy this interface for Lancelot's `AgentContext`):

| Field | Type | Meaning |
|---|---|---|
| `deps` | `DepsT` | User dependency object (typed). |
| `model` | `Model` | The model running this turn. |
| `usage` | `RunUsage` | Cumulative usage (shared across nested agents when threaded). |
| `prompt` | `str \| Sequence[UserContent] \| None` | Original user prompt. |
| `messages` | `list[ModelMessage]` | Full transcript so far. |
| `retries` | `dict[str, int]` | Per-tool retry counts. |
| `tool_name` / `tool_call_id` | `str \| None` | Identity of the currently-executing tool call. |
| `retry` / `max_retries` | `int` | Counters for the current tool/output validation. |
| `run_step` | `int` | Current model-request step count. |
| `tool_call_approved` | `bool` | True if approval-gated call was approved (HITL). |
| `run_id` / `conversation_id` | `str` | UUID7 identifiers. |
| `partial_output` | `bool` | True during partial (streaming) arg validation. |

`run_id` and `conversation_id` are UUID7 (time-ordered) generated at run init (`_agent_graph.py:125-126`). UUID7 is the right choice here — time-ordered, sortable, useful for pagination of run history.

---

## 12. Conversation threading (copy the `resolve_conversation_id` rule)

`_agent_graph.py:90-114`: `conversation_id` threads multiple runs.
- Explicit `'new'` sentinel → fresh UUID7.
- Explicit string → as-is.
- Else: most recent non-None `conversation_id` in the supplied `message_history`.
- Else: fresh UUID7.

`result.new_messages()` returns only messages from `new_message_index` onward (set at `UserPromptNode.run` init, `_agent_graph.py:141`). This is the primitive for "give me only the new turn to persist" without re-sending the whole history.

---

## 13. Exact constants table

| Constant | Value | Source |
|---|---|---|
| Request limit (loop cap) | `50` | `usage.py:272` |
| Per-tool default retries | `1` | `tool_manager.py:92` |
| End strategy default | `'early'` | `agent/__init__.py:285` |
| Default structured output mode | `'tool'` | `profiles/__init__.py:95` |
| Output tool name | `'final_result'` | `_output.py:72` |
| Output tool description | `'The final response which ends this conversation'` | `_output.py:73` |
| Parallel execution mode default | `'parallel'` | `tool_manager.py:38,166` |
| Streaming debounce | `0.1` (100 ms) | `result.py:76` |
| Partial-validation mode | `'trailing-strings'` | `tool_manager.py:234` |
| `NEW_CONVERSATION` sentinel | `'new'` | `_agent_graph.py:85` |
| `run_id`/`conversation_id` generator | UUID7 | `_agent_graph.py:125-126` |

---

## 14. Byte-path implications (Lancelot lens)

| Pydantic AI primitive | Lancelot byte-path status |
|---|---|
| `TransportPool.request` / `stream` / `fanout` / `spawn_child` | **Already built.** Pydantic AI's `Model.request` + retry + rate-limit is the same job. The transport layer is done. |
| `lancelot.upstream_metrics` (token extraction, cost, error taxonomy) | **Already built.** Pydantic AI's `RunUsage` + `UsageLimits` is the usage-accounting layer — Lancelot already has `cost_usd`/`tokens_in`/`tokens_out` ContextVars + OTel histograms. |
| `lancelot.context` (`agent_id`, `request_id`, `tenant_id`, `trace_id`) | **Already built.** `RunContext.run_id` + `conversation_id` maps to `request_id` + a new `conversation_id` ContextVar. |
| `@app.tool` / `lancelot.mcp.mount` | **Already built.** The Pydantic AI toolset abstraction and `MCPServer` toolset are more general; the 4KB warn discipline from `lancelot.mcp.mount` should stay. The gap is the toolset COMPOSITION layer (CombinedToolset, FilteredToolset, etc.) and per-step `for_run_step` re-resolution. |
| `lancelot.canonical.dumps_canonical` | **Provides prompt-cache stability** — maps to Pydantic AI's `_clean_message_history` sorting (tool-return parts sorted to front). Both are about deterministic transcript ordering. Use `dumps_canonical` for any JSON serialised into prompts. |
| `@app.batch` | **Provides request coalescing** — no analogue in Pydantic AI. Relevant if Lancelot's agent layer needs to batch tool calls to a shared LLM backend. |
| `lancelot.stream` (R3 pump, backpressure, TTFT) | **Provides the byte path for streaming agent responses**. Pydantic AI's `debounce_by=0.1` + `part_delta` events map to Lancelot's `LANCELOT_STREAM_BUFFER` + SSE events. The TTFT metric fires on first `part_delta` of kind `'text'`. |
| Graph-node persistence (snapshot before/after each node) | **NOT built.** This is the main gap. Lancelot has `lancelot.secrets`/`lancelot.context` but no per-node snapshot seam. This is the new primitive to build. |
| HITL / deferred tools / approval gate | **NOT built.** Falls out of graph persistence. One consequence: `ToolApproved` / `ToolDenied` / `CallDeferred` are not in Lancelot's tool surface. |
| Two independent retry budgets (per-tool + output-validation) | **NOT built.** Lancelot's error taxonomy covers `ok/quota/timeout/permanent/transient/upstream_5xx` at the transport layer but not tool-execution retry accounting inside an agent loop. |
| Output modes (`tool`/`native`/`prompted`) + `final_result` tool | **NOT built.** Lancelot's `@app.tool` + `output_schema` auto-derive exists but there is no `OutputSchema.build()` dispatcher choosing between modes based on model capability. |
| History processor / `_clean_message_history` | **NOT built.** The merge rules (consecutive requests merged, tool-return parts sorted to front) are not in Lancelot's message layer. |
| Capability onion (`wrap_model_request`, `before/after_node_run`, etc.) | **NOT built.** Lancelot has `@app.middleware` for HTTP-layer middleware; there is no per-agent-step hook framework. |

---

## 15. Anti-patterns (do not copy)

**Do not copy `pydantic_graph` as a dependency.** It is a separate PyPI package (`pydantic-graph`), and adding it violates the cardinal rule (runtime dep = pydantic >= 2.7 only). Copy the ENGINE DESIGN (typed-return-annotation nodes + generic `GraphRun.next` driver) and implement it in ~300 LoC. The full `pydantic_graph` package is ~5k LoC with Temporal/DBOS integrations, Mermaid diagram export, and Logfire instrumentation — none of which Lancelot needs.

**Do not copy the toolset wrapping hierarchy as 9 classes.** `FilteredToolset`, `RenamedToolset`, `PrefixedToolset`, `WrapperToolset`, `PreparedToolset`, `ApprovalRequiredToolset`, `ExternalToolset`, `_DynamicToolset`, `CombinedToolset` is a 9-class type hierarchy where the useful primitives are: (1) per-step `for_run_step` re-resolution, (2) sequential-flag on individual tools, and (3) HITL approval gating. Implement those three; skip the class hierarchy.

**Do not copy `FallbackModel`'s try-in-order approach as the resilience primitive.** `TransportPool` already provides retry + backoff + rate-limit + Retry-After honoring — that is the correct resilience layer. Wrapping multiple models is a policy decision, not infrastructure.

**Do not copy the `debounce_by=0.1` constant as a hardcoded default.** Lancelot's `LANCELOT_STREAM_BUFFER` already tunes the mpsc buffer depth for backpressure. The debounce is a client-side batching concern; expose it as a parameter on Lancelot's streaming agent response, not hardcoded.

**Do not copy the capability hook framework as-is before the core loop is built.** The 21 built-in capabilities and the 6-stage × 4-hook taxonomy is correct for a mature production framework. For the first Lancelot agent layer, implement `wrap_model_request` (the critical one — middleware around the actual LLM call) and defer the remaining hooks. They are an extension point, not a core.

---

## 16. What is genuinely novel (worth preserving in the harvest)

1. **Return-annotation-as-edge-definition.** No other Python agent framework in the batch uses the type checker to enforce graph topology. This is durable — it survives model generations, is zero-overhead, and makes illegal state unrepresentable.

2. **Structured validation error AS the retry prompt.** `ValidationError.errors(include_url=False, include_context=False)` is sent directly to the model as the corrective payload. The model gets precise field-level errors, not a vague "please try again." This is the right design and matches what the research corpus calls "show the model its own mistakes."

3. **Deferred-tool / HITL as a graph edge, not a bespoke checkpoint.** The run ends normally with `DeferredToolRequests` as output; resume jumps straight to `CallToolsNode`. No separate "workflow state" concept — it is the same `message_history` + `deferred_tool_results` parameter. This is the minimal correct design.

4. **Per-step tool manager rebuild with retry carry-over.** `for_run_step` rebuilds the tool manager at each `ModelRequestNode`, re-resolving dynamic toolsets and carrying over per-tool retry counts from failed calls. This is the correct design for dynamic tool corpora (MCP servers that change, toolsets that depend on run state). Most frameworks rebuild tools once at agent init.
