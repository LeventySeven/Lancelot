# External value-mine: agent frameworks + MCP → Lancelot agent-serving primitives

> Mandate: Lancelot is a **framework that serves agent backends**, not an agent framework. Default
> verdict **skip**. Only patterns Lancelot must *host/expose* count. The plan's `design/04 §1–5`
> (typed step events, SSE+side-channel POST, MCP Streamable HTTP 2025-06-18, durable
> step-checkpoint+replay, out-of-proc SessionStore, disconnect→abort) is already very deep — this
> doc only surfaces things that concretely *beat* or *sharpen* it. Sources: `teardowns/` + `core/`.

---

## 1. Verdict (5 lines)

1. **Plan does NOT need a structural change.** `design/04 §3.4` already commits to the right model (framework-owned step boundary, checkpoint+replay, SSE `id:` as the one resume cursor).
2. The plan describes durable mode at *design* level ("persist `(conversation_id, step_index, step_result)`"). LangGraph's **actual implementation** reveals the missing granularity: a **separate per-task pending-writes table** so a crash *mid-step* doesn't re-run/re-bill the parts that already finished. This is the one genuine sharpening — adopt it.
3. Three smaller concrete adds (content-addressed blob dedup; idempotent interrupt-id; `multitask_strategy` for concurrent runs on one conversation) — all cheap, all beat the plan's current silence on those exact edges.
4. MCP: nothing beyond `design/04 §2` except one Claude-Agent-SDK detail (in-process MCP server as a routed JSON-RPC callback, not a transport) worth a one-liner — Robyn's `mcp.py` shape + the plan's Streamable-HTTP productionization already covers the rest.
5. Everything else (ReAct loops, planner prompts, orchestration DSLs, handoff graphs, Pregel supersteps) is **agent-framework internals Lancelot must NOT host** — correctly out of scope.

---

## 2. Genuinely-useful findings (concrete, reimplementable, beats the plan)

### F1 — Durable agents need a per-task *pending-writes* table, not just per-step checkpoints

`file teardowns/LANGGRAPH.md` §2.4 (`pregel/_loop.py:268` `_reapply_writes_to_succeeded_nodes`), §R2.6 (verbatim Postgres DDL).

**Why it beats the plan.** `design/04 §3.4` checkpoints *after each completed step* (`(conversation_id, step_index, step_result)` persisted before yielding). That correctly handles "crash *between* steps." It does **not** handle "crash *during* a step that issued N parallel tool calls and 3 of 4 finished" — on replay the plan re-executes the whole step, re-calling 3 tools and **re-billing the model call that triggered them**. LangGraph solves exactly this with a second table written *as each task finishes*, separate from the step checkpoint:

```sql
-- the per-step state snapshot (plan already has the equivalent)
CREATE TABLE checkpoints (
    thread_id TEXT, checkpoint_ns TEXT DEFAULT '', checkpoint_id TEXT,
    parent_checkpoint_id TEXT, type TEXT, checkpoint JSONB, metadata JSONB,
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id));

-- the missing piece: per-task writes from the IN-PROGRESS step
CREATE TABLE checkpoint_writes (
    thread_id TEXT, checkpoint_ns TEXT DEFAULT '', checkpoint_id TEXT,
    task_id TEXT, idx INTEGER, channel TEXT, type TEXT, blob BYTEA,
    task_path TEXT DEFAULT '',                       -- deterministic replay order
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx));
```

