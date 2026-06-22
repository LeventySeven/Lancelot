# LlamaIndex — Mechanism-level distillation for Lancelot agents

Source: `researchfms/teardowns/LLAMAINDEX.md` (3 rounds: agent surface Round 1, deep pass Round 2 2026-05-15, workflow engine Round 3 2026-05-30, ingestion §I3 2026-05-27).
LlamaIndex version: `llama-index-core==0.14.21/0.14.22`, `llama-index-workflows==2.20.0`.

---

## What's actually novel (the 3 load-bearing ideas)

1. **Agent-as-Workflow (FSM via typed events).** Every agent method is a `@step` that consumes one event type and emits another. The graph is inferred entirely from type annotations — no explicit graph declaration. The runtime schedules concurrent step instances by type-matching, gates fan-out concurrency via `num_workers=4` per step, and joins with `ctx.collect_events`.

2. **Parallel tool calls via event fan-out + ordered join.** `parse_agent_output` emits N `ToolCall` events via `ctx.send_event`; each runs as an independent `call_tool` step instance (concurrent, up to `num_workers`). `aggregate_tool_results` calls `ctx.collect_events(ev, expected=[ToolCallResult]*N)` which returns `None` until all N have buffered, then returns them in the declared order. **The fan-in guarantees ordering regardless of completion order.**

3. **Tick-based deterministic-replay control loop.** The workflow engine (separate `llama-index-workflows` package, re-architected in 2026) runs a single `tick_buffer` with one `await` point per iteration. All state transitions are serial ticks. A `rewind_in_progress(state, start)` call replays in-progress work from a serialized `BrokerState` snapshot. This is the durable/resumable seam — same convergent design as LangGraph checkpoint-replay and Pydantic AI graph-snapshot.

---

## Primitives worth copying (mechanism-level, cited)

### 1. `ToolOutput(content, tool_name, raw_input, raw_output, is_error, exception)` — tool exception capture

Source: `multi_agent_workflow.py:347-378`.

```python
async def _call_tool(self, ctx, tool, tool_input):
    try:
        tool_output = await tool.acall(**tool_input)
    except Exception as e:
        event_exception = _get_waiting_for_event_exception()
        if event_exception and isinstance(e, event_exception):
            raise        # re-raise workflow pause signals
        tool_output = ToolOutput(
            content=str(e),
            tool_name=tool.metadata.get_name(),
            raw_input=tool_input,
            raw_output=str(e),
            is_error=True,
            exception=e,
        )
    return tool_output
```

Why: tool exceptions become model-visible error strings (`"content": "<msg>", "is_error": true`), not agent crashes. The model sees the error and can recover, retry, or fall back to another tool. **The one critical exception**: workflow pause signals (the `WaitForEvent` exception class used by HITL) are re-raised, not captured. So the try/except must distinguish "tool failure" from "workflow control flow."

Lancelot byte-path: `lancelot.tool.ToolSpec` auto-derives schemas but has no `is_error` field or exception-capture contract. This is a pure agent-loop primitive — adopt it in the Lancelot agent layer's `call_tool` dispatch. The `exception` field is useful for logging + structured error taxonomy (`lancelot.upstream_metrics` already has `ok/quota/timeout/permanent/transient/upstream_5xx`; tool errors should parallel this).

### 2. `return_direct=True` + handoff exception — short-circuit mechanism

Source: `base_agent.py:686-719`, `multi_agent_workflow.py:215-245`.

In `aggregate_tool_results`:
```python
if any(r.return_direct and not r.tool_output.is_error for r in tool_call_results):
    return_direct_tool = next(r for r in tool_call_results if r.return_direct and not r.tool_output.is_error)
    ...
    if return_direct_tool.tool_name != "handoff":     # handoff is the one return_direct tool that DOESN'T stop
        return StopEvent(result=result)
```

`return_direct: bool = False` is a per-tool flag on `ToolMetadata`. If True and the tool does not error, the tool's output IS the final answer — the loop terminates without another LLM call. The `handoff` tool is the explicit exception: it has `return_direct=True` so it short-circuits the normal "call LLM again" path, but does NOT terminate the agent — it mutates `ctx.store["next_agent"]` and the workflow switches the active agent on the next turn.

Why: this is the clean way to give a tool "veto power" over the loop (return_direct=True stops, is_error=True continues). LangGraph uses `StopAtTools`; OpenAI Agents SDK uses `tool_use_behavior="stop_on_first_tool"` at the agent level. LlamaIndex's version is declared at the TOOL level, which is more composable. Copy both the flag and the handoff exception — any "control-flow tool" (not just handoffs) needs the same escape hatch.

