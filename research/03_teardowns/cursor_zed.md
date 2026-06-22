# Cursor + Zed — Mechanism-Level Teardown Notes

**Sources read:** `teardowns/CURSOR.md` (4,659 lines, Rounds 1–3, April 2026) and `teardowns/ZED.md` (13,898 lines, Rounds 1–3, May 2026).

**Focus per task:** Prompt assembly (Handlebars-conditional on active tools), MCP Tool Search 77K→8.7K token win, fetch_rules pull-mode, apply-model.

---

## 1. Handlebars-Conditional Prompt Assembly (Zed)

**What it is.** Zed's agent system prompt is compiled from `crates/agent/src/templates/system_prompt.hbs` using Handlebars. Sections are wrapped in `{{#if (contains available_tools 'X')}}` guards. If the user has `update_plan` disabled, the entire planning-instructions block is omitted from the prompt the model sees (ZED.md §11.5 + §35.5).

**Concrete mechanism.**
```handlebars
{{#if (contains available_tools 'update_plan')}}
## Planning
When given a complex task, create a plan using update_plan before starting...
{{/if}}
```
The prompt is **dynamically sized to the active tool set**. There are multiple sibling templates: `experimental_system_prompt.hbs` (A/B variant), `edit_file_prompt_diff_fenced.hbs`, `edit_file_prompt_xml.hbs`, `create_file_prompt.hbs`, `diff_judge.hbs`. This is not one monolithic prompt — it's a family of per-mode templates.

**Why it matters for Lancelot.** The agent layer Lancelot is building needs a prompt assembly step. Zed's Handlebars-conditional pattern is the right shape: declare tool-conditional sections once, compiled at request time. Token cost scales with what's enabled, not with every tool that ever existed. This is durable — the mechanism persists across model generations, only the prompt content churns.

**Lancelot implication.** `lancelot.mcp.mount` already exists. The system prompt for any Lancelot agent should be template-assembled at session start from the tool set that's actually mounted, not a static blob. The assembler can be 50 lines of Python (Jinja2 / string substitution); the structural insight is the guard pattern.

---

## 2. MCP Tool Search — 77K → 8.7K Token Win (Cursor)

**What it is.** January 2026, Cursor added "MCP Tool Search" (sourced to Anthropic feature). When many MCP servers are connected, loading all tool schemas upfront costs ~77K tokens per context turn. MCP Tool Search loads tool definitions **on-demand** rather than upfront, cutting overhead to ~8.7K tokens — an 85% reduction (CURSOR.md §9.1, line 1375: "cuts token overhead from ~77K to ~8.7K tokens").

**Mechanism.** Instead of listing all tool schemas in the system prompt, the agent gets a compact tool-search capability. When the model needs a specific tool, it calls a `ToolSearch`-style primitive with a query; the framework returns the matching tool's full schema, which the model then calls. This is lazy-loading for tool definitions — the same pattern as lazy imports in code.

**Cross-product convergence (CURSOR.md Round-3 §19, item 3):**
> "Cursor materialises descriptors on disk (`Misc/cursor.md:190`); Claude Code lazy-loads via `ToolSearch`; Codex deferred-tools via 'Hide deferred tools from code mode prompt' (codex-rs commit `a52c91d`). Same problem: MCP tool inflation. Same solution shape: don't inline."

All three major agent products converged on this independently within one quarter. This is a durable primitive.

**Lancelot implication.** `lancelot.mcp.mount` warns when a tool returns >4 KB (minimal-return discipline). The token problem is symmetric: if Lancelot's agent layer mounts many MCP servers, the tool schema inflation can exhaust context before the first user turn. The correct architecture is: (a) keep a compact tool index in context, (b) hydrate full schemas on first invocation or on agent `ToolSearch`. Lancelot's `@app.tool` already auto-derives `output_schema`; the registry could expose a search endpoint that returns schemas on demand.

---

## 3. fetch_rules Pull-Mode — Context-Scaling Rules (Cursor)

**What it is.** Cursor's rules system has two injection modes (CURSOR.md §9.3):

- **Push mode** (for `alwaysApply: true` rules): every Composer message includes these rules in the system context, unconditionally.
- **Pull mode**: the agent calls `fetch_rules` (a tool with `fetch_rules_params`) with a query, and the IDE returns matching rules. The agent decides *when* to fetch rules based on what it's working on.