On resume, before re-running the step, LangGraph re-applies the recorded writes of tasks that
*already completed* and only re-executes the uncompleted ones (`tick()` step 6: "resume: re-apply
prior writes for tasks that completed before crash"). Net effect: **a parallel-tool-call step is
idempotent at the individual-tool granularity, and the model call that produced the tool calls is
never re-billed.** That is strictly stronger than the plan's step-level granularity and is the exact
"stop paying for tokens" guarantee `design/04 §3.4` aims for, extended to the mid-step crash case.

**How to apply.** In `@app.agent(durable=True)`'s `DurableStore` ABC, add a second keyed surface
alongside the step checkpoint: `put_task_write(conversation_id, step_index, task_id, idx, blob)`
called *the instant each tool/model task resolves*, before the step checkpoint is finalized. Resume
algorithm: load step checkpoint → load its pending task-writes → for each task with a recorded
write, return the recorded result (do **not** re-invoke `app.llm` or the tool, do **not** re-bill) →
execute only tasks with no recorded write → finalize the step checkpoint → continue. `task_path`
(stable per Send/tool-call position) is what makes parallel-fan-out replay order deterministic — the
plan's §3.1 parallel-tool-calls (`app.gather(concurrency=…)`) needs this same stable index or replay
ordering is nondeterministic. ~1 extra table + 1 write per resolved task on the existing
SessionStore backend; reuses the §1.3 event-id as `idx`.

### F2 — Content-address checkpoint state by (key, version); blobs immutable, upsert DO NOTHING

`file teardowns/LANGGRAPH.md` §R2.6 (the `checkpoint_blobs` table + the `jsonb_each_text` join SELECT, verbatim DDL lines 1413–1483).

**Why it beats the plan.** The plan persists `step_result` per step. A long agent run (20+ steps,
growing message history) re-serializes and re-stores the *entire* conversation state every step →
O(steps²) storage and write bandwidth on the SessionStore hot path — a real cost for the durable
mode the plan wants on by default for production agents. LangGraph stores each state channel as a
**content-addressed immutable blob keyed `(thread_id, ns, channel, version)`** with
`ON CONFLICT DO NOTHING`; the checkpoint row stores only the `channel→version` map; a single SELECT
reconstructs full state by joining the version map against the blob table. Unchanged channels across
steps share one row — storage is O(distinct values), not O(steps × state size).

**How to apply.** When the plan's session/durable state has independently-versioned parts (message
history vs. tool-result cache vs. scratchpad), store each as an immutable
`(conversation_id, part, content_hash)` blob with insert-or-ignore, and make the per-step checkpoint
row a thin `{part: hash}` manifest. Resume = load manifest + fetch the referenced blobs. Cheap on
Redis (HSET by hash) or SQLite; eliminates the quadratic re-serialization. Direct upgrade to the
`SessionStore`/`DurableStore` ABC, no API change.

### F3 — Make the interrupt/resume id idempotent across replays (deterministic, not a fresh UUID)

`file teardowns/LANGGRAPH.md` §2.8: interrupt id = `xxh3_128_hexdigest(ns.encode())` — "the same
node always gets the same interrupt ID (idempotent across replays)"; §R2.4 uses a synthetic
`INTERRUPT` observer in `versions_seen` so "consecutive interrupts on the same checkpoint don't fire
twice." Corroborated `file teardowns/OPENAI_AGENTS_SDK.md` §Approval (`NextStepInterruption` →
`RunState.approve(call_id)` resumes by stable `call_id`, not position).

**Why it beats the plan.** `design/04 §3.3` routes interactive tool-approval via
`POST /agent/{conversation_id}/signal`, but the plan never says how the client/server *name* the
pending interrupt. If the id is positional or a fresh UUID per attempt, a resumed/replayed run
re-raises an already-answered interrupt (double human-approval prompt) or mis-correlates an approval
to the wrong tool call. The fix is one line of policy: **the interrupt/approval id is a
deterministic hash of `(conversation_id, step_index, tool_call_id)`**, so a replay produces the same
id and the framework can recognize "this interrupt was already answered, skip it." This closes a
correctness bug the plan's HITL design currently leaves implicit.

**How to apply.** In the `@app.agent` loop, when a tool needs approval emit a typed
`Interrupt(id=hash(conversation_id, step_index, tool_call_id), payload=…)` step; the side-channel
`POST …/signal` carries that exact id; on resume/replay, an interrupt whose id already has a recorded
resume value in the SessionStore returns the recorded value instead of re-raising. Same mechanism
serves SSE-reconnect, MCP `Last-Event-ID`, and crash-recovery — consistent with the plan's
"one resumability mechanism, three consumers."

### F4 — `multitask_strategy`: define what happens when a 2nd run hits a live conversation