Lancelot byte-path: `@app.tool` has no `return_direct` equivalent. Add `return_direct: bool = False` to `ToolSpec` in the agent layer. No byte-path change required.

### 3. `ctx.collect_events(ev, expected=[T]*N)` — typed ordered fan-in join

Source: `base_agent.py:669-670`, workflow engine `context/context.py:436-475`.

```python
# aggregate_tool_results
tool_call_results = ctx.collect_events(ev, expected=[ToolCallResult] * num_tool_calls)
if not tool_call_results:
    return None    # not all results arrived yet; return None to tell the engine "re-enter when next event arrives"
```

`collect_events` buffers events of the declared type(s) until ALL expected have arrived, then returns them in the declared order (not arrival order). Returning `None` from a step is the "don't terminate yet" signal — the engine re-invokes this step the next time a matching event arrives.

The buffer is keyed by `buffer_id` (derived from the task name if not given) so concurrent workers don't cross-contaminate each other's join buffers.

Why: the natural parallel-tool-call join pattern without needing `asyncio.gather` inside a step. `asyncio.gather` couples the fan-out and fan-in; `collect_events` decouples them — the sender emits events sequentially via `ctx.send_event`, and the receiver accumulates asynchronously. Also, `asyncio.gather` inside a step blocks the step's worker slot; event-based fan-in uses the scheduler's concurrency slots.

Lancelot byte-path: `TransportPool.fanout(first_n, deadline, cancel_laggards)` is the byte-path analogue — same concurrent-N-then-join pattern. The difference is `fanout` runs N HTTP calls; `collect_events` joins N in-process steps. For the agent layer, copy `collect_events` as the in-process join primitive and let `fanout` remain the outbound-LLM primitive.

### 4. `scratchpad` — per-turn in-context buffer, flush on finalize

Source: `workflow/function_agent.py:22, 112-134, 188-194`.

```python
scratchpad = await ctx.store.get(self.scratchpad_key, default=[])
current_llm_input = [*llm_input, *scratchpad]
...
scratchpad.append(last.message)
await ctx.store.set(self.scratchpad_key, scratchpad)
# ... finalize() flushes scratchpad into memory atomically
```

The scratchpad accumulates assistant messages and tool results across iterations WITHIN a turn. They're NOT committed to `memory` until `finalize()` at end-of-turn. This is the key for multi-tool turns: scratchpad shows the model its own previous tool calls in the same turn without writing intermediate failures to persistent memory. `finalize()` commits atomically.

Why durable: if a tool call fails mid-turn, the failed partial scratchpad is NOT written to `memory`. The next turn starts clean. This is also what enables parallel tool calls — the scratchpad accumulates all N tool results before `aggregate_tool_results` fires `finalize`.

Lancelot byte-path: no equivalent. This is a pure agent-layer primitive. The `lancelot.context` ContextVars (`cost_usd`, `tokens_in`, etc.) are request-scoped; the scratchpad is agent-turn-scoped — different lifetime.

### 5. Verbatim prompts (production-grade, copy these)

All cited from `core/agent/workflow/prompts.py` or `core/agent/react/templates/system_header_template.md`.

**Max-iterations graceful stop prompt** (fires at `DEFAULT_MAX_ITERATIONS=20` with `early_stopping_method="generate"`):
```
You have reached the maximum number of iterations ({max_iterations}).
Based on the information gathered so far, please provide a helpful final response to the user's original query.
Do not attempt to use any more tools. Simply summarize what you have learned and provide the best possible answer.
```

**State injection into user message** (verbatim `DEFAULT_STATE_PROMPT`):
```
Current state:
{state}

Current message:
{msg}
```
Applied exactly once per run (latch `formatted_input_with_state`), to the LAST `TextBlock` of the LAST user message, iterating blocks in reverse. Image/audio blocks preserved untouched. If applied every turn the prompt exponentially nests.

**Handoff prompt** (verbatim `DEFAULT_HANDOFF_PROMPT`):
```
Useful for handing off to another agent.
If you are currently not equipped to handle the user's request, or another agent is better suited to handle the request, please hand off to the appropriate agent.

Currently available agents:
{agent_info}
```

**Handoff output** (verbatim `DEFAULT_HANDOFF_OUTPUT_PROMPT`):
```
Agent {to_agent} is now handling the request due to the following reason: {reason}.
Please continue with the current request.
```

