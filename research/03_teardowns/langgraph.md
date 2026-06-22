# LangGraph + LangServe — Mechanism-Level Teardown for Lancelot

**Sources:** `researchfms/teardowns/LANGGRAPH.md` (Rounds 1-3, `076e2a3`/`1fcb768`) + `researchfms/teardowns/LANGSERVE.md` (Rounds 1-2, `27e57af`).
**Focus:** graph tax to counter-position against; what is genuinely durable vs what is complexity for its own sake.

---

## 1. The graph tax — what you're actually paying for

LangGraph is explicitly Pregel/BSP (Bulk Synchronous Parallel, the 2010 Google paper). That is not metaphorical — the `Pregel` class docstring says so (`pregel/main.py:448`). The BSP cost is real and structural:

**Three-phase overhead per agent step (not per run, per STEP):**
- Plan: `prepare_next_tasks` walks `channel_versions` vs `versions_seen` for each node.
- Execute: parallel dispatch to thread-pool futures or asyncio tasks.
- Commit: `apply_writes` + checkpoint snapshot + (optionally) Postgres write.

For a standard single-path ReAct loop — the overwhelmingly common case — this is 3 phases of infrastructure per turn of a `while` loop. There is a fast path when only one task fires per step (`runner.tick` single-task runs inline on the calling thread, `_runner.py:862`) but the channel/checkpoint machinery always runs.

**The recursion limit is a STEP BUDGET, not a recursion depth.**
`self.stop = self.step + config["recursion_limit"] + 1`. Each BSP step (plan-execute-commit) increments `self.step`. Default `recursion_limit=25` in `_loop.py:1676`. The runtime default constant is `10007` (`LANGGRAPH_DEFAULT_RECURSION_LIMIT`, `_internal/_config.py:31`) — effectively unbounded unless set by the user. Old tutorials cite 25; that is the compile-level default, NOT the runtime sentinel. Explicit: pass `config={"recursion_limit": N}` per invocation.

**The mental model is the product; the code follows.** (`LANGGRAPH.md §11`) The 72k-line repo is not 72k lines of unique ideas — it's 3,200 lines of core runtime, then 70k lines of persistence backends (5 savers), functional API, RemoteGraph, store+ANN search, CLI, SDK (Python + JS), tracing integration, and backwards-compat shims.

---

## 2. What IS genuinely good — copy or counter-position against

### 2.1 `remaining_steps` managed value — the graceful budget primitive (COPY)

The `RemainingSteps` managed value (`managed/is_last_step.py`, `LANGGRAPH.md §R2.9`) is a read-only state key computed from `scratchpad.stop - scratchpad.step`. It is NOT checkpointed. It is visible to the node through a special `Annotated[int, RemainingStepsManager]` type annotation on the state schema.

The agent guard in `create_react_agent` (`chat_agent_executor.py:620-634`):
```python
remaining_steps = state.get("remaining_steps")
if remaining_steps is not None:
    if remaining_steps < 1 and all_tools_return_direct:
        return True  # stop
    elif remaining_steps < 2 and has_tool_calls:
        return True  # stop before making a tool call you can't follow up on
```

Graceful exit string (hardcoded): `"Sorry, need more steps to process this request."` (`chat_agent_executor.py:689`).

**Lancelot implication:** The agent loop needs this exact primitive — not an exception, not a silent stop, but a budget visible to the node body that triggers a clean final message rather than a `RecursionError`. The `< 2` threshold (not `< 1`) is critical: it prevents dead-ending one step short of the limit mid-tool-call.

### 2.2 Durable checkpoint — the event-sourced state machine (STUDY, ADAPT NOT COPY)

The Postgres DDL (`checkpoint-postgres/base.py:43-91`, `LANGGRAPH.md §R2.6`) is the production shape:

```sql
checkpoints(thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, checkpoint JSONB, metadata JSONB)
checkpoint_blobs(thread_id, checkpoint_ns, channel, version, type, blob BYTEA)   -- content-addressed per version
checkpoint_writes(thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, blob BYTEA, task_path TEXT)
```

Key insight: blobs are **content-addressed by (thread_id, ns, channel, version)**. Identical values across checkpoints share one row. Checkpoint load is one SELECT with subqueries that join `channel_versions` JSON against `checkpoint_blobs` — fetches only the blobs at the current versions, not full snapshots.

The `checkpoint_writes` table (pending writes from in-progress steps) is the crash-recovery mechanism. On resume:
1. Load `checkpoint_pending_writes` from the table.
2. For tasks that already completed (writes exist), restore `task.writes` from the table.
3. Driver filters: `[t for t in tasks if not t.writes]` — only tasks with no writes re-execute.

