# Agentic Architecture Catalog — Pattern Taxonomy, When-to-Use, Defaults

**Sources:** `researchfms/core/06_AGENTIC_ARCHITECTURES.md` (all sections), `researchfms/core/13_AGENTIC_AI_ARCHITECTURE_MAP.md`
**Lens:** Lancelot byte-path filter applied to every primitive. See `00_LENS.md` for what is already built.

---

## 0. The Governing Insight (cite: 06 §1, 13 §0)

> "The loop itself is 10 lines. The product is the surrounding machinery."

Every production agent reduces to:
```python
messages = [system_prompt, user_message]
while True:
    response = llm(messages, tools=schemas)
    if not response.tool_calls: return response.text
    results = [execute(call) for call in response.tool_calls]
    messages += [response, tool_results(results)]
```

Three factors explain 95% of performance variance: token usage (80%), tool call count, model choice. Multi-agent uses ~15× more tokens than chat, ~4× more than single agent. The improvement from multi-agent is mostly "more tokens / more reasoning," not the architecture itself. (cite: 06 §4)

---

## 1. Pattern Taxonomy — When Each Applies

From Anthropic's official breakdown (cite: 06 §6.1, §4):

| Pattern | When to use | Default choice? |
|---|---|---|
| **Prompt chaining** | Fixed sequential steps, each validates before next | YES — start here |
| **Routing** | Distinct input categories needing specialized handling | YES — fast + cheap |
| **Parallelization** | Independent subtasks OR voting for consensus | YES — for I/O-bound fan-out |
| **Orchestrator-Workers** | Complex tasks where subtasks cannot be predicted in advance | Reach for this when chaining hits limits |
| **Evaluator-Optimizer** | Clear eval criteria + iterative refinement | Only when criteria are measurable |
| **Autonomous ReAct loop** | Open-ended tasks, unpredictable step count | Most common default |

**Decision rule (cite: 06 §6.1):** "Consider adding complexity only when it demonstrably improves outcomes." Start with the simplest. Agents trade latency and cost for task performance — justify the trade.

---

## 2. The Canonical Patterns — Mechanisms

### 2.1 ReAct Loop (default for open-ended)

The `while stop_reason == "tool_use"` loop. State = message array. No state machine, no graph.

**Concrete implementation detail (cite: 06 §14.1 — Claude Code `queryLoop()`):**
```
State carries:
  messages, toolUseContext, autoCompactTracking,
  maxOutputTokensRecoveryCount, pendingToolUseSummary,
  stopHookActive, turnCount, transition (WHY loop continued)

Seven continue-transitions (never silent infinite loop):
  max_output_tokens_escalate  → retry at 64K on 8K cap hit
  max_output_tokens_recovery  → inject nudge (max 3×)
  reactive_compact_retry      → 413 prompt-too-long → compact → retry
  collapse_drain_retry        → context stages exhausted
  stop_hook_blocking          → hook error → re-query with error
  token_budget_continuation   → inject budget nudge
  (implicit)                  → tool_use → execute → loop
```

**Budget auto-continue constants (cite: 06 §14.1):**
```typescript
COMPLETION_THRESHOLD = 0.9      // 90% usage = done
DIMINISHING_THRESHOLD = 500     // <500 new tokens → no progress
// Diminishing returns: continuations >= 3 AND delta < 500 AND prior delta < 500 → stop
```

**Transcript-first reliability:** User message written to disk BEFORE API call. Resumable on process termination. (cite: 06 §14.1)

**Byte-path implication:** Lancelot has no loop primitive yet. `TransportPool` handles the outbound LLM call; the while-loop + stop-reason dispatch is the missing agent layer.

---

### 2.2 Orchestrator-Workers (for decomposable parallelism)

```
Orchestrator (big model, extended thinking for planning)
  → spawn 3-5 parallel workers (smaller model each, own context)
  → workers store to external systems, pass lightweight refs back
  → orchestrator merges + post-processes (e.g. CitationAgent)
```

**Performance data (cite: 06 §6.2):** Anthropic's multi-agent research system: 90.2% improvement over single-agent Opus 4 on research eval. Token cost: ~15× more than chat.