`file teardowns/LANGGRAPH.md` §R7 SDK: `multitask_strategy: "reject" | "interrupt" | "rollback" |
"enqueue"` and `cancel(thread_id, run_id, action="interrupt"|"rollback")`,
`if_not_exists:"reject"|"create"`.

**Why it beats the plan.** The plan's SessionStore is keyed by `conversation_id` and assumes one
active run. It is silent on the very common production race: user double-submits, or a reconnect
fires a new run while the old one is still streaming on the same conversation. Without a defined
policy you get interleaved tool calls writing the same session state (the ADK
`ParallelAgent`-shared-state race, `file teardowns/GOOGLE_ADK.md` §State, is the same hazard
in-framework). LangGraph's four-value enum is the complete, battle-tested taxonomy:

- `reject` — 409 if a run is active on this `conversation_id` (safest default for a web framework).
- `interrupt` — cancel the active run, start the new one.
- `rollback` — cancel + discard the active run's uncommitted state, then start.
- `enqueue` — serialize: new run waits for the active one (FIFO per conversation).

**How to apply.** Add `@app.agent(on_concurrent="reject")` (default `reject`) honoring this exact
enum. Cheap: the SessionStore already has the `conversation_id` key; add a per-key active-run lease
(lease token + TTL) and branch on the strategy. Eliminates an entire class of
silent-state-corruption bugs the plan doesn't currently address.

### F5 — In-process MCP server = a routed JSON-RPC *callback*, not a 4th transport

`file teardowns/CLAUDE_AGENT_SDK.md` §3 control protocol: `mcp_message` — "the CLI is asking an
in-process SDK MCP server to handle a JSONRPC message; SDK routes to the named `Server` instance"
(`create_sdk_mcp_server`). Plus `interrupt` and `control_cancel_request` are first-class
out-of-band control messages with `request_id`s.

**Why it (mildly) beats the plan.** `design/04 §2` productionizes Robyn's `mcp.py` over Streamable
HTTP — correct for *remote* MCP clients. But Lancelot's `@app.agent` loop (§3.1) also calls the
*same tool registry* in-process. The Claude-SDK pattern is the clean abstraction: the in-process
agent loop doesn't open an HTTP/stdio transport to itself — it dispatches a JSON-RPC envelope
directly to the registered handler by name. The plan already says "one registry, two consumers
(`@app.mcp.tool` and `@app.agent`)" (§3.1) — this just confirms the *dispatch* should be a direct
in-proc JSON-RPC call sharing the wire schema, never a loopback transport (avoids a needless
serialize/socket round-trip on every in-process tool call). One design-note, not a structural change.

### F6 (minor) — Never persist partial/streaming events; one writer owns persistence

`file teardowns/GOOGLE_ADK.md` §Runner (`runners.py:954`): "persists ONLY non-partial events …
Partial events are never persisted, so a crash mid-stream doesn't corrupt the session"; "**the
Runner is the only persister** — agents yield events, agents never touch storage."

**Why it's worth one line.** A small correctness invariant for `@app.agent(durable=True)`: the
durable checkpoint must record only *finalized* step events (a completed tool result, a completed
model turn), never mid-token `model_delta` SSE frames. Otherwise resume replays a half-token state.
The plan's §3.4 implies this ("after each step … persist … *before* yielding") but doesn't state
the invariant. State it: **only the framework loop persists, and only non-partial step events** —
`model_delta`/token frames are stream-only, never checkpointed.

---

## 3. One-line-per-file "nothing useful beyond the plan"