This gives exactly-once-ish semantics at super-step granularity with no node-level idempotency required.

**Lancelot implication:** The durable seam Lancelot lacks is this event-sourcing pattern. NOT the full Pregel runtime — the specific primitive is: `(thread_id, step_id, task_id, channel, value)` pending writes + a resume query that restores completed task outputs so only incomplete tasks rerun. About 600 lines of real work (not 72k).

### 2.3 `interrupt(value)` / `Command(resume=...)` — HITL as a durable exception (COPY SHAPE)

`interrupt(value)` (`types.py:801`): raises `GraphInterrupt`, but the checkpoint is written BEFORE the raise. Resume = `graph.invoke(Command(resume=value), config={"thread_id": ..., "checkpoint_id": ...})`. The interrupt ID is `xxh3_128_hexdigest(ns.encode())` — deterministic, idempotent across replays.

The mechanism: the interrupted node's `interrupt(...)` call is tracked in the `__interrupt__` channel. On resume, the replay detects the checkpoint and re-runs the node, but this time `interrupt(...)` returns the `resume` value instead of raising.

**Lancelot implication:** The HITL seam needs this exact 2-call shape: `interrupt(question)` halts and serializes the question; a second `resume(answer)` call re-enters. Everything else (approval UI, webhook callback, async wait) is above this seam.

### 2.4 `apply_writes` atomic commit with `PASSTHROUGH`/`SKIP_WRITE` (`_write.py:22-23`, `LANGGRAPH.md §R3.6`)

The 6-step `apply_writes` algorithm (`_algo.py:232-345`, `LANGGRAPH.md §R2.2`) is worth understanding in full:
1. Mark `versions_seen[node][chan]` = current version for all consumed channels.
2. Compute one new `next_version` for the entire step (single int increment).
3. `consume()` input channels (clears ephemeral ones, bumps version).
4. Group writes by channel, **skip reserved channels** (`NO_WRITES`, `PUSH`, `RESUME`, etc.).
5. `channel.update(vals)` — runs the reducer; bump version IFF update returns True.
6. Channels not updated get `.update([])` (allows `LastValueAfterFinish` to work). If no triggers remain, call `.finish()` on all channels (fixpoint detection).

`PASSTHROUGH` means "write the node's output to this channel"; `SKIP_WRITE` means "this entry is a no-op, don't trigger downstream." This is what makes conditional edges efficient — a channel that isn't written doesn't bump its version, so downstream nodes don't fire.

**Lancelot implication:** The channel version counter pattern (`channel_versions[c] > versions_seen[node][c]` → trigger) is an elegant O(1)-per-channel scheduler. Lancelot's simpler linear loop doesn't need BSP, but if fan-out is ever added, this version-counter trigger is the right primitive to copy.

### 2.5 RetryPolicy defaults — the exact backoff constants (COPY)

`RetryPolicy` (`types.py:406`, `LANGGRAPH.md §R3.3`):
```python
initial_interval = 0.5   # seconds
backoff_factor   = 2.0
max_interval     = 128.0  # seconds (~2 minutes)
max_attempts     = 3
jitter           = True   # adds random.uniform(0, 1) seconds
retry_on         = default_retry_on  # transient: connection errors, 5xx, openai.RateLimitError
```

Backoff math: `interval = min(128.0, 0.5 * 2.0**(attempts-1)) + random.uniform(0, 1)`.
Schedule: `~0.5s → ~1s → ~2s → ~4s → ... → 128s`.

`default_retry_on` does NOT retry `ValueError`, `TypeError`, or `GraphBubbleUp`. Retries transient (network/rate-limit) errors only.

**On retry, `task.writes.clear()` before each attempt** — prevents double-write to channels if a node partially executed before crashing.

**`CONFIG_KEY_RESUMING=True` is patched on retry** — subgraphs know to resume rather than restart.

**`node_attempt` (1-indexed)** in `ExecutionInfo` — a retrying node can read this to e.g. lower temperature.

**Lancelot implication:** `TransportPool` already has retry + `Retry-After` honoring. These constants are worth cross-checking. The `node_attempt` injection into context is a clean pattern for `lancelot.context`.

### 2.6 `pre_model_hook` for context compaction — the v2 create_react_agent hook (COPY SHAPE)

`create_react_agent(version="v2", pre_model_hook=...)` (`chat_agent_executor.py:296`, `LANGGRAPH.md §R3.7`):

