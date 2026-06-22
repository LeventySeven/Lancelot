# Letta (MemGPT) — mechanism-level distillation for Lancelot

**Source:** `~/Desktop/researchfms/teardowns/LETTA.md` (Tier-1A, repo cloned HEAD `1131535`, 2026-05-30 deep re-read)
**What it is:** MemGPT paper (Packer et al., arXiv:2310.08560, UC Berkeley 2023) productionized into a multi-tenant agent platform. The singular primitive: the LLM edits its own system prompt via tool calls, while recall (full message history search) and archival (vector store) memory tiers expand its effective context beyond the context window.

---

## The thesis (what Letta owns that nobody else does)

Letta's product is not the loop. It is **virtual context management**: the agent's context window is "main memory" (small, fast, in-prompt); recall + archival are "disk" (larger, slower, fetched via tools). The agent navigates its own memory the way a developer navigates code. Three tiers:

```
core memory       100K-char labeled blocks, IN prompt, agent-editable via tools
recall memory     full message DB, agent-searchable (hybrid text+vector)
archival memory   vector store of passages, agent-searchable, truly unbounded
files             open/grep/semantic-search over attached documents
```

Every write to every tier is the agent's explicit choice — via tool call. Nothing is automatic.

---

## Primitives worth copying (mechanism-level)

### 1. `Block` — labeled system-prompt segments, agent-editable as tools

**Mechanism:** `schemas/block.py:13`, `CORE_MEMORY_BLOCK_CHAR_LIMIT = 100000` chars (`constants.py:435`). Each block has `label: str` (e.g. `"human"`, `"persona"`), `value: str` (the content), `limit: int`, `description: str` ("how this block influences behavior"), `read_only: bool`. Blocks are first-class DB rows. The compiled system prompt contains `<label description="...">\n{value}\n</label>` for each block in order. `Memory.compile_to_string()` (`schemas/memory.py`) produces this from the block set.

**The `{CORE_MEMORY}` placeholder** (`IN_CONTEXT_MEMORY_KEYWORD`): the system prompt template has a single `{CORE_MEMORY}` token; at every turn `compile_system_message` swaps it for the rendered block XML + a `<memory_metadata>` page-table block (`services/helpers/agent_manager_helper.py:310`):

```
<memory_metadata>
- AGENT_ID: {agent_id}
- System prompt last recompiled: {timestamp in agent timezone}
- {N} previous messages between you and the user are stored in recall memory
- {M} total memories you created are stored in archival memory (use tools to access them)
- Available archival memory tags: {comma-joined tags}
</memory_metadata>
```

This tells the agent how much context is *outside* its window. It is the literal page-table: "you have N messages and M facts you can't see; use your tools." Rebuilt on every turn when blocks change.

**Lancelot implication:** Lancelot's `lancelot.context` ContextVars carry scalar values (`cost_usd`, `tokens_in/out`, etc.) but have no structured "named writable segment" primitive. `Block` is the durable-memory half of the agent seam. The `{CORE_MEMORY}` swap pattern is trivial to implement on top of a prompt templating step — just str-replace before handing to TransportPool. **Copy the data model and the `<memory_metadata>` page-table; skip the Postgres ORM.**

### 2. Memory edit tools: `str_replace`-exact, `memory_rethink`, and the omni-tool

**Mechanism:** `function_sets/base.py:263,283,311,10`. Three design generations:

- `core_memory_replace(label, old_content, new_content)`: raises `ValueError` if `old_content not in current_value` (no silent overwrite). Simple string replace. (`base.py:263`)
- `memory_replace(label, old_string, new_string)` (v2, `base.py:311`): stricter — copied from Anthropic's computer-use-demo `edit.py:154`; requires `old_string` to match *exactly once* including whitespace.
- `memory_insert(label, insert_line, insert_text)`: insert at line number; lines use prefix regex `MEMORY_TOOLS_LINE_NUMBER_PREFIX_REGEX = r"^[ \t]*\d+→[ \t]*"` (`constants.py:161`).
- `memory_rethink(new_memory, target_block_label)`: "rewrite the entire block integrating new information; result should be organized, readable, comprehensive." This is active consolidation — the agent distills its own block (`base.py:283`).
- `memory(command="create|str_replace|insert|delete|rename", ...)` (v3, `base.py:10`): the omni-tool. Anthropic models (Sonnet 4.x) prefer one well-designed multi-command tool over many small ones; older models worked better with separate tools. The split sets are kept for backwards compat (`constants.py:129`: `BASE_MEMORY_TOOLS_V3 = ["memory"]` for Anthropic).

