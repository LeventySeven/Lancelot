# AutoGen + Microsoft Agent Framework — Mechanism Distillation

**Sources:** `researchfms/teardowns/AUTOGEN.md` (2 rounds, AutoGen v0.4 @ `027ecf0`) + `researchfms/teardowns/MS_AGENT_FRAMEWORK.md` (MAF `main` @ `2026-05-30`, Python core + `agent_framework_orchestrations`).

**Lens:** what is mechanism-level, load-bearing, and durable across model generations — not hype, not tactics that churn on a 1-2yr model clock.

---

## 1. The most durable primitive: the function-calling loop constants

### AutoGen: `max_tool_iterations=1` default (AUTOGEN.md §2.2, §2.16)

AutoGen's `AssistantAgent` has `max_tool_iterations=1` by default. **A single agent call does NOT loop on tool calls without explicit opt-in.** The design pushes multi-step looping to the GroupChat layer, not into a single agent's loop. With default settings, the agent does: one model call → execute tool calls in parallel via `asyncio.gather` → either `reflect_on_tool_use` (second LLM call) or `tool_call_summary_format="{result}"` (no LLM call). To get ReAct-style multi-step you set `max_tool_iterations=5` or higher.

**Lancelot implication:** This is the key constant to steal. The agent loop's DEFAULT should be conservatively bounded — one inference + parallel tool execution + summary, not an open-ended while-True. Budget opt-in (`max_iterations=N`) is the right interface.

### MAF: `DEFAULT_MAX_ITERATIONS=40`, `DEFAULT_MAX_CONSECUTIVE_ERRORS_PER_REQUEST=3` (MS_AGENT_FRAMEWORK.md §1.1)

MAF's `FunctionInvocationLayer` (`_tools.py:90`) caps at 40 LLM round-trips and 3 consecutive errors per request. On error threshold or `max_function_calls` hit: **force `tool_choice="none"` for a final turn** — this ensures pending `function_call_output` items are submitted before exit rather than abandoning mid-flight. The streaming loop has the exact same shape.

**Lancelot implication:** The "force a final non-tool turn on budget exhaustion" pattern is the right graceful exit. A Lancelot agent loop should have this: when the tool budget runs out, set `tool_choice=None` (or equivalent) and do one final model call to produce a text summary with all prior tool results in context, instead of hard-stopping.

Two orthogonal caps are load-bearing:
- `max_iterations` = LLM round-trips (default 40)
- `max_function_calls` = total individual tool invocations across iterations (optional, best-effort, checked post-batch)

---

## 2. Tool-call parallelism (AUTOGEN.md §2.2)

AutoGen executes all tool calls in one model turn via `asyncio.gather`:
```python
asyncio.gather(*[execute_tool_call(call, ...) for call in function_calls])
```
Results stream into a `asyncio.Queue` and are yielded as events. This is load-bearing: if you serialize tool calls (one await at a time), a 5-tool response takes 5× longer. The parallel execution pattern must be in the default path.

**Lancelot implication:** `TransportPool.fanout` is the byte-path analogue for parallel upstream LLM/tool calls. For local tool execution, native `asyncio.gather` is correct. The Lancelot agent loop should `asyncio.gather` all tool calls in one turn rather than awaiting sequentially.

---

## 3. Context compaction — the four variants + the non-obvious algorithms

### AutoGen `ChatCompletionContext` variants (AUTOGEN.md §2.14, §R2.4)

The four variants and their actual algorithms:

- **`BufferedChatCompletionContext(buffer_size=N)`**: `messages[-N:]`, then check if `messages[0]` is `FunctionExecutionResultMessage` (orphan from a sliced tool pair) and drop it. Critical: a naive slice without the orphan check causes provider rejection.

- **`HeadAndTailChatCompletionContext(head_size, tail_size)`**: keep first H + last T, insert `UserMessage(content=f"Skipped {num_skipped} messages.", source="System")` as a gap placeholder. Also strips orphan FunctionCall at the head boundary and orphan FunctionExecutionResultMessage at the tail start.

- **`TokenLimitedChatCompletionContext(model_client, token_limit)`** (`@experimental`): pop from **`messages[len(messages) // 2]`** (the MIDDLE), not the front. This preserves the system prompt (head) and most recent context (tail) and squeezes the middle. Re-count after each pop (expensive but correct). Drop orphan `FunctionExecutionResultMessage` after truncation.

