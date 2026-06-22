# LangChain — Mechanism-level teardown for Lancelot agent harvest

Source: `/Users/seventyleven/Desktop/researchfms/teardowns/LANGCHAIN.md`
Repo commit: `365c36c2` (2026-05-13) + round-2 deep pass at `e208f38` (2026-05-14).
All code blocks and constants marked KNOWN = verified from source.

---

## Executive verdict

LangChain is a **composition runtime + integration surface**, not an agent framework in the modern sense. The actual value is 700+ pre-built integrations all speaking the same `invoke/stream/batch` interface. The agent loop itself is trivially small (50 lines); the surrounding machinery (callbacks, tracing, config propagation, parsers) is 8,000 lines. The LCEL universal-Runnable model is the anti-pattern to refuse. The `init_chat_model` provider abstraction, the v1 middleware ordering, the tool error policy shape, and the `remaining_steps` self-limit are the primitives worth copying.

---

## 1. Anti-pattern to refuse: LCEL / universal-Runnable indirection

Source: §R2.1 (`runnables/base.py:619-638`, `coerce_to_runnable` lines 6489-6513).

`prompt | model | parser` is three `RunnableSequence` wraps. Every step fires a callback manager. The `**kwargs` only flows to step 0 (§R2.1, lines 3332-3335: `if i == 0: step.invoke(input_, config, **kwargs) else: step.invoke(input_, config)`). `RunnableParallel.invoke` spawns a `ThreadPoolExecutor` (§R2.3, `get_executor_for_config`). Cold path overhead: 10-50ms per hop (§10, honest-gap section).

The cost: every "what does this chain do" becomes "trace the Runnable graph". The benefit (universal composition) is wiped out by the fact that the interesting units (LLM call, tool call) are already well-typed. The 8,000-line runtime exists to track the graph — the tracing infrastructure IS the complexity, not incidental to it.

**Do NOT build a universal-Runnable interface for Lancelot.** Typed function → tool call → observation → loop is 50 lines. Any generic composition layer imposes the LangChain overhead tax for no user-visible win.

---

## 2. Provider abstraction shape — copy this

Source: §2.14, `langchain_classic/chat_models/__init__.py`, `factory.py:150`.

`init_chat_model(model="openai:gpt-4o", **kwargs)` — the string is `"<provider>:<model_name>"`. The function lazy-imports the partner package and returns a `BaseChatModel`. Provider dispatch is a plain `if/elif` chain (not a registry) over 20 providers: openai, anthropic, azure_openai, azure_ai, google_vertexai, google_genai, bedrock, bedrock_converse, cohere, fireworks, together, mistralai, huggingface, groq, ollama, deepseek, xai, perplexity, openrouter.

Fallback capability list (KNOWN, `factory.py:150`):
```python
FALLBACK_MODELS_WITH_STRUCTURED_OUTPUT = [
    "grok", "gpt-5", "gpt-4.1", "gpt-4o", "gpt-oss",
    "o3-pro", "o3-mini",
]
```
If a model's profile isn't registered, string-match against this list to assume native structured-output support.

Structured output routing (§2.8): three strategies depending on model capability:
1. `method="function_calling"` — bind a tool whose schema is the response schema, force `tool_choice` to that tool.
2. `method="json_schema"` — provider's native `response_format`.
3. `method="json_mode"` — JSON mode + post-hoc Pydantic validation.

**What to copy for Lancelot:** the `"<provider>:<model>"` string format and the lazy-import dispatch. `lancelot.llm.TransportPool` does transport; a thin `ProviderProfile` layer over it should do message-shape translation (system-message placement, tool-call format) using the same `provider:model` key. The `FALLBACK_MODELS_WITH_STRUCTURED_OUTPUT` list is a useful bootstrap for capability detection before a full profile registry is built.

---

## 3. v1 middleware ordering — copy the hook surface

Source: §2.5, `factory.py`, `middleware/types.py`.

The v1 `create_agent` wraps a LangGraph graph with 6 ordered hook points:

```
before_agent(state, runtime)           # once at START
  └── before_model(state, runtime)     # before each LLM call
        └── wrap_model_call(request, handler)   # surrounds the LLM call itself
              └── after_model(state, runtime)   # after each LLM call
                    └── wrap_tool_call(request, handler)  # surrounds each tool call
                          └── after_agent(state, runtime)  # once at END
```