**ReAct system header** (verbatim, `templates/system_header_template.md`):
```
...
Please ALWAYS start with a Thought.
NEVER surround your response with markdown code markers. You may use code markers within your response if you need to.
Please use a valid JSON format for the Action Input. Do NOT do this {'input': 'hello world', 'num_beams': 5}}. If you include the "Action:" line, then you MUST include the "Action Input:" line too, even if the tool does not need kwargs, in that case you MUST use "Action Input: {{}}".
```
Note the explicit "Action Input: {{}}" rule — production models still forget to include the key, causing parse failures.

**ReAct stop condition** (the model emits this when done):
```
Thought: I can answer without using any more tools. I'll use the user's language to answer
Answer: [your answer here]
```

Lancelot byte-path: `lancelot.canonical.dumps_canonical` is the cache-stability primitive for prompt serialization. The verbatim prompts above feed the message construction layer — no byte-path change.

### 6. Single `handoff(to_agent, reason)` tool vs N `transfer_to_X` tools

Source: `multi_agent_workflow.py:72-91, 215-245`.

```python
async def handoff(ctx: Context, to_agent: str, reason: str) -> str:
    agents = await ctx.store.get("agents")
    if to_agent not in agents:
        valid = ", ".join([x for x in agents if x != current_agent_name])
        return f"Agent {to_agent} not found. Please select a valid agent to hand off to. Valid agents: {valid}"
    await ctx.store.set("next_agent", to_agent)
    return handoff_output_prompt.format(to_agent=to_agent, reason=reason)
```

The model sees ONE `handoff(to_agent: str, reason: str)` tool regardless of how many agents are defined. The `to_agent` argument's valid values are injected into the description (`{agent_info}`). Advantage: cleaner tool schema at scale (10 agents = 1 tool, not 10 tools). Disadvantage: `to_agent` is free-form text — the framework validates and may need retries.

Per-agent allowlist: `Agent.can_handoff_to: Optional[List[str]]`. `None` = any; empty list = no handoff tool generated.

Lancelot byte-path: `TransportPool.spawn_child` is the byte-path analogue for spinning up a sub-agent call, but it doesn't implement the "active agent switch" pattern. The single-handoff-tool pattern is a pure agent-loop primitive.

### 7. ReAct JSON repair chain — `dirtyjson` + last-resort regex

Source: `agent/react/output_parser.py:628-654` (§R2.1).

```python
json_str = extract_json_str(action_input)           # find {...} substring
try:    dirtyjson.loads(json_str)                   # lenient JSON parse (single quotes, trailing commas, etc.)
except: action_input_parser(json_str)               # regex: extract '"key": "value"' pairs
```

`action_input_parser` (lines 28-32): converts `'foo'` → `"foo"`, matches `"(\w+)":\s*"([^"]*)"` — lossy (drops numeric/boolean) but never raises. Fallback for "horrible JSON strings" from weaker models.

Why: any ReAct-style loop on a non-function-calling model will hit malformed JSON. The `dirtyjson` lenient parse handles most cases; the regex last-resort guarantees the loop never crashes on parse failures. This is the production-grade fallback chain.

Lancelot byte-path: `lancelot.canonical.dumps_canonical` produces stable JSON for outbound calls. Inbound tool-call parsing (from the model) needs the repair chain — no byte-path help here. Copy this for the Lancelot ReAct agent variant.

### 8. `@step(num_workers=4)` — per-step concurrency constant

Source: `decorators.py:99`, `control_loop.py:698-1011` (§W4.1–W4.2).

The default per-step concurrency is **4 workers**. When `parse_agent_output` emits 4+ `ToolCall` events via `ctx.send_event`, only 4 `call_tool` steps run concurrently; the rest queue. Worker slots are assigned deterministically (`[i for i in range(num_workers) if i not in used]`) for replay stability. Override per-step with `@step(num_workers=N)`.

The engine keeps a `scheduled_wakeups` min-heap of `(at_time, seq, tick)` for retry delays and timeouts — retries are scheduled ticks, not `asyncio.sleep` inside a worker, preserving determinism.

Lancelot byte-path: `TransportPool.fanout(concurrency=N)` is the outbound analogue. The `num_workers=4` is the in-process step-level parallel budget. Use 4 as the default for tool-call parallelism in the Lancelot agent layer.

### 9. `ctx.wait_for_event(event_type, waiter_event=None, timeout=2000)` — HITL pause primitive

Source: `context/context.py:526+` (§W4.3).