- **`UnboundedChatCompletionContext`**: `return self._messages` — no compaction.

### MAF compaction (MS_AGENT_FRAMEWORK.md §5)

MAF's `CompactionStrategy` protocol (`_compaction.py`): strategies trigger when a metric exceeds `max_n` and trim to a target. Three types: `SelectiveToolCallCompactionStrategy`, `ToolResultCompactionStrategy`, and a summarizing strategy ("summarizes the oldest groups and retains the newest content") which tags summarized messages with `_summarized_by_summary_id`. Compaction is passed into the chat client as `compaction_strategy=` in the loop.

**Lancelot implication:** Context management is NOT already built — `lancelot.memory_pool` is byte-OOM, not message-window management. The correct build target is a `ContextWindow` primitive with:
1. `head+tail` variant with the orphan-check (most useful, no model calls)
2. `token_limited` with middle-pop (expensive, opt-in)
3. A gap placeholder message so the model knows messages were dropped
4. Tool-call pair integrity: never break a `(AssistantMessage[FunctionCall], FunctionExecutionResultMessage)` pair across a window boundary

---

## 4. MagenticOne: the orchestrator control loop (durable pattern) (AUTOGEN.md §2.12, MS_AGENT_FRAMEWORK.md §11.3)

This is the most architecturally novel multi-agent primitive in the corpus. It is NOT hype — it's a concrete, research-backed, productized pattern (two independent MS teams each reimplemented it, AutoGen v0.4 and MAF both carry it verbatim).

### What it does

The orchestrator LLM maintains two ledgers per task:
1. **Fact ledger** — categorizes known facts, facts to look up, facts to derive, educated guesses.
2. **Progress ledger** — per-turn JSON: `{is_request_satisfied, is_in_loop, is_progress_being_made, next_speaker, instruction_or_question}`.

**Constants (KNOWN):**
- `max_stalls = 3` — consecutive stalled turns before triggering a replan
- `max_rounds = 20` (AutoGen) / `max_round_count` (optional, MAF)
- On replan: update fact ledger + plan, reset `stall_count`, continue

### Why the progress ledger is durable

The progress ledger forces three self-checks per turn via structured JSON output:
1. `is_request_satisfied` — explicit completion check prevents indefinite loops
2. `is_in_loop` — explicit loop detection (checks if "repeating the same requests and/or getting the same responses")
3. `is_progress_being_made` — distinguishes "stuck" from "just started"

The `{reason: string, answer: boolean/string}` schema forces the model to articulate reasoning before asserting — this is a chain-of-thought pattern baked into the schema.

**Verbatim progress-ledger prompt** (both AutoGen `_prompts.py:1` and MAF `_magentic.py:202` carry identical text, modulo whitespace):
```
- Is the request fully satisfied? (True if complete, or False if the original request has yet to be SUCCESSFULLY and FULLY addressed)
- Are we in a loop where we are repeating the same requests and / or getting the same responses as before? Loops can span multiple turns...
- Are we making forward progress? (True if just starting, or recent messages are adding value. False if recent messages show evidence of being stuck in a loop...)
- Who should speak next? (select from: {names})
- What instruction or question would you give this team member? (Phrase as if speaking directly to them, and include any specific information they may need)
```

**Verbatim initial fact-survey prompt** (AUTOGEN.md §2.12, `_prompts.py`):
```
1. GIVEN OR VERIFIED FACTS
2. FACTS TO LOOK UP
3. FACTS TO DERIVE
4. EDUCATED GUESSES
```
("DO NOT include any other headings or sections. DO NOT list next steps or plans until asked to do so.")

**Lancelot implication:** The stuck-detect pattern (`is_in_loop`, `is_progress_being_made`, stall counter → replan) is the load-bearing novelty here. The exact prompt is effectively free to copy. A Lancelot agent harness that runs multi-step tasks should have a `StuckDetector` primitive — either as a structured field in the model output schema or as a metacognitive check after N consecutive non-text turns. The `stall_count >= max_stalls → replan` loop is durable across model generations.

---

## 5. Termination conditions as composable predicates (AUTOGEN.md §2.15)

AutoGen's termination conditions compose with `|` (OR) and `&` (AND):

