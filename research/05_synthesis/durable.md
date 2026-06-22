# Durable Layer — Synthesis Brief (for Lancelot)

**Layer:** durable execution seam (event-sourced checkpoint / resume / HITL / budget)
**Altitude:** "just build agents, brutal" — the smallest primitive that earns its keep.
**Date:** 2026-06-22
**Sources synthesized:** `03_teardowns/{mastra,langgraph,pydantic_ai,openai_agents,strands,letta,durable_exec,antigravity,claude_agent_sdk,google_adk}.md` · `02_core/{transcripts_stanford_palantir,transcripts_yc,agentic_deep}.md` · `04_byte_path/{lancelot_model_io,lancelot_serve_tools}.md`

---

## 0. The thesis, and the evidence it survives

**The tactical↔durable seam: ONE loop, `durable=False` (default) keeps state in-memory, `durable=True` swaps a checkpointer behind the same event-sourced internals. Byte-identical when off.**

This is not my invention — it is the convergent design every serious framework arrived at independently:

- **Mastra's dual-array compile is the proof.** Every control-flow call pushes to BOTH `stepFlow` (live closures, in-process engine) AND `serializedStepFlow` (durable twin, `fn.toString()` predicates, `canSuspend` precomputed). "This dual push is why durability is 'free': a different process can re-hydrate the graph topology from the serialized twin." The engine runs the *same path*; the snapshot store is the only thing that differs (`mastra.md` §1a). The loop IS `.dowhile(executionWorkflow, stopPredicate)` — "Remove the workflow engine and the agent loop does not exist. That is the test for a rebuilt primitive vs. a wrapper, and Mastra passes it" (`mastra.md` thesis).
- **Pydantic AI's pluggable `BaseStatePersistence` is the same seam, cleaner.** `SimpleStatePersistence` (one in-memory snapshot — enough for next step), `FullStatePersistence` (deep-copied history), `FileStatePersistence` (JSON, survives restart) — *three implementations behind one protocol, identical engine* (`pydantic_ai.md` §10). The driver snapshots before AND after every node transition through the same `GraphRun.next` regardless of which store is plugged (`pydantic_ai.md` §1).
- **OpenAI Agents SDK runs identically; only `RunState.to_json()` + resume differ.** "Run → tool needs approval → `NextStepInterruption` → loop breaks → `state.to_json()` → persist anywhere → `Runner.run(agent, input=state)` in any process" (`openai_agents.md` §5). Same loop, durability is a serialization boundary.
- **LangGraph: checkpointer vs `MemorySaver` is the flip.** The Postgres checkpoint shape and the in-memory saver are the same event-sourced state machine with different backends (`langgraph.md` §2.2).
- **Palantir Orchestrator states the user-facing thesis verbatim:** "Normally, if you're trying to build some kind of suspension like this, you have to think about state machines, queues, you might have to set up Kafka… But I can just here with the `await condition` express what I want to happen. I want to pause until this thing becomes true. I don't have to worry about it consuming resources in the background." (`transcripts_stanford_palantir.md` §7). This is the 2+yr-production north star for the API ergonomics.