`after_model` can set `state["jump_to"] = "tools" | "model" | "end"` to force routing. `wrap_model_call` can swap models, short-circuit, retry. All hooks have `a*` async variants.

Built-in middleware sizes (line counts from source):
- `SummarizationMiddleware` — 658 lines. Trigger: `max_tokens_before_summary` (default 4000 tokens). Action: invoke a cheaper `summary_model` with the rolling-summary prompt (see §5 below), replace older messages with a `SystemMessage` containing the summary.
- `HumanInTheLoopMiddleware` — 412 lines. Interrupts on `before_tool_call`.
- `ModelCallLimitMiddleware` — 267 lines.
- `ToolCallLimitMiddleware` — 488 lines.
- `ModelRetryMiddleware` — 312 lines.
- `ModelFallbackMiddleware` — 138 lines.
- `PIIDetectionMiddleware` — 376 lines.
- `ContextEditingMiddleware` — 298 lines.

**What to copy for Lancelot:** the 6-hook ordering is the right interface shape for Lancelot's agent safety/governance layer. `wrap_model_call` = where confidence routing, cost caps, and model fallback live. `wrap_tool_call` = where tool caching and permission gates live. Keep each hook as a plain `async def` taking `(state, runtime)` — no Runnable wrapping needed.

---

## 4. Tool error policy — copy the 4-shape enum

Source: §2.7, `tools/base.py:405`, §2.13.

`BaseTool` has two independent error switches, each with the same 4-shape type:

```python
handle_tool_error:       False | True | str | Callable[[ToolException], str]
handle_validation_error: False | True | str | Callable[[ValidationError], str]
```

Semantics:
- `False` (default) → exception propagates, kills the loop.
- `True` → `str(exception)` goes back to the model as a `ToolMessage` observation.
- `str` → that literal string goes back to the model.
- `callable` → `callable(exception)` result goes back to the model.

Tool-not-found: routes to `InvalidTool` which returns `f"{requested_tool_name} is not a valid tool, try one of {available_tool_names}."` as observation (§2.13). This is the correct behavior — the model self-corrects.

Validation: `args_schema.model_validate(tool_args)` on the incoming args BEFORE calling the tool. On `ValidationError`, the `handle_validation_error` policy applies (tool not invoked; error fed back).

`InjectedToolArg` pattern (§2.7, `FILTERED_ARGS`): args annotated with `InjectedToolArg`, `InjectedToolCallId`, `InjectedState`, `InjectedStore` are stripped from the schema sent to the model but wired in by the framework. This is how server-side context (user_id, session state) reaches a tool without leaking into the LLM's view of the tool schema.

**What to copy for Lancelot:** the 4-shape `handle_tool_error` type and the `InjectedToolArg` sentinel. Lancelot's `@app.tool` already auto-derives `output_schema`; it should also accept `handle_tool_error` with the same 4-shape semantics, and the `InjectedToolArg` annotation for framework-injected args (e.g. `request_id`, `tenant_id` from `lancelot.context`).

Default error template (KNOWN, `factory.py:109`): `"Error: {error}\n Please fix your mistakes."`

---

## 5. v0→v1 rewrite lesson — the four-year mistake

Source: §1, §2.1, §2.5, §2.9, §10.

LangChain shipped in 2022 with `AgentExecutor` (a `Chain` with a `while` loop), custom memory classes, and LCEL as the composition primitive. By 2024 (v0.3):
- `Chain`, `AgentExecutor`, all memory classes are `@deprecated(since="0.3.1", removal="2.0.0")`.
- v1 replaces them with a 200-line `StateGraph` builder over LangGraph's Pregel runtime.
- The new state model (`AgentState` TypedDict with `add_messages` reducer) is what `ConversationBufferMemory` should have been.

The lesson: every memory class (`ConversationBufferMemory`, `ConversationBufferWindowMemory`, `ConversationTokenBufferMemory`, `ConversationSummaryMemory`, `ConversationSummaryBufferMemory`, `ConversationEntityMemory`, `ConversationKGMemory`, `VectorStoreRetrieverMemory`, `CombinedMemory`) with 30+ chat-history backends is now dead. The correct primitive is a checkpointed state dict with a `messages` key and an `add_messages` reducer.