Default timeout: **2000 seconds** (not 2000ms). A step pauses execution with `result = await ctx.wait_for_event(HumanResponseEvent)` after emitting `InputRequiredEvent` via `waiter_event`. The run suspends; the `WorkflowHandler` surfaces the `InputRequiredEvent` to the caller; `handler.ctx.send_event(HumanResponseEvent(...))` resumes. The 2000s timeout is a production default — human-in-the-loop needs minutes, not milliseconds.

Lancelot byte-path: no equivalent. The Lancelot agent layer needs this as a first-class HITL primitive. The graceful drain and SIGTERM handling in the Rust server is the byte-path complement (in-flight requests are drained, not cut) — so the HTTP session stays alive while waiting for human input.

### 10. `StructuredRefineResponse(answer: str, query_satisfied: bool)` — noise-chunk filtering

Source: `response_synthesizers/refine.py:228-230` (§R2.10).

```python
class StructuredRefineResponse(BaseModel):
    answer: str
    query_satisfied: bool   # if False, current chunk doesn't help; keep previous answer
```

When `structured_answer_filtering=True`, the refine loop sends each retrieved chunk through a validation step: if `query_satisfied=False`, skip that chunk — it doesn't improve the answer. This prevents noise/irrelevant chunks from degrading a good answer through the refine loop. Incompatible with streaming (`ValueError` on init).

Lancelot byte-path: no equivalent; this is a RAG synthesis primitive. Relevant to any Lancelot tool that retrieves and synthesizes over multiple chunks.

---

## Constants canon (from `core/constants.py`)

```python
DEFAULT_MAX_ITERATIONS     = 20           # agent loop budget (base_agent.py:66)
DEFAULT_TEMPERATURE        = 0.1          # (constants.py)
DEFAULT_CHUNK_SIZE         = 1024         # tokens
DEFAULT_CHUNK_OVERLAP      = 20           # tokens (TokenTextSplitter)
SENTENCE_CHUNK_OVERLAP     = 200          # tokens (SentenceSplitter — 10× larger, deliberate)
DEFAULT_SIMILARITY_TOP_K   = 2            # retriever top-k (floor; most operators use 5-10)
DEFAULT_EMBED_BATCH_SIZE   = 10           # texts per embedding HTTP call
SENTENCE_WINDOW_SIZE       = 3            # ±3 sentences of context metadata per node
HIERARCHICAL_CHUNK_SIZES   = [2048, 512, 128]   # auto-merging tree levels (node_parser/relational/hierarchical.py)
SEMANTIC_BREAKPOINT_PCT    = 95           # cosine-distance percentile for SemanticSplitter
@step num_workers          = 4            # per-step concurrent invocations (decorators.py:99)
wait_for_event timeout     = 2000         # seconds for HITL pause (context/context.py:526)
@catch_error max_recoveries = 1           # per-lineage error recovery budget (decorators.py:256)
```

---

## Anti-patterns to avoid

### 1. Re-applying state injection every turn
Source: §R2.6. The `formatted_input_with_state` latch exists precisely because state injection rewrites the last user message. Applying it on every turn creates nested prompts: `Current state: {state}\n\nCurrent message: Current state: {state}\n\nCurrent message: ...`. One latch per run, reset at `_init_context`.

### 2. Committing the scratchpad to memory before `finalize()`
Source: §"scratchpad / memory split". Intermediate tool failures and partial reasoning paths write to the scratchpad, NOT to `memory`. Flushing before `finalize()` pollutes persistent memory with failed-turn artifacts. Keep the scratchpad separate and atomic.

### 3. Using `asyncio.sleep` inside a step for retries
Source: §W4.5. Retries must be scheduled as `TickRetry` into the min-heap. An `asyncio.sleep` inside a worker holds that worker's slot (blocking a fan-out concurrency unit), breaks replay determinism, and prevents the timeout from firing. Use the `retry_policy` protocol instead.

### 4. Emitting structured output from BOTH `output_cls` and `structured_output_fn`
Source: §R2.16 correction 2. Setting both makes `__init__` zero out `structured_output_fn`. The two paths are mutually exclusive; the `output_cls` path wins.

### 5. Registering a `tool_retriever` tool with `requires_context=True` in CodeAct
Source: §R2.4. CodeAct tools are exposed as plain Python function signatures in the system prompt — there is no injection point for a `Context` parameter. Raises `ValueError` at setup time. Don't use context-injected tools in CodeAct; use them in FunctionAgent/ReActAgent only.

