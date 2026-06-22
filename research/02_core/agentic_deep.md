# Agentic AI Deep — Mechanism-Level Distillation

**Sources:** `core/12_AGENTIC_AI_DEEP.md` (2603 lines, April 2026) + `core/09_FRONTIER_AGENTIC_SYSTEMS.md` (1186 lines)  
**Lens:** What does Lancelot's byte path already cover, change, or obviate? Extract only durable primitives — mechanism-level, cited.

---

## 1. The Agent Loop Is Not the Product

Every production system — Claude Code, Codex CLI, Manus — uses a 5-10 line while loop. The reference implementation from the Rust rewrite of Claude Code is 88 lines total including error handling (12_DEEP §2.2). The 10,000+ engineering lines are in:

1. Context management (what tokens are present)
2. Tool design (how the model interacts with the world)
3. Security / approval (what it can do)
4. Error recovery (what happens when things fail)

**Durable:** Loop shape is stable for 3+ model generations. Invest complexity in the harness.

**Lancelot:** Lancelot lacks the agent loop. Build it as ~10 lines; do not complicate it.

---

## 2. Context Engineering — The Concrete Mechanisms

Source: 12_DEEP §15, 09_FRONTIER §2

### 2.1 KV-Cache Discipline (Manus — Their #1 Metric)

Input:output ratio in agentic workloads is **100:1**. Cache optimization matters 100× more than output optimization. With Claude Sonnet: cached = `$0.30/MTok`, uncached = `$3.00/MTok` — a **10× cost difference** (09_FRONTIER §2.4).

Exact rules Manus ships:

1. **Static system prompt prefix** — never put timestamps/dynamic content before the cache boundary. Single-character difference invalidates entire downstream KV-cache.
2. **Append-only context** — never modify previous messages. All prior turns' KV-cache reused.
3. **Deterministic JSON serialization** — sorted keys for byte-identical prefixes. (`lancelot.canonical.dumps_canonical` does this already.)
4. **Session → consistent worker routing** — session ID routes to same vLLM worker → warm KV-cache reuse. Lancelot's `set_session_affinity` is this primitive.
5. **Logit masking instead of tool removal** — keep the tool list constant; mask unavailable tools during decoding. This keeps the KV-cache prefix intact. Dynamic tool add/remove = cache bust + model confusion. (09_FRONTIER §2.4)
6. **Claude Code cache boundary marker**: `SYSTEM_PROMPT_DYNAMIC_BOUNDARY = "<!-- DYNAMIC -->"` separates the static cached prefix from the dynamic suffix. The dynamic suffix includes: MCP server definitions, CLAUDE.md, memory index, active plan/TODOs. (12_DEEP §20.18)

**Lancelot implication:** `dumps_canonical` and `set_session_affinity` already cover rules 3 and 4. The agent layer needs to enforce rules 1, 2, and 5 (the dynamic boundary split and logit masking for tool availability). These are not HTTP-level primitives — they're agent harness concerns.

### 2.2 Observation Masking (Best Academic Finding)

From 12_DEEP §15.4: Keep full action and reasoning history; mask older **observations** with placeholders; retain recent 10 turns of full observations. Result: **52% cost savings with 2.6% solve-rate improvement**. Old tool outputs are rarely re-read; old reasoning informs current decisions.

This is a concrete compaction strategy distinct from full summary compaction. Implement as: after N turns, replace `tool_result` content with `"[cleared]"` but keep the `tool_use` record. Anthropic ships this as `clear_tool_uses_20250919` beta header (12_DEEP §15.2).

Tool-result clearing benchmark: 335K peak context → 173K peak (7 clearing events, zero API calls).

### 2.3 Multi-Phase Compaction (Not One Strategy)

From 09_FRONTIER §2.7 + 12_DEEP §2.5:

| Tier | Mechanism | Cost |
|---|---|---|
| MicroCompact | Surgically truncate high-token tool outputs, preserve cache boundaries | Zero API calls |
| Tool-result clearing | Replace old `tool_result` content with `"[cleared to save context]"` | Zero API calls |
| AutoCompact | LLM-generated ~20K token structured summary. Circuit breaker after 3 consecutive failures | 1 API call |
| Full compact | Compress everything; re-inject 5 most recently accessed files (5K token cap each) | 1 API call |

**Trigger**: AutoCompact fires at `ContextWindow - 13,000` tokens (~95% capacity) in Claude Code. Industry best practice (Zylos research, 09_FRONTIER §2.3) is **70% of context budget**. The 70% trigger prevents the context from ever reaching the expensive emergency tier.

**Circuit breaker**: Claude Code added this after 1,279 sessions hit 50+ consecutive AutoCompact failures, wasting ~250K API calls/day globally. Must implement (12_DEEP §2.5).

### 2.4 ACON: Failure-Driven Compression Guidelines

From 09_FRONTIER §2.5: Given paired trajectories where full context succeeds but compressed context fails, an LLM analyzes the cause and updates compression guidelines. Result: **26-54% memory reduction while preserving 95%+ accuracy**. This is an offline optimization pass, not a runtime primitive — run it on eval traces to tune what the MicroCompact and AutoCompact tiers preserve.

### 2.5 System Prompt Architecture (NOT Monolithic)

Claude Code dynamically assembles 110+ prompt strings, conditionally included (12_DEEP §2.3). The structural split:

```
Static prefix (maximizes prompt cache hits)
──── SYSTEM_PROMPT_DYNAMIC_BOUNDARY ────
Dynamic suffix:
  + environment-specific tools
  + MCP server definitions
  + CLAUDE.md / project rules
  + memory index
  + active plan/TODOs
  + skill schemas
```

**14 cache-break vectors** tracked with "sticky latches" preventing mode toggles from invalidating cache. Cache breaks = 10× token cost spike, so each break vector must be audited.

**Durable principle:** Keep the static prefix frozen. Put everything session-specific after the boundary.

---

## 3. Tool Design — The Non-Obvious Mechanics

Source: 09_FRONTIER §1.3-1.6, 12_DEEP §2.4

### 3.1 Deferred Tool Loading

**Problem:** 50+ MCP tools = ~72K tokens of tool definitions loaded upfront. This both consumes context and blows the KV-cache prefix on every session start.

**Solution:** `defer_loading: true` on tools. Only the ToolSearch tool itself (~500 tokens) loads. When the model needs a capability, it calls ToolSearch with a regex or BM25 query; the harness returns 3-5 relevant definitions. **85% context reduction: 72K → 8.7K tokens** (12_DEEP §2.4, 09_FRONTIER §1.4).

Two search variants:
- `toolSearchRegex_20251119` — Claude constructs Python `re.search()` patterns
- `toolSearchBM25_20251119` — natural language queries, BM25 scoring

**Lancelot relevance:** `lancelot.mcp.mount` already implements MCP tool registration. The deferred loading pattern is a tool *metadata* concern, not transport. The agent layer needs to implement the ToolSearch tool and lazy schema expansion.

### 3.2 Programmatic Tool Calling (37% Token Reduction)

From 09_FRONTIER §1.5: Claude generates Python code containing tool orchestration logic. The code executes in a sandboxed environment, pausing at tool calls. Tool results route to the **code executor**, not Claude's context. This prevents intermediate data (2,000+ items) from polluting the context — only the final `stdout` summary returns.

Token savings: 43,588 → 27,297 tokens (**37% reduction**). Latency savings: 20+ tool calls in one code block removes 19+ inference round-trips.

`caller` field in tool invocations identifies them as originating from code: `"type": "code_execution_20250825"`. Only tools with `allowed_callers` including this type can be invoked from scripts.

**Durable:** This pattern decouples "orchestration logic" from "inference passes." High-value for batch pipelines.

### 3.3 Response Format Parameter