```python
termination = MaxMessageTermination(10) | TextMentionTermination("DONE")
```

Full inventory (KNOWN, `conditions/_terminations.py`):
- `MaxMessageTermination(max_messages)` — turn count
- `TextMentionTermination(text)` — sentinel keyword
- `StopMessageTermination()` — typed `StopMessage` from an agent
- `FunctionalTermination(callable)` — custom predicate on the thread
- `TokenUsageTermination(max_prompt_tokens, max_completion_tokens)` — cost/budget cap
- `HandoffTermination(target)` — stop when handing off to a named external (e.g., "user")
- `SourceMatchTermination(sources)` — stop when a specific agent has spoken
- `ExternalTermination()` — programmatic `.set()` from outside
- `TimeoutTermination(timeout_seconds)` — wall-clock

**Lancelot implication:** `StopReason` in the lens already covers this conceptually. The composable `|` / `&` interface is worth replicating — each condition is a stateful predicate that accumulates the message thread, not a one-shot check. `TokenUsageTermination` is the cost-cap — it reads cumulative usage from the thread, which aligns with `lancelot.context.cost_usd` accumulation.

---

## 6. Handoff routing — typed peer-to-peer (AUTOGEN.md §2.5, §2.11)

AutoGen's Swarm pattern: each agent declares `handoffs=["bob", "carol"]`. The framework synthesizes a zero-argument tool `handoff_to_bob()`. When the model calls it, `_check_and_handle_handoff` extracts the target name, yields a `HandoffMessage(target="bob")`, and exits. The GroupChat manager routes the next turn to `HandoffMessage.target`.

```python
class Handoff(BaseModel):
    target: str
    description: str | None = None
    name: str = "handoff_to_<target>"
    message: str = "transferred to <target>, ..."
```

Swarm manager's `select_speaker`:
```python
for message in reversed(thread):
    if isinstance(message, HandoffMessage):
        return message.target
```

**Lancelot implication:** The handoff primitive is the Swarm replacement for a central LLM-based selector. It's cheaper (no extra LLM call for routing) and more predictable (agents declare their own routing logic). A Lancelot orchestration layer should support this — an agent can call a zero-arg `handoff_to_X` tool that the framework intercepts at the transport layer, and `TransportPool.spawn_child` is the byte-path primitive to reuse for the sub-agent invocation.

---

## 7. The actor model's load-bearing parts (AUTOGEN.md §R2.1, §R2.2, MS_AGENT_FRAMEWORK.md CE-2)

AutoGen's actor model is sophisticated in the addressing layer, not just the dispatch:

**`AgentId(type, key)`** — uniquely addresses an agent instance. Type regex: `^[\w\-\.]+\Z`. Wire format: `type/key`. Key insight: `TypeSubscription.map_to_agent(topic_id) = AgentId(type=agent_type, key=topic_id.source)` — the **topic's `source` field becomes the agent instance key**. Publishing to `(type="task", source="conversation-7")` automatically routes to agent instance `AgentId("worker", "conversation-7")`. Per-conversation agent instances fall out of topic addressing without explicit management.

**Intervention handlers** — run as middleware on every message envelope (send/publish/response). Can drop, mutate, or pass through. Used for rate limiting, security filters, deduplication. The `on_send`/`on_publish`/`on_response` hooks are the actor-level equivalent of MAF's three middleware layers.

**Distributed transport:** publishes are wire-encoded as CloudEvents v1.0 (`TopicId.type` regex: `^[\w\-\.\:\=]+\Z` — CloudEvents-compatible with colons/equals for namespaced types). This is the correct wire format for cross-language/cross-service agent message buses.

**Lancelot implication:** Lancelot does NOT need the full actor runtime (Tier-D — the distributed gRPC runtime adds massive complexity, and the single-threaded runtime is explicitly "not production-grade"). The **load-bearing concepts** worth taking: (a) agent identity as `type/key` tuple (not just a string name — the key dimension allows per-conversation/per-tenant isolation), (b) topic subscription's `source → key` mechanic for instance isolation, (c) intervention handlers as the security/rate-limit seam at the message level. These are 50-200 lines of code, not a 69K-line framework.

---

## 8. Pregel workflow = AutoGen GroupChat replacement (MS_AGENT_FRAMEWORK.md §2, §11)