The hook returns `{"llm_input_messages": trimmed_messages}`. The node reads from `llm_input_messages` (if present) instead of `messages`. The original `messages` history is untouched — the transcript stays complete while the model sees a windowed/summarized view.

This is the correct architecture for context compaction: a separate write key that shadows the full history for the model call only, without mutating the persistent state. No graph round-trip needed.

**Lancelot implication:** The agent context-bundle construction should follow this pattern — assemble the model-visible context separately from the full history, written to a different key. This is the non-LangGraph way to implement what `pre_model_hook` provides.

### 2.7 `send_react_agent` `remaining_steps < 2 and has_tool_calls` guard (COPY)

The full guard (`chat_agent_executor.py:620-634`):
```python
remaining_steps = _get_state_value(state, "remaining_steps", None)
if remaining_steps is not None:
    if remaining_steps < 1 and all_tools_return_direct:
        return True  # stop — a direct-return tool still needs no follow-up
    elif remaining_steps < 2 and has_tool_calls:
        return True  # stop — would need at least 2 more steps (tool call + final response)
```
The `< 2` threshold is not arbitrary — a tool call requires at minimum 1 step to execute the tool + 1 step for the model to synthesize the result. Running a tool call with only 1 step left would dead-end the agent.

### 2.8 `MultitaskStrategy` — concurrent-run collision handling (STUDY)

From the Platform SDK (`_async/runs.py:1737`, `LANGGRAPH.md §R2.10`):
- `"reject"` — fail if a run is already active on the thread.
- `"interrupt"` — interrupt active run, start new one.
- `"rollback"` — interrupt + roll back state.
- `"enqueue"` — queue new run after active one completes.

**Lancelot implication:** For a durable agent endpoint, the server needs to handle concurrent requests to the same thread. This enum is the full design space. `"enqueue"` is the safest default for conversational agents.

---

## 3. The graph-as-default trap — anti-patterns to refuse

### 3.1 `Annotated[list[BaseMessage], add_messages]` as the universal state format

`add_messages` (`graph/message.py:61`, `LANGGRAPH.md §2.2`) is a 137-line reducer that deduplicates messages by UUID, handles `RemoveMessage(id=X)` selective deletion, and `RemoveMessage(id="__remove_all__")` full-clear with tail append. It is correct and thorough.

**The trap:** it enforces LangChain `BaseMessage` as the state primitive. For Lancelot, the message format should be provider-canonical (OpenAI dict or a Lancelot `Message` dataclass), not a LangChain object. The `add_messages` semantics (deduplicate by ID, remove by ID, bulk-clear with `__remove_all__`) are worth copying; the LangChain type dependency is not.

### 3.2 `StateGraph.add_node` / `add_edge` / `compile` for a simple linear ReAct loop

This is 400+ lines of graph builder for what is functionally a `while` loop with a tool dispatcher. The complexity pays off only when you have genuine fan-out (parallel tool execution across nodes, map-reduce over a corpus, multi-agent with genuine parallelism). For a single ReAct agent, the graph tax is pure overhead.

**Counter-position:** Lancelot's agent loop should be a named Python `while` (or `async for`) with a `StopReason` enum and explicit budget. The graph API makes sense as an *optional* surface for users who want the visual debugger and checkpointed multi-agent topologies — not as the default primitive.

### 3.3 LangChain `BaseChatModel` coupling

`create_react_agent("openai:gpt-4o", tools=...)` uses `init_chat_model` — you're importing `langchain_openai`. The model is "just a Runnable" but that Runnable is a LangChain object, not a raw dict-in/dict-out function.

**Counter-position:** Lancelot's model adapter layer should be a thin dict translator (OpenAI-canonical → provider dialects), not a class hierarchy. `TransportPool` already owns the HTTP call; the message-shape translation is the gap to fill.

### 3.4 Node caching via pickle as default hash key

`CachePolicy(key_func=default_cache_key)` where `default_cache_key` hashes the input with **pickle** (`_internal/_cache.py`, `LANGGRAPH.md §R3.10`). Pickle hashing is non-deterministic across processes and fragile across Python versions.

**Better approach:** `lancelot.canonical.dumps_canonical(input)` already produces stable sorted-key JSON bytes — the correct cache key for LLM prompt inputs where canonical serialization equals cache-stability.

### 3.5 `DEFAULT_RECURSION_LIMIT = 10007` — the silent unbounded loop