**Lancelot implication:** the `str_replace`-exact error (`old_content not in current_value → ValueError`) is the key safety property — it prevents silent overwrite bugs. The omni-tool collapse is a model ergonomics insight: one `command` parameter reduces model confusion over "which tool to call." Worth copying for any tool set that has multiple related write operations.

### 3. `_decide_continuation` — the V3 loop FSM (not heartbeats)

**Mechanism (CORRECTION):** `letta_agent_v3.py:1987-2036`. LettaAgentV3 **removed** model-controlled heartbeats — in V3, `request_heartbeat` is hardcoded `False` (`:2071`). The loop now continues by default after any tool call; it stops when the model returns content with no tool call. The continuation state machine:

```
tool_call_name is None (no tool) →
    if uncalled required tools + not final_step → continue ("You must call X before exiting")
    if finish_reason=="length" → stop: max_tokens_exceeded
    else → stop: end_turn

tool_call_name is set (tool called) →
    if terminal tool → stop: tool_rule
    if has child tools → continue
    if is continue tool → continue
    if is_final_step → stop: max_steps
    if uncalled required tools → continue ("Continuing, user expects these tools: [...]")
    else → continue by default
```

Continuation reasons are injected as `NON_USER_MSG_PREFIX` coach messages: `"[This is an automated system message hidden from the user] Continuing: child tool rule."` — the model is *told why* it is being looped, so it can reason about the constraint.

**Stop reasons (`schemas/letta_stop_reason.py`):** `end_turn`, `error`, `insufficient_credits`, `invalid_tool_call`, `max_steps`, `tool_rule_violation`, `user_cancellation`, `cancelled`, `context_window_exceeded`, `rate_limit_exceeded`, `llm_api_error` — 11+ named reasons; machine-readable for callers.

**`DEFAULT_MAX_STEPS = 50`** (`constants.py:75`).

**Lancelot implication:** Lancelot needs an agent loop. The V3 FSM is the right model: continue-by-default-after-tool, stop on no-tool (unless required tools unmet), explicit stop reasons as a typed enum. **`NON_USER_MSG_PREFIX` coach injection for forced continuation is a direct copy.** The V2 heartbeat boolean (`request_heartbeat: bool` appended to every tool schema, default False, the agent sets True to self-chain) is worth keeping as a fallback for non-V3-style agents and for tool-schema generation.

### 4. Tool-rule constraint solver — per-step allowed-set intersection

**Mechanism:** `helpers/tool_rule_solver.py:96-170`. `ToolRulesSolver` holds five lists: `init_tool_rules`, `continue_tool_rules`, `child_based_tool_rules`, `terminal_tool_rules`, `requires_approval_tool_rules`. Per step it computes `get_allowed_tool_names(available_tools)`:

```python
if not tool_call_history and init_tool_rules:
    allowed = [r.tool_name for r in init_tool_rules]   # first step: only Init tools
else:
    valid_sets = [rule.get_valid_tools(history, available, last_resp)
                  for rule in child_based + parent rules]
    final = set.intersection(*valid_sets) & available_tools
    if not final: raise ValueError("No valid tools found based on tool rules.")
    allowed = list(final)
```

Rules: `InitToolRule` (must call X first), `TerminalToolRule` (calling X ends loop), `ContinueToolRule` (must call X after Y), `ChildToolRule` (calling P unlocks children), `MaxCountPerStepToolRule` (rate-limit per step), `RequiresApprovalToolRule` (pause → HITL). The solver also **prefills args**: `InitToolRule.args` at step 0, `ChildToolRule` arg-maps when parent just called (`:152-156`) — pinning argument values at the rule level.