From 09_FRONTIER §1.6:
```
DETAILED: 206 tokens — includes thread_ts, channel_id (IDs for chaining)
CONCISE:   72 tokens — no IDs, ~65% token savings
```

Let tools accept a `response_format` enum so the model can choose verbosity. The model knows whether it needs IDs for chaining or just a confirmation.

### 3.4 Tool Documentation Quality

From 09_FRONTIER §1.6: Tool examples improve accuracy from **72% to 90%** on complex parameter handling (Anthropic internal). Namespacing by service (`asana_projects_search`, `asana_users_search`) improves LLM routing. Truncation guidance should be actionable: "Try using `date_range` filter" not generic error codes.

**Lancelot:** `@app.tool` + `lancelot.mcp.mount` already auto-derive JSON schemas. The schema quality (examples, namespacing, truncation hints) is on the user. The tool for adding examples to `@app.tool` docstrings is the right intervention point.

---

## 4. Multi-Agent Coordination — The Three Durable Patterns

Source: 12_DEEP §14, 20

### 4.1 Pattern 1: Orchestrator-Workers With External Shared State

```
Lead (expensive model) → spawns N workers (cheap model)
Workers store output in EXTERNAL systems (files, artifacts)
Workers pass lightweight REFERENCES back to lead
Lead synthesizes
```

Anthropic's research system: Opus lead + Sonnet workers + Citation agent. Outperforms single Opus by **90.2%** on internal eval. Token economics: 15× more tokens than chat; token usage explains **80% of performance variance** (09_FRONTIER §6.1).

Known limitation (stated by Anthropic): "The lead can't steer subagents, subagents can't coordinate." The artifact pattern (external storage, lightweight refs) is the fix — workers are not required to communicate through the lead.

**Lancelot:** `TransportPool.fanout(first_n, deadline, cancel_laggards)` covers the parallel dispatch. `TransportPool.spawn_child(parent)` covers sub-agent context propagation. The agent layer needs the external artifact store primitive (simple file/KV write from workers, refs back to lead).

### 4.2 Pattern 2: Peer-to-Peer With Shared Task List (Claude Code Agent Teams)

```
Shared Task List: pending → in_progress → completed | blocked
  + dependency tracking (blocked tasks cannot be claimed)
  + file-locked claiming (atomic mkdir POSIX — exactly one agent claims each task)
Peer-to-Peer Mailbox: any → any, or broadcast
  No routing through lead — eliminates lead bottleneck
```

Atomic task claiming (12_DEEP §20.19): `mkdir .coord-<SESSION>/task-N.json/` — POSIX mkdir is atomic, so exactly one agent wins. Winner writes claim `{ "agent": "<id>", "worktree": "...", "claimedAt": "..." }`.

Recommended scale: 3-5 teammates, 5-6 tasks per teammate (12_DEEP §2.8).

**Lancelot:** This is a pure application-layer coordination pattern. Lancelot's `lancelot.context` ContextVars (`agent_id`, `tenant_id`) and `/fleet/status` cover observability. The task list itself is a small, buildable primitive.

### 4.3 Pattern 3: Cross-Model Review (Cheapest Multi-Agent Pattern)

GitHub Rubber Duck: second model from a **different AI family** as reviewer. When orchestrator is Claude, reviewer is GPT-5.4. Activates at 3 checkpoints: post-planning, post-implementation, pre-test execution. Also triggers reactively when agent is stuck (12_DEEP §9.3).

**Performance:** Claude Sonnet + Rubber Duck closes **74.7%** of the gap between Sonnet and Opus. **+4.8%** on hardest problems.

**Why:** Models from different families have different error modes. Neither catches what the other misses. This is the cheapest multi-agent upgrade.

**Lancelot:** `TransportPool` routes to multiple providers. A `@app.tool` for "review this" that routes to a different provider is trivial. The agent harness needs the "trigger reviewer at checkpoints" logic.

### 4.4 The Critical Finding: Scaffolding > Model Quality