### 6. Using per-chunk streaming with `structured_answer_filtering`
Source: §R2.10. The two features are incompatible: structured answer filtering requires a complete response per chunk to evaluate `query_satisfied`, which is incompatible with streaming that emits deltas. The `__init__` raises `ValueError("Streaming not supported with structured answer filtering.")`.

---

## Workflow-vs-loop assessment (the FOCUS question)

LlamaIndex's agent-as-workflow is **structurally superior to a hand-rolled while-loop** for the following reasons:

1. **Parallel tool calls are native.** `parse_agent_output` emits N `ToolCall` events; they run concurrently up to `num_workers=4`. A while-loop needs explicit `asyncio.gather` coupling. The event model decouples fan-out from fan-in, making partial-failure handling (one tool errors, others succeed) cleaner.

2. **The loop IS the durable seam.** The tick-based control loop's single-tick-buffer + single-await-point makes the entire run state snapshotable and replayable. A while-loop cannot provide this without bolting on a separate checkpoint system.

3. **Extension points are clean.** Any step can be overridden independently. An observability step that listens for every `AgentOutput` requires ZERO modification to the base agent — just add a new `@step` that accepts `AgentOutput`. A while-loop needs monkey-patching or callback hooks.

**Honest cost of the event model:**

- More infrastructure to build (event bus, Context, `collect_events`, WorkflowHandler, tick loop) — §W4.6 estimates ~1,540 lines for a faithful clone.
- `collect_events` semantics are subtle: returning `None` to "wait for more" is a framework contract, not a Python primitive. Easy to get wrong in a first implementation.
- The tick-based control loop adds ~1-2 µs overhead per event vs a direct `asyncio.gather` call — irrelevant for LLM-rate-limited workloads but worth noting for pure in-process compute.

**Recommendation for Lancelot:** implement a lightweight step-based loop (NOT the full workflow engine), borrowing:
- `ToolOutput(is_error=True)` exception capture (copy verbatim)
- `return_direct=True` flag on `ToolSpec`
- `collect_events`-style fan-in as a utility function (not a full engine)
- The scratchpad/finalize pattern for multi-tool turns
- The verbatim prompts

The full tick-based replay engine is valuable for durable state (checkpoint/resume/HITL) but is a large standalone build — scope it as a later milestone once the basic loop is validated under load.

---

## Byte-path implications summary

| Primitive | Lancelot byte-path status | Action |
|---|---|---|
| `ToolOutput.is_error` exception capture | Not in `lancelot.tool` | Add to agent-layer `call_tool` |
| `return_direct` tool flag | Not in `ToolSpec` | Add to `ToolSpec` in agent layer |
| `collect_events` fan-in join | `TransportPool.fanout` is the outbound analogue | Build as separate in-process utility |
| Scratchpad (per-turn buffer) | `lancelot.context` is request-scoped (shorter lifetime) | Agent-layer only; different from ContextVars |
| Verbatim prompts | Not applicable | Copy directly |
| Single `handoff` tool | `spawn_child` is outbound byte-path analogue | Agent-layer pattern only |
| `wait_for_event` HITL | No equivalent | New primitive; Rust graceful drain is the transport complement |
| ReAct JSON repair chain | `canonical.dumps_canonical` stabilizes outbound | Inbound repair is agent-layer only |
| `@step num_workers=4` | `TransportPool.fanout(concurrency=N)` outbound | Default 4 for in-process step concurrency |
| Tick-based replay engine | Not applicable | Scope as durable-state milestone |

---

## What to skip (hype / depreciating)

- **LlamaHub's 500+ tool packages** — the LlamaHub ecosystem is a commercial moat, not a primitive to copy. The `FunctionTool.from_defaults` pattern is what to take; the packages themselves churn.
- **LlamaCloud (LlamaParse Premium, LlamaExtract, LlamaCloud Index)** — SaaS product, not an open primitive.
- **The old `ChatMemoryBuffer` / `ChatSummaryMemoryBuffer` / `VectorMemory` APIs** — all deprecated. The current primitive is `Memory` with `BaseMemoryBlock` composites. Copy the block interface, not the deprecated classes.
- **Workflow visualization (`drawing.py`)** — useful for debugging, not a production primitive.
- **`AgentStreamStructuredOutput` dual-path** (§R2.16) — implementation detail of the structured-output layer; only relevant if you mirror the `output_cls` vs `structured_output_fn` split exactly.
- **`CodeActAgent`'s `<execute>...</execute>` XML extraction** — the code-execution agent variant is niche (sandbox required) and the XML parsing is fragile. FunctionAgent is the production path.