Parallel tool calls are disabled whenever any non-approval rule exists (`:1113-1126` in `letta_agent_v3.py`): `"parallel tool calls disabled because tool rules attached"`.

**Lancelot implication:** the FSM over tool rules is a first-class primitive for building constrained agents (structured interviews, multi-step forms, approval workflows). The InitToolRule + TerminalToolRule pair is the minimal useful subset. **Copy the intersection logic; the arg-prefill is a neat bonus for forced-tool patterns.**

### 5. Shared memory blocks — one block, N agents, force-recompile on write

**Mechanism:** `services/block_manager.py:61-68`. Blocks are first-class DB rows in `blocks_agents` junction. When any agent writes to a shared block via `core_memory_replace`:
```python
agent_ids = await self.get_agent_ids_for_block_async(block_id, actor)
for agent_id in agent_ids:
    await self.agent_manager.rebuild_system_prompt_async(agent_id, actor, force=True, update_timestamp=False)
```
One write propagates to all connected agents' system prompts. This is multi-agent shared state without a message bus — just a block. `update_timestamp=False` prevents recompile-loop storms.

`blocks_conversations` junction allows **conversation-scoped block overrides** — the same agent can maintain different core memory per conversation thread (`apply_isolated_blocks_to_agent_state`, `letta_agent_v3.py:264`).

**Lancelot implication:** not directly needed for Lancelot's current scope. The pattern is interesting for multi-agent scenarios but mandates a DB and a propagation cascade. A simpler first-class version for Lancelot: store blocks in the ContextVar / request-scope, share across an agent group by passing block content explicitly. Skip the ORM propagation machinery.

### 6. Sleep-time agents — async post-conversation memory consolidation

**Mechanism:** `groups/sleeptime_multi_agent_v4.py:132-157`. After each main agent run, fire background sleeptime agents:
```python
async def run_sleeptime_agents(self):
    if turns_counter % group.sleeptime_agent_frequency == 0:
        for sleeptime_agent_id in group.agent_ids:
            sleeptime_run_id = await self._issue_background_task(...)  # non-blocking
```
`sleeptime_agent_frequency` controls every-N-turns triggering (`schemas/group.py:43`). Each sleeptime agent has only consolidation tools (`BASE_SLEEPTIME_TOOLS = ["memory_replace","memory_insert","memory_rethink","memory_finish_edits"]`). It reads the recent conversation, rewrites blocks, archives stale info, then calls `memory_finish_edits` to terminate. Dispatched as a background task so the user-facing response is never blocked.

The streaming variant (`:98-129`) fires sleeptime agents *after the stream completes* to guarantee it doesn't delay the response.

**Lancelot implication:** the pattern is "background task triggered at N-turn frequency, tools restricted to memory-only, run non-blocking after the main response." Lancelot could implement this as `@app.background` hook or via `asyncio.create_task` post-response. The frequency-gate (`turns_counter % N`) is the right knob. **Durable enough to copy; avoids the shared-state overhead of the full ORM.**

### 7. Context-window compaction — partial evict at 90%, snap to assistant boundary, summary at index 1

**Mechanism:** `services/summarizer/thresholds.py` + `summarizer.py:136-210`. Production default (`settings.py:78`): `PARTIAL_EVICT_MESSAGE_BUFFER`. Constants:
- Trigger at `SUMMARIZATION_TRIGGER_MULTIPLIER = 0.9` of context window (`constants.py:83`).
- Keep last 70% of messages verbatim (`partial_evict_summarizer_percentage = 0.30` — evict 30%).
- Snap eviction boundary **forward** to the next assistant message to never split a tool-call/tool-return pair.
- Summarize the evicted messages with a **separate cheaper model** (`compact.py:67`: Haiku 4.5 by default, fall back to ZAI GLM-5 for `letta/auto` agents).
- Insert summary as `role=user` message at **index 1** (just after system message).
- Emit a pre-compaction warning to the model: `NON_USER_MSG_PREFIX + "conversation history will soon reach its maximum length"` (`constants.py:416`).

