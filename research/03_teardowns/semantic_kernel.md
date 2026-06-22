# Semantic Kernel — Mechanism-level distillation for Lancelot

Source: `SEMANTIC_KERNEL.md` (two-round RE, v1.8.0 + master v1.42.0)

---

## 1. Filter pipeline — the one durable primitive worth copying verbatim

Three filter types, each `async def filter(context, next): await next(context)`, composed via `construct_call_stack` into an express-style middleware chain:

```python
class FilterTypes(str, Enum):
    FUNCTION_INVOCATION    = "function_invocation"        # wraps any kernel function call
    PROMPT_RENDERING       = "prompt_rendering"           # wraps template render → string
    AUTO_FUNCTION_INVOCATION = "auto_function_invocation" # wraps LLM-initiated tool calls
```

Source: `filters/filter_types.py:12-14` (stable across v1.8 → v1.42 master).

`construct_call_stack(filter_type, inner_function)` returns a composed callable. Each filter receives a typed `context` object (not a raw dict) and a `next` callable. Post-`await next(context)` is post-hook territory: logging, mutation, audit, termination.

**What makes this durable:** it is the middleware pattern applied to LLM orchestration — not an LLM-specific trick. Survives model churn because it does not depend on any model's behaviour.

**Lancelot byte-path implication:** Lancelot already has ASGI middleware for HTTP. The filter pipeline is the AGENT-LEVEL analogue — tool-call interceptors, not HTTP interceptors. Lancelot has `@app.middleware("http")`; it needs `@agent.filter(FilterTypes.AUTO_FUNCTION_INVOCATION)`. These are different abstraction levels; one does not substitute for the other.

---

## 2. `context.terminate = True` — handoff-as-filter, not handoff-as-primitive

The core insight from SK's handoff design: **make handoff a kernel function whose auto-invocation filter sets `context.terminate = True`.** The connector's auto-invoke loop checks this flag after every tool call and breaks.

```python
async def _handoff_function_filter(self, context, next):
    await next(context)
    if context.function.plugin_name == HANDOFF_PLUGIN_NAME:
        context.terminate = True   # connector loop breaks here
```

Source: `agents/orchestration/handoffs.py:229-233`.

Downstream: `kernel.invoke_function_call` returns `invocation_context if invocation_context.terminate else None`. The connector loop checks the return and exits.
Source: `kernel.py:411`.

This means handoff logging, gating, and mutation all live in the same filter chain as every other tool interceptor. No special handoff primitive needed.

**The handoff functions themselves are zero-argument:**
```python
function_metadata = KernelFunctionMetadata(
    name=f"transfer_to_{handoff_agent_name}",
    description=handoff_description,
    parameters=[],          # zero args
    return_parameter=KernelParameterMetadata(name="return", type_="None", is_required=False),
    plugin_name=HANDOFF_PLUGIN_NAME,
)
```
Source: `agents/orchestration/handoffs.py:190-222`.

Every handoff agent also gets `complete_task(task_summary: str)` — the "I'm done" sentinel, structurally parallel to `UserInteraction-SendFinalAnswer` in the Stepwise Planner.

**Per-agent kernel clone** (line 175: `self._kernel = agent.kernel.clone()`) — handoff plugins are injected into a clone so they don't pollute the shared kernel. Cheap isolation.

---

## 3. Tool-invocation error recovery — exceptions as model-visible strings

`_inner_auto_function_invoke_handler` catches all exceptions from tool invocations and converts them to model-visible strings:

```python
async def _inner_auto_function_invoke_handler(self, context):
    try:
        result = await context.function.invoke(context.kernel, context.arguments)
        if result: context.function_result = result
    except Exception as exc:
        value = f"An error occurred while invoking the function {context.function.fully_qualified_name}: {exc}"
        context.function_result = FunctionResult(function=context.function.metadata, value=value)
        return
```
Source: `kernel.py:413-426`.

Three validation errors also become model-visible before execution even starts (malformed JSON args, function not found, missing required params):
```python
"The tool call arguments are malformed. Arguments must be in JSON format. Please try again."
"The tool call with name `{name}` is not part of the provided tools..."
```
Source: `kernel.py:321-411` (`invoke_function_call`).

This is the same pattern as LlamaIndex. The model gets actionable error text rather than a traceback; it can retry. **Durable primitive** — works regardless of which model is in play.