MAF's Workflow is the clearest evidence that durable orchestration converges on the same primitive:

**Pregel superstep semantics:**
```python
while iteration < max_iterations (100):
    deliver all queued messages through edge runners (per-edge FIFO, cross-edge concurrent)
    run handlers
    state.commit()          # pending changes become visible only at boundary
    checkpoint()            # after every superstep
    if no pending messages: break  # convergence
raise WorkflowConvergenceException if not converged after 100 supersteps
```

**Checkpoint payload** (KNOWN, `_checkpoint.py:31-88`):
```
graph_signature_hash: str          # validates checkpoint belongs to THIS topology
checkpoint_id + previous_checkpoint_id
messages: dict[str, list]          # the in-flight message frontier
state: dict                        # committed shared state
pending_request_info_events: dict  # outstanding HITL gates (survive process restart)
iteration_count: int
```
File-backed: JSON envelope + base64-encoded `pickle` for messages/executor state.

**HITL gate:** `await ctx.request_info(Request(...), ResponseType)` — pauses the executor, emits a `request_info` WorkflowEvent carrying `request_id`/`request_type`/`response_type`, yields to the host (human/external system), resumes when the host submits a response keyed by `request_id`. The pending gate survives a process restart (stored in `pending_request_info_events`).

**Topology validation:** `graph_signature_hash` checked on checkpoint load — you cannot resume a checkpoint into a structurally different workflow. This prevents silent data corruption from graph evolution.

**Lancelot implication:** This is the durable seam design. Lancelot's "durable seam" (the lens's build target) should: (a) checkpoint at step boundaries (not mid-step), (b) include an in-flight message frontier so a resume can restart at the same superstep, (c) store `pending_request_info_events` so HITL gates survive process restart, (d) validate topology hash on resume. `lancelot.canonical.dumps_canonical` is directly useful here — the `graph_signature_hash` should be built from a canonically-serialized graph topology.

---

## 9. The `SocietyOfMindAgent` pattern (AUTOGEN.md §R2.5)

A single agent that internally runs an entire `Team`, then asks the model to synthesize the transcript into one response. Verbatim prompts (KNOWN, `_society_of_mind_agent.py:111-119`):
```
DEFAULT_INSTRUCTION = "Earlier you were asked to fulfill a request. You and your team worked diligently to address that request. Here is a transcript of that conversation:"
DEFAULT_RESPONSE_PROMPT = "Output a standalone response to the original request, without mentioning any of the intermediate discussion."
```

The synthesis call: `[SystemMessage(DEFAULT_INSTRUCTION), *inner_transcript_as_user_messages, SystemMessage(DEFAULT_RESPONSE_PROMPT)]` → one model call. Then `team.reset()`.

**Lancelot implication:** "Team as a single agent" is the composability primitive for nested orchestrations. It's cheap to implement on top of `on_messages_stream` — run the inner team, collect `TaskResult.messages`, construct the synthesis prompt, one final model call. No new infrastructure required.

---

## 10. Selector group chat: LLM-based routing (AUTOGEN.md §2.10)

The SelectorGroupChat's default selector prompt (KNOWN, `_selector_group_chat.py:607`, verbatim):
```
You are in a role play game. The following roles are available:
{roles}.
Read the following conversation. Then select the next role from {participants} to play. Only return the role.

{history}

Read the above conversation. Then select the next role from {participants} to play. Only return the role.
```

Configuration:
- `allow_repeated_speaker = False` — exclude previous speaker from candidates
- `max_selector_attempts = 3` — retry parsing on invalid name
- `selector_func: Callable` — bypass LLM if provided (custom routing)
- `candidate_func: Callable` — narrow the candidate pool per turn

For OpenAI: selector prompt sent as `SystemMessage`. For Anthropic/others: as `UserMessage` (KNOWN, line 241). This cross-provider difference is real and must be handled.

**Lancelot implication:** The selector prompt's "Only return the role" + retry-on-invalid-parse is the right minimal interface. The `candidate_func` reduction (filter which agents are eligible before calling the LLM) is a cost/correctness lever worth replicating — it prevents the LLM from routing to irrelevant specialists.

---

## 11. MAF middleware order (MS_AGENT_FRAMEWORK.md CE-4)