Memory-class proliferation cause (§2.9): each class was a different ad-hoc truncation strategy. The right abstraction was always "messages list + compaction hook" — LangChain discovered this four years in.

`ConversationEntityMemory` anti-pattern: extracts proper nouns per turn, calls a summarization LLM per entity per turn → N entities = N extra LLM calls per message. Never ship this default-on.

**What to NOT copy:** any memory class hierarchy. For Lancelot: a `messages: list` state key + a single `compaction(messages, threshold) -> messages` hook is the correct primitive. The `SummarizationMiddleware` (658 lines, triggered at 4000 tokens, using a cheaper `summary_model`) is the right implementation of that hook.

---

## 6. The `remaining_steps` self-limit — copy verbatim

Source: §2.6, `chat_agent_executor.py:620` (`_are_more_steps_needed`).

```python
def _are_more_steps_needed(state, response):
    has_tool_calls = isinstance(response, AIMessage) and response.tool_calls
    all_tools_return_direct = all(
        call["name"] in should_return_direct for call in response.tool_calls
    ) if has_tool_calls else False
    remaining_steps = _get_state_value(state, "remaining_steps", None)
    if remaining_steps is not None:
        if remaining_steps < 1 and all_tools_return_direct:
            return True
        elif remaining_steps < 2 and has_tool_calls:
            return True
    return False
```

If `remaining_steps < 2` and the model wants to call tools, eject with:
`"Sorry, need more steps to process this request."` (line 689).

This is graceful-not-crash termination. The key insight: check `remaining_steps < 2` (not `< 1`) because a tool call needs at minimum one more LLM turn after it. `remaining_steps < 1` covers the `return_direct` case where the tool result is the final answer.

Default `AgentExecutor.max_iterations = 15` (v0), LangGraph `recursion_limit = 25` (v1). The v1 number covers more because each "superstep" is coarser (model + tools = 1 iteration of the outer loop, but 2 LangGraph supersteps).

**What to copy for Lancelot:** the agent budget check must be `< 2` (not `< max`). Surface `remaining_steps` in the state so the model can self-limit. Graceful exit message is better than raising an exception that propagates to the user.

---

## 7. Output parser error path — the `OutputParserException.send_to_llm` flag

Source: §2.4, §R2.8, `agents/output_parsers/`, `output_parsers/base.py`.

`OutputParserException(text, observation=str, send_to_llm=bool)`:
- `send_to_llm=True`: the parser's exception message goes back to the model as the observation for the next turn (self-correction).
- `send_to_llm=False`: the canned `"Invalid or incomplete response"` goes back instead.

This is the correct abstraction: the parser decides whether its error is recoverable by the model or not. A structured-output parse failure where the model returned truncated JSON is recoverable. A completely-off-format response is not.

`OutputFixingParser` (§R2.8): wraps another parser, on `OutputParserException` feeds `{instructions}\n{completion}\n{error}\nPlease try again.` back through an LLM (often a cheaper one). `max_retries=1` default.

Format instructions injected by `JsonOutputParser` (KNOWN, `format_instructions.py`, §R2.8): explicitly says "do NOT wrap in code fences" — this is the current version. Older prompts said the opposite. The evolution shows that models learned to add markdown fences to JSON, and the prompt had to be updated to fight that.

**What to copy for Lancelot:** the `send_to_llm` flag on tool/parse errors. For Lancelot's `@app.tool`, the error policy callable receives the exception and returns either `ToolMessage(content=str(e))` (send to model) or raises (propagate). The flag is the clean split between those two.

---

## 8. `InjectedToolArg` / `FILTERED_ARGS` — server-side context without schema pollution

Source: §2.7, `tools/base.py:74`.

```python
FILTERED_ARGS = ("run_manager", "callbacks")
```

Args annotated with `InjectedToolArg`, `InjectedToolCallId`, `InjectedState`, `InjectedStore` are excluded from `tool_call_schema` (sent to model) but injected by the framework at call time.

The `_is_injected_arg_type` check in `tool_call_schema` property (§2.7, line 586):
```python
for name, type_ in get_all_basemodel_annotations(full_schema).items():
    if not _is_injected_arg_type(type_):
        fields.append(name)
```