The five sub-mechanisms in the thesis each have direct, multi-source grounding (table below). **This layer is genuinely durable** (survives 3+ model generations — it's a state-machine property, not a prompt trick) and is the single largest gap in Lancelot's agent half (`lancelot_model_io.md` §6, `langgraph.md` §7, `pydantic_ai.md` §14 all rank it the #1 missing primitive).

| Thesis mechanism | Primary citations |
|---|---|
| Same loop, `durable=True` flips runtime; byte-identical off | Mastra dual-array (`mastra.md` §1a); Pydantic AI 3 persistence impls (`pydantic_ai.md` §10); OpenAI RunState (`openai_agents.md` §5) |
| Event-sourced internally (in-mem tactical / checkpointer durable) | LangGraph `checkpoint_writes` pending-writes table (`langgraph.md` §2.2); Mastra snapshot jsonb (`mastra.md` §1i); ADK prefix-partitioned state (`google_adk.md` §4) |
| `step()` bracket — side effects skipped on resume | LangGraph "driver filters `[t for t in tasks if not t.writes]` — only tasks with no writes re-execute… exactly-once-ish at super-step granularity with no node-level idempotency required" (`langgraph.md` §2.2); Strands "on resume: model phase is SKIPPED; saved `tool_use_message` reused" (`strands.md` §1c/§1d); Pydantic AI `snapshot_node_if_new` idempotent dedup (`pydantic_ai.md` §10); Antigravity "skips to EXECUTION on resume ONLY if planning was already approved" (`antigravity.md` §1) |
| Sync default (async re-fires LLM → corrupt log) | The deterministic-replay invariant: LangGraph `task.writes.clear()` before each retry "prevents double-write to channels if a node partially executed" (`langgraph.md` §2.5); Pydantic AI "results always appended in `sorted(call_index)` order regardless of completion order — Parallel execution, deterministic transcript… missing it makes replays non-deterministic" (`pydantic_ai.md` §5); Strands "Do NOT parse tool args incrementally. Accumulate as string, parse once at stop. Any framework that tries per-chunk JSON parsing will corrupt mid-stream tool calls" (`strands.md` §2a) |
| submit / attach / cancel | OpenAI `to_json()` → persist → resume in any process (`openai_agents.md` §5); Letta resumable SSE over Redis Streams: `seq_id` cursor + `starting_after` resume + `athrow()` cancel (`letta.md` §8/§10); Antigravity async-mailbox + `ResumeFromSubagentId` checkpoint (`antigravity.md` §6); Claude SDK `interrupt` control-request + `SessionStore.append/load` (`claude_agent_sdk.md` mech 1/5) |
| `ctx.hitl.request` — durable promise, **checkpoint-before-pause** | LangGraph `interrupt(value)`: "the checkpoint is written BEFORE the raise"; resume re-runs the node but `interrupt()` returns the value instead of raising; interrupt id = `xxh3_128_hexdigest(ns)` deterministic (`langgraph.md` §2.3); Mastra `suspend(payload)` → snapshot persists → `run.resume({resumeData})` (`mastra.md` §2c); Pydantic AI deferred-tool resume "jumps straight to CallToolsNode, bypasses the model" — HITL as a literal graph edge (`pydantic_ai.md` §10/§16); OpenAI `ToolApprovalItem` + `approve/reject` keyed by `(tool_name, call_id)` (`openai_agents.md` §5); Palantir clarification-object `await condition: obj.human_response IS NOT NULL` (`transcripts_stanford_palantir.md` §7); Strands `interrupt_state.context = {tool_use_message, tool_results}` (`strands.md` §1d) |
| `max_llm_calls` / `max_budget_usd` | Claude Code `error_max_budget_usd` StopReason + `total_cost_usd` on ALL subtypes (`agentic_deep.md` §7); Strands turns→total_tokens→output_tokens cap priority (`strands.md` §1b); Pydantic AI request-limit `50` (`pydantic_ai.md` §13); LangGraph `remaining_steps < 2 and has_tool_calls` graceful budget (`langgraph.md` §2.1) |

---

## 1. DECISION (what Lancelot builds)

Build **`lancelot.durable`** — a small (~600–900 LoC, the size LangGraph's real work is, not its 72k) event-sourced agent-step engine with a **pluggable `Checkpointer` protocol**, defaulting to in-memory. The seam is the loop's `step()` bracket; durability is a backend swap, not a code path. Concretely:

1. A **`@step` bracket** that records `(run_id, step_idx, kind, output)` to an append-only log and, on resume, **returns the recorded output instead of re-executing** (the side-effect-skip invariant). Default backend = `InMemoryCheckpointer` (a dict); `durable=True` (or passing any non-memory `Checkpointer`) is the only difference — the engine path is byte-identical.
2. **Sync-by-default step execution** with the deterministic-replay rules baked in: append-only event log, completed-step outputs restored before any re-run, tool args buffered-and-parsed-once (never per-chunk on the replay path), tool results appended in `sorted(call_index)` order. An `async`/concurrent step is allowed but must be *idempotent-by-recorded-output* — the bracket enforces this by short-circuiting on replay so the LLM never re-fires.
3. **`ctx.hitl.request(schema) -> awaitable`** — the durable promise. Mechanism: **checkpoint is written BEFORE the pause** (LangGraph's load-bearing ordering), a typed `PendingRequest` row is emitted, the run returns control (HTTP 202 + run_id). A later `resume(run_id, response)` re-enters; the `hitl.request` call returns the human's typed answer instead of suspending. Idempotent across replays via a deterministic request id (`xxh3`/sha over `(run_id, step_idx, ns)`).
4. **`await_condition(predicate)`** — Palantir's await-condition as the second suspend primitive (suspend until a named typed value is non-null), implemented on the same checkpoint-before-pause machinery. This is what makes "pause until the document is uploaded / the human fills the form" express as one line with zero background resource cost.
5. **`submit / attach / cancel` HTTP surface** built ON Lancelot's byte path: `submit` → 202 + run_id; `attach` → `@app.stream` SSE replay from a `seq_id` cursor (Letta pattern) for live runs, polling for completed; `cancel` → `generator.athrow(RunCancelled)` so the agent loop's `finally`/`aclose` runs and partial state checkpoints cleanly.
6. **Budget gate as a StopReason**, not an exception: `max_llm_calls`, `max_budget_usd` (read from `lancelot.context.cost_usd`, which already accumulates across sub-LLM calls), `max_steps`. Trip → clean final message + typed `StopReason`, not a crash.

**The cardinal-rule line:** runtime dep stays **pydantic-only**. `InMemoryCheckpointer` is stdlib. `SqliteCheckpointer` uses stdlib `sqlite3` (NOT a new dep). `PostgresCheckpointer` lazy-imports its driver only when constructed. Snapshot serialization uses **pydantic-core `to_json`** (already Lancelot's fast path — `langgraph.md` §8 explicitly says use this, not JsonPlus/Msgpack). `sqlite-vec` is lazy-imported and only relevant to the *memory* layer, not this one.

**What we do NOT build:** a graph DSL (the loop is a `while` / `.dowhile`, not `StateGraph.add_node`); a Pregel/BSP runtime; a hosted control plane; provider-specific durable backends (Inngest/Temporal coupling). See REFUSE.

---

## 2. MUST BUILD (mechanism-level, with the exact constants the sources hand us)

### B1. `Checkpointer` protocol + the event log shape (the seam)
Two-method required protocol (copy OpenAI/Claude-SDK SessionStore shape — `openai_agents.md` §6, `claude_agent_sdk.md` mech 5):
```python
class Checkpointer(Protocol):
    async def append(self, run_id: str, entries: list[StepRecord]) -> None   # REQUIRED
    async def load(self, run_id: str) -> list[StepRecord] | None             # REQUIRED
    async def load_pending(self, run_id: str) -> PendingRequest | None       # OPTIONAL (HITL)
    async def cancel(self, run_id: str) -> None                              # OPTIONAL
```
- **Event log, not snapshot diff.** Append-only `StepRecord(uuid, step_idx, kind, payload, parent_uuid, ts)`. `uuid` is the idempotency key (Claude SDK `SessionStoreEntry.uuid`, `claude_agent_sdk.md` mech 5). On resume, `[r for r in records if r.has_output]` are restored; only steps with no recorded output re-run (LangGraph `checkpoint_writes` filter, `langgraph.md` §2.2).
- **Content-address blobs by `(run_id, step_idx, channel, version)`** so identical values across checkpoints share one row — LangGraph's load-only-current-version trick (`langgraph.md` §2.2). Worth it for long runs; skip for v1 if it complicates the SQLite path.
- **Sequence via time-ordered UUID7, never `MAX+1`** — Pydantic AI + Prefect both use uuid7 so ordering is embedded without a DB sequence lock (`pydantic_ai.md` §11, `durable_exec.md` §8.4/§10.4).
- **Persist snapshots only on `pending|paused|suspended`, NEVER `running`** — clobbering an existing suspended snapshot is Mastra's named footgun (`mastra.md` §1j, anti-pattern #4).
- **Default `InMemoryCheckpointer`** (dict) = tactical. `SqliteCheckpointer` (stdlib `sqlite3`, two tables: `runs` + `step_records`, index `(run_id, step_idx)`, `ON DELETE CASCADE` for one-DELETE cleanup — OpenAI's exact schema, `openai_agents.md` §6) = durable, zero-new-dep.

### B2. The `@step` bracket — side-effect skip on resume (the heart)
```python
async def step(ctx, kind, fn):
    rec = ctx._recorded(kind)            # look up by (run_id, step_idx)
    if rec is not None:
        ctx.step_idx += 1
        return rec.payload               # REPLAY: return recorded output, fn NOT called
    out = await fn()                     # FRESH: execute the side effect
    await ctx.checkpointer.append(ctx.run_id, [StepRecord(..., payload=out)])
    ctx.step_idx += 1
    return out
```
- This is the line that makes resume safe: an LLM call wrapped in `step(ctx, "llm", ...)` **re-fires on a fresh run, is skipped on a replay**. Strands' "model phase SKIPPED on resume, saved message reused" (`strands.md` §1c/§1d) and LangGraph's "only tasks with no writes re-execute" (`langgraph.md` §2.2) are the same mechanism.
- **Sync-default rationale:** the bracket guarantees that on replay the recorded output is returned *before* `fn` can run, so the "async re-fires the LLM → corrupt log" failure is structurally impossible on the replay path. Concurrent fan-out (`TransportPool.fanout`) is allowed *inside* a single step whose aggregate output is recorded atomically.

### B3. `ctx.hitl.request` + `await_condition` — durable promise, checkpoint-BEFORE-pause
```python
async def request(self, schema: type[BaseModel]) -> BaseModel:
    rid = _det_id(self.run_id, self.step_idx, "hitl")   # xxh3/sha — idempotent across replays
    rec = self._recorded_response(rid)
    if rec is not None: return schema(**rec)             # resumed: return human's typed answer
    await self.ctx.checkpointer.append(self.run_id, [snapshot()])   # CHECKPOINT FIRST
    await self.ctx.checkpointer.put_pending(self.run_id, PendingRequest(rid, schema.model_json_schema()))
    raise _Suspend(rid)                                  # unwinds to the run boundary → HTTP 202
```
- **Checkpoint-before-raise is the load-bearing ordering** (`langgraph.md` §2.3). Resume = `resume(run_id, response)` → the `request` call returns the value instead of raising. Approval keyed deterministically; `(tool_name, call_id)` for tool approvals (`openai_agents.md` §5).
- **Typed HITL object, NOT a chat box** — Palantir's 2yr finding: emit a Pydantic schema the human fills (form/dropdown/upload generated from `model_json_schema()`), the agent detects the fill via the resume. "The ontology is how you encode opinion around exactly the information that should pass between the human and the agent" (`transcripts_stanford_palantir.md` §8). Lancelot's `@app.tool` already produces these schemas — reuse the machinery.
- `await_condition(name, predicate)` is the same suspend mechanism gated on a named typed value becoming non-null (`transcripts_stanford_palantir.md` §7) — "pause until true, zero background cost."
- **Tool-approval as a graph edge, not a bespoke checkpoint** — Pydantic AI's minimal design: resume jumps straight to tool execution, bypassing the model; `override_args` lets the human edit args before execution (`pydantic_ai.md` §10/§16). Copy this shape.

### B4. `submit / attach / cancel` on the byte path
- **`submit`** → run starts, returns `202 + {run_id}`. If it suspends (HITL/condition/budget), the 202 carries the `PendingRequest` schema.
- **`attach`** → `@app.stream` SSE with a `seq_id` resume cursor: assign monotonic `seq_id` per emitted chunk, accept `?starting_after=N`, tail-poll every 100 ms when caught up, stop on a `complete` sentinel (Letta's resumable-SSE writer/reader, `letta.md` §8). For background runs that outlive the client, the durable log IS the replay source — no Redis required for v1 (use the checkpointer; document Redis Streams as the scale path, `letta.md` §8).
- **`cancel`** → `generator.athrow(RunCancelled(run_id))` so the agent loop's own `try/except`/`finally` fires and partial state checkpoints; distinguish `RunCancelled` (user cancel → emit `event: cancelled`, exit 200) from `asyncio.CancelledError` (client disconnect → re-raise for ASGI teardown) (`letta.md` §10). `[DONE]`/terminal-sentinel guarantee: detect a missing terminal frame and synthesize it (`letta.md` §12).
- **`StreamingResponseWithStatusCode` / survive-client-disconnect:** for background durable runs, `asyncio.shield` the run coroutine so a client disconnect does NOT abort the agent — it keeps running and checkpointing, just stops sending bytes (`letta.md` §11). This is the Python-ASGI-wrapper layer above the R3 pump (the Rust pump can't see the first chunk's status).

### B5. Budget + StopReason (typed termination, not exceptions)
- **`StopReason` enum** — copy verbatim, the union is convergent: `end_turn`, `max_steps`, `max_llm_calls`, `max_budget_usd`, `tool_rule`, `error`, `cancelled`, `context_window_exceeded`, `rate_limit_exceeded`, `hitl_pending`, `condition_pending` (Letta 11-variant `letta.md` §3; Claude Code 5-subtype incl. `error_max_budget_usd` `agentic_deep.md` §7; Mastra 10-state `mastra.md` §1j).
- **Budget read from `lancelot.context.cost_usd`** — it already accumulates across all sub-LLM calls through TransportPool (`lancelot_model_io.md` §2.4, `lancelot_serve_tools.md` §4). The gate is `if cost_usd.get() > max_budget_usd: stop(StopReason.max_budget_usd)`. `total_cost_usd` reported on EVERY terminal including errors (`agentic_deep.md` §7).
- **Graceful budget, not a cliff:** stop one step BEFORE the limit when a tool call is pending (`remaining < 2 and has_tool_calls` — a tool call needs ≥1 step to run + 1 to synthesize; `langgraph.md` §2.1/§2.7). Emit a clean final message, not `RecursionError`.
- **Default constants** (§5 below).

### B6. Resume-safe streaming-state capture
A suspended *streaming* run must resume mid-stream: capture `__streamState` in the suspend payload, re-inject as the initial stream state on resume (Mastra `resumeStream` `mastra.md` §1h). For v1 this can be coarse (re-run the streaming step from its start on resume, since the step bracket records the final aggregate); record the finer `__streamState` only if mid-stream resume becomes a real requirement.

---

## 3. REUSE (byte path already provides — do NOT rebuild)

| Need | Byte-path primitive | Cite |
|---|---|---|
| The LLM call inside `step()` (retry, Retry-After, budget cap, cancel, idempotency-key) | `TransportPool.request` — POST never retried; idempotency-key stable across retries | `lancelot_model_io.md` §1.1/§1.5 |
| Cost/token accumulation feeding `max_budget_usd` | `lancelot.context.cost_usd`/`tokens_in`/`tokens_out` + `upstream_metrics`; `X-Lancelot-Cost-USD` trailer | `lancelot_model_io.md` §2/§3 |
| Streamed-usage capture for budget on streaming steps | `pool.stream(track_usage=True)` + `_scan_last_data_frame` | `lancelot_model_io.md` §1.3 |
| Error taxonomy → retry/escalate/HITL branch | `classify_status` → `ok/quota/timeout/permanent/transient/upstream_5xx` (422=permanent=fix-the-input) | `lancelot_model_io.md` §2.3 |
| `attach` live stream (backpressure, keepalive, TTFT, disconnect→cancel) | R3 pump + `@app.stream` + `Event`/`sse_adapter`/`_clean_field` | `lancelot_serve_tools.md` §1 |
| Cancel that runs the agent's `finally` | `cancel_on_disconnect` + R3 Closed-channel upstream cancel; pair with `generator.athrow` | `lancelot_serve_tools.md` §11, `letta.md` §10 |
| HITL typed-object schemas | `@app.tool` → `ToolSpec` auto JSON schema; `app.tool_schemas()` | `lancelot_serve_tools.md` §2 |
| Snapshot serialization | pydantic-core `to_json` (the fast path; explicitly preferred over JsonPlus/Msgpack) | `langgraph.md` §8 |
| Cache-stable prompt bytes for replay determinism | `lancelot.canonical.dumps_canonical` (sorted-key, `allow_nan=False`) | `lancelot_model_io.md` §4 |
| Sub-agent context propagation across a suspend/resume task boundary | `TransportPool.spawn_child` snapshots traceparent/request_id/agent_id at construction (survives ContextVar reset) | `lancelot_model_io.md` §1.8 |
| `submit` 202 + background daemon lifetime | `@app.lifetime_task` (supervised, drain-bounded) | `lancelot_serve_tools.md` §11 |
| HITL callback to user-supplied URL (signed, replay-safe) | `lancelot.webhook.sign`/`verify` (Standard Webhooks) + `lancelot.net.assert_safe_url` (SSRF) | `lancelot_serve_tools.md` §6/§10 |
| Per-tenant durable-run admission | `TenantConcurrencyLimiter` + `RateLimit`; `tenant_id` ContextVar | `lancelot_serve_tools.md` §8/§9 |
| Distributed trace across a resumed run | `traceparent` auto-inject + `context.trace_id` | `lancelot_model_io.md` §2 |

The transport/streaming/resilience half of durable execution is **done**. The durable layer is purely the *application-state* half: the event log, the step bracket, the suspend/resume promise, and the HTTP submit/attach/cancel surface that rides the existing byte path.

---

## 4. REFUSE (off-thesis / overkill / depreciating)

| Refuse | Why | Cite |
|---|---|---|
| **Graph DSL** (`StateGraph.add_node/add_edge/compile`, Pregel/BSP) as the durable substrate | The loop is a `while`/`.dowhile`, not a graph. BSP is "3 phases of infrastructure per turn for what is functionally a while loop with a tool dispatcher" — pure overhead for the common single-path ReAct case. Optional viz surface at most, never the core. | `langgraph.md` §1/§3.2; `agentic_deep.md` §1 |
| **VM/hypervisor snapshot** (blockdiff, Firecracker mem-snapshot) as the durable primitive | Wrong layer — it's filesystem/hypervisor infra below Lancelot, and it is NOT event-sourced: "a crash mid-write leaves the VM in an undefined state that `blockdiff apply` cannot recover from. For agent frameworks that need replay, blockdiff is NOT the right primitive." | `durable_exec.md` §1/§10.2 |
| **Mandatory Postgres + pgvector + Alembic** (Letta's 60+ migrations) | Overkill; default in-memory + stdlib-sqlite covers 95%. Postgres is a lazy-imported opt-in backend, never a runtime dep. | `letta.md` anti-patterns; cardinal rule |
| **Inngest/Temporal-coupled "evented engine"** | "Inngest coupling is tactical. The in-process engine with a Postgres snapshot store covers 95% of use cases and is much simpler." | `mastra.md` hype-flags |
| **`.waitForEvent` as a primitive** | Removed in Mastra in favor of suspend/resume; the durable promise (`hitl.request`/`await_condition`) subsumes it. | `mastra.md` §1g, anti-pattern #5 |
| **Persisting `running`-status snapshots** | Clobbers an existing suspended snapshot — only persist on `pending|paused|suspended`. | `mastra.md` §1j |
| **Per-chunk JSON parse of tool args on the replay path** | Corrupts mid-stream tool calls and breaks deterministic replay; accumulate-and-parse-once. | `strands.md` §2a |
| **`MAX+1` sequence for event ordering** | Use uuid7/time-ordered ids; no DB sequence lock. | `durable_exec.md` §10.4; `pydantic_ai.md` §11 |
| **`pydantic-graph` (or any) as a new dependency** | Copy the ENGINE DESIGN in ~300–600 LoC; the full package is ~5k LoC of Temporal/DBOS/Mermaid/Logfire none of which Lancelot needs. Cardinal rule: pydantic-only. | `pydantic_ai.md` §15 |
| **Chat-box HITL** | Palantir 2yr finding: a text box exposes LLM ambiguity to the human; typed objects are the durable pattern. | `transcripts_stanford_palantir.md` §8 |
| **Cross-language schema-versioned durability as a v1 goal** | "Cross-language agent durability is aspiration, not production reality" (RunState 1.10 Py vs 1.12 TS, not interop). Single-runtime first. | `openai_agents.md` honest-flags |
| **Reintroducing per-chunk cross-thread bridging in the attach stream** | The R3 on-loop `_drain`/`try_push` design is what makes streaming fast; per-chunk `call_soon_threadsafe` = the 16–26× TIER-D cliff. | `lancelot_serve_tools.md` anti-pattern #1 |

---

## 5. Recommended API sketch + exact default constants

```python
from lancelot.durable import durable_agent, StopReason, InMemoryCheckpointer, SqliteCheckpointer

@durable_agent(
    checkpointer=InMemoryCheckpointer(),   # default = tactical; swap for durable=True
    max_steps=50,
    max_llm_calls=50,
    max_budget_usd=None,                    # None = no cap; reads lancelot.context.cost_usd
)
async def support_agent(ctx, task: str):
    # every side effect goes through ctx.step — recorded once, skipped on replay
    plan = await ctx.step("plan", lambda: ctx.llm(messages=[...], tools=app.tool_schemas()))

    # durable promise: checkpoint-before-pause, returns the human's typed answer on resume
    if plan.needs_human:
        approval: Approval = await ctx.hitl.request(Approval)   # Approval is a pydantic model
        if not approval.ok:
            return ctx.stop(StopReason.end_turn, "declined")

    # await-condition: suspend with zero background cost until a typed value lands
    doc = await ctx.await_condition("doc", lambda s: s.uploaded_text is not None)

    for tool_call in plan.tool_calls:
        # tool execution recorded; budget gate trips to a clean StopReason, not a crash
        await ctx.step(f"tool:{tool_call.name}", lambda: app.call_tool(tool_call.name, **tool_call.args))
    return ctx.finish(plan.answer)

# submit / attach / cancel ride the byte path:
app.mount_durable(support_agent, path="/agent")
#   POST /agent            -> 202 {run_id}              (suspends -> 202 + PendingRequest schema)
#   GET  /agent/{id}/stream?starting_after=N  -> @app.stream SSE replay from seq_id cursor
#   POST /agent/{id}/resume  {response}        -> re-enters; hitl.request returns the value
#   POST /agent/{id}/cancel                    -> generator.athrow(RunCancelled) -> finally runs
```

**Default constants (all overridable via kwarg + `LANCELOT_*` env, per the env-knob convention):**

| Constant | Default | Source / rationale |
|---|---|---|
| `max_steps` | **50** | Letta `DEFAULT_MAX_STEPS=50` (`letta.md` §3); Pydantic AI request-limit 50 (`pydantic_ai.md` §13). Convergent. NOT LangGraph's 10007 silent-unbounded (`langgraph.md` §3.5). |
| `max_llm_calls` | **50** | matches `max_steps`; one LLM call per step in the common loop |
| `max_budget_usd` | **None** (no cap) | hard-cap is opt-in; when set, gate reads `lancelot.context.cost_usd` (`agentic_deep.md` §7) |
| graceful-budget threshold | stop when `remaining_steps < 2 and has_tool_calls` | a tool call needs ≥2 steps (`langgraph.md` §2.1/§2.7) |
| per-tool retries (self-heal on bad args) | **1** | Pydantic AI `default_max_retries=1`; structured `ValidationError.errors()` IS the retry prompt (`pydantic_ai.md` §2) |
| step-call backoff (transient) | `0.5s → ×2 → cap 128s`, `max_attempts=3`, jitter `uniform(0,1)` | LangGraph RetryPolicy (`langgraph.md` §2.5); cross-check vs Strands `4→240s, 6 attempts` (`strands.md` §1a) — Lancelot's `TransportPool` already owns transport-level backoff; this is loop-level re-issue |
| HITL request id | `xxh3/sha(run_id, step_idx, ns)` | deterministic/idempotent across replays (`langgraph.md` §2.3) |
| event-id ordering | uuid7 (time-ordered) | `pydantic_ai.md` §11; no `MAX+1` |
| snapshot persist statuses | `pending`, `paused`, `suspended` only | never `running` (`mastra.md` §1j) |
| attach SSE poll interval (caught-up) | **100 ms** | Letta resumable-SSE reader (`letta.md` §8) |
| zombie-stream silence kill | **30 min** | `max_stream_silence` (`letta.md` §9) — pairs with existing `@app.stream max_stream_silence` |
| default checkpointer | `InMemoryCheckpointer` (dict) | tactical default; durable = swap to `SqliteCheckpointer` (stdlib) |
| serializer | pydantic-core `to_json` | `langgraph.md` §8 |
| graceful exit message (budget) | "Sorry, need more steps to process this request." | LangGraph hardcoded string (`langgraph.md` §6) — or a Lancelot-branded equivalent |

---

## 6. Open questions (for the build phase)

1. **Where does `ctx.step_idx` live for safe replay under concurrency?** A monotonic counter in a request-scoped ContextVar works for the sync-default path; concurrent steps inside one bracket need a stable child-index scheme (Pydantic AI `sorted(call_index)`, `pydantic_ai.md` §5). Resolve before allowing fan-out inside a step.
2. **Native server vs durable runs.** Metrics + the ASGI seam fire only on the Python path; the native Rust `_native.run` emits no OTel and can't host middleware (`lancelot_serve_tools.md` §4). Durable runs must route through the ASGI/uvicorn seam — document this as a hard constraint (the const-tier is for fixed bytes, not agent runs).
3. **Multi-worker checkpoint sharing.** `InMemoryCheckpointer` is per-process; a `submit` on worker A and `resume` on worker B needs a shared backend (SQLite-on-shared-volume, or Postgres). Document the single-worker-default / shared-backend-for-scale boundary, same shape as the MCP session-store gap (`lancelot_serve_tools.md` §3).
4. **Content-addressed blob sharing** (LangGraph §2.2) — worth it for long runs but adds a join to the SQLite path. Ship the simple full-record log first; add content-addressing only if checkpoint size becomes a real cost.
5. **`__streamState` mid-stream resume granularity** — coarse (re-run the streaming step) vs fine (Mastra `resumeStream`). Default coarse; revisit if mid-stream resume is demanded.
6. **Relationship to the loop/memory layers.** The durable seam is the `step()` bracket *inside* the agent loop; the loop's `StopReason`/budget overlap with this brief's B5 — coordinate so there is ONE `StopReason` enum and ONE budget gate shared across the loop and durable layers, not two.

---

## 7. Honest verdict

This layer is **durable, not depreciating** — it's a state-machine property that survives model generations (`00_LENS.md` §5 weighting). It is the **#1 missing primitive** in Lancelot's agent half by triple consensus (`lancelot_model_io.md` §6, `langgraph.md` §7, `pydantic_ai.md` §14, `transcripts_stanford_palantir.md` §closing-table). The byte path already owns the *entire transport/streaming/resilience half* of it — so the build is small and surgical: an event log, a step bracket, a checkpoint-before-pause promise, and an HTTP surface that rides primitives Lancelot already shipped. The thesis (one loop, `durable=True` flips the runtime, byte-identical off) is not aspirational — it is the exact design four independent production frameworks converged on. Build the seam; default it to in-memory; keep it pydantic-only.