Other constants: `message_buffer_limit=60`, `message_buffer_min=15`, `desired_memory_token_pressure=0.3` (target ≤30% context fill after compaction), `max_summarizer_retries=3` (`settings.py:79-106`).

**Lancelot implication:** the "separate cheap model for summarization" is a real cost lever — Haiku 4.5 is ~10× cheaper than Sonnet. The 90%-trigger + 30%-evict + snap-to-assistant-boundary are production-hardened constants. **Copy these constants and the snap-forward logic.** The summary-at-index-1 insertion is a well-reasoned ordering (system→summary→recent is the cleanest structure for most models). TransportPool already handles the cheaper-model call; the summarizer just needs to route to a different model.

### 8. Resumable SSE over Redis Streams — `seq_id` cursor + batched `XADD`

**Mechanism:** `server/rest_api/redis_stream_manager.py:92+463`. Writer:
- Assign monotonic `seq_id` per chunk.
- Buffer up to `flush_size` chunks, flush at max `flush_interval` seconds or on-complete.
- Flush via Redis **pipeline** (batch `XADD` → single round-trip).
- Cap stream at `maxlen=10000, approximate=True` (the `~MAXLEN` O(1) trim).
- Set 3h TTL on the stream key (`sse:run:{run_id}`).
- On flush failure: **prepend un-flushed chunks back onto buffer** (retry with no data loss).

Reader (`redis_sse_stream_generator`):
- `starting_after: int` parameter for resume cursor.
- Dual cursor: `last_redis_id` advances the `XRANGE` window; `cursor_seq_id` enforces the resume point.
- Tail-polls every 100ms when caught up, stops on `complete="true"` sentinel.

**Lancelot implication:** this is the full durable-stream backbone for background/resumable agent runs. Lancelot's `@app.stream` Rust pump (R3) already provides backpressure + disconnect-cancel for foreground streams. For *background* runs (agent continues after client disconnect), Redis Streams is the right solution — `XADD`/`XRANGE` not pub/sub (pub/sub loses disconnected clients). The batched-pipeline + rebuffer-on-failure writer pattern is directly reusable. **Defer until background-run support is a real requirement.** When it lands, copy the writer/reader verbatim with Lancelot's stream types.

### 9. `keepalive_interval`-on-`maxsize=1`-queue pattern for SSE keepalives

**Mechanism:** `streaming_response.py` (extracted section R2-L1). The non-obvious pattern for injecting keepalive pings into an async generator without losing backpressure or stalling on long tool calls:

```python
queue = asyncio.Queue(maxsize=1)   # maxsize=1 preserves backpressure

async def stream_reader():
    async for item in stream_generator:
        await queue.put(("data", item))
    await queue.put(("end", None))

reader_task = safe_create_task(stream_reader())
while True:
    try:
        msg_type, data = await asyncio.wait_for(queue.get(), timeout=keepalive_interval)
        ...yield data...
    except asyncio.TimeoutError:
        if silence > max_stream_silence:  # 30 min -- kill zombie connections
            reader_task.cancel(); break
        yield f"data: {LettaPing(seq_id=last_seq_id, ...)}\n\n"
```

`maxsize=1` is the backpressure key — a slow client throttles the producer because `queue.put` blocks when full. The keepalive carries `last_seq_id` (sniffed from passing chunks via `re.search(r'"seq_id":(\d+)'`) so a resuming client knows exactly how far it got. `max_stream_silence` (30 min) kills zombie connections that only ever get pings.

**Lancelot implication:** Lancelot's existing `@app.stream` SSE keepalive injects pings directly in the Rust pump. The Letta pattern is the Python-side equivalent and handles an important case the Rust pump doesn't: a tool call that takes tens of seconds while the generator is simply not yielding. The `maxsize=1` queue is the load-bearing piece — **copy this pattern for the Python agent loop that drives TransportPool calls with long tool-call gaps.**

### 10. `generator.athrow()` for cooperative mid-stream cancel