On SWE-bench Pro, the same model (Opus 4.5) scored **50.2-55.4%** depending on the harness — a 5-point spread from architecture alone (12_DEEP §18.4). 09_FRONTIER §7.1 found a **6x performance gap** with the same model using different scaffolding.

**This is the single most important number in agentic AI.**

---

## 5. Context Isolation in Multi-Agent — The Exact Inheritance Contract

Source: 12_DEEP §20.3 (Claude Code), 20.9 (Codex CLI)

### 5.1 What Subagents Inherit and Don't (Claude Code)

The only channel from parent → subagent is the `Agent` tool's **prompt string**. Pass file paths, error messages, and decisions explicitly in that string.

| Receives | Does NOT Receive |
|---|---|
| Own system prompt (frontmatter body) | Parent's conversation history |
| Agent tool's prompt string | Parent's system prompt |
| Project CLAUDE.md | Skills (unless listed in `skills` frontmatter) |
| Tool definitions | Session state |
| Same `cwd` | Memory (unless `memory` frontmatter set) |

**Return path:** only the subagent's **final message** (verbatim) returns as the tool result. All intermediate tool calls, file reads, and internal outputs are isolated and discarded.

**Implication for Lancelot:** Sub-agent context isolation is not a transport concern — it's a harness concern. The agent layer must explicitly serialize the relevant parent context into the spawn prompt. Lancelot's `TransportPool.spawn_child` handles the W3C `traceparent` / `x-request-id` propagation at the HTTP layer; the *semantic* context (what to work on) must be passed explicitly.

### 5.2 Codex CLI's Fork Mode (More Expressive)

From 12_DEEP §20.15:
```rust
pub enum SpawnAgentForkMode {
    None,              // subagent starts with empty history
    FullHistory,       // subagent receives complete parent conversation
    LastNTurns(usize), // subagent receives only last N turns
}
```

When forking, **stripped**: tool calls, tool outputs, reasoning tokens, ghost commit snapshots.  
**Kept:** user messages, developer messages, system messages, final-answer assistant messages.

This is the correct default: strip the "how" (tool call noise), keep the "what" (decisions and facts).

### 5.3 The 16 Injected Rules for Sub-Agents (Codex)

From 12_DEEP §20.15 — auto-injected into every subagent at spawn. Key rules worth copying:

- Rule 4: "Be concise in your final response — the orchestrator reads it programmatically."
- Rule 8: "Use `send_message` for non-urgent coordination; use `followup_task` to interrupt."
- Rule 12: "If two agents must write to the same file, coordinate via `send_message` first."
- Rule 13: "Emit a concise summary as your final message; the orchestrator uses it."
- Rule 16: "Report tool failures explicitly — do not silently retry more than twice."

---

## 6. Context Engineering API-Level Primitives (Anthropic, April 2026)

Source: 12_DEEP §15.2

Three beta headers that change context management behavior:

**1. Compaction** (`compact-2026-01-12`):
```python
client.messages.create(
    betas=["compact-2026-01-12"],
    compaction_config={"trigger_token_count": 50000},  # min 50K
)
```
Triggers server-side compaction. Model generates structured summary. All prior messages dropped. Multiple compactions produce "summary of summaries."

**2. Tool-result clearing** (`clear_tool_uses_20250919`):
```
Zero inference cost. Purely mechanical.
Replaces old tool_result content with "[cleared to save context]"
Keeps tool_use record (model knows WHAT happened, not the raw output)
Benchmark: 335K peak → 173K peak (7 clearing events)
```

**3. Memory tool** (`memory_20250818`):
Six operations: `view`, `create`, `str_replace`, `insert`, `delete`, `rename`. Client-side persistent storage across sessions.

**Combined deployment:** Clear at 50K tokens (keep 6 most recent, exclude memory tool results), compact at 180K tokens.

---

## 7. The StopReason Primitive — Tracked Termination Causes

From 12_DEEP §20.16 — `ResultMessage.subtype` in the Claude Code SDK:

```python
subtype: Literal[
    "success",
    "error_max_turns",
    "error_max_budget_usd",
    "error_during_execution",
    "error_max_structured_output_retries"
]
total_cost_usd: float  # present on ALL subtypes including errors
```

**Lancelot implication:** Lancelot's `lancelot.context` ContextVars already accumulate `cost_usd`, `tokens_in`, `tokens_out`. The agent loop's termination must emit a `StopReason` that maps to these categories. The `error_max_budget_usd` hard stop requires a `max_budget_usd` parameter on the agent loop — Lancelot's `upstream_metrics` cost accumulation feeds into this naturally.

---

## 8. Hooks Architecture — The 4 Types + 25 Events

From 12_DEEP §20.17

### Hook Handler Types (4)

| Type | Mechanism | Cost |
|---|---|---|
| `"command"` | Shell script, stdin=JSON, exit code signals outcome | Cheap |
| `"http"` | POST to endpoint, body=JSON | Network round-trip |
| `"prompt"` | Fast single-turn LLM eval, no tools | ~1 inference pass |
| `"agent"` | Full sub-agent with tools | Expensive |

Exit code 2 from a hook = reject/block the action with feedback. This is the universal "gate" pattern.

### Key Multi-Agent Hooks

- `TeammateIdle` — teammate about to go idle; hook can assign more work via stdout JSON
- `TaskCreated` — fires on task creation; exit 2 = prevent task
- `TaskCompleted` — fires on completion; exit 2 = force re-work
- `WorktreeCreate` — replaces `git worktree add` entirely; hook must print worktree path on stdout
- `PreToolUse` with `"defer"` action (v2.1.89+) — returns `stop_reason: "tool_deferred"` with deferred tool use block; resumes by passing deferred result in next query

**Lancelot:** The hook system is an application-layer pattern over HTTP endpoints. Lancelot's `@app.middleware("http")` + `@app.tool` already provide the substrate. A Lancelot agent hook system is just named middleware endpoints with the right JSON protocol.

---

## 9. Memory Architecture — Three Tiers, Not One

From 12_DEEP §20.18 (Claude Code autoDream) + 3.7 (Codex CLI)

### 9.1 Claude Code Three-Layer Memory

```
Layer 1: MEMORY.md index
  ~150 chars/pointer. Always loaded. Truncated at line 200.
  Written by autoDream + explicit "remember" requests.

Layer 2: Topic files (memory/*.md)
  Loaded on-demand when topic relevant.
  Frontmatter: name, description, type, tags.

Layer 3: Daily transcript logs (logs/YYYY/MM/YYYY-MM-DD.md)
  Append-only, never loaded. Searchable via grep only.
  Source material for autoDream Curate phase.
```

autoDream consolidation gates (ALL three required before running):
1. ≥24 hours since last consolidation
2. ≥5 sessions since last consolidation
3. Consolidation lock acquired (prevents concurrent runs)

Four phases: Orient → Curate (grep daily logs) → Consolidate → Prune+Index. Memory cap: ~25KB.

### 9.2 Codex CLI Two-Phase Pipeline

Phase 1 (Extraction): `gpt-5.4-mini` with low reasoning effort, 8 parallel jobs, scans up to 5,000 threads. No-op gate: "Will a future agent plausibly act better because of what I write here?"  
Phase 2 (Consolidation): `gpt-5.3-codex` with medium reasoning, global lock, deduplication.

Stored in `memory_summary.md`, injected at session start, capped at **5,000 tokens**.

**Durable principle:** Memory is tiered by decay rate — hot (always loaded, 150-char pointers), warm (on-demand topic files), cold (append-only logs, grep-only). This is the right architecture. Lancelot's `MemoryPool` is byte-budget OOM protection, not semantic memory. The agent layer needs the 3-tier semantic memory with the MEMORY.md index pattern.

---

## 10. The Agent Loop Shutdown Protocol

From 12_DEEP §3.7 / 20.9

