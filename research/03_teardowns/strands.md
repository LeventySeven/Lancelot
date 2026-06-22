# Strands Agents SDK (AWS) — Lancelot Distillation

> Source: `/Users/seventyleven/Desktop/researchfms/teardowns/STRANDS.md` (full read, 2026-06-22).
> Clone date of Strands SDK: 2026-05-30, `strands-py/src/strands`. All file:line cites are to that tree.
> Labels: **KNOWN** = confirmed in source code; **INFERRED**; **DESIGNED**.

---

## Thesis (what makes Strands worth mining)

Strands is AWS's "thin loop, fat model" design: the entire control plane is ~700 lines (`event_loop/event_loop.py`). No graph DSL, no scratchpad string — the loop IS the recursion; the model drives everything via `stop_reason`. The three primitives worth copying verbatim into Lancelot are: (1) the **model-driven recursive event loop** with exact budget constants, (2) the **stream normalizer** that turns a raw Converse/provider event stream into typed events and handles the pathological cases (blank text, coerce-on-`end_turn`-with-tool, mid-stream `max_tokens`), and (3) the **hook registry** as the clean extension spine for sessions/retries/telemetry without touching the loop.

---

## 1. The recursive event loop — verbatim structure

**`event_loop/event_loop.py`** (716 L, KNOWN).

### 1a. Budget constants (copy these exactly)

```python
# event_loop.py:54-56
MAX_ATTEMPTS = 6
INITIAL_DELAY = 4       # seconds
MAX_DELAY = 240         # seconds — 4 minutes

# backoff schedule (_retry.py:28): 4 → 8 → 16 → 32 → 64 s, then raise on attempt 6
# formula: delay = min(INITIAL_DELAY * 2**attempt, MAX_DELAY)
```

**Why these numbers matter:** 6 attempts with 4→240 s exp-backoff is calibrated for Bedrock's throttle behavior. The `MAX_DELAY=240` cap prevents indefinite holds when a provider is degraded. Copy these as the Lancelot default; expose as env overrides (`LANCELOT_AGENT_MAX_ATTEMPTS`, etc.) per the Lancelot env-knob convention.

### 1b. Budget cap priority (non-obvious)

```python
# event_loop.py:64 — simultaneous trip priority
# turns → total_tokens → output_tokens
```