- `teardowns/LANGCHAIN.md` — middleware stack (HITL/summarize/retry) + delegates all persistence to LangGraph; nothing Lancelot must host. **skip** (LangGraph covers the durable substrate).
- `teardowns/OPENAI_AGENTS_SDK.md` — `RunState` pickle-the-whole-run-and-resume + `Session.pop_item()` rewind-on-retry are the same checkpoint/replay idea as F1/F3, less granular (whole-run blob, no per-task table); confirms F3's stable-`call_id` resume. No new mechanism. **skip** (subsumed by F1/F3).
- `teardowns/CLAUDE_AGENT_SDK.md` — yielded F5; rest is CLI-subprocess control protocol / hooks, not framework-hostable. **skip beyond F5.**
- `teardowns/GOOGLE_ADK.md` — yielded F4-hazard + F6; long-running-tool "return None → suspend → resume" is just the plan's interrupt/resume by another name; MCP is consumer-side (3 transports, same as plan's §2). **skip beyond F4/F6.**
- `teardowns/SEMANTIC_KERNEL.md` — MCP has a 4th transport (WebSocket) beyond stdio/SSE/StreamableHTTP; plan already offers WS as an explicit `@app.agent(transport="ws")` opt-in (§3 note) so this is covered. Process state-machine is an orchestration DSL Lancelot must not host. **skip.**
- `teardowns/AUTOGEN.md` — `save_state`/`load_state` is *manual, opt-in, no per-step persistence, no time-travel* — explicitly weaker than the plan; distributed-runtime actor model is agent-framework internals. **skip.**
- `teardowns/DSPY.md` — "checkpoint" = optimizer minibatch full-eval cadence (training), unrelated to agent-run durability. **skip.**
- `teardowns/CREWAI.md` — checkpoint = "restart this kickoff where it crashed" (per-task output cache, no channel versions, no parallel-replay, no durable interrupts) — strictly weaker than F1; HITL is blocking `input()`. **skip.**
- `teardowns/OPENAI_SWARM.md` — explicitly stateless: "no threads, no checkpoints, no DB." Anti-pattern reference only. **skip.**
- `core/06_AGENTIC_ARCHITECTURES.md` — ReAct/plan-execute/event-stream taxonomy + planner prompts; agent-framework internals, prompt engineering (out of mandate). **skip.**
- `core/12_AGENTIC_AI_DEEP.md` — deep agent-loop/context-engineering patterns; nothing Lancelot must expose as a serving primitive beyond what `design/04` has. **skip.**
- `core/13_AGENTIC_AI_ARCHITECTURE_MAP.md` — confirms the categories the plan already covers (state survives retries/resumption; MCP = connector layer not memory; "state must survive partial failures") — validates F1/F4, no new mechanism. **skip (corroboration only).**
- `core/09_FRONTIER_AGENTIC_SYSTEMS.md` — frontier-lab loop/prompt/training detail; no framework-serving primitive. **skip.**

---

## 4. Report — does anything change the plan?

**Plan structure: NO change. Plan detail in `design/04 §3.3–3.4`: YES — 5 concrete sharpenings.**

The plan's agent-serving design is already correct and deep; these are precision adds that close
specific correctness/cost gaps the design states only at a high level:

1. **F1 (highest value)** — durable mode needs a **per-task pending-writes table** alongside the
   per-step checkpoint, so a crash *mid-step* (parallel tool calls / model+tools) doesn't
   re-execute or **re-bill** the parts that finished. Add `put_task_write(...)` to the
   `DurableStore` ABC; replay re-applies recorded task writes before re-running only the
   uncompleted tasks. Requires a stable `task_path`/index on `app.gather` fan-out.
2. **F2** — content-address durable state by `(part, content_hash)` with insert-or-ignore; the
   step checkpoint is a thin `{part: hash}` manifest. Kills the O(steps²) re-serialization of
   growing conversation state.
3. **F3** — interrupt/approval id = deterministic `hash(conversation_id, step_index,
   tool_call_id)`, not a fresh UUID; a replayed/answered interrupt returns its recorded resume
   value instead of re-prompting the human. Closes a HITL double-prompt/mis-correlation bug.
4. **F4** — add `@app.agent(on_concurrent=...)` with LangGraph's exact taxonomy
   `reject|interrupt|rollback|enqueue` (default `reject`) + a per-`conversation_id` active-run
   lease. Closes the double-submit / reconnect state-corruption race the SessionStore is silent on.
5. **F5/F6 (notes, no API change)** — in-process tool dispatch is a direct in-proc JSON-RPC call
   on the shared registry, never a loopback transport; and only the framework loop persists, only
   non-partial step events (token deltas are stream-only, never checkpointed).