**Lancelot relevance:** Lancelot's `@app.tool` exposes tools over HTTP and MCP. The same pattern applies: tool exceptions at the agent-loop level (not HTTP level) should become model-visible strings, not 500s. Lancelot's byte path doesn't handle this; the agent layer must.

---

## 4. Stepwise Planner constants — copy the conservative defaults

Two prompts + one loop. The exact constants (source: `planners/function_calling_stepwise_planner_options.py:23-24`):

```python
max_iterations: int = 15
min_iteration_time_ms: int = 100    # explicit rate-limit sleep between steps
```

Generation prompt settings (`generate_plan.yaml`):
```yaml
temperature: 0.0
top_p: 0.0
max_tokens: 256
```
Maximally deterministic. 256 tokens forces a short plan (3-5 steps). This is a deliberate design choice — a verbose plan wastes tokens and drifts.

The sentinel termination pattern:
```python
USER_INTERACTION_SEND_FINAL_ANSWER = "UserInteraction-SendFinalAnswer"
STEPWISE_USER_MESSAGE = (
    "Perform the next step of the plan if there is more work to do. "
    "When you have reached a final answer, use the UserInteraction-SendFinalAnswer "
    "function to communicate this back to the user."
)
```
Source: `function_calling_stepwise_planner.py:44-50`.

The planner uses `FunctionChoiceBehavior.Auto(auto_invoke=False)` — the connector returns tool-call objects without executing them; the planner controls each step manually. This is the only way to inspect/log/gate each step.

**Honest assessment:** The Stepwise Planner pattern (generate explicit plan → step through) is **tactically valuable but churns on a ~1yr model clock.** Better models need fewer explicit steps; GPT-4o-class models with native function calling often outperform explicit stepwise planners. The durable part is the constants (`max_iterations`, `min_iteration_time_ms`, `max_tokens=256`) and the sentinel function pattern — those are worth copying. The two-stage plan generation is optional.

---

## 5. Magentic: `ProgressLedger` schema + stall detection — the durable loop-detection primitive

The Magentic orchestrator makes a structured LLM call every round to assess progress. The Pydantic schema (source: `magentic.py:84-99`):

```python
class ProgressLedgerItem:
    reason: str
    answer: str | bool

class ProgressLedger:
    is_request_satisfied: ProgressLedgerItem   # boolean
    is_in_loop: ProgressLedgerItem             # boolean
    is_progress_being_made: ProgressLedgerItem # boolean
    next_speaker: ProgressLedgerItem           # string (name from team)
    instruction_or_question: ProgressLedgerItem # string
```

The verbatim progress ledger prompt (`ORCHESTRATOR_PROGRESS_LEDGER_PROMPT`, source: `prompts/_magentic_prompts.py`):
```
To make progress on the request, please answer the following questions, including necessary reasoning:
    - Is the request fully satisfied? (True if complete...)
    - Are we in a loop where we are repeating the same requests and / or getting the same responses as before?
    - Are we making forward progress?
    - Who should speak next? (select from: {{$names}})
    - What instruction or question would you give this team member?

{
    "is_request_satisfied":     {"reason": string, "answer": boolean},
    "is_in_loop":               {"reason": string, "answer": boolean},
    "is_progress_being_made":   {"reason": string, "answer": boolean},
    "next_speaker":             {"reason": string, "answer": string},
    "instruction_or_question":  {"reason": string, "answer": string}
}
```

Stall/reset constants (source: `magentic.py:134-141`):
```python
max_stall_count: int = 3    # consecutive non-progress rounds → trigger reset
max_reset_count: int | None = None  # unlimited replans
max_round_count: int | None = None  # unlimited rounds
```

`MagenticContext.reset()` clears `chat_history` and increments `reset_count` but preserves `task` + `participant_descriptions` — goal preservation on replan.

**What is durable here:** the `(reason, answer)` struct on every ledger field. Logging `reason` at every round turns an otherwise opaque orchestration into a debuggable audit trail. This pattern is independent of model and framework. **Worth copying directly** into Lancelot's agent-level stuck-detection.

**What is hype:** the full Magentic two-stage plan + per-round orchestrator LLM call is expensive (O(rounds) orchestrator calls on top of the worker calls). Microsoft-scale engineering team; the complexity is justified at their scope but not for a lean framework's default. The stall-detection structure (3 booleans + reason strings) is the extractable primitive.