**Mechanism:** `streaming_response.py:153` (R2-L2). When a run is cancelled via a separate HTTP request, the wrapper checks DB status every 0.5s and, on cancellation:
```python
await stream_generator.athrow(RunCancelledException(run_id, ...))
```
instead of `break` or `aclose()`. `athrow` injects the exception at the generator's current suspension point, so the generator's own `try/except RunCancelledException` blocks fire and the agent loop can persist partial state and close LLM connections cleanly. The wrapper distinguishes two cancel origins: `RunCancelledException` (explicit user cancel — swallow, exit 200, send `event: cancelled` frame) vs. `asyncio.CancelledError` (client timeout/disconnect — re-raise for proper ASGI teardown).

**Lancelot implication:** Lancelot's R3 stream pump already handles client-disconnect → upstream cancel via the bounded-channel's Closed state. For **business-logic cancellation** (cancel-run endpoint while stream is live), `generator.athrow(MyCancel())` is the right mechanism — it lets the agent loop's `finally` blocks run. **This is a direct copy for the agent loop's cancel path.** TransportPool's `cancel_on_disconnect` does the transport-level half; the agent loop needs the athrow to close its own resources.

### 11. `StreamingResponseWithStatusCode` — dynamic status + survive-client-disconnect

**Mechanism:** `streaming_response.py:232` (R2-L3). Two hard problems solved in one class:
- **Dynamic status from first chunk**: pulls the first chunk via `body_iterator.__anext__()`, extracts `status_code` if it's a `(content, status_code)` tuple, then emits `http.response.start` with that code — the only window before the status is committed to the wire.
- **Survive client disconnect**: `asyncio.shield(self._protected_stream_response(send))` — a client-disconnect `CancelledError` does NOT abort the agent coroutine; it keeps running. Every `send()` call catches `anyio.ClosedResourceError`/`BrokenResourceError` and sets `_client_connected=False`, so the coroutine just stops sending bytes but finishes its work (persist state, mark run complete).

**Lancelot implication:** Lancelot's Rust `StreamingResponse` R3 pump handles backpressure and GIL-drop across `__anext__`. It does NOT currently support dynamic status from the first chunk or explicit client-disconnect survivability for the *agent* case (where work must finish even without a live client). The `asyncio.shield` + `_client_connected` flag pattern is **directly portable to Lancelot's Python streaming middleware layer**. The dynamic-status first-chunk trick requires reading the body iterator before calling `http.response.start`, which the Rust pump can't do — this must live in a Python ASGI wrapper above the pump.

### 12. `[DONE]` sentinel guarantee — detect missing terminal, synthesize it

**Mechanism:** `streaming_service.py:600` (R2-L5). The stream wrapper tracks `saw_done`/`saw_error` by sniffing passing chunks with line-anchored matches (`chunk.startswith("data: [DONE]")` or `"\ndata: [DONE]" in chunk`). If the generator exits having seen neither:
```python
yield f"data: {stop_reason.model_dump_json()}\n\n"
yield f"event: error\ndata: {error_message.model_dump_json()}\n\n"
yield "data: [DONE]\n\n"
```
This defends against the agent's *own future bugs* — any code path that exits without a terminal frame would leave clients hung.

The line-anchored check is important: `"\ndata: [DONE]"` + `"data: [DONE]"` avoids matching `[DONE]` that appears inside message content (e.g. an LLM that outputs the string "data: [DONE]").

**Lancelot implication:** Lancelot's `@app.stream` already emits `data: [DONE]` on generator completion. For the *agent loop* wrapper (which drives multiple tool calls + multiple generator yields), the "detect missing terminal and synthesize" pattern is worth adding. **Low-code, high-safety.**

### 13. GBNF grammar-constrained generation for local/non-native-tool models

**Mechanism:** `local_llm/chat_completion_proxy.py:96-125`, `local_llm/grammars/gbnf_grammar_generator.py`. For backends without native tool-calling (llamacpp, koboldcpp, webui, webui-legacy), Letta generates a GBNF grammar from the function schema and hands it to the inference server, forcing the output to be syntactically-valid tool-call JSON. `grammar_supported_backends = ["koboldcpp", "llamacpp", "webui", "webui-legacy"]` — the exact allowlist. For vllm/lmstudio (OpenAI-compat API, no grammar support), fall back to JSON parse + retry.