Codex CLI `run_turn` shutdown paths:
```
NetworkError         → retry with exponential backoff (max 3)
ContextOverflow      → return error to orchestrator
CancellationToken    → clean shutdown, ghost commit if configured
```

Ghost commits (Codex CLI `ghost_commits.rs`):
```rust
const DEFAULT_COMMIT_MESSAGE: &str = "codex snapshot";
// create_ghost_commit() — unreferenced commit, no branch pollution
// restore_ghost_commit(sha) — git restore --source <sha> --worktree -- .
```

Ignored dirs: `.git`, `node_modules`, `target`, `.venv`, `__pycache__`, `dist`, `build`, `.next`, `.nuxt`, `vendor`.  
File size cap: skip files > 10MB, dirs > 200 files.

**Lancelot:** `App.initiate_shutdown()` / `drain(timeout)` handle HTTP-level drain. The agent loop needs its own shutdown protocol: graceful stop on max_turns, budget, or SIGTERM; ghost commit if there's a working tree with changes. Lancelot's `SIGTERM` drain and graceful-loop-teardown are the right substrate.

---

## 11. Safety Architecture — What's Durable

Source: 12_DEEP §2.6, 09_FRONTIER §7.10

### 11.1 Two-Stage Permission Classifier (Claude Code)

Stage 1: Fast single-token yes/no classifier — 8.5% FPR on raw traffic.  
Stage 2: Chain-of-thought reasoning — invoked only if Stage 1 flags — reduces FPR to **0.4%**.

The classifier sees only **user messages + tool call payloads** — assistant messages and tool outputs are stripped. This prevents the model from "talking the classifier into bad calls" via its own outputs.

Final rates: **0.4% FPR, 17% FNR** on production traffic.

### 11.2 Five-Layer Safety Stack (Terminal Coding Agent Paper, 09_FRONTIER §7.10)

1. Prompt-level guardrails (read-before-edit, git workflow)
2. Schema-level tool restrictions (plan-mode whitelists, per-subagent allowed_tools)
3. Runtime approval (Manual/Semi-Auto/Auto with persistent permissions, pattern/command rules)
4. Tool-level validation (DANGEROUS_PATTERNS blocklist, stale-read detection, output truncation, execution timeouts)
5. Lifecycle hooks (pre-tool blocking scripts via exit code 2, argument mutation)

**Context-aware system reminders** (09_FRONTIER §7.10): Event detectors trigger on conditions (repeated failures, tool misuse, approaching iteration limits) and inject targeted guidance as **user messages** (not system, to preserve attention weight).

**Lancelot:** `lancelot.net` SSRF guard covers Layer 4 network safety. `@app.middleware` + hooks cover Layer 5. Layers 1-3 are agent harness concerns.

---

## 12. Harness Engineering for Long-Running Agents (Concrete Protocol)

Source: 09_FRONTIER §2.6

**Two-agent pattern** for long sessions that span multiple context windows:

**Initializer Agent** (first session only):
1. Creates `init.sh` — validates working state (tests, lint)
2. Creates `claude-progress.txt` — current state summary
3. Creates git baseline commit
4. Creates `feature_list.json` — 200+ features as JSON, each `{ "category": ..., "description": ..., "steps": [...], "passes": false }`

**Coding Agent** (subsequent sessions):
1. `pwd` → establish cwd
2. `git log --oneline -20` → recent changes
3. Parse `claude-progress.txt`
4. Review `feature_list.json`, select next single feature
5. Execute `init.sh` → validate working state
6. Implement feature, commit, update progress

Constraint: "It is unacceptable to remove or edit tests."