Three nested layers, outer→inner: `Agent → Chat → Function`.

- **Agent middleware** wraps the entire `agent.run()` call.
- **Chat middleware** wraps each model call (can mutate request options/messages or rewrite response — caching, redaction).
- **Function middleware** wraps each individual tool invocation (can short-circuit without calling the tool — approval flow, caching).

The approval flow uses function middleware short-circuit: approval middleware intercepts `FunctionInvocationContext`, emits a HITL request instead of executing the tool, returns a synthetic result.

**Lancelot implication:** The three-layer middleware model is load-bearing for cross-cutting concerns. Lancelot's ASGI `@app.middleware("http")` covers HTTP-level concerns. For agent-level concerns, the build target is a `FunctionMiddleware` hook — a callable that wraps each tool invocation. This is where tool-approval, tool-result caching, and tool-call audit logging live without touching agent logic.

---

## 12. Component serialization (AUTOGEN.md §R2.3)

Every AutoGen primitive implements `Component[Config]`:
```json
{
  "provider": "module.path.ClassName",
  "component_type": "agent",
  "version": 1,
  "config": { ... }
}
```
Secret placeholders: `"${OPENAI_API_KEY}"` in config, resolved from env at load time (never serialized). This powers AutoGen Studio's team-as-JSON API — an entire multi-agent team is one JSON file.

**Lancelot implication:** Agent configurations as serializable JSON is the right design for developer UX. A `lancelot.agent.AgentConfig` (Pydantic model, `model_dump()` → JSON, `model_validate()` → rehydrate) is a first-class primitive. The secret-placeholder convention (`${VAR}`) is worth copying verbatim.

---

## 13. What is hype / depreciating tactics

- **AutoGen's distributed gRPC runtime** — explicitly "not suitable for high-throughput or high-concurrency scenarios" (their own doc). Single-threaded asyncio queue. Use a real message broker for distributed agents in production.
- **AutoGen's maintenance mode** — v0.4 is feature-frozen. All new development from Microsoft is MAF. The actor model abstractions are durable; the specific AutoGen APIs are not.
- **MagenticOne's worker agents** (WebSurfer/FileSurfer/ComputerTerminal) — require Playwright + Docker. Heavy. Not a "spin up anywhere" primitive. The orchestrator PATTERN is durable; the specific worker implementations are not.
- **SK's old planners** (Handlebars, FunctionCalling stepwise) — gone from Python SDK. Superseded by native function-calling loop. Not relevant.
- **File-backed checkpoints as `pickle`** — fragile across Python version/class renames. Production durability requires a proper schema (Cosmos DB / Durable Functions backends exist but add infra).
- **`UserProxyAgent`** blocking on `input()` — explicitly called out as "puts the team in a blocked state." Use `HandoffTermination(target="user")` to stop the team and resume externally.

---

## 14. API shape notes worth stealing

From AUTOGEN.md and MS_AGENT_FRAMEWORK.md:

- **`CreateResult.finish_reason`**: `Literal["stop", "length", "function_calls", "content_filter", "unknown"]` — this is the right typed enum for the agent loop's stop-reason signal.
- **`ModelInfo`** dict: `{vision, function_calling, json_output, structured_output, family, multiple_system_messages}` — the capability matrix that drives feature gates. The `multiple_system_messages` flag is non-obvious (some providers reject more than one `SystemMessage`).
- **`CancellationToken`**: `is_cancelled()` / `cancel()` / `link_future(future)`. Every async API takes one. Cancellation propagates from the team stop → through all message contexts → through all LLM calls (checked between chunks) → through all tool calls. This is the right propagation model; `TransportPool`'s cancellation at the transport layer should be wired to this.
- **`ReplayChatCompletionClient`** — a `ChatCompletionClient` that replays a recorded sequence. Invaluable for deterministic agent tests without LLM calls. A Lancelot agent eval harness needs this.
- **`CacheChatCompletionClient`** — wraps any client with a cache layer. Pairs with `lancelot.canonical.dumps_canonical` for cache-key stability.
- **`tool_call_summary_format = "{result}"`** — the default format string. When `reflect_on_tool_use=False`, results are stringified with this pattern. The formatter is a `Callable[[FunctionCall, FunctionExecutionResult], str]` — per-call, composable, not a global hook.