**Lancelot implication:** this is a **depreciating tactical hack** — it exists because models without native tool support needed a workaround, and frontier models (GPT-4o, Claude Sonnet 4.6, Gemini) have native tool calls. By 2026 the percentage of production deployments that need GBNF is shrinking rapidly. **Do not copy; the model clock is against it.** Flag and skip.

### 14. `OptimisticJSONParser` for streaming tool args → content deltas

**Mechanism:** `chat_completions_interface.py:203` (R2-L6). The OpenAI-compat layer re-exposes Letta's `send_message` tool call as assistant content. The model streams `{"message":"..."}` as partial JSON token-by-token. Instead of buffering the full JSON and then `json.loads()` (kills streaming), Letta uses `OptimisticJSONParser().parse(combined_args)` — a tolerant parser that returns a best-effort partial object from incomplete JSON. After each token delta, it re-parses the accumulated buffer and emits the newly-appended fragment only if the extracted `message` value *changed* from last time.

**Lancelot implication:** this is the general bridge from "agent that replies via a tool" to "OpenAI-compatible text stream." TransportPool streams raw HTTP chunks; the agent layer may need to unwrap tool-call argument tokens into content deltas for OpenAI-compat consumers. **Copy the pattern: accumulate tool arg deltas, partial-parse on each, emit delta only on field-value change.**

### 15. Deterministic sub-message IDs via `message_id + index` on type-transition

**Mechanism:** `rest_api/interface.py:492` (R2-L7). One LLM turn can produce interleaved `reasoning_message`, `tool_call_message`, `assistant_message` etc. The interface threads `prev_message_type` and increments `message_index` on every type transition, deriving `otid = Message.generate_otid_from_id(message_id, message_index)`. Same type → same otid (deltas coalesce). Type boundary → new otid. No extra UUID generation per delta; derived deterministically.

Also: Anthropic's first streaming chunk is empty (`message_delta.content is None` + `chunk.model.startswith("claude-")`) — suppress it.

**Lancelot implication:** when building the agent stream's type-event dispatcher, derive sub-message IDs as `base_id + type_index` rather than fresh UUIDs. **Copy the `prev_message_type` tracking pattern.**

---

## Anti-patterns to avoid

| Anti-pattern | Why |
|---|---|
| The 2134-line monolith `letta_agent_v3.py` | One file per agent version, accumulation of version-specific flags, deeply coupled. Copy the FSM logic, not the file. |
| Mandatory PostgreSQL + pgvector | Letta's entire persistence layer mandates Postgres with pgvector extension and Alembic migrations (60+ migrations). Overkill for the Lancelot agent layer; the interesting primitives (blocks, recall, archival) can be implemented against an interface with pluggable storage. |
| Model-controlled heartbeats (`request_heartbeat: bool` on every tool) | The V3 production agent removed this. It added per-model ambiguity about "should I keep going?" and made the loop non-deterministic from the framework's perspective. The rule-based FSM (`_decide_continuation`) is the right model. Keep heartbeats only as a compat shim for simple V2-style use cases. |
| Agent-to-agent messaging as built-in tools | `send_message_to_agent_and_wait_for_reply` is synchronous blocking. In Lancelot's context, TransportPool's `fanout` + `spawn_child` already provide the byte-path primitives for multi-agent. The tool-as-API pattern makes sense for the user to configure, not as a hard runtime dependency. |
| GBNF grammar-constrained generation | Depreciating with model capability growth. Do not build. |
| Pickle for sandbox result transport | HEAD commit `1131535` fixed this to JSON. Never use pickle across process boundaries. |

---

## API shape notes (ergonomics worth stealing)