`_check_limits` reads `agent.event_loop_metrics.latest_agent_invocation` (scoped to the **current** invocation — a reused agent doesn't carry forward prior invocation counts). Returns one of: `"limit_turns" | "limit_total_tokens" | "limit_output_tokens"` or `None`. Checked at the top of **every** cycle before any model call.

### 1c. The cycle — pseudocode (KNOWN, `event_loop.py:204-341`)

```
async event_loop_cycle(agent, invocation_state, structured_output_context, limits):
    limit_stop_reason = _check_limits(agent, limits)
    if limit_stop_reason:
        yield EventLoopStopEvent(limit_stop_reason, ...); return

    # RESUME PATH — skip model call if tool results are already pending
    if agent._interrupt_state.activated:
        stop_reason = "tool_use"; message = interrupt_state.context["tool_use_message"]
    elif _has_tool_use_in_latest_message(agent.messages):
        stop_reason = "tool_use"; message = agent.messages[-1]
    else:
        # MODEL PHASE — with throttle-retry
        events = _handle_model_execution(...)
        async for e in events:
            if not isinstance(e, ModelStopReason): yield e
        stop_reason, message, *_ = event["stop"]
        yield ModelMessageEvent(message=message)

    # DISPATCH
    if stop_reason == "max_tokens":
        raise MaxTokensReachedException(...)   # HARD FAIL — not recoverable in-loop

    if stop_reason == "tool_use":
        # run tools, append results as one user message, RECURSE
        yield from _handle_tool_execution(...)
        return

    # stop_reason == "end_turn"
    if structured_output_context.is_enabled and not force_attempted:
        # model ended turn without calling SO tool — force it
        structured_output_context.set_forced_mode()
        await agent._append_messages({"role":"user","content":[{"text": "You must format the previous response as structured output."}]})
        yield from recurse_event_loop(...)
        return

    yield EventLoopStopEvent(stop_reason, message, metrics, request_state)
```

**Key design choices to replicate:**
- `max_tokens` is a **hard raise** (`MaxTokensReachedException`), not a soft stop. Matches `end_turn` / other failure types for consistent error surface.
- Tool results are appended as a **single user message** with one `toolResult` content block per tool — not one message per tool. This is load-bearing for Bedrock's Converse format.
- Recursion via `recurse_event_loop` is trivial (re-enter `event_loop_cycle`). The depth = number of tool-use turns; budget caps + model `end_turn` terminate it.

### 1d. `_handle_tool_execution` + HITL interrupt shape

```python
# event_loop.py:555-716
validate_and_prepare_tools(message, tool_uses, tool_results, invalid_tool_use_ids)
# invalid → immediate error ToolResult for self-correction
# interrupts → save {tool_use_message, tool_results} to interrupt_state; stop loop
# on resume: model phase is SKIPPED; saved tool_use_message is reused
tool_result_message = {"role":"user","content":[{"toolResult": r} for r in tool_results]}
await agent._append_messages(tool_result_message)
recurse_event_loop(...)
```

HITL shape: tool raises an interrupt exception → loop saves `interrupt_state.context = {tool_use_message, tool_results}` → yields `EventLoopStopEvent("interrupt")` → on resume, model phase is skipped, saved message reused. **This is the cleanest HITL design in the corpus** — no separate state machine, no message reconstruction.

---

## 2. Stream normalizer — the load-bearing cases

**`event_loop/streaming.py`** (515 L, KNOWN). This is the state machine that turns a raw Bedrock Converse event stream into a typed `Message + StopReason + usage`. The pathological cases are what matter:

### 2a. Tool input streams as JSON fragments — string-append, parse at stop

```python
# streaming.py:224 — handle_content_block_delta, toolUse branch
state["current_tool_use"]["input"] += delta["toolUse"]["input"]
# (this is string concatenation of partial JSON, not a parsed object)

# streaming.py:292-302 — handle_content_block_stop, finalize
# json.loads(accumulated_input_string) → ToolUse{toolUseId, name, input}
```

Do NOT parse tool args incrementally. Accumulate as a string, parse once at `contentBlockStop`. Any framework that tries to do per-chunk JSON parsing will corrupt mid-stream tool calls.

### 2b. `max_tokens` during a tool call — partial message recovery

`event_loop/_recover_message_on_max_tokens_reached.py` handles the case where `max_tokens` truncates mid-tool-call (e.g. an incomplete `toolUse` block). It drops the incomplete block so the conversation stays valid even though the cycle raises `MaxTokensReachedException`. **Without this, a truncated tool call corrupts the message history.** Lancelot needs an equivalent when relaying streaming responses that can be truncated.

### 2c. `guardrail_intervened` as a `StopReason`

```
messageStop → stop_reason ∈ {end_turn, tool_use, max_tokens, stop_sequence, guardrail_intervened}
```

`guardrail_intervened` is a valid terminal `stop_reason` from Bedrock Converse (`streaming.py`, KNOWN). The loop does not special-case it — it falls through to `EventLoopStopEvent` like `end_turn`. Lancelot's loop should pass it through as a named stop reason rather than treating it as an error.

### 2d. `reasoningContent` — three sub-channels

Bedrock's reasoning (extended thinking) streams as a `reasoningContent` delta with three sub-types:
- `.text` → accumulate `reasoningText`, yield `ReasoningTextStreamEvent`
- `.signature` → accumulate `signature` (cryptographic proof)
- `.redactedContent` → guardrail-redacted reasoning

The `.signature` is attached to the content block at `handle_content_block_stop` alongside `reasoningText`. A stream normalizer that drops `.signature` will produce a content block the Converse API rejects on the next turn (it's needed for cache/verification). **Copy the three-channel handling or drop the block entirely — don't drop the signature silently.**

### 2e. `has_tool_use` check (resume guard)

```python
# streaming.py:137
has_tool_use(content) → bool
```

This is what `_has_tool_use_in_latest_message` reads to decide: "is the last message already a pending tool call I should resume from?" — as opposed to making a fresh model call. A normalizer without this check will re-invoke the model when it should be running tools, causing duplicate LLM calls.

### 2f. `is_callback_event` guard on empty deltas

```python
# types/_events.py:134-137  (ModelStreamEvent)
is_callback_event = len(event.get("delta","")) > 0
# or non-empty for other event types
```

Empty deltas (heartbeats, control frames) must NOT be forwarded to the callback/SSE client. Strands gates on `is_callback_event` before invoking the handler. **Without this, blank SSE lines can desync a streaming client.** This is the "strip blank text" normalizer rule.

---

## 3. Typed event surface (31 classes) — the callback contract

**`types/_events.py`** (KNOWN, `class TypedEvent(dict)` — events ARE their payload).

The load-bearing keys for `callback_handler`:

| Event class | dict keys to read |
|---|---|
| `TextStreamEvent` | `{"data": text, "delta": delta}` — `data` is the printable token |
| `ToolUseStreamEvent` | `{"type":"tool_use_stream", "delta", "current_tool_use"}` |
| `ReasoningTextStreamEvent` | `{"reasoningText", "delta", "reasoning": True}` |
| `EventLoopStopEvent` | `{"stop_reason", "message", "metrics", "request_state"}` |
| `AgentAsToolStreamEvent` | extends `ToolStreamEvent`, adds `agent_as_tool` ref |
| `ToolCancelEvent` | `{"tool_use", "message"}` |
| `StructuredOutputEvent` | carries parsed Pydantic instance |

`ModelStreamEvent.prepare()` merges `invocation_state` into the event when `delta` is present — this is how context propagates to the callback without a separate call.

The **31-class** surface is the full API surface. Lancelot needs a corresponding typed event class hierarchy to make streaming agent responses composable (the SSE client can key off event type rather than parsing content).

---

## 4. Hook registry — the extension spine

**`hooks/registry.py:146-340`** (KNOWN). The design pattern:

```python
# Registration
registry.add_callback(EventType, fn)         # one type
registry.add_callback(None, fn)              # all events
registry.add_callback([EventType, ...], fn)  # list of types

# Firing
registry.invoke_callbacks(event)             # → (event, interrupts)
# events with should_reverse_callbacks=True fire LIFO (teardown/After* events)
```

**15 hook event types** (`hooks/events.py`, KNOWN). Load-bearing ones:

| Event | What a hook can DO (mutable fields) |
|---|---|
| `BeforeToolCallEvent` | **swap `selected_tool`**, rewrite `tool_use.input`, **cancel the call** |
| `AfterToolCallEvent` | rewrite `result: ToolResult` |
| `AfterModelCallEvent` | set `stop_response` to request a retry |
| `MessageAddedEvent` | read-only; this is how `SessionManager` persists every message |
| `AfterInvocationEvent` | fires LIFO (reverse of After order = cleanup contract) |

**`BeforeToolCallEvent.cancel_tool`** is the clean way to implement: tool allowlists, sandboxing, rate limits, dry-run mode — without touching the loop. The hook fires before the tool runs; cancel returns a `ToolCancelEvent` to the model with a message.

`ModelRetryStrategy`, `SessionManager`, telemetry spans — all are `HookProvider`s. This means they compose without coupling:

```python
agent = Agent(
    model=...,
    hooks=[ModelRetryStrategy(), SessionManager(S3SessionManager(...)), my_tracer]
)
```

**For Lancelot**: the hook registry is the right seam for plugging in `lancelot.upstream_metrics` (fire on `AfterModelCallEvent`), `lancelot.context` cost accumulation, and any future safety/permit layer — without touching the loop.

---

## 5. Conversation managers — exact constants

**`agent/conversation_manager/`** (KNOWN).

```python
DEFAULT_COMPRESSION_THRESHOLD = 0.7       # conversation_manager.py:16
DEFAULT_CONTEXT_WINDOW_LIMIT = 200_000    # conversation_manager.py:17
```

**SlidingWindowConversationManager** (`sliding_window_conversation_manager.py`):
```python
window_size = 40                # default messages to keep
should_truncate_results = True  # oversized tool results are truncated, not dropped
```

**SummarizingConversationManager** (`summarizing_conversation_manager.py`):
```python
summary_ratio = 0.3             # fraction to summarize (clamp [0.1, 0.8])
preserve_recent_messages = 10   # always keep last 10
```

**Summarization prompt** (verbatim, `summarizing_conversation_manager.py:24`):
```
You are a conversation summarizer. Provide a concise summary of the conversation history.

Format Requirements:
- You MUST create a structured and concise summary in bullet-point format.
- You MUST NOT respond conversationally.
- You MUST NOT address the user directly.
- You MUST NOT comment on tool availability.

Assumptions:
- You MUST NOT assume tool executions failed unless otherwise stated.

Task:
Your task is to create a structured summary document:
- It MUST contain bullet points with key topics and questions covered
- It MUST contain bullet points for all significant tools executed and their results
- It MUST contain bullet points for any code or technical information shared
- It MUST contain a section of key insights gained
- It MUST format the summary in the third person
```

**Proactive compression**: when `proactive_compression=True`, the manager checks estimated token count against `DEFAULT_CONTEXT_WINDOW_LIMIT` (200k) on every `MessageAddedEvent` hook and calls `reduce_context()` proactively when usage > `threshold` (0.7) — i.e. compress at 140k tokens before hitting the 200k hard limit.

---

## 6. `@tool` — docstring → spec contract

**`tools/decorator.py`** (833 L, KNOWN). The exact extraction algorithm:

1. `docstring_parser.parse(func.__doc__)` — Google/Sphinx/Numpy docstring.
2. Per-param description from docstring Args section, fallback `f"Parameter {name}"`.
3. Pydantic `create_model(name, **field_definitions)` from type hints — the model's JSON Schema IS the `inputSchema`.
4. `Annotated[T, "desc"]` → description from annotation string; `Annotated[T, FieldInfo]` → constraints from FieldInfo. Priority: **Annotated-string > docstring-param-desc > `f"Parameter {name}"`**.
5. `context_param` — one named parameter (e.g. `tool_context`) is injected as `ToolContext(agent, tool_use, invocation_state)` at call time; excluded from the schema.
6. Final `tool_spec = {name, description, inputSchema: {json: <pydantic schema>}}`.

**For Lancelot**: `lancelot.tool` already does `@app.tool` → `ToolSpec` w/ auto JSON schema. The gap is the `context_param` injection (letting a tool reach the agent/session without polluting the schema) and the `docstring_parser` integration for description extraction. Both are worth porting.

---

## 7. Structured output — forced-tool mechanism

**Correct flow** (KNOWN, `tools/structured_output/_structured_output_context.py`, CE-5 correction):

1. Model is allowed to call the SO tool naturally on first pass.
2. If it ends turn without calling it: append `"You must format the previous response as structured output."` (the actual one-liner, NOT the long prompt in §4 of the teardown — that's the summarization prompt).
3. Force via `tool_choice = {"any": {}}` (Bedrock Converse "any tool" choice) — forces the model to call *some* tool.
4. If forcing was already attempted and it STILL didn't call the tool: raise `StructuredOutputException`.

**Why `{"any": {}}` not `{"tool": {"name": so_tool_name}}`**: forces any tool call, which works even with models that don't support named-tool forcing. More portable.

---

## 8. Multi-agent — Swarm constants + anti-ping-pong

**`multiagent/swarm.py:245`** (KNOWN):

```python
max_handoffs = 20
max_iterations = 20
execution_timeout = 900.0   # 15 min total
node_timeout = 300.0        # 5 min per node
repetitive_handoff_detection_window = 0    # 0 = disabled
repetitive_handoff_min_unique_agents = 0
```

**Anti-ping-pong**: if the last `repetitive_handoff_detection_window` handoffs used fewer than `repetitive_handoff_min_unique_agents` unique agents, stop. Disabled by default (window=0) but the mechanism exists. Detects A→B→A→B infinite loops.

**Graph** (`multiagent/graph.py`, 1296 L): conditional-edge DAG, topological execution, output of one node feeds the next, `max_node_executions` caps total runs. Entry nodes = nodes with no deps.

**Agents-as-tools**: `Agent.as_tool(name, description, preserve_context=...)` → `_AgentAsTool` — a full `AgentTool`. Sub-agent events bubble up as `AgentAsToolStreamEvent` carrying the `_AgentAsTool` reference for attribution. `preserve_context` controls whether sub-agent sees parent's conversation.

---

## 9. MCPClient — background-thread isolation model

**`tools/mcp/mcp_client.py`** (KNOWN):

```python
MCPClient(transport_callable: () → (read_stream, write_stream))
# transport_callable abstracts stdio / SSE / streamable-HTTP
```

The MCP `ClientSession` runs on a **dedicated background thread** (`_background_thread_session`). Sync methods marshal coroutines via `_invoke_on_background_thread(coro).result()` — this isolates the MCP session from the agent's own asyncio loop. **Without this isolation, MCP's async session and the agent's event loop contend on the same loop, causing intermittent deadlocks.**

`list_tools_sync(pagination_token)` → `PaginatedList[MCPAgentTool]` with `nextCursor` for full pagination. MCP tools merge into the agent's registry exactly like local `@tool`s.

**For Lancelot**: `lancelot.mcp.mount` is the MCP *server* side. The MCPClient pattern is the *client* side (consuming external MCP servers as agent tools). The background-thread isolation is the right pattern for the Lancelot agent layer's MCP client.

---

## 10. What to skip / honest assessments

**Bedrock Guardrails integration** (`bedrock.py:95-137`): `guardrail_id/version/trace/stream_processing_mode/redact_input/redact_output` — full first-class wiring. **Lancelot-specific**: Bedrock-only. The guardrail mechanism is not portable to Anthropic direct / OpenAI. Flag as depreciating if Lancelot targets multi-provider.

**Hot-reload via `watchdog`** (`tools/watcher.py`): `ToolWatcher` watches tool directories and reloads on file change. Interesting for dev DX but: (a) `watchdog` is a new hard dep, (b) it's a dev-only path. **SKIP** — violates Lancelot's pydantic-only runtime dep rule.

**`experimental/bidi/`** (voice/Nova Sonic): bidirectional audio streaming. **SKIP** — not relevant to the Lancelot byte-path positioning. Different problem domain.

**A2A protocol** (`multiagent/a2a/`): cross-framework interoperability. **Worth noting but not copying now** — it's a protocol spec client/server; the value depends on whether other frameworks adopt A2A broadly. Monitor as a future integration point once A2A stabilizes.

**SessionManager hook-driven durability**: elegant architecture, but Lancelot's durable seam is TBD (not yet in scope). The pattern (subscribe to `MessageAddedEvent` + `AfterInvocationEvent`, write incrementally) is the right design when Lancelot builds it.

---

## 11. Lancelot byte-path implications

| Strands primitive | Lancelot byte-path status |
|---|---|
| Throttle-retry backoff (`MAX_ATTEMPTS=6, 4→240s`) | `TransportPool` has exp-backoff + Retry-After honoring — **already better**; expose the constants as env knobs |
| `lancelot.upstream_metrics` error taxonomy | Already built (`ok/quota/timeout/permanent/transient/upstream_5xx`) — maps exactly to Strands' `ModelThrottledException` classification |
| Token extraction + cost accumulation | Already built (`tokens_in/out`, `cost_usd` ContextVar, `X-Lancelot-Cost-USD` header) |
| `traceparent` propagation | Already built on `TransportPool` — Strands has no equivalent (Bedrock-only OTel spans) |
| Stream normalizer (strip blank, coerce SO, handle max_tokens) | **NOT built** — Lancelot's `@app.stream` handles HTTP streaming, not LLM response normalization |
| Hook registry (15 event types, LIFO teardown, mutable selected_tool) | **NOT built** — the correct seam for plugging in upstream_metrics, context, safety |
| Budget caps (turns/total_tokens/output_tokens, current-invocation scoping) | **NOT built** — trivial to add given `event_loop_metrics` design |
| `context_param` injection in tools | **NOT built** — `@app.tool` doesn't inject agent context into tool functions |
| Conversation managers (SlidingWindow+Summarizing w/ exact constants) | **NOT built** |
| HITL interrupt (save `{tool_use_message, tool_results}`, skip model on resume) | **NOT built** |
| MCPClient background-thread isolation | **NOT built** (`lancelot.mcp.mount` is server-side only) |
| `dumps_canonical` for prompt-cache stability | Already built — Strands has no equivalent |
| `@app.batch` coalescing | Already built — Strands has no equivalent |
| Rust const tier, backpressure, TTFT metric | Already built — Strands has no equivalent |
