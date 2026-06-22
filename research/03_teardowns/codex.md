# Codex CLI + Desktop — Mechanism-Level Distillation

Source cluster: `researchfms/teardowns/CODEX.md` (7145 lines, 4 RE rounds) + `researchfms/teardowns/CODEX_DESKTOP.md` (1617 lines).
Focus: agent loop, context budget, auto-compact, AGENTS.md walk, Code Mode isolate, ToolSearch deferred loading, parallel RwLock.

---

## 1. Budget-only sampling loop (`run_turn`)

**File:** `codex-rs/core/src/session/turn.rs`
**Source:** CODEX.md §1.2, §1.3, Round-N deep pass

```
run_turn(sess, turn_context, input, prewarmed_client_session, cancellation_token)
  ├── pre-sampling: run_pre_sampling_compact (if near context limit)
  ├── loop:            ← NO max_iterations, unbounded
  │     ├── drain pending_input
  │     ├── build Prompt { input, tools, parallel_tool_calls, base_instructions }
  │     ├── run_sampling_request:
  │     │     ├── stream = client_session.stream(...)
  │     │     ├── in_flight = FuturesOrdered::new()   ← parallel tool futures
  │     │     ├── loop over stream events:
  │     │     │     ├── OutputItemDone → tool_future pushed to in_flight
  │     │     │     └── Completed → drain in_flight, return SamplingRequestResult
  │     │     └── return { needs_follow_up, last_agent_message }
  │     ├── if token_limit_reached && needs_follow_up:
  │     │     → run_auto_compact(CompactionReason::ContextLimit, MidTurn)
  │     │     → client_session.reset_websocket_session()
  │     │     → continue
  │     ├── if !needs_follow_up: run stop hooks; break
  │     ├── CancellationToken cancelled → CodexErr::TurnAborted → break
  │     └── Fatal/CodexErr propagates out
```

**Termination:** token budget exhausted + compaction + retry; or `!needs_follow_up` (model stops calling tools); or `CancellationToken`. No hardcoded `max_iterations` constant anywhere in `run_turn`.

**Stop hooks:** `run_turn_stop_hooks` fires on `!needs_follow_up`. A hook can return `continuation_fragments` to force another sampling round (blocking the turn from ending). This is the "hooks GA" mechanism — a `Stop` hook can extend the agent's work.

**Lancelot lens:** TransportPool already owns the outbound HTTP call; it does NOT own the sampling loop shell. The loop logic above (unbounded-iteration-until-no-tool-call, pre/mid-turn compact, stop hooks, `CancellationToken`-based abort) is the exact missing "agent loop" primitive from `00_LENS.md`. Copy the shape; wire `TransportPool.stream()` as the inner streaming call.

---

## 2. Token budget accounting and auto-compact trigger

**Files:** `session/token_budget.rs`, `state/auto_compact_window.rs`, `compact.rs`
**Source:** CODEX.md §3.4, CODEX_DESKTOP.md §3

```rust
// token_budget.rs
TOKEN_BUDGET_USAGE_THRESHOLDS = [25, 50, 75]   // % events driving the context meter UI
effective_context_window_percent = 95           // fire compact at 95% of window
context_window = 272_000                        // for gpt-5 family

// auto_compact_window.rs
AutoCompactWindow { prefill_input_tokens: ServerObserved | Estimated }
// Decision: auto_compact_scope_tokens (since window baseline)
//        vs auto_compact_scope_limit
// Plus hard full_context_window_limit check
```

**The trigger in `run_turn`:**
```rust
let auto_compact_limit = model_info.auto_compact_token_limit().unwrap_or(i64::MAX);
// ... inside loop after sampling:
if token_limit_reached && needs_follow_up {
    run_auto_compact(..., CompactionReason::ContextLimit, CompactionPhase::MidTurn).await?;
    client_session.reset_websocket_session();
    continue;
}
```

`auto_compact_token_limit` returns `None` for models with no configured context window (→ `i64::MAX` = disabled). The `effective_context_window_percent = 95` scalar is per-model config; 272k is the gpt-5 family window.