- **`PATCH /attach/{id}` + `PATCH /detach/{id}`** for many-to-many relations (tools↔agents, blocks↔agents): makes the action explicit vs. `POST /<rel>` / `DELETE /<rel>`. Especially clear for tools-on-agents and blocks-on-agents.
- **`operation_id` on every route** (e.g. `operation_id="list_agents"`) to enable SDK auto-generation (Fern/Stainless). Zero-cost, high-value for a framework that will have client SDKs.
- **`ownership` field in error responses**: `{"message": "Unauthorized", "details": "...", "ownership": "This is api.letta.com..."}` — the error tells you which server it is and where to find the right one. Useful for a framework with both cloud and self-host surfaces.
- **`CompactionStats`** in responses: `{messages_before, messages_after, tokens_before, tokens_after, compaction_time_ms, summary_token_count}` — surfaces compression work to the operator.
- **Named SSE events for control frames**: `event: cancelled\ndata: {...}` and `event: error\ndata: {...}` vs. regular `data:` frames for message payloads. Clients can branch on `event:` type without parsing every `data:` as JSON.
- **`SafeORJSONResponse`**: orjson + lone-surrogate safety shim. On `TypeError: str is not valid UTF-8: surrogates not allowed`, sanitize and re-dump. Protects against LLM output containing U+D800–U+DFFF surrogates.
- **`swagger_ui_parameters={"docExpansion": "none"}`**: collapses a large Swagger UI by default. Obvious, often forgotten.
- **`include_in_schema=False` on `/latest` alias**: mount all routers twice — `/v1/...` (in OpenAPI) and `/latest/...` (hidden). The `/latest` alias is stable for clients that always want newest.

---

## Byte-path implications (Lancelot angle)

| Primitive | Lancelot provides | Change needed |
|---|---|---|
| Outbound LLM calls (transport) | `TransportPool` (retries, backpressure, mTLS, traceparent) | None — use as-is |
| Outbound multi-agent fan-out | `TransportPool.fanout` + `spawn_child` | None — these are the byte-path analogues |
| Streaming with keepalives | `@app.stream` R3 Rust pump (backpressure, SSE keepalive, TTFT) | Add Python-side `maxsize=1` queue pattern for agent loops with long tool-call gaps |
| Client-disconnect cancel | R3 pump Closed channel → upstream cancel | Add `generator.athrow(AgentCancelled())` for business-logic cancel path |
| `[DONE]` terminal guarantee | `@app.stream` emits `[DONE]` on completion | Add "detect missing terminal, synthesize" wrapper for agent loop |
| Dynamic HTTP status from first chunk | Not in Rust pump | Needs Python ASGI wrapper above the pump |
| Resume-cursor SSE | Not built | Redis Streams pattern (R2-L4) when background runs are needed |
| Cost tracking | `lancelot.upstream_metrics` + `lancelot.context.cost_usd` | Already done |
| TTFT metric | `lancelot.http.stream.ttft` | Already done |
| Memory tier (blocks/recall/archival) | Not built | The main build target from this teardown |
| Compaction routing to cheap model | Not built | Route summarizer calls to a cheaper model via TransportPool |
| Tool-rule constraint solver | Not built | InitToolRule + TerminalToolRule are the minimal useful subset |
| Named stop reasons | Not built | `StopReasonType` enum is a direct copy |

---

## What to build first (priority order from this teardown)

1. **`Block` + `Memory.compile_to_string()` + `{CORE_MEMORY}` placeholder** — the minimum viable memory layer. No DB required; blocks can live in a dict. The `<memory_metadata>` page-table inject is free.
2. **`StopReasonType` enum** — machine-readable stop reasons for the loop. 13 variants, copy verbatim.
3. **`_decide_continuation` FSM** — continue-by-default-after-tool, stop on no-tool/terminal/max-steps. `NON_USER_MSG_PREFIX` coach messages for forced continuation. `DEFAULT_MAX_STEPS = 50`.
4. **`ToolRulesSolver`** subset: `InitToolRule` + `TerminalToolRule` — the minimal constraints for structured agent behavior.
5. **`memory_rethink` + sleep-time dispatch pattern** — background consolidation as an `asyncio.create_task` post-response, frequency-gated by turn counter.
6. **Compaction**: 90% trigger, 30% evict, snap-to-assistant-boundary, summary at index 1, cheap model routing.
7. **`generator.athrow()` cancel path** — cooperative mid-stream cancel that lets the agent loop clean up.