The runtime default is effectively unlimited. The docstring says "you can increase the limit by setting `recursion_limit`" but doesn't say the default IS effectively unlimited. Users who rely on the default and build a graph with an exit condition bug will spin until OOM or timeout.

**Counter-position:** Lancelot's loop should default to a hard budget (e.g. 25 steps) and surface a clear `BudgetExhausted(StopReason.budget, step=N, budget=25)` rather than a `GraphRecursionError`.

---

## 4. LangServe — transplantable FastAPI patterns

### 4.1 Two-parallel-routes trick (COPY)

`server.py:496-503, 764-777` (`LANGSERVE.md §1.2`): For any endpoint that needs raw `Request` access (streaming, custom validation) but should have typed OpenAPI docs:

1. Register real handler first: `@app.post("/path", include_in_schema=False)` takes `request: Request`.
2. Register docs stub second: same path, typed signature, `response_model=...`, body `raise AssertionError("unreachable")`.

First-registered-wins for routing; the second exists only for OpenAPI schema generation. The stub body raises loudly if routing ever breaks (not silently 500s).

**Lancelot implication:** Any `@app.stream` or `@app.tool` endpoint that exposes a typed OpenAPI schema should use this pattern. Relevant for the agent `/run/stream` endpoint.

### 4.2 Config-key projection security boundary (COPY)

`api_handler.py:157-359` (`LANGSERVE.md §R2-LS2`):
```python
projected_config = {k: config[k] for k in config_keys if k in config}
```
Three-layer merge order (trust model):
1. Overridable defaults (lowest trust).
2. Client config filtered by `config_keys` allowlist.
3. Non-overridable server config (highest trust — always wins).

The `per_req_config_modifier` hook runs AFTER projection — it can inject auth-derived config only into already-allowlisted keys.

**Lancelot implication:** Any endpoint that accepts a `config` body (agent runtime parameters, model choice, temperature) needs this exact allowlist + three-layer merge. Without it, a client can inject `recursion_limit=10**9` or override model choice.

### 4.3 SSE event taxonomy (COPY)

`api_handler.py:1177-1222` (`LANGSERVE.md §3.1`):
```
event: metadata  → {"run_id": ..., "feedback_key"?: ...}  (sent first)
event: data      → serialized chunk payload
event: end       → no data (terminal)
event: error     → {"status_code": 500, "message": "Internal Server Error"}  (error text SCRUBBED)
```

Critical: **the real error message is never sent to the client** (comment: "may contain sensitive information"). The SSE generator catches `BaseException`, emits the generic envelope, then re-raises internally.

### 4.4 Dynamic Pydantic model generation with recursive namespacing (COPY)

`validation.py:38-66` + `api_handler.py:308-360` (`LANGSERVE.md §R2-LS1`):
```python
model = create_model(f"{namespace}RequestModel", field=(FieldType, Field(...)))
model.model_rebuild()
```
**Recursive rename**: nested `BaseModel` fields must ALSO be prefixed — `_rename_pydantic_model` recurses into all fields. Without this, two mounts with a field named `Input` produce OpenAPI component name collisions that silently break the whole spec.

**Lancelot implication:** `lancelot.mcp.mount` and any multi-tool endpoint that generates per-tool schemas needs recursive namespacing. Already relevant for `@app.tool` if multiple tools share a nested type name.

---

## 5. Channel types — the useful subset

Of the 9 channel types (`LANGGRAPH.md §2.1`), two are worth understanding for Lancelot's durable seam design:

**`BinaryOperatorAggregate`** — `Annotated[list, operator.add]` or `Annotated[set, set.union]`. The `Overwrite(value)` escape hatch (`{"__overwrite__": value}`) bypasses the reducer to do a hard reset. Useful for a message list that normally appends but occasionally needs a `RemoveAll`-equivalent.

**`EphemeralValue`** — exists only for the step it's written; cleared after. The correct primitive for tool call arguments (pass through once, don't persist in the checkpoint).

**`UntrackedValue`** — like `LastValue` but never checkpointed. The correct primitive for stateful runtime objects (HTTP clients, DB connections) that live in the graph's in-memory state but must not be serialized.

---

## 6. Constants table (all KNOWN from source)