**Pre-turn compact** also runs before the first sampling request if the session is already near the limit (`run_pre_sampling_compact`). Mid-turn compact fires inside the loop. So there are two trigger points.

**Lancelot lens:** `lancelot.context` tracks `tokens_in/out` ContextVars already; it does NOT implement the `AutoCompactWindow` ring-buffer logic or the pre/mid-turn compact decision. That logic is the `context engineering` primitive needed. The 95% threshold and the "ServerObserved vs Estimated prefill" accounting are directly copy-worthy.

---

## 3. Compact prompt (verbatim)

**File:** `codex-rs/core/templates/compact/prompt.md`
**Source:** CODEX.md §3.4

```
You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff summary for
another LLM that will resume the task. Include: Current progress and key decisions
made; Important context, constraints, or user preferences; What remains to be done
(clear next steps); Any critical data, examples, or references needed to continue.
Be concise, structured, and focused on helping the next LLM seamlessly continue
the work.
```

**Summary prefix** (`templates/compact/summary_prefix.md`, injected so the resuming model knows it's reading a handoff):
> "Another language model started to solve this problem and produced a summary of its thinking process. You also have access to the state of the tools that were used… Use this to build on the work that has already been done and avoid duplicating work."

Items beginning with this prefix are recognized as compaction summaries (`compact.rs:486`). Both prompts are overridable via `config.compact_prompt`.

**Remote compaction v2 retained-message rule** (`compact_remote_v2.rs:437-470`): after the server's `compaction_output` arrives, `build_v2_compacted_history` keeps only items that are `Message` with role `user|developer|system` AND pass `should_keep_compacted_history_item`, truncated to `RETAINED_MESSAGE_TOKEN_BUDGET = 64_000` tokens, then appends the compaction output. Assistant/tool history is replaced; recent user instructions survive.

**Lancelot lens:** This is the exact `observation-masking compaction` primitive missing from the Lancelot agent half. The local compact prompt (8 lines) is the minimal viable form — the user's instructions survive; the agent's working memory is replaced by a handoff summary.

---

## 4. AGENTS.md hierarchical walk

**File:** `core/src/agents_md.rs`
**Source:** CODEX.md §3.3, Round 2

```rust
pub const DEFAULT_AGENTS_MD_FILENAME: &str = "AGENTS.md";
pub const LOCAL_AGENTS_MD_FILENAME: &str = "AGENTS.override.md";
const AGENTS_MD_SEPARATOR: &str = "\n\n--- project-doc ---\n\n";

// Walk order: git project root → down to cwd
// Root-first, cwd-last (recency = later in prompt)
// Files exceeding remaining budget are truncated, not skipped
// Feature::ChildAgentsMd injects explanation of the hierarchy into the prompt
```

**Budget:** `project_doc_max_bytes` (config). Files are read in order; each uses from the remaining budget. A file that doesn't fit is truncated (not dropped), so the top bytes of each file still land in context.

**Personal override:** `AGENTS.override.md` (the `LOCAL_AGENTS_MD_FILENAME`) sits alongside `AGENTS.md` and is loaded last — it overrides without mutating the checked-in instructions. Useful for operator-local additions.

**SKILL system** (`core-skills/src/loader.rs`):
```rust
const MAX_SCAN_DEPTH: usize = 6;               // from skills root
const MAX_SKILLS_DIRS_PER_ROOT: usize = 2000;
const MAX_NAME_LEN: usize = 64;
const MAX_DESCRIPTION_LEN: usize = 1024;
const DEFAULT_SKILL_METADATA_CHAR_BUDGET: usize = 8_000;
const SKILL_METADATA_CONTEXT_WINDOW_PERCENT: usize = 2; // skills ≤ 2% of context window
const APPROX_BYTES_PER_TOKEN: usize = 4;
```

Skills are enumerated at startup; only metadata (name + description) is injected. Full skill content is lazy-loaded on invocation.

**Lancelot lens:** Lancelot's project-level `CLAUDE.md` plays the same role. The walk pattern (git-root → cwd, root-first → recency), budget truncation (not drop), and personal override file are all directly adoptable for a Lancelot `ContextBundle` assembly primitive.

---

## 5. Parallel tool RwLock

**File:** `codex-rs/core/src/tools/parallel.rs`
**Source:** CODEX.md §1.1, §1.8

```rust
// Within run_sampling_request → handle_output_item_done:
let _guard = if supports_parallel {
    Either::Left(lock.read().await)   // read lock = concurrent
} else {
    Either::Right(lock.write().await) // write lock = exclusive
};
// The future is tokio::spawn'd with this guard held;
// in_flight: FuturesOrdered drives all concurrent tool futures.
```

**MCP server declaration:**
```toml
[mcp_servers.docs]
command = "docs-server"
supports_parallel_tool_calls = true  # enables read-lock parallelism for this server
```

Tools marked `supports_parallel_tool_calls = true` acquire a read lock; they run concurrently with each other. Tools that are stateful / mutating acquire the write lock (exclusive). The lock is a single `Arc<RwLock<()>>` per turn — not per-tool.

**`FuturesOrdered`** (not `FuturesUnordered`) is used so tool results are delivered to the model in the order the model requested them, even though they complete concurrently.

**Lancelot lens:** `TransportPool.fanout(coros, first_n, deadline, cancel_laggards, concurrency)` handles the fan-out case. For the WITHIN-a-turn parallel-dispatch case (N tools from one model response), `FuturesOrdered` + `Arc<RwLock<()>>` is the exact primitive to copy. Lancelot doesn't have a tool dispatch layer yet; this is the correct concurrency shape when we build one.

---

## 6. Deferred tool loading — `tool_search` / `ToolSearchHandler`

**Files:** `tools/src/tool_search.rs`, `tool_search_spec.rs`, code-mode
**Source:** CODEX.md §4.7

**The mechanism:**

Tools with `defer_loading = Some(true)` are NOT sent to the model in the initial `tools` array. Instead the model receives a single `tool_search` tool:

```rust
create_tool_search_tool(limit_param: &str) // default_limit = 8 (tool_search_spec.rs:92)
// ToolSpec::ToolSearch { execution, description, parameters: { query, limit } }
```

When the model calls `tool_search({query, limit})`:
1. `ToolSearchHandlerCache::get_or_build` builds (and caches) a search index from `default_tool_search_text(name + description + schema property names)`.
2. Ranks deferred tools by text similarity against the query.
3. Returns the top-N matches; they become callable in subsequent turns.

**Code Mode integration** (`DEFERRED_NESTED_TOOLS_GUIDANCE`):
> "Some deferred nested tools may be omitted… filter `ALL_TOOLS` by `name` and `description`"

`ALL_TOOLS` is a global tool registry exposed inside Code Mode's V8 sandbox, allowing the JS runtime to introspect deferred tools and decide which to load.

**`ToolSpec` wire variants** (`tools/src/tool_spec.rs:17-53`):
- `Function(ResponsesApiTool)` — classic JSON-schema function
- `Namespace(ResponsesApiNamespace)` — grouped MCP namespace
- `ToolSearch { execution, description, parameters }` — deferred discovery
- `ImageGeneration { output_format }` — hosted gpt-image-1.5
- `WebSearch { external_web_access, ... }` — hosted web search
- `Freeform(FreeformTool)` — apply_patch and exec (no strict JSON schema)

**JsonSchema sanitization** (`tools/src/json_schema.rs`): MCP schemas are sanitized before rendering to the model — `additionalProperties`, `anyOf`/`oneOf`/`allOf`, `enum`, `const` are all normalized. This is what prevents arbitrary MCP-server schemas from breaking the Responses API's strict-schema expectations.

**Lancelot lens:** `lancelot.mcp.mount` already handles MCP tool registration; the 4KB-return WARN discipline (`lancelot.mcp`) is the minimal-return discipline Codex uses for tool output (`ToolSearchHandlerCache` similarly bounds result counts). The deferred-schema pattern + `ToolSearch` wire type is directly relevant to the future Lancelot tool dispatch layer when tool count grows large (i.e., many MCP servers connected). Not an immediate build target, but the exact shape to adopt.

---

## 7. Fork history filter (subagent context isolation)

**File:** `agent/control.rs`
**Source:** CODEX.md §5.3, Round-N pass

```rust
fn keep_forked_rollout_item(item: &RolloutItem) -> bool {
    match item {
        RolloutItem::ResponseItem(ResponseItem::Message { role, phase, .. }) => match role.as_str() {
            "system" | "developer" | "user" => true,          // always keep
            "assistant" => *phase == Some(MessagePhase::FinalAnswer), // ONLY final answer
            _ => false,
        },
        // Tool calls, tool outputs, reasoning, ghost snapshots → ALL DROPPED
        RolloutItem::ResponseItem(
            ResponseItem::Reasoning { .. }
            | ResponseItem::LocalShellCall { .. }
            | ...
        ) => false,
        ...
    }
}
```

When a subagent is spawned from an existing thread's history, this filter strips: tool calls, tool results, reasoning steps, ghost commit snapshots. It keeps: user/developer/system messages + the assistant's final-answer messages only. The subagent sees the cleaned narrative, not the full working history.

**Lancelot lens:** This is the `subagent context-isolation` primitive from `00_LENS.md`. When Lancelot's orchestration layer forks a history into a sub-agent, this filter pattern is the correct stripping logic.

---

## 8. Mailbox + `trigger_turn` — async agent wake

**Files:** `agent/mailbox.rs`, `protocol/protocol.rs`
**Source:** CODEX.md §5.3 (Round-N deep pass)

```rust
pub(crate) struct Mailbox {
    tx: mpsc::UnboundedSender<InterAgentCommunication>,
    next_seq: AtomicU64,
    seq_tx: watch::Sender<u64>,   // ← watch channel for seq-based wake
}

pub(crate) fn has_pending_trigger_turn(&mut self) -> bool {
    self.sync_pending_mails();
    self.pending_mails.iter().any(|mail| mail.trigger_turn)
}
```

`InterAgentCommunication.trigger_turn: bool`:
- `true` → the message wakes the recipient agent's sampling loop immediately (synchronous delegation)
- `false` → message is queued silently; the recipient processes it on its next natural turn (fire-and-forget)

`watch::Sender<u64>` provides the seq-based wakeup: subscribers `watch::changed()` to know when new mail arrives, without polling. `mpsc::UnboundedSender` delivers the actual message payload.

**CAS-based spawn slot reservation** (`agent/registry.rs`):
```rust
fn try_increment_spawned(&self, max_threads: usize) -> bool {
    let mut current = self.total_count.load(Ordering::Acquire);
    loop {
        if current >= max_threads { return false; }
        match self.total_count.compare_exchange_weak(
            current, current + 1, Ordering::AcqRel, Ordering::Acquire
        ) {
            Ok(_) => return true,
            Err(updated) => current = updated,
        }
    }
}
```

`AgentRegistry` (one per session tree) enforces `max_threads` via lock-free atomic CAS — no mutex, no queue, instant backpressure.

**Lancelot lens:** `TransportPool.spawn_child` already provides the parent-context-propagation side of subagent spawning. The Mailbox pattern (watch for wake + unbounded channel for delivery, `trigger_turn` flag for synchronous vs fire-and-forget) is the inter-agent coordination primitive we'd need when Lancelot grows a real subagent scheduler. The CAS registry is the correct concurrency shape for `max_threads` enforcement.

---

## 9. Memory pipeline (2-phase background summarization)

**Files:** `memories/write/src/phase1.rs`, `phase2.rs`, `state/src/lib.rs`
**Source:** CODEX.md §6, CODEX_DESKTOP.md §5

**Phase 1 — per-session extraction (gpt-5.4-mini, ReasoningEffort::Low):**
- Runs at root-session startup only; gates on `DEFAULT_MEMORIES_MIN_RATE_LIMIT_REMAINING_PERCENT = 25`
- Eligibility: age ≤ 10 days, idle ≥ 6 hours, not already claimed
- `THREAD_SCAN_LIMIT = 5_000`, `DEFAULT_MEMORIES_MAX_ROLLOUTS_PER_STARTUP = 2`
- Concurrency: `CONCURRENCY_LIMIT = 8` (`buffer_unordered(8)`)
- Output: `{ raw_memory, rollout_summary, rollout_slug }`, secrets redacted before storage
- Stored in `memories_1.sqlite` (`stage1_outputs` table); content on disk at `~/.codex/memories/`

**Phase 2 — global consolidation (gpt-5.4, ReasoningEffort::Medium, sandboxed):**
- Serialized: single global lock, 3600s lease, 90s heartbeat
- Takes top-256 stage-1 outputs ranked by `usage_count` then recency
- Ignores memories unused beyond 30 days
- Writes `phase2_workspace_diff.md` (git diff from last baseline, max 4 MiB)
- Spawns a sub-agent: no approvals, no network, local write only, collaboration disabled
- Sub-agent rewrites `MEMORY.md` / `memory_summary.md` / `skills/`
- Git dirtiness (not DB watermarks) decides whether Phase 2 runs

**Read path citations:** The model MUST append exactly one `<oai-mem-citation>` block as its last content. `parse_memory_citation` extracts per-line `<file>:<start>-<end>|note=[...]` entries and `<rollout_ids>` UUIDs. Cited rollout IDs increment `usage_count` — the citation loop drives future Phase 2 ranking.

**Lancelot lens:** `lancelot.memory_pool` is byte-OOM, not agent memory. The 2-phase background pipeline (cheap extraction model + medium consolidation sub-agent, git-as-versioned-filesystem, citation-driven ranking) is the `typed memory` primitive from `00_LENS.md`. The rate-gate (`≥25% quota remaining`) and the "not a sub-agent session" guard are production-critical guards worth copying verbatim.

---

## 10. Collaboration mode templates

**Files:** `collaboration-mode-templates/templates/plan.md`, `execute.md`
**Source:** CODEX.md §3.2 (Round-N pass)

**Plan mode (3-phase protocol):**

PHASE 1 — Ground: perform ≥1 non-mutating exploration before asking. Verbatim: *"Eliminate unknowns by discovering facts, not by asking."*

PHASE 2 — Intent chat: ask until goal + success criteria + audience + scope + constraints + current state are clear.

PHASE 3 — Implementation chat: ask until decision-complete (approach, interfaces, data flow, edge cases, testing, rollout). End state: `<proposed_plan>` block that *"can be handed to another engineer or agent to be implemented right away."*

**Execute mode verbatim constraint:**
> "spend only a few seconds on most turns and no more than 60 seconds when doing research; make assumptions, state them briefly in the final message, proceed"

**Switched by developer messages only** — collaboration mode is not switched by user messages; only `developer` role messages change the mode.

**Lancelot lens:** These are the mode-prompt templates for the agent's collaboration surface. Not a byte-path concern — but the 3-phase Plan protocol and the "developer-message-only mode switch" are both ergonomics worth copying into Lancelot's agent-facing API shape.

---

## 11. Code Mode (V8 JS sandbox — flag for Lancelot context)

**Files:** `code-mode/src/runtime/mod.rs`
**Source:** CODEX.md §3.6 (Round-N pass)

```rust
pub const DEFAULT_EXEC_YIELD_TIME_MS: u64 = 10_000;
pub const DEFAULT_WAIT_YIELD_TIME_MS: u64 = 10_000;
pub const DEFAULT_MAX_OUTPUT_TOKENS_PER_EXEC_CALL: usize = 10_000;
```

Code Mode runs a **V8-sandboxed JS runtime** (rusty_v8, V8 sandbox enabled). The model generates JS that calls `tools.<name>({...})` to invoke N tools in a single sampling round — no N round-trips. This is a fan-out-within-one-turn optimization.

**Not a copy target for Lancelot.** Lancelot's byte path provides no V8 runtime and has no reason to add one. The `FuturesOrdered` parallel dispatch (§5 above) achieves the same within-turn parallelism without a language sandbox. Flag: V8/Code Mode is OpenAI-specific infrastructure, not a durable primitive.

---

## 12. Ghost commits (flag for scope)

**File:** `codex-rs/utils/git/src/ghost_commits.rs`
**Source:** CODEX.md §1.5

```rust
const DEFAULT_IGNORE_LARGE_UNTRACKED_DIRS: i64 = 200;   // files
const DEFAULT_IGNORE_LARGE_UNTRACKED_FILES: i64 = 10 * 1024 * 1024;  // 10 MiB
const DEFAULT_IGNORED_DIR_NAMES: &[&str] = &[
    "node_modules", ".venv", "venv", "env", ".env",
    "dist", "build", ".pytest_cache", ".mypy_cache",
    ".cache", ".tox", "__pycache__",
];
```

Ghost commits are unreferenced git commits (no branch pointer) that snapshot working tree state for undo/rollback. `create_ghost_commit` + `restore_ghost_commit`. Default thresholds skip large untracked dirs and files.

**Lancelot lens:** Not a byte-path concern. Could be adopted by a Lancelot agent that manages a user's workspace; skip for now — not in scope for the transport-layer agent half.

---

## 13. Desktop IPC + goal-loop (architecture pointer)

**Source:** CODEX_DESKTOP.md §2, §4

The engine is spawned as `codex app-server --analytics-default-enabled` over stdio NDJSON JSON-RPC 2.0. ~190 v2 methods; 9 server→host request types (approval gates); 67 server→host notifications. Two DB split: `~/.codex/sqlite/codex-dev.db` (better-sqlite3, automations/inbox) vs `state_5.sqlite` (sqlx WAL mode, threads/memory/spawn-DAG).

**Goal loop:** `thread/goal/{set,get,clear,update}` RPCs. Continuation: turn goes idle → debounce → re-set `status: active` → engine drives another turn. This is the "autonomous continuation" pattern.

**Steering vs queue:** steering (`steeringUserMessage`) injects into the current turn; queue runs after turn completes and is ordered (failure blocks rest of queue). Two distinct interrupt channels.

**Streaming UI delta coalescing** (`targetCharsPerFrame = 24`, `maxDrainFrames = 8`): typewriter cadence. Not a Lancelot concern (Lancelot owns the byte path, not the UI).

**Automations:** `rrule TEXT NOT NULL DEFAULT 'FREQ=HOURLY;INTERVAL=24;BYMINUTE=0'`, two kinds (cron = standalone, heartbeat = thread wake-up). RFC-5545 RRULE. Not a Lancelot concern for now.

**Lancelot lens:** The stdio NDJSON JSON-RPC protocol shape and the steering/queue dual-channel are worth examining when designing Lancelot's durable seam (event-sourced checkpoint). The `trigger_turn` / steering distinction directly maps to "inject into active turn" vs "queue for next turn" — a clean binary that prevents race conditions.

---

## What to copy (priority order)

| # | Primitive | Exact mechanism | Why | Cite |
|---|---|---|---|---|
| 1 | Budget-only agent loop | `run_turn` shape: unbounded loop, `!needs_follow_up` OR `CancellationToken` terminates, NO `max_iterations` | The durable loop primitive; budget + cancel is the correct termination surface | CODEX.md §1.2-§1.3 |
| 2 | Pre/mid-turn compact | `effective_context_window_percent = 95`; `AutoCompactWindow { prefill: ServerObserved | Estimated }` | 95% trigger with ServerObserved prefill is the correct production-grade threshold — not naive token counting | CODEX.md §3.4 |
| 3 | Compact prompt (8 lines) | Verbatim from §3 above | Minimal viable compaction handoff; overridable via config; `RETAINED_MESSAGE_TOKEN_BUDGET = 64_000` for user instructions | CODEX.md §3.4 |
| 4 | AGENTS.md walk | git-root→cwd, root-first/cwd-last; truncate-not-drop on budget; `AGENTS.override.md` personal override; `--- project-doc ---` separator | Direct model for Lancelot `ContextBundle` assembly | CODEX.md §3.3 |
| 5 | Parallel RwLock | `Arc<RwLock<()>>` read=concurrent / write=exclusive; `FuturesOrdered` not `FuturesUnordered` | Correct ordering + parallelism shape for N-tool dispatch within one sampling round | CODEX.md §1.1, §1.8 |
| 6 | Fork history filter | Strip tool calls/reasoning/ghost-snapshots; keep user/developer/system + assistant final-answer only | Subagent context isolation without leaking working history | CODEX.md §5.3 |
| 7 | `trigger_turn` + Mailbox | `watch::Sender<u64>` for seq wake + `mpsc::UnboundedSender` for delivery; `true`=synchronous / `false`=fire-and-forget | Two distinct inter-agent communication modes with no polling | CODEX.md §5.3 |
| 8 | CAS spawn slot | `compare_exchange_weak` loop on `AtomicU64` for `max_threads` enforcement | Lock-free backpressure on concurrency limit | CODEX.md §5.4 |
| 9 | Memory rate-gate | `DEFAULT_MEMORIES_MIN_RATE_LIMIT_REMAINING_PERCENT = 25`; sub-agent guard | Never burn quota on background summarization when already tight | CODEX.md §6.2 |
| 10 | Plan mode 3-phase | Phase 1 = explore first (no asking), Phase 2 = intent until decision-complete, Phase 3 = impl until decision-complete | Developer-message-only mode switch; "developer-messages gate mode changes" is the safe surface | CODEX.md §3.2 |

---

## Anti-patterns

| Anti-pattern | Why |
|---|---|
| `max_iterations` termination | Codex explicitly has NONE. Loop terminates on budget/cancel/`!needs_follow_up`. A hardcoded iteration cap is an arbitrary ceiling that breaks long-running legitimate work. |
| Per-chunk tool validation in streaming | Codex validates tool schemas at registration/dispatch, not per-chunk during streaming. Per-chunk validation is a perf cliff (CLAUDE.md Tier-D). |
| `FuturesUnordered` for tool dispatch | Codex uses `FuturesOrdered` — model requested tools in order; results must arrive in that order to avoid confusing the next context. `FuturesUnordered` silently reorders. |
| Shipping model pricing tables | Codex's `telemetry` module removed hardcoded price tables after production issues. `lancelot.upstream_metrics` already follows this discipline (`LANCELOT_LLM_RATES` env). |
| Mode switching from user messages | Collaboration mode is switched ONLY by `developer` role messages — never `user` role. Allowing user messages to switch mode opens a prompt-injection surface. |
| Eager memory consolidation | Phase 2 runs only if the workspace has git-tracked changes. Running on every session startup would burn quota and thrash the memory files. |
| Compact without retained user instructions | Remote compact v2 explicitly keeps `RETAINED_MESSAGE_TOKEN_BUDGET = 64_000` tokens of user/developer/system messages. Collapsing those too discards the constraints the user actually cares about. |

---

## Byte-path implication

Lancelot's byte path (TransportPool, upstream_metrics, context ContextVars, stream, canonical, batch) provides the **outbound transport substrate** for every LLM call the agent loop makes. Specifically:

- `TransportPool.stream()` IS the `client_session.stream()` call inside `run_turn`. Wire it directly.
- `lancelot.context.tokens_in/out` are the raw counters; `AutoCompactWindow`'s ring-buffer + ServerObserved/Estimated accounting is the missing layer on top.
- `TransportPool.fanout(first_n, deadline, cancel_laggards)` is the byte-path analogue of `FuturesOrdered` tool fan-out — but fanout is for N-provider calls, not N-tool dispatches within one turn. Both are needed; they are complementary, not redundant.
- `TransportPool.spawn_child(parent, session_affinity)` propagates traceparent + agent_id into the child call — that is the `SubAgentSource::ThreadSpawn` propagation from the Mailbox/trigger_turn layer. The byte-path side is done; the scheduling/wake side (Mailbox, CAS registry) is not.
- `lancelot.canonical.dumps_canonical` directly serves prompt-cache stability (the KV-cache prefix stability that the AGENTS.md walk + stable ordering must preserve).

The agent loop, compaction, and context-assembly primitives are genuinely missing. The byte-path does NOT provide them and cannot be stretched to cover them.