**What to copy for Lancelot:** Lancelot's `@app.tool` schema derivation should support the same annotation. `InjectedToolArg` or equivalent lets tools receive `lancelot.context.request_id`, `tenant_id`, etc. without those appearing in the JSON schema sent to the model. Currently `@app.tool` auto-derives `output_schema` but there is no mechanism to inject server-side context args.

---

## 9. Scratchpad to messages conversion — the dedup trick

Source: §R2.11, `agents/format_scratchpad/tools.py`.

When converting `intermediate_steps` to messages for the next LLM call:
```python
messages.extend([new for new in new_messages if new not in messages])
```

`BaseMessage.__eq__` compares content + id, so if one model turn produced N tool calls, the single `AIMessage` that contains all N `tool_calls` is only appended once even though it appears in N `(AgentAction, observation)` pairs.

**What to copy:** any scratchpad→messages conversion must dedup by id. Without this, a model turn with 3 tool calls generates an `AIMessage` with 3 tool_calls entries, then the converter naively produces 3 copies of that same message → the model sees it 3 times → context explosion.

---

## 10. Streaming event schema — the v2 event format

Source: §2.12, §R2.10, `tracers/event_stream.py:1100 lines`.

`astream_events(version="v2")` event shape:
```python
{"event": str, "name": str, "run_id": str, "parent_ids": [...], "tags": [...], "metadata": {...}, "data": {...}}
```

Critical event types for an SSE-based agent UI:
- `on_chat_model_stream`: `data["chunk"]` is the delta content — wire this to SSE.
- `on_tool_start`: `data["input"]` is the tool args — surface to HITL.
- `on_tool_end`: `data["output"]` is the ToolMessage — include in trace.
- `on_chat_model_end`: `data["output"]` is the full AIMessage with `tool_calls` — use for routing.

The `dotted_order` trace field (§R2.10, `tracers/schemas.py`): a sortable string like `20260515T123456789Z<root>.20260515T123456790Z<child>` that encodes both temporal order and parent chain. Lets an event stream reconstruct a correct trace tree even if events arrive out of order.

**Lancelot byte-path implication:** `lancelot.stream` + `lancelot.http.stream.ttft` already handle the byte-level SSE output. The agent layer needs to emit typed events (not raw tokens) that Lancelot's SSE pump carries. The `on_tool_start`/`on_tool_end` events are the natural HITL interrupt points to wire to Lancelot's `lancelot.webhook` or a long-poll endpoint.

---

## 11. `RunnableConfig` context propagation — the ContextVar trick

Source: §R2.2, `runnables/config.py:49-163`.

```python
var_child_runnable_config: ContextVar[RunnableConfig | None] = ContextVar(
    "child_runnable_config", default=None
)
```

`ensure_config()` (called at the top of every `invoke`) first reads `var_child_runnable_config.get()` to inherit the parent's tags/metadata/callbacks, then merges the caller-provided config. The result: a deeply nested `chain.invoke(x)` call can see the root's callbacks without the user explicitly passing `config=` at every level.

`COPIABLE_KEYS = ["tags", "metadata", "callbacks", "configurable"]` — these get `.copy()` on inherit so mutations in a child don't propagate back up.

`CONFIGURABLE_TO_TRACING_METADATA_EXCLUDED_KEYS = frozenset(("api_key",))` — secrets are NOT auto-promoted into trace metadata.

**Lancelot byte-path implication:** `lancelot.context` ContextVars (`request_id`, `tenant_id`, `cost_usd`, `agent_id`, `trace_id`) already do this. No new mechanism needed. The agent layer should push `agent_id` into the context at loop entry and read `cost_usd` at exit — this is already the primitive. The LangChain insight that validates the design: ContextVar inheritance is the right model; explicit passing is the wrong model.

---

## 12. Rolling summary prompt (verbatim)

Source: §2.9, `langchain_classic/memory/prompt.py` (SUMMARY_PROMPT section).

```
Progressively summarize the lines of conversation provided, adding onto the previous summary returning a new summary.

EXAMPLE
Current summary:
The human asks what the AI thinks of artificial intelligence. The AI thinks artificial intelligence is a force for good.

New lines of conversation:
Human: Why do you think artificial intelligence is a force for good?
AI: Because artificial intelligence will help humans reach their full potential.

New summary:
The human asks what the AI thinks of artificial intelligence. The AI thinks artificial intelligence is a force for good because it will help humans reach their full potential.
END OF EXAMPLE

Current summary:
{summary}

New lines of conversation:
{new_lines}

New summary:
```