**A tool-testing agent that rewrites tool descriptions → 40% decrease in task completion time.** (cite: 06 §6.2)

**Critical implementation constraint:** Workers MUST store output in external systems and pass lightweight references. Passing full output back to orchestrator = context explosion when truncated.

**When it fails (cite: 13 §6):** Orchestrator bottleneck + context compression loss. Use only when subtasks are genuinely independent with clean artifact boundaries.

**Byte-path implication:** `TransportPool.fanout(first_n, deadline, cancel_laggards, concurrency)` is the byte-path analog for the parallel-worker dispatch. The orchestration policy (what to spawn, how to merge) is the missing piece.

---

### 2.3 Parallelization — Two Sub-Forms

**Form A: Independent subtask fan-out** — e.g., run 3 subqueries simultaneously, merge results. `TransportPool.fanout` is Lancelot's existing primitive.

**Form B: Voting / sampling for consensus** — e.g., Cursor Bugbot's 8 parallel passes with randomized diff ordering → majority vote → validator. (cite: 06 §20.7.3)

Concrete Bugbot mechanism:
```
8 parallel passes, each with randomized diff chunk ordering
→ each pass proposes bugs independently
→ majority vote: bugs flagged by multiple passes = stronger signal
→ validator model catches remaining false positives
→ Resolution: 52% → 70%+
```

---

### 2.4 Evaluator-Optimizer

The inner loop has an explicit evaluator role — not "does the model think it's done" but a SEPARATE eval step (model, test suite, or human). The evaluator's output feeds back as context. Only valid when eval criteria are deterministic or measurable.

Devin's planning RFT is the strongest example: reward = file prediction F1 score. Emergent behavior: model learned parallel tool calls without being taught. (cite: 06 §16.2, §20.6.3)

---

### 2.5 Planning/Standard Mode Split (Devin)

Two explicit phases with different tool sets:
```
Planning Mode: read-only tools only (open_file, shell grep/find, go_to_definition, go_to_references)
  → produce: numbered list of files + changes + order

Standard Mode: full tools
  → execute, run lint/tests, CI gating (fail 3× → ask user)
```

(cite: 06 §3.3)

**Why it matters:** The model cannot make premature writes during planning. This reduces irreversible mistakes. It also makes "what files am I changing" explicit before any changes happen — audit-friendly.

---

### 2.6 Multi-Path Exploration with Rollback (Amazon Q)

**Anti-forward-only:** Most agents try to recover forward on failure. Amazon Q's Critic Agent has explicit rollback authority. When dead end detected, return to known-good state and try a new branch. (cite: 06 §18, §20.7.2)

Three-agent system:
- Memory Management Agent: analyzes iteration results, appends relevant portions to inter-iteration memory
- Critic Agent: detects dead ends, triggers rollback
- Debugger Agent: has full tool access, takes direction from critic

Result: 85% higher success rate vs single-agent on 62 apps with 100K+ LOC.

**Byte-path implication:** Rollback is a session-state operation. Lancelot has no checkpoint/restore primitive for agent state. This is in the "durable seam" gap in the lens.

---

### 2.7 Cognition's Anti-Pattern: Don't Build Multi-Agents (as a default)

**Source (cite: 06 §20.9):** "Parallel multi-agent architectures create unreliable systems because decision-making is too dispersed and context cannot be shared thoroughly between agents."

Two principles:
1. Share full agent traces, not individual messages
2. Decision consistency requires unified context

**Recommended default:** Single-threaded linear agent + compression layer for context overflow.

**Resolution with §2.2:** The apparent contradiction with Anthropic's 90.2% improvement dissolves: the improvement comes from more tokens/reasoning, not the architecture. For reliability-first use cases, single-agent with large context beats multi-agent with fragmented context. Multi-agent is correct when the task genuinely partitions into independent work streams.

---

## 3. Context Engineering Patterns — The Durable Layer

This is where the actual product moat lives. More valuable than prompt engineering. (cite: 13 §4)

### 3.1 Static Prefix + Append-Only History (Manus — cite: 06 §5.2, §20.2.3)

**The five rules as architectural constraints:**