**Schema of `.cursor/rules/*.mdc` frontmatter** (CURSOR.md §9.2):
```yaml
---
description: <string — short summary, used by the LLM to decide whether to inject>
globs: <string|string[] — glob patterns; rule applies when active file matches>
alwaysApply: <bool — if true, inject regardless of globs>
---
<body — markdown rule text, injected into system context when triggered>
```

**Priority hierarchy** (CURSOR.md §9.4):
1. User Rules (global, from Settings) — always first
2. Repo Rules `alwaysApply: true`
3. Repo Rules matched by glob
4. Repo Rules selected by `fetch_rules` tool — per-turn-as-needed
5. `.cursorrules` legacy file

**Why it scales.** A rule with `description: "Use TanStack Query for data fetching"` only gets injected when the agent is actively working on a data-fetching component. Cursor can have hundreds of rules without context overflow — only the relevant subset lands in any given turn.

**Lancelot implication.** This is the context engineering primitive Lancelot's agent layer lacks. When building the `ContextBundle` assembler, rules/skills should be pull-mode by default (agent fetches on-demand via description match), with an `always_inject` flag for the small set that must always be present. The `description` field + a lightweight vector similarity or keyword match is the retrieval mechanism. Lancelot's `lancelot.canonical.dumps_canonical` is relevant here for cache-stable rule retrieval keys.

---

## 4. Apply Model — Planner/Applier Split (Cursor)

**What it is.** Cursor uses a two-model pipeline for file edits (CURSOR.md §7):
1. **Planning model** (Claude Sonnet / GPT-5 / Composer 2): decides WHAT to change, produces a draft edit with `// ... existing code ...` markers.
2. **Apply model** (Llama-3-70B fine-tuned, "Fast Apply" via Fireworks AI): takes the plan + original file and produces the exact patched file. This is a separate service (`FastApplyService`, 5 source variants).

**Speculative decoding mechanism** (CURSOR.md §7):
- Fine-tuned Llama-3-70B via `llama-70b-ft-spec`.
- Throughput: ~1,000 tokens/second (~3.5k chars/s), 13× speedup over vanilla Llama-3-70B.
- The algorithm: "users speculate a larger number of tokens in parallel and consume them without deviating from the provided context." Server finds the longest prefix of the speculated text that matches the model's temperature=0 output, then continues from there.
- Works because Apply is low-creativity: the planner decided WHAT; Apply just reproductions it accurately in file context. High predictability = high speculation acceptance rate.

**Reapply tool** (CURSOR.md §7, §5.agent tool schema): when Apply produces wrong output, the agent calls `reapply` — a `round-2` escalation to a "smarter apply model". The `instructions` field in `edit_file` is a one-sentence English description for the apply model; it's the FastApply contract (original file + draft + sentinel markers → apply model performs the merge).

**Shadow workspace for lint feedback** (CURSOR.md §7.1): a hidden Electron window runs LSP-based linting on Apply's output WITHOUT disrupting the user's editor. Applies in-memory to TextModel objects (not disk files). Apply → shadow lint → if bad, invoke `reapply` loop (capped at 3 iterations, hardcoded constant, CURSOR.md §6.1 + Round-3 §10 item 5).

**Lancelot implication.** The planner/applier split is partially Tier-D for Lancelot (no custom edit prediction model). BUT the structural insight transfers: for agent edit tasks, separate "decide what to change" from "apply the change to the actual file". The apply step can use a fast, cheap model or even a deterministic regex/diff tool if the plan is structured enough. The 3-iteration lint-fix cap is a concrete budget constant worth adopting: `max_lint_iterations = 3`.

---

## 5. Zed Agent Prompt Architecture (Zed)

**Seven subagent types** (ZED.md §11.7 + CURSOR.md Round-3 §10 correction): Cursor's prompt defines 7 types: `generalPurpose`, `explore`, `shell`, `browser-use`, `cursor-guide`, `best-of-n-runner`, `codex-rescue`. Zed implements its own via `spawn_agent_tool.rs` (256 lines) — each subagent is a separate `Thread` with own model + tools + conversation.

**System prompt conditional on plan tool** (ZED.md §35.5): Only include planning instructions if `update_plan` tool is in `available_tools`. The Handlebars guard is `{{#if (contains available_tools 'update_plan')}}`.