| Constant | Value | Source |
|---|---|---|
| `DEFAULT_RECURSION_LIMIT` | `10007` (env `LANGGRAPH_DEFAULT_RECURSION_LIMIT`) | `_internal/_config.py:31` |
| `recursion_limit` per-compile default | `25` | `_loop.py:1676` |
| `RetryPolicy.initial_interval` | `0.5` s | `types.py:422` |
| `RetryPolicy.backoff_factor` | `2.0` | `types.py:424` |
| `RetryPolicy.max_interval` | `128.0` s | `types.py:426` |
| `RetryPolicy.max_attempts` | `3` | `types.py:428` |
| `RetryPolicy.jitter` | `True` (`random.uniform(0,1)` s added) | `types.py:430` |
| `Durability` default | `"async"` | `types.py:87` |
| `REMOVE_ALL_MESSAGES` | `"__remove_all__"` | `graph/message.py:38` |
| `Command.PARENT` | `"__parent__"` | `types.py:749` |
| `NULL_TASK_ID` | `"00000000-0000-0000-0000-000000000000"` | `_constants.py` |
| `NS_SEP` | `"|"` | `_constants.py` |
| `NS_END` | `":"` | `_constants.py` |
| `_are_more_steps_needed` guard | `remaining_steps < 2 and has_tool_calls` | `chat_agent_executor.py:631` |
| `create_react_agent` graceful exit string | `"Sorry, need more steps to process this request."` | `chat_agent_executor.py:689` |
| `runner.tick` wait mode | `FIRST_COMPLETED` | `_runner.py:285` |
| `RemainingSteps` default (Pydantic) | `25` | `chat_agent_executor.py:74` |
| `MultitaskStrategy` values | `reject/interrupt/rollback/enqueue` | `_async/runs.py:1737` |
| `DELTA_MAX_SUPERSTEPS_SINCE_SNAPSHOT` | `5000` | `_constants.py` |
| `EncryptedSerializer` cipher | AES-256-GCM | `serde/encrypted.py` |

---

## 7. Byte-path lens — what Lancelot already provides

| LangGraph primitive | Lancelot status |
|---|---|
| `TransportPool.request/stream` — the LLM HTTP call | BUILT. LG's model call is just this. |
| `TransportPool.fanout(first_n, deadline, cancel_laggards)` | BUILT. LG's `Send` fan-out is Lancelot's fanout with checkpoint wrapping. |
| `lancelot.upstream_metrics` — token/cost tracking | BUILT. LG has no built-in cost tracking. |
| `lancelot.context.cost_usd/tokens_in/tokens_out/agent_id` | BUILT. LG stores these in `config["configurable"]` or nowhere. |
| `lancelot.canonical.dumps_canonical` — cache-stable JSON | BUILT. LG uses pickle for cache keys — worse. |
| `lancelot.mcp.mount` — tool serving | BUILT. |
| `@app.stream` — SSE with backpressure | BUILT. LG's streaming is slower (SSE over httpx over the LG SDK). |
| `@app.batch` — request coalescing | BUILT. No LG analogue. |
| Durable checkpoint (event-sourced, Postgres blob table) | MISSING. The main Lancelot gap vs LangGraph. |
| `interrupt(value)` / `Command(resume=value)` HITL seam | MISSING. Depends on durable checkpoint. |
| `remaining_steps` managed value (budget visible to node) | MISSING. Needs to be built into the agent loop. |
| Context isolation across subagents (`spawn_child`) | BUILT (`TransportPool.spawn_child`). LG uses nested graph namespaces. |
| `pre_model_hook` for context compaction | MISSING. The pattern (shadow key `llm_input_messages`) is simple to add without LangGraph. |
| Message deduplication reducer (`add_messages` semantics) | MISSING. Worth building as a standalone function (no LangChain dep). |
| Config-key allowlist projection | MISSING. Needed for the agent run endpoint. |

---

## 8. What to skip entirely

- The full `StateGraph` / `add_node` / `add_edge` / `compile` API — this is the graph tax. Lancelot's agent loop is a `while`, not a graph. Optional future surface, not core.
- `BaseChatModel` / `init_chat_model` — LangChain dep. Lancelot owns the HTTP layer; the model is a dict shape, not a class.
- LangSmith callbacks / `feedback_key` / `feedback_token` — not the observability path for Lancelot (use OTel + `lancelot.upstream_metrics`).
- The `RemoteGraph` proxy — Lancelot's multi-agent cross-process call is `TransportPool.spawn_child` over HTTP. No graph proxy needed.
- `DeltaChannel` — BSP-specific delta-snapshot optimization. Not relevant without the Pregel runtime.
- The LangGraph Platform / Studio / hosted API — a managed wrapper around this whole stack. Lancelot competes at a lower layer.
- `JsonPlusSerializer` / `MsgpackSerializer` — Lancelot's checkpoint serializer should use pydantic-core `to_json` (already the fast path).