**Why 200+ features:** prevents premature completion (the #1 failure mode in long-running agents).

---

## 13. AlphaEvolve — The Evolutionary Pattern (Off-Thesis Flag)

Source: 09_FRONTIER §5

AlphaEvolve uses LLMs as **mutation operators** in an evolutionary search, not as conversational agents. No chat interface, no tool calling. Evaluation is automated and objective (code execution).

Real results: 0.7% of Google's worldwide compute recovered via data center scheduling, 23% Gemini training speedup, 32.5% FlashAttention kernel speedup.

**Lancelot verdict:** This is off the byte-path thesis — AlphaEvolve is algorithm discovery, not HTTP transport or agent orchestration. The evolutionary pattern is not a framework primitive. **Skip.**

---

## 14. What the Best Systems Have in Common (SWE-bench Pro >50%)

From 12_DEEP §19.4 — all share these 7 properties:

1. Typed tool system with specific tools (not bash-for-everything)
2. Context compaction/clearing strategy
3. **Linter-in-the-loop** (run after every edit, fix up to N times — Cursor: max 3 iterations)
4. File reading before editing (never guess about existing code)
5. Plan/TODO tracking to maintain coherence over long sessions (`todo.md` pattern)
6. Parallel tool calls where independence allows
7. Error as feedback (failed tool calls inform next action, not crash)

**Lancelot:** Items 1, 3, 6, 7 are application behaviors. Items 2, 4, 5 are harness primitives that Lancelot's agent layer must provide.

---

## 15. Hype / Depreciating Tactical Hacks — Explicitly Flagged

| Item | Source | Why It Churns |
|---|---|---|
| Specific Claude API beta headers (`compact-2026-01-12`, `clear_tool_uses_20250919`) | 12_DEEP §15.2 | Beta headers deprecate on model releases; the *pattern* (compaction, clearing) is durable, not the exact header |
| Cursor's 5-hour RL checkpoint cycle | 09_FRONTIER §3.3 | Requires Cursor's specific production data flywheel; not replicable without that data |
| Grok 4.20 cross-attention debate (≤180 token overhead) | 09_FRONTIER §12 | Requires training on MARL end-to-end; not a prompting trick; only xAI has built this |
| SWE-bench Verified scores | 12_DEEP §18.2 | OpenAI audit confirms contamination (frontier models reproduce verbatim gold patches) |
| Ghost commits (`codex snapshot`) | 12_DEEP §20.15 | Implementation detail of Codex CLI's undo. Useful pattern but the mechanism churns with git tooling |
| Anti-distillation fake tools + cryptographic signatures | 12_DEEP §20.18 | DRM for Anthropic's product, not a framework pattern |

---

## 16. Byte-Path Implications Summary

| Primitive | Lancelot Status |
|---|---|
| KV-cache prefix stability (sorted JSON) | **PROVIDED** — `lancelot.canonical.dumps_canonical` |
| Session → worker affinity | **PROVIDED** — `Response.set_session_affinity` |
| Traceparent propagation to sub-agents | **PROVIDED** — `TransportPool.spawn_child` |
| Parallel upstream fan-out | **PROVIDED** — `TransportPool.fanout` |
| Cost accumulation across agent turns | **PROVIDED** — `lancelot.context.cost_usd` ContextVar |
| TTFT metric on first streamed chunk | **PROVIDED** — `lancelot.http.stream.ttft` |
| Backpressure streaming pump | **PROVIDED** — R3 pump, `@app.stream` |
| Static prefix / dynamic boundary split | **NEEDED** in agent harness — not an HTTP primitive |
| Logit masking for tool availability | **NEEDED** in agent harness — not exposed via HTTP |
| Observation masking (tool-result clearing) | **NEEDED** in agent harness — message manipulation |
| AutoCompact with circuit breaker | **NEEDED** in agent harness |
| 3-tier memory (index / topic files / logs) | **NEEDED** — `MemoryPool` is byte-budget OOM, not this |
| StopReason taxonomy + budget hard stop | **NEEDED** — `cost_usd` feeds in; stop logic is harness |
| Ghost commit / rollback | **NEEDED** — agent harness; Lancelot has no git primitives |
| Deferred tool loading (ToolSearch) | **NEEDED** — extension of `lancelot.mcp.mount` |