**Grep tool constants** (ZED.md §11.3, `grep_tool.rs`):
```rust
const RESULTS_PER_PAGE: u32 = 20;
const CONTEXT_LINES: u32 = 2;
const MAX_ANCESTOR_LINES: u32 = 10;
```
20 matches per page, 2 context lines per match, up to 10 ancestor scope lines. Pagination via `offset` param; model is taught to call with `offset: 0`, then `offset: 20`, etc.

**Lancelot implication.** Lancelot's `@app.tool` and `lancelot.mcp.mount` are the tool registry layer. The pagination + context-line constants above are implementation-ready defaults for any grep/search tool Lancelot exposes. The `RESULTS_PER_PAGE = 20` is calibrated by Zed against real model behavior — not an arbitrary number.

---

## 6. Parallel Agents — git worktree Coordination Protocol (Cursor)

**What it is.** Cursor's local parallel agents (up to 8) use git worktrees. The coordination protocol is a filesystem-lock-based task-claiming protocol (CURSOR.md §2.2):

- Parent coord directory: `../.coord-<SESSION_ID>/`
- Each agent claims a task slot by `mkdir task-N.lock/` (atomic mkdir — fails if exists)
- On success: write `task-N.json` with `{"task": N, "worktree": "<path>", "claimed_at": "...", "pid": PID}`
- Each agent writes `.agent-id` and `.session-id` in its worktree
- Stale coords (>60 min) cleaned on next run

**Three-layer isolation model** (CURSOR.md §3.1-3.2): subagent types `explore` / `bash` / `browser` each get own context window. Custom agents defined as `.md` files in `.cursor/agents/`, `.claude/agents/`, `.codex/agents/`. Background (`is_background: true`) runs without blocking parent.

**Subagent schema key fields** (CURSOR.md §3.2):
```json
{
  "subagent_id": "string",
  "is_parallel_worker": false,
  "git_branch": "string (optional)"
}
```
`is_parallel_worker: true` indicates parallel execution. `git_branch` identifies the worktree branch.