1. System prompt = stable prefix (no timestamps, no dynamic content) + dynamic suffix
2. Conversation is append-only (never mutate prior messages)
3. JSON serialization is deterministic (sorted keys — explicitly enforced, many languages don't guarantee this)
4. Tool masking via logit manipulation at decode time, NOT tool removal from prompt
5. Session affinity routing (session ID → consistent vLLM worker → warm KV-cache)

**Cost impact:** Claude Sonnet cached $0.30/MTok vs uncached $3.00/MTok. 10× reduction from purely architectural decisions, zero model changes.

**Byte-path implication:** `lancelot.canonical.dumps_canonical` (sorted-key stable JSON) is the serialization part of rule 3 — already built. Session affinity routing via `Response.set_session_affinity` — already built. Tool masking (rule 4) is an agent-layer concern — not yet built.

### 3.2 Observation Masking (JetBrains NeurIPS 2025 — cite: 06 §20.2.2)

```
For each turn in conversation:
  IF action/reasoning: KEEP always
  IF observation (tool output):
    IF within last N turns: KEEP
    ELSE: replace with "[observation masked]"

Optimal N = 10 turns (SWE-agent calibrated)
```

**Benchmark:** 52% cheaper, +2.6% solve rate improvement vs LLM summarization.

**Why it beats summarization:** Zero API overhead. Summarization causes 13-15% longer trajectories (summaries obscure stopping signals). Old tool outputs are rarely re-read; old reasoning is always needed.

**Byte-path implication:** Pure agent-layer concern. Lancelot's `lancelot.context` ContextVars capture request metadata but not conversation history. Observation masking is a context assembly primitive to build.

### 3.3 Compaction — Three-Tier (Claude Code — cite: 06 §5.1, §14.4)

```
Tier 1 — MicroCompact (zero API calls):
  Surgically truncates high-token tool outputs
  Replaces with "output truncated" marker
  Preserves cache boundaries

Tier 2 — AutoCompact (one API call):
  Trigger: ContextWindow - 13,000 tokens (~95% capacity)
  Generates ~20K token structured summary
  Circuit breaker: MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3
    (1,279 sessions had 50+ consecutive failures → 250K API calls/day wasted)

Tier 3 — Full Compact:
  Compresses everything including older summaries
  Re-injects: recently accessed files (capped 5K tokens each), active plans/TODOs
```

**API primitive (Anthropic beta — cite: 06 §8.1):**
```python
response = client.messages.create(
    model="claude-opus-4-6",
    betas=["compact-2026-01-12"],
    compaction_config={
        "type": "compact_20260112",
        "trigger": {"type": "input_tokens", "value": 50000},
        "instructions": "Preserve decisions, bugs, open questions"
    },
    messages=conversation_history
)
```

**Tool-result clearing (cite: 06 §8.2, §20.2.4) — zero inference cost:**
```python
context_management={
    "edits": [{
        "type": "clear_tool_uses_20250919",
        "trigger": {"type": "input_tokens", "value": 30000},
        "keep": {"type": "tool_uses", "value": 4},
        "clear_at_least": {"type": "input_tokens", "value": 10000},
        "exclude_tools": ["memory"]
    }]
}
# Result: 335K peak → 173K peak (7 clearing events). 96.3% of context was file-read results.
```

**Byte-path implication:** None of these are built in Lancelot. The context/compaction layer is a core missing piece.

### 3.4 Deferred Tool Loading (cite: 06 §6.4)

**Problem:** 58 MCP tools consume ~55K tokens before any conversation.
**Solution:** `defer_loading: true` on tool definitions. Only names loaded. Full schemas loaded on-demand via `tool_search_tool_regex`.

**Impact:** 77K → 8.7K tokens (95% reduction). Opus 4 accuracy: 49% → 74%.

**Byte-path implication:** `lancelot.mcp.mount` serves MCP tools. Deferred loading is a protocol-level feature — the tool list stays small until the agent explicitly searches. Worth adding to `lancelot.tool` / MCP layer.

---

## 4. Sub-Agent / Multi-Agent Patterns — Mechanisms

### 4.1 Codex CLI Inter-Agent Protocol (cite: 06 §7.6, 13 §6)

```rust
pub struct InterAgentCommunication {
    pub author: AgentPath,        // hierarchical: /root/researcher/worker
    pub recipient: AgentPath,
    pub other_recipients: Vec<AgentPath>,
    pub content: String,
    pub trigger_turn: bool,       // wakes agent vs passive delivery
}
```

`trigger_turn: bool` is the key field: distinguishes "fire and forget to mailbox" from "wake now." Channel: `tokio::sync::mpsc::UnboundedSender` + `watch::Sender<u64>` for sequence numbers.

**Three built-in roles (cite: 06 §7.6):**
- `explorer` — read-only, fast/authoritative model, separate config
- `worker` — execution, explicit ownership, warned about concurrent edits
- `default` — no config overlay

**Fork modes:**
- `FullHistory` — child gets complete conversation (KV cache sharing — 90% cache discount on shared context)
- `LastNTurns(n)` — child gets only recent N turns
- Strip tool calls/outputs on fork; keep only system/developer/user messages + assistant final answers

**Depth limiting:** `exceeds_thread_spawn_depth_limit()` — "Agent depth limit reached. Solve the task yourself." Prevents recursive spawn explosions.

**Byte-path implication:** `TransportPool.spawn_child(parent, session_affinity)` handles the transport layer (traceparent, request_id, agent_id propagation). The inter-agent mailbox (trigger_turn semantics, hierarchical AgentPath) is the missing orchestration seam.

### 4.2 Fork KV-Cache Sharing (cite: 06 §14.12)

Fork agents inherit parent's full conversation history and system prompt for byte-identical API request prefixes → 90% cache discounts on shared context.

**Known bug:** Context compaction can leave orchestrator's CWD pointing inside a previous worktree path → subsequent worktree dispatches nest inside the old one.

### 4.3 Coordinator Mode — Four-Phase (cite: 06 §14.7)

Claude Code's coordinator pattern:
1. Research — workers investigate in parallel
2. Synthesis — coordinator reads findings, crafts "Synthesized Spec" with file paths + line numbers
3. Implementation — workers execute targeted changes per spec
4. Verification — workers test via VERDICT system (adversarial proof, not existence checking)

**Key constraint:** When coordinator mode active, main session's tool pool REDUCED to coordinator-only tools — NO Bash, FileRead, FileEdit, Grep, Glob. Purely management layer. This prevents the orchestrator from doing direct work (tempting, dangerous).

---

## 5. Memory Architecture — Four Layers

(cite: 06 §20.5, §7.7)

### 5.1 The Four-Layer Model (Claude Code)

| Layer | Stores | Persistence | Writes |
|---|---|---|---|
| Project instructions (CLAUDE.md) | Conventions, constraints, rules | Permanent (version controlled) | Human |
| Auto Memory | Per-session observations, corrections | Survives sessions | Model (automatic) |
| Session Memory | Conversation continuity within session | Single session | System |
| Auto Dream | Consolidated, deduplicated, pruned | Permanent | Model (background subagent) |

### 5.2 autoDream Consolidation — Three-Gate Trigger (cite: 06 §14.10, §20.5.1)

Three gates, cheapest first (all must pass):
1. Time gate: 24 hours since last dream
2. Session gate: 5+ sessions since last dream
3. Lock gate: file-based advisory consolidation lock (prevents concurrent corruption)

**Four phases:**
1. Orient — directory scan, read MEMORY.md, skim topics (avoid duplicates)
2. Gather Signal — grep session transcripts for corrections, decisions, patterns
3. Consolidate — merge overlapping, convert relative dates to absolute, delete contradicted facts
4. Prune — keep MEMORY.md under 200 lines / ~25KB (index with one-line descriptions → topic files)

**Dream subagent constraints:** Read-only bash. Separate process (prevents corrupting primary context). 913 sessions consolidated in ~9 minutes.

**Theoretical backing:** UC Berkeley "Sleep-Time Compute" (Letta + UCB, arXiv:2504.13171): pre-processing context during idle windows reduces test-time compute for same accuracy by ~5×.

### 5.3 Codex Memory Two-Phase Pipeline (cite: 06 §7.17, §7.18, §20.5.2)

Phase 1 extraction: model `gpt-5.4-mini` (reasoning: Low), 8 parallel jobs, scans 5,000 threads.
**No-op gate (exact text):** "Will a future agent plausibly act better because of what I write here?" — if no, skip.

Memory YAML frontmatter:
```yaml
description: "One-line summary"
task: "What user asked"
task_group: "Category"
task_outcome: success|partial|fail|uncertain
keywords: [...]
```

Phase 2 consolidation: produces `memory_summary.md` (always in system prompt), `MEMORY.md` (keyword-searchable), `skills/<name>/SKILL.md` (reusable procedures), `rollout_summaries/<slug>.md`.

**Byte-path implication:** Lancelot `lancelot.memory_pool.MemoryPool` is byte-OOM management, NOT agent memory. The typed 4-tier memory + consolidation pipeline is entirely unbuilt.

---

## 6. Model Routing — Production Patterns

(cite: 13 §2)

The minimal production routing split:
```
Fast/cheap: classification, intent, short structured outputs → gpt-4.1-mini (temp=0)
Reasoning: planning, decomposition → o4-mini (reasoning_effort="high")
Synthesis: full reports → gpt-4.1 (temp=0.4, max_tokens=4000)
Apply/edit: code transformation → fine-tuned smaller model (temp=0)
Browser grounding: UI interaction → claude-sonnet (temp=0, max_tokens=2048)
```

**Route deterministically from request complexity; never let the LLM choose its own model.** (cite: 13 §2, AgentQL)

AgentQL concrete routing constants (cite: 13 §2):
```python
automation:       temperature=0.0, max_tokens=2048
data extraction:  temperature=0.0, max_tokens=8192
query generation: temperature=0.1, max_tokens=1024
# is_complex if: tree_node_count > 500 OR query_field_count > 10 OR has_lists OR has_descriptions
```

**Byte-path implication:** Lancelot `lancelot.llm.TransportPool` handles transport + retry + cost metering. Model routing / provider selection per purpose is the missing adapter layer (lens calls this "ProviderProfile / OpenAI-canonical → N-dialects").

---

## 7. Tool Design — The Load-Bearing Decisions

### 7.1 Typed Tools vs Generic Shell (cite: 06 §2.1)

Claude Code's design thesis: dedicated typed tools (Grep, Glob, Read, Edit) > generic bash-for-everything. Benefits: better observability, safety constraints, prompt optimization surface.

**Practical rule:** Each tool gets explicit parameters, descriptions, constraints. The tool description is what the prompt engineer optimizes, not the loop. A tool-testing agent that rewrites descriptions → 40% improvement in task completion time.

### 7.2 Tool Safety Classification (cite: 06 §3.2, §7.2)

| Tier | Example | Behavior |
|---|---|---|
| Read-only | grep, read, ls | Parallelizable, no approval needed |
| Workspace write | edit, shell (scoped) | Needs approval or sandbox |
| Dangerous | network, system config, credentials | Always ask |

**Codex runtime parallel execution:** `ToolOrchestrator` partitions reads (parallel) from mutating tools (sequential). Tool locks enforce read-write separation. (cite: 06 §7.2)

**SafeToAutoRun pattern (Windsurf — cite: 06 §3.6):** Every terminal command gets a `SafeToAutoRun: bool`. "NEVER run unsafe commands automatically even if user asks." Simple binary classification at the tool level.

### 7.3 Programmatic Tool Calling (cite: 06 §6.3, §20.2.5)

Model writes Python code that calls tools and processes results. Tool outputs from code calls are NOT added to context — only final stdout.

```json
{
  "tools": [
    {"type": "code_execution_20250825", "name": "code_execution"},
    {
      "name": "query_database",
      "input_schema": {...},
      "allowed_callers": ["code_execution_20250825"]
    }
  ]
}
```

**Token reduction: 37% on complex research tasks.** Eliminates 19+ inference passes when orchestrating 20+ tool calls.

Combined with MCP progressive disclosure: 98.7% token reduction (150K → 2K tokens) for tool-heavy agents.

**Byte-path implication:** `lancelot.mcp.mount` exposes tools via MCP. Code execution as a tool-calling substrate is a new API surface (not just serving MCP tools, but letting the model orchestrate them via code).

---

## 8. Safety / Permission Model

(cite: 06 §2.1, §7.8, 13 §9)

### 8.1 Two-Layer Classifier (Claude Code — cite: 06 §2.1)

- Stage 1: Fast single-token yes/no (8.5% FPR on raw traffic)
- Stage 2: Chain-of-thought only if Stage 1 flags (reduces FPR to 0.4%)
- Classifier sees ONLY user messages + tool call payloads — assistant messages and tool outputs stripped (prevents "talking classifier into bad calls")
- 0.4% FPR, 17% FNR on production traffic

### 8.2 Guardian System (Codex CLI — cite: 06 §7.8)

AI security reviewer:
- Model: `gpt-5.4`, 90-second timeout
- Transcript budget: 10K tokens messages + 10K for tools, max 40 recent entries
- Output: `{risk_level, user_authorization, outcome: allow|deny, rationale}`
- **Fails closed:** timeout/failure/malformed = deny
- Delta mode: only sends new transcript entries on retry (cursor-based)

Risk taxonomy (cite: 06 §7.16): low (routine code/git) → medium (system config, network access) → high (credential access, persistent changes) → critical (exfiltration, security weakening). Critical = always DENY regardless of authorization.

### 8.3 Bash Security — 23 Checks (cite: 06 §14.5, §20.3.4)

Input validation BEFORE the shell. Novel attack vectors:
- Check 11: IFS injection — `$IFS` or `${...IFS...}` redefines word boundaries
- Check 18: Unicode whitespace — 13 non-ASCII spaces (U+00A0 through U+FEFF) cause shell-quote tokenize differently than bash
- Check 14: Malformed token injection — unbalanced delimiters with separators (HackerOne disclosed)

Tree-sitter integration: when WASM available, AST is authoritative — eliminates entire classes of parser differential attacks.

**Byte-path implication:** `lancelot.net` provides SSRF guard. Shell command input validation (the 23-check pattern) is agent-layer security, not transport-layer. If Lancelot exposes a shell tool, this checklist is the starting point.

---

## 9. Durable State / Checkpoint Pattern

(cite: 13 §1, 13 §12)

The ADK commit-boundary pattern — the cleanest formulation:
```
Runner receives query
  → append user event through SessionService (committed)
  → call agent.run_async(context)
  → agent yields Event
  → Runner commits event actions through SessionService / ArtifactService / MemoryService
  → yield upstream
  → agent resumes after commit (guaranteed: event is durable)
```

**Minimal schemas (cite: 13 §12):**
```sql
sessions: (id, user_id, status, state_json, created_at, updated_at)
events: (id, session_id, seq, author, type, content_json, state_delta_json, created_at)
       UNIQUE(session_id, seq)    -- sequence = audit trail
tool_calls: (id, session_id, event_id, tool_name, args_json, result_ref,
             status, error_type, latency_ms, created_at)
memories: (id, user_id, workspace, memory_type, content, embedding,
           lineage_json, expires_at, created_at)
```

State and artifacts are separate: state is a serializable key-value scratchpad; artifacts (large binary outputs) go to ArtifactService with references stored in state.

**Byte-path implication:** The durable seam (checkpoint/resume/HITL) is explicitly in the "what Lancelot lacks" list. The ADK pattern is the recommended default starting point.

---

## 10. Observability — Minimum Trace Schema

(cite: 13 §8)

OTel GenAI semantic conventions as baseline:
- `gen_ai.operation.name`: `chat`, `generate_content`, `execute_tool`, `invoke_agent`
- Tool span: `execute_tool {gen_ai.tool.name}`
- Tool attributes: call id, name, description, type, arguments, result
- Error attribute: low-cardinality `error.type`
- Content capture: opt-in only (privacy + volume)

What to log for calibration:
- Exact model/provider/version
- Prompt hash and prompt components
- Tool schema hash
- Token counts and cache hit/miss
- Latency per stage
- Retry count and error class
- Final answer + citations
- Human/eval score

**Byte-path implication:** Lancelot has RED metrics, OTel histograms (`lancelot.upstream.*`), TTFT, `lancelot.http.stream.ttft`. The agent-level trace (session → turn → tool_call hierarchy) is not yet in any Lancelot OTel output. W3C `traceparent` is already propagated through `TransportPool` — agent trace spans should attach to this.

---

## 11. Anti-Patterns to Avoid

(cite: 06 §12.2, §20.9, 13 §13)

| Anti-pattern | Why | Source |
|---|---|---|
| Build multi-agent before single-agent hits a limit | Performance comes from more tokens, not the architecture | 06 §20.9 (Cognition) |
| Forward-only error recovery | Dead ends compound; critic+rollback beats retry | 06 §18 (Amazon Q) |
| Timestamps in system prompt | One character difference breaks KV-cache from that token onward | 06 §5.2 (Manus) |
| Dynamic tool removal mid-session | Breaks KV-cache prefix; use logit masking instead | 06 §20.2.3 (Manus) |
| LLM summarization as the default compaction | 13-15% longer trajectories, obscures stopping signals, costs tokens | 06 §20.2.2 (JetBrains) |
| One inference pass per tool call (sequential) | 10 tool calls = 10 inference passes; programmatic calling = 37% token reduction | 06 §6.3 |
| Full tool schemas in context at startup | 58 MCP tools = 55K tokens; defer to names-only, load schemas on demand | 06 §6.4 |
| Screenshot-only browser control | Brittle; semantic AX-tree + deterministic grounding + CDP layer | 13 §5 |
| "Frameworks solve agents" | Frameworks provide loop/state. Tool schemas, data plane, evals still determine quality | 13 §13 |
| Claim a blanket "faster server" | Honest envelope: wins at specific shapes (large payload, streaming, const tier) | CLAUDE.md |

---

## 12. Hype Flags (Depreciating on ~1-2yr Model Clock)

| Claim | Verdict | Cite |
|---|---|---|
| Specific RL training recipes (SWE-grep, Composer 2 router replay) | Model-version specific. Architecture (separate subagent, RL with F1 reward) is durable; training constants churn. | 06 §20.6 |
| Anti-distillation fake-tool injection | Tactical, easily bypassed via MITM proxy. Durable idea (content watermarking) but specific implementation = ephemeral. | 06 §14.8 |
| Cursor Tab 90-minute retrain loop | Durable pattern (continuous RL from production); 90-minute number specific to 2025 infra. | 06 §20.6.1 |
| Specific model codenames (Capybara, Tengu, Fennec) | Churns on every release cycle. | 06 §14.15 |
| Hashline edit format over exact-string-replace | Mixed results, model-specific. Language-dependent (hurts Python). Fuzzy matching triggered zero times in 134 edits — LLMs either reproduce exactly or hallucinate. | 06 §20.4.1 |

**Durable primitives (persist across 3+ model generations):**
- The agent loop structure
- Observation masking / context compaction
- Append-only history + cache-stable serialization
- Tool-result clearing
- 4-tier memory with consolidation
- Session/event/checkpoint schema
- Typed tools with permission tiers
- OTel trace schema

---

## 13. Lancelot Byte-Path Implications — Summary

| Pattern | Status in Lancelot | Action needed |
|---|---|---|
| Agent loop (while stop_reason == "tool_use") | NOT BUILT | Core loop primitive |
| Observation masking / tool-result clearing | NOT BUILT | Context assembly layer |
| Append-only history | NOT BUILT (protocol concern) | Enforce in session model |
| KV-cache stable serialization | BUILT (`dumps_canonical`) | Use in context compiler |
| Session affinity routing | BUILT (`set_session_affinity`) | Wire into agent session routing |
| TransportPool for worker dispatch | BUILT (fanout, spawn_child) | Reuse as orchestrator tool |
| Deferred tool loading | NOT BUILT | Add `defer_loading` to `lancelot.tool` |
| 4-tier memory + consolidation | NOT BUILT | Agent memory layer |
| Durable checkpoint (session/event schema) | NOT BUILT | Durable seam |
| SSRF guard | BUILT (`lancelot.net`) | Extend to shell commands too |
| OTel agent trace (session→turn→tool) | NOT BUILT | Add to existing metrics |
| Cost accumulation per agent session | BUILT (`lancelot.context.cost_usd`) | Hook into agent loop |
| TTFT / streaming | BUILT (R3 pump, TTFT metric) | Stream tool outputs through same pipe |
| MCP server mount | BUILT (`lancelot.mcp.mount`) | Add defer_loading support |