`SummarizationMiddleware` trigger: 4000 tokens in the message list. Uses a separate `summary_model` (cheaper than the main model). The summary replaces older messages as a `SystemMessage`.

---

## 13. Key constants

Source: §2.11 + §R2.15.

| Constant | Value | File |
|---|---|---|
| `AgentExecutor.max_iterations` | `15` | `agent.py:1023` |
| LangGraph `recursion_limit` | `25` | `runnables/config.py:163` |
| `early_stopping_method` | `"force"` | `agent.py:1032` |
| Stopped response | `"Agent stopped due to iteration limit or time limit."` | `agent.py:132` |
| Out-of-steps response | `"Sorry, need more steps to process this request."` | `chat_agent_executor.py:689` |
| Tool error template | `"Error: {error}\n Please fix your mistakes."` | `factory.py:109` |
| RRF constant `c` | `60` | `retrievers/ensemble.py` (Cormack-Buettcher SIGIR'09) |
| `OutputFixingParser.max_retries` | `1` | `output_parsers/fix.py` |
| Summary trigger | `4000` tokens | `SummarizationMiddleware` |
| `MessagesPlaceholder.optional` | `False` | `prompts/chat.py` |
| `KG_TRIPLE_DELIMITER` | `"<|>"` | `memory/prompt.py:112` |
| `FILTERED_ARGS` | `("run_manager", "callbacks")` | `tools/base.py:74` |

---

## 14. What LangChain gets wrong (honest gap section)

Source: §10 (gaps), §2.13 (error policies).

- **10-50ms cold path overhead per Runnable hop** (§10) — the callback manager walk is the cost. Every `invoke` fires `on_chain_start`/`on_chain_end`.
- **Mixed sync/async**: `_arun` defaults to `run_in_executor(None, self._run, ...)` (§2.7). Tool calls in an async agent land in thread-pool threads, wasting the event loop.
- **Pydantic v1+v2 dual support throughout** (`is_pydantic_v1_subclass` / `is_pydantic_v2_subclass` branches in `tools/base.py`) — permanent source of subtle bugs. Lancelot requires pydantic>=2.7 only; never carry this.
- **`ConversationEntityMemory` N-LLM-calls-per-message** — N entities extracted → N summarization LLM calls per user turn. The cost cliff is invisible to users.
- **Default system prompts leak provider identity** (§R2.13): `_DEFAULT_ENTITY_MEMORY_CONVERSATION_TEMPLATE` says "powered by a large language model trained by OpenAI" — this goes to Anthropic/Mistral/Groq models unchanged. Never ship provider-specific identity in default prompts.
- **LCEL `**kwargs` only flows to the first step** (§R2.1) — non-obvious, source of subtle bugs when users try to pass `stop=["END"]` through a chain.

---

## Byte-path implications summary

| Primitive | Lancelot status |
|---|---|
| `"<provider>:<model>"` lazy dispatch | NOT BUILT — `TransportPool` does transport; no message-shape translation layer yet |
| `handle_tool_error` 4-shape enum | NOT BUILT — `@app.tool` has no error-policy hook |
| `InjectedToolArg` (schema filtering) | NOT BUILT — no mechanism to inject server context into tools without schema pollution |
| `remaining_steps` self-limit | NOT BUILT — agent loop not yet built |
| `before_model` / `wrap_tool_call` hooks | NOT BUILT — middleware ordering not yet defined |
| ContextVar config propagation | ALREADY BUILT — `lancelot.context` ContextVars do this |
| SSE / stream event emission | ALREADY BUILT — `lancelot.stream` + TTFT metric |
| Trace / span propagation | ALREADY BUILT — `traceparent` + `trace_id` ContextVar + `TransportPool` forwarding |
| Rolling summary at token threshold | NOT BUILT — but `SummarizationMiddleware` pattern is the right shape |
| Cost accumulation in context | ALREADY BUILT — `lancelot.context.cost_usd` ContextVar + `X-Lancelot-Cost-USD` trailer |
| Tool JSON schema auto-derivation | ALREADY BUILT — `@app.tool` does this |
| Retry w/ exponential backoff | ALREADY BUILT — `TransportPool` has retry + Retry-After honoring |