**Lancelot implication.** `TransportPool.spawn_child` + `TransportPool.fanout` already handle the BYTE-PATH side of this. The git-worktree/filesystem coordination protocol is for the ORCHESTRATION side (which Lancelot's agent layer is building). The atomic-mkdir claim protocol is a robust, zero-dependency coordination mechanism worth adopting for agent-to-agent task assignment.

---

## 7. Hooks System — Agent Lifecycle Extension Points (Cursor)

**What it is.** Cursor's hooks system (CURSOR.md §4) is a 19-event lifecycle that fires shell commands at every stage of agent execution. Every hook receives `conversation_id`, `generation_id`, `model`, `cursor_version`, `workspace_roots`, `transcript_path` as universal fields.

**Key hooks for agent control:**
- `preToolUse`: can `allow|deny` any tool call + inject `updated_input` + `agent_message` into context
- `stop`: fires when agent loop ends; returning `followup_message` re-injects a user turn → continues the loop (up to `loop_limit`, default 5)
- `preCompact`: fires at 85% context utilization with `context_usage_percent`, `context_tokens`, `messages_to_compact` — enables pre-compaction observation
- `subagentStart`: can allow/deny spawning of a subagent
- `sessionStart`: can inject `env` vars and `additional_context` at session creation

**Exit code semantics:**
- Exit 0 → success, use JSON stdout as output
- Exit 2 → block action (equivalent to `permission: "deny"`)
- Exit other → fail-open unless `failClosed: true`

**The `stop` hook pattern** (CURSOR.md §4.4):
```typescript
const MAX_ITERATIONS = 5;
if (input.status === "completed" && input.loop_count < MAX_ITERATIONS) {
  console.log(JSON.stringify({
    followup_message: `[Iteration ${input.loop_count + 1}/${MAX_ITERATIONS}] Continue working...`
  }));
}
// Else: output nothing → loop terminates
```

**Lancelot implication.** This is the safety/governance primitive Lancelot lacks. The hooks are the 5-stage agent safety model (pre/post tool, pre/post agent, pre/post context compaction). For Lancelot's agent layer: implement a hook registry with at minimum `preToolUse` (permission gate), `stop` (loop control), and `preCompact` (context budget observation). The `failClosed` flag on hooks is a security-relevant default: deny-by-default on hook failure for sensitive operations.

---

## 8. Numeric Constants Worth Adopting

From both sources, these are production-calibrated constants:

| Constant | Value | Source | Meaning |
|---|---|---|---|
| Linter-fix iteration cap | `3` | CURSOR.md §6.1 + Round-3 §10 | Max retries before user escalation on lint errors |
| `block_until_ms` | `30000` ms | CURSOR.md Round-3 §20 | Shell foreground/background boundary |
| `RESULTS_PER_PAGE` | `20` | ZED.md §11.3 grep_tool.rs:67 | Grep tool pagination size |
| `CONTEXT_LINES` | `2` | ZED.md §11.3 grep_tool.rs:121 | Context lines around each grep match |
| `MAX_ANCESTOR_LINES` | `10` | ZED.md §11.3 grep_tool.rs:122 | Max ancestor scope context lines |
| MCP tool search token floor | `~8.7K` tokens | CURSOR.md §9.1 | Target after lazy tool loading (vs 77K upfront) |
| loop_limit (stop hook default) | `5` | CURSOR.md §4.1 | Max auto-continuations per stop event |
| Zeta `PREFILL_RATIO` | `0.1` (10%) | ZED.md §12.2 zeta_prompt.rs:22 | Max fraction of editable region that can be prefilled |
| Zeta token estimate | `bytes / 3` | ZED.md §12.2 zeta_prompt.rs:24 | ~3 bytes per token for code |
| Zeta prompt budget margin | `0.9×` | ZED.md §12.2 zeta_prompt.rs:29 | 10% safety margin |

---

## 9. What Is Hype / Depreciating

**Hype / skip:**
- The Apply model (Llama-3-70B fine-tune for Cursor's specific edit format) — the training methodology is interesting but the artifact is proprietary and churns with model generations. The structural insight (planner/applier split) is durable; the specific fine-tune is not replicable.
- Cursor's 57-service gRPC mesh + Anyrun/Firecracker RL training infra — this is Cursor's vertical integration moat, not a copy target. Requires multi-hundred-person engineering org.
- Zed's GPUI (retained-mode GPU renderer) — extremely impressive engineering but zero relevance to Lancelot's HTTP/agent use case.
- `SumTree` — brilliant for text editors / CRDTs; not relevant for Lancelot's context management (which is token-sequence, not text-rope).

**Durable (persists 3+ model generations):**
- Handlebars-conditional prompt assembly by active tool set
- Pull-mode rules/context (`fetch_rules` pattern)
- MCP lazy tool schema hydration
- Agent lifecycle hooks (`preToolUse`, `stop`, `preCompact`)
- Planner/applier split (structural, model-agnostic)
- `SKILL.md` frontmatter + on-demand `Read` for skill loading (all three major products converged)

---

## 10. API Shape Notes Worth Stealing

**Cursor subagent definition schema** (CURSOR.md §3.2) — clean frontmatter for agent cards:
```yaml
---
name: string
description: string    # guides automatic delegation
model: "fast"|"inherit"|<specific model id>
readonly: boolean
is_background: boolean
---
```
The `description` field drives automatic delegation (agent reads the description to decide which subagent to spawn). `readonly: true` restricts write permissions cheaply.

**Cursor hooks.json structure** (CURSOR.md §4.1) — compact, composable:
```json
{
  "version": 1,
  "hooks": {
    "preToolUse": [{ "command": "./script.sh", "matcher": "Shell", "failClosed": true }],
    "stop": [{ "command": "./grind.ts", "loop_limit": 5 }]
  }
}
```
The `matcher` field (regex or literal) scopes hooks to specific tool names. `loop_limit` on `stop` is the budget guard. `failClosed` flips the default to deny-on-hook-failure.

**Zed's grep tool input schema** — pagination + include pattern as first-class:
```rust
pub struct GrepToolInput {
    pub regex: String,
    pub include_pattern: Option<String>,
    pub offset: u32,              // for pagination
    pub case_sensitive: bool,
}
```
The tool description explicitly teaches the model to use `offset` for subsequent pages.

**Cursor mcp.json config interpolation variables** (CURSOR.md §9.1):
```
${env:NAME}                   — environment variable
${userHome}                   — home directory
${workspaceFolder}            — project root
${workspaceFolderBasename}    — project name
```
Simple string interpolation in config values eliminates a class of hardcoded-path bugs.