---

## 6. Multi-agent strategy pattern — selection + termination as separate pluggable policies

`AgentGroupChat` (source: `agents/group_chat/agent_group_chat.py`):

```python
for _ in range(termination_strategy.maximum_iterations):   # default 99
    selected = await selection_strategy.next(agents, history)
    async for msg in invoke_agent(selected):
        is_complete = await termination_strategy.should_terminate(selected, history)
        yield msg
    if is_complete: break
```

Selection strategies:
- `SequentialSelectionStrategy` — `self._index = (self._index + 1) % len(agents)` (round-robin).
- `KernelFunctionSelectionStrategy` — LLM-as-picker via template vars `_agent_` (comma-separated names) + `_history_` (serialized messages). Source: `agents/strategies/selection/kernel_function_selection_strategy.py:28-29`.

Termination strategies:
- `DefaultTerminationStrategy(maximum_iterations=1)` — one round then stop.
- `KernelFunctionTerminationStrategy` — LLM-as-judge.
- `AggregatorTerminationStrategy` — AND/OR compose multiple.

**Lancelot relevance:** these are the exact two axes Lancelot needs for multi-agent orchestration. Decouple "who speaks next" from "when to stop" and make both pluggable. The `maximum_iterations=99` default is suspiciously high (SK's own default is `maximum_iterations=1` via `DefaultTerminationStrategy`) — use 10-20 as a bounded safe default for Lancelot. Unlimited is a foot-gun.

---

## 7. Concurrent / Sequential orchestration message shapes

**Concurrent** (`concurrent.py:51-97`):
- Broadcast `ConcurrentRequestMessage(body)` to N agents.
- Collect via `CollectionActor` with `asyncio.Lock`. Fire result callback when `len(results) == expected_answer_count`.
- No deduplication or merging — caller gets the raw list.

**Sequential** (`sequential.py:205 LOC`):
- Each `SequentialAgentActor` knows its `next_agent_type`. Chain built at construction time.
- Output of A becomes body of B's `SequentialRequestMessage`. Last agent sends `SequentialResultMessage`.

These map directly onto `TransportPool.fanout(first_n=N)` (concurrent) and a chain of `spawn_child` calls (sequential). Lancelot's byte path already provides the transport substrate; it needs the message-routing and collection wrapper.

---

## 8. MCP connector — 4 transports, SamplingConsentCallback

Source: `connectors/mcp.py` (master).

```python
MCPStdioPlugin           # stdio transport
MCPSsePlugin             # SSE transport
MCPStreamableHttpPlugin  # streamable HTTP
MCPWebsocketPlugin       # WebSocket transport
```

**`SamplingConsentCallback`** — when an MCP server requests the host to perform LLM sampling (server-initiated LLM call), this callback returns whether to allow it. This is an explicit consent gate for "your AI is being controlled by an external server." No other framework in this batch has this as a first-class hook.

**Lancelot relevance:** `lancelot.mcp.mount` is a server-side MCP surface. The client-side MCP consumer (4 transport types, sampling consent gate) is a separate primitive. The `SamplingConsentCallback` pattern is worth noting as a security primitive for any future Lancelot client-side MCP consumer.

---

## 9. OpenAPI → kernel plugin — any REST API as agent tool

`OpenAPIPlugin.from_url(swagger_url)` — parses OpenAPI 3.0 spec, generates one `KernelFunction` per operation, parameters from request schema, auth from middleware. Source: `connectors/openapi_plugin/`.

This is the SK signature pattern: any documented REST API becomes callable by the LLM with one line. The generator is not RE'd to internals, but the pattern is:
1. Fetch and parse OpenAPI spec.
2. Per `paths[path][method]`: create a function with name = `operationId`, description = `summary`/`description`, parameters = request body schema + query params.
3. Execution: format and fire the HTTP request.

**Lancelot relevance:** Lancelot already has `@app.tool` (server-side). The OpenAPI-to-tool generator is a client-side pattern — auto-register external HTTP services as tools. Lancelot could provide `lancelot.tool.from_openapi(url)` using its existing `TransportPool` as the HTTP substrate. Non-trivial to build but would be genuinely useful if Lancelot has an agent loop that needs external API tools.

---

## 10. Anti-patterns — what NOT to copy

### Agent loop inside the connector (SK's biggest structural flaw)

The auto-invoke loop lives per-connector (`open_ai_chat_completion.py`, `anthropic_chat_completion.py`, etc.), not in a central place. Every provider implements its own loop with its own tool-schema translation. This makes the loop logic impossible to audit in one place and creates a combinatorial maintenance surface.

**For Lancelot:** put the agent loop in one place (the runner), pass the provider connector as a pure `async def call(messages, tools) → response` — not as something that executes tools internally.

### The Kernel registry as a god-object

`Kernel` holds plugins, services, filters, selector, and retry mechanism. Everything touching the LLM must be routed through the kernel. This creates tight coupling and makes testing any single component without the full kernel expensive.

**For Lancelot:** keep the agent loop decoupled from the tool registry. Tools should be registerable on the loop, not on a shared global kernel.

### Stepwise Planner as a default recommendation

The planner adds 1-2 LLM calls overhead (plan generation) before any work happens. For simple tasks this is pure waste. SK's own docs recommend ReAct-style (`FunctionChoiceBehavior.Auto`) over the Stepwise Planner for most use cases.

**For Lancelot:** don't make planning the default. Default to native function calling (let the model decide tool use). Expose explicit planning as an optional wrapper for users who need auditable step-by-step execution.

### `maximum_iterations=99` as a default

GroupChat's default is 99 iterations, which means runaway agents can burn thousands of tokens. `DefaultTerminationStrategy` defaults to 1 (which is correct), but `TerminationStrategy` (base class) defaults to 99.

**For Lancelot:** default `max_iterations` to 10 or 20. Make it explicit and loud. Users should opt into high limits.

### Actor runtime for simple use cases

The v1.42 actor runtime (`InProcessRuntime`, `TopicId`, `MessageContext`, etc.) is production-grade but heavyweight: three envelope types, `SubscriptionManager`, `RunContext` background task, `TraceHelper` spans. Total ~600 LOC for the in-process variant alone. For concurrent/sequential patterns, `asyncio.gather` + `asyncio.Queue` does 95% of the same work in 50 LOC.

**For Lancelot:** start with `asyncio.gather` and typed message passing. Don't build an actor runtime unless the user base needs remote agent distribution.

---

## Summary of extractable primitives (ranked by durability × applicability)

| Rank | Primitive | Why durable | Where to put it in Lancelot |
|------|-----------|-------------|----------------------------|
| 1 | Filter pipeline (3 types, express-style chain) | Not model-specific; survives model churn | `lancelot.agent.filters` |
| 2 | `context.terminate = True` handoff signal | Decouples trigger from effect; clean | Agent loop `invoke_function_call` |
| 3 | Exception → model-visible string (tool errors) | Universal recovery pattern | Inner tool handler |
| 4 | `(reason, answer)` struct on every loop-state field | Makes orchestration debuggable | `ProgressLedger` / stuck-detection |
| 5 | Stall detection: 3 booleans + `max_stall_count=3` | Cheap, stops runaway agents | Loop termination |
| 6 | `complete_task(task_summary: str)` sentinel | Every handoff chain needs a clean exit | Handoff plugin |
| 7 | Selection + termination as separate pluggable policies | Clean separation of concerns | Multi-agent runner |
| 8 | Zero-arg handoff functions | LLM just calls `transfer_to_X()` — unambiguous | Handoff plugin |
| 9 | `SamplingConsentCallback` on MCP | Security gate for server-initiated LLM calls | Future MCP client |
| 10 | `min_iteration_time_ms=100` in planner | Quiet rate-limit hygiene | Any step loop |

---

## Honest hype flags

- **Magentic orchestration** — Microsoft research paper implementation. 911 LOC. Impressive but 90% of the complexity is the ledger/replan cycle that general-purpose production agents rarely need. Extract the `ProgressLedger` schema; skip the full implementation.
- **Actor runtime** — over-engineered for the common single-process case. Worth knowing exists; not worth building for Lancelot v1.
- **Stepwise Planner** — depreciating on a ~1-2yr model clock. Modern models with native function calling do better on most tasks. Keep the sentinel pattern; skip the two-stage plan generation as a default.
- **OpenAPI plugin generator** — durable primitive but the implementation is ~1000 LOC and the primary value is "ease of tool registration." Lancelot already has `@app.tool`. The client-side generator is a v2 feature.
- **Cross-language SDK** — irrelevant for Lancelot (Python-only agent layer on Rust transport).
