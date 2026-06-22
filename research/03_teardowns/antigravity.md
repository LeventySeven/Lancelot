# Antigravity — Mechanism-level teardown distillation

**Source:** `researchfms/teardowns/ANTIGRAVITY.md` (Tier-1A: two leaked system prompts `Fast Prompt.txt` 611L + `planning-mode.txt` 213L; verbatim TypeScript tool namespace; AI Studio Build cross-confirm; Round-2 direct binary RE of `language_server_linux_x64` v1.23.2, SHA-256 `4b23f0a2...`)
**RE date:** 2026-05-20
**Distilled:** 2026-06-22

---

## §1 The mode-gate (PLANNING → EXECUTION → VERIFICATION) — the most important primitive

This is an **architectural pattern, not a prompt suggestion**. It is enforced via a dedicated `task_boundary` tool that accepts `Mode: PLANNING | EXECUTION | VERIFICATION`. The agent CANNOT make code edits in PLANNING mode — the tool schema forbids it. The transition graph (verbatim from `planning-mode.txt:21-23`):

```
null → PLANNING                  (always start here on new user request)
PLANNING → PLANNING              (user requests plan changes)
PLANNING → EXECUTION             (user approves implementation_plan.md)
EXECUTION → PLANNING             (unexpected complexity found)
EXECUTION → VERIFICATION         (implementation done)
VERIFICATION → EXECUTION         (minor bug, same TaskName)
VERIFICATION → PLANNING          (fundamental flaw, new TaskName)
```

The agent skips to EXECUTION on resume ONLY if planning was already approved.

**Why it matters for Lancelot agents:** This is the "stuck detect" and "scope creep" fix in one mechanism. Without the mode gate, agents loop (re-planning without making progress) or overreach (editing unrelated code while "planning"). The gate is cheap to implement as a ContextVar + per-step tool allowlist check.

**Copy for Lancelot:** Implement `AgentMode(PLANNING | EXECUTING | VERIFYING)` as a `lancelot.context` ContextVar; dispatch layer checks it before allowing state-mutating tool calls. The agent transitions via `set_mode(new_mode)` which is itself a tool call (observable, traceable, hookable). PLANNING produces a `plan.md`; VERIFYING produces a `walkthrough.md`.

---

## §2 `task_boundary` tool — the ABI that makes the mode-gate real

Verbatim from `planning-mode.txt:78-109`:

```
TaskName:    Header of the UI block
TaskSummary: Description of this task (update cumulatively to reflect progress)
TaskStatus:  What you WILL do next (future, not past)
Mode:        PLANNING | EXECUTION | VERIFICATION
```

**Key rule:** Call `task_boundary` BEFORE calling other tools in parallel (the `TaskStatus` declares intent). Do NOT call it if the work is 1-2 tool calls (overhead exceeds value).

Two UI behaviors:
- Same `TaskName` + updated `TaskSummary`/`TaskStatus` → accumulates in one UI block
- New `TaskName` → starts a fresh UI block

**Backtrack semantics** (`planning-mode.txt:22`): "When backtracking mid-task (e.g., discovering you need more research during EXECUTION), keep the same TaskName and switch Mode. Update TaskSummary to explain the change in direction." This is the anti-thrash rule — the user sees "agent re-planned because X" rather than silent churning.

---

## §3 `notify_user` — the only channel to the user during task mode

Verbatim from `planning-mode.txt:24-26`:

```
notify_user parameters:
  PathsToReview:           absolute paths to artifact files
  ConfidenceScore:         numeric (inferred 1-10)
  ConfidenceJustification: free-text explanation
  BlockedOnUser:           bool — true ONLY if cannot proceed without approval
```

Every notification surfaces the agent's self-assessed confidence. `BlockedOnUser: false` means the agent is informing, not waiting. The IDE renders a green/yellow/red badge based on `ConfidenceScore`. Batching rule: "Batch all independent questions into one call to minimize interruptions."

**Lancelot relevance:** `notify_user` maps to `X-Lancelot-Notification` response header or an SSE event on a `/tasks/{id}/events` stream. The `ConfidenceScore` + `BlockedOnUser` fields are worth copying as structured fields rather than free text — they let the caller decide whether to block.

---

## §4 Mixin-based prompt assembly with budget fallback

The system prompt is NOT a static string. From binary `cortex/mixins/*` (`ANTIGRAVITY.md §R2.13`):

```go
type Mixin interface {
    ShouldInclude(ctx) bool        // gate: include this turn?
    Content() string               // static full-budget text
    DynamicContent(ctx) string     // compressed fallback when over-budget
    TokenType() TokenBucket        // which budget pool this spends from
    GetJudgeCriteria() *JudgeCriteria
    GetName() string
}
```

Assembly order:
1. Iterate registered mixins, call `ShouldInclude`
2. Sum token budget; if over-budget → use `DynamicContent` (compressed) for lowest-priority mixins
3. Concatenate surviving blocks in topological order
4. Identity prompt first; user-rules mixin last (highest precedence)
5. Per-turn dynamic context (open files, cursor pos, recent diffs) appended

The `DynamicContent` fallback is the key innovation: the harness gracefully degrades when context is tight (uses the compressed variant) rather than hard-dropping mixins. This avoids the "lost system instructions at long context" failure mode.

**Lancelot relevance:** This maps directly to Lancelot's `ContextBundle` design target. Implement `ContextSection` as the mixin interface; `ContextBundle.build(budget)` does the topological merge with DynamicContent fallback. The `TokenType` → `TokenBucket` maps to Lancelot's observation-masking compaction (the lens calls this "observation-masking compaction" in the What Lancelot Lacks section).

---

## §5 `waitForPreviousTools` per-tool DAG parallelism

Every tool in the registry has an optional `waitForPreviousTools?: boolean` (`ANTIGRAVITY.md §17.2`):

```
// If true, wait for all previous tool calls from this turn to complete.
// If false or omitted, execute immediately in parallel with other tools.
```

This is more granular than OpenAI's binary `parallel_tool_calls` (per-request). Antigravity's runtime topologically sorts tool calls by this flag: tools without it run immediately and concurrently; tools with `waitForPreviousTools: true` wait for ALL prior tools in the turn to finish.

**Copy for Lancelot tool dispatch:** Add `wait_for_previous: bool = False` to `ToolSpec`. The Lancelot MCP server and tool dispatch layer (`lancelot.tool`) can enforce this in the asyncio event loop — tools that don't need sequential ordering fire via `asyncio.gather`; tools that do `await` the gather before starting.

---

## §6 KNOWLEDGE SUBAGENT — the durable cross-task memory architecture

Verbatim subagent system prompt (`ANTIGRAVITY.md §R2.4 Prompt 5`):

```
You are operating as a background agent that runs automatically after
conversations to extract and preserve knowledge. This role comes with
important restrictions:
You have a different set of tools than the main agent, limited to doing
research and editing the knowledge base. These are the ONLY tools that are
available to you.
You can ONLY create, update, or delete files in the authorized knowledge
base locations, listed. Any attempt to modify files outside of these
locations will result in an error.
Note: You can use research tools to read files in any location, you just
cannot modify them if they are not in the authorized locations.
```

KI filesystem layout (`ANTIGRAVITY.md §2.6`):
```
~/.gemini/antigravity/knowledge/<ki-id>/
  metadata.json   ← summary, timestamps, references to original sources
  artifacts/      ← related files, documentation, implementation details
```

The subagent fires via `stop_hook` (§R2.6) — after every task completion. It has a RESTRICTED toolset: read-everywhere, write ONLY to the KI allowlist. The main agent gets KI summaries pre-loaded at conversation start and must check them BEFORE doing independent research (verbatim: "MANDATORY FIRST STEP: Check KI Summaries Before Any Research").

**Lancelot relevance:** This is the typed memory → cross-task persistence layer the lens identifies as missing. The `stop_hook` trigger maps to Lancelot's `stop_hooks` in the hooks ABI (§R2.6 below). KI storage is just a structured directory; the memory agent is a restricted subagent call with `Model: fast` and a tool allowlist. Cheap to implement on top of the existing `lancelot.secrets`/filesystem layer.

---

## §7 The 5-stage hooks ABI

From binary `cortex/customization/hooks/jsonhook` (`ANTIGRAVITY.md §R2.6`):

| Stage | When it fires | Primary use case |
|---|---|---|
| `pre_invocation` | Before each LLM call | Inject context, redact PII |
| `post_invocation` | After each LLM call | Capture token usage, output scan |
| `pre_tool` | Before each tool call | DLP / safety classifier |
| `post_tool` | After each tool result | Prompt-injection detection on results |
| `stop` | When agent decides to stop | KI-distillation trigger, state snapshot, commit-message gen |

Hooks are **declarative JSON configs** (`enable_json_hooks` boolean gates the subsystem), not code callbacks — users ship hooks via `~/.gemini/antigravity/hooks.json` without touching the binary. Each hook gets the trajectory context + stage-specific payload (LLM req/resp for invocation, tool name/args/result for tool, finish reason for stop).

**Lancelot already has** `LANCELOT_HEADER_TIMEOUT_MS`, `LANCELOT_MAX_CONNECTIONS`, etc. as runtime knobs. The hooks ABI extends this to **semantic lifecycle events** — a clean place to plug safety classifiers, cost guards, and KI distillation without hardcoding them into the agent loop. The `stop_hook` is the right trigger for the knowledge-subagent dispatch described in §6 above.

---

## §8 Subagent invocation ABI — 3-tier model routing + workspace branching

Full `invoke_subagent` argument schema (`ANTIGRAVITY.md §R2.5`):

| Arg | Description |
|---|---|
| `TaskDescription` | "A clear, actionable task description. Be specific about what to return." |
| `Role` | "A 2-5 word job-title description. E.g. 'Codebase Researcher'." |
| `Model` | `inherit` (default) / `fast` (Flash) / `heavy` (Ultra) |
| `Tools` | List of tool names; empty = inherit defaults |
| `CustomPromptSections` | Array of `{title, content}` injected into subagent system prompt |
| `WorkspaceMode` | `inherit` (share parent's workspace) / `branch` (git worktree) |
| `ResumeFromSubagentId` | Resume a cancelled subagent from its checkpoint |

The `Model: fast / heavy` enum is the cost-optimisation primitive: most research/lookup subagents use `fast` (Flash); deep reasoning tasks use `heavy` (Ultra). **The default is `inherit`** — conservative, no surprise cost escalation.

Subagent return-value protocol (verbatim Prompt 2, `ANTIGRAVITY.md §R2.4`):

```
You MUST use send_message to communicate all results, reports, and updates back
to the caller. Your response is NOT automatically relayed. If you do not call
send_message, the caller will only know that you have gone idle. Always use the
caller's id as the Recipient and %[1]q as the RecipientName.
Text you generate outside of send_message will NOT be seen by the caller.
```

This is the **async-mailbox idiom**: the parent does NOT wait synchronously; the subagent pushes results into a `ManageMessageToolConfig` inbox. The parent polls via `manage_message(action='list')` or subscribes to events. The `ResumeFromSubagentId` checkpoint mechanism lets a cancelled subagent be resumed with the same context.

**Lancelot relevance:** `TransportPool.spawn_child` + `TransportPool.fanout` are the byte-path analogue. This adds the orchestration-layer semantics on top: `Model` tier routing maps to `TransportPool`'s provider routing; `WorkspaceMode: branch` maps to `git worktree` isolation; the mailbox maps to a Lancelot SSE stream or the `lancelot.context` ContextVar chain.

---

## §9 `ScheduleTaskToolConfig` — verbatim cron spec for wake-up scheduling

Full tool prompt (verbatim, `ANTIGRAVITY.md §R2.7`):

```
Schedule a one-shot timer or a recurring cron job that sends notifications
in the background.

NOTE: This tool call returns immediately and does not pause execution.
To wait for the timer to fire, you must stop calling tools to end your turn.

1. One-shot timer: DurationSeconds + Prompt
   - Cancelled automatically if ANY message arrives from ANY task or subagent
2. Recurring cron: CronExpression (5-field standard), MaxIterations, Prompt

Never run a background 'sleep' command to set a timer, use this tool instead.
```

Data model: `DurationSeconds | CronExpression` (mutually exclusive), `MaxIterations` (optional cap), `Prompt` (wake-up message), returns `task_id` (cancellation handle). Cron format: `minute hour dom month dow` (5-field, no seconds).

The one-shot timer auto-cancels on ANY incoming message — it's a "wake me up if nothing else does" mechanism for subagent orchestration (fire subagents, go idle, get woken up by either subagent `send_message` or the timer, whichever comes first).

**Lancelot relevance:** Implementable in <200 LOC. The 5-field cron parser maps directly to `lancelot.mcp.mount`'s scheduling surface or a new `lancelot.schedule` primitive. The one-shot timer with auto-cancel-on-message is the correct wake-up mechanism for long-running agent tasks that spawn subagents and need a deadline. The Lancelot `drain_timeout` + SIGTERM mechanics are the server-side analogue.

---

## §10 FastApply prompt — the apply-model pattern

Verbatim Prompt 3 (`ANTIGRAVITY.md §R2.4`):

```
You are a helpful assistant whose purpose is to help a user edit a code document.
You will be given the full contents of the code document as well as the change
that the user wants to make. Your task will be to make the change and return the
full contents of the new code document after the change.
Please make the change faithfully and without making any unrelated edits. Be
sure to keep pre-existing comments/empty lines that are not explicitly removed
by the change, and to respond with only the new file and nothing else. Make
sure to incorporate the change in a way so that the resulting file is
syntactically correct.
```

This is a **separate, smaller LLM call** (the `cortex/fastapply` package). The main reasoning loop decides WHAT to change; a smaller/faster model does the mechanical APPLY. This decoupling means the expensive model handles only intent; a cheap model handles execution.

**Lancelot relevance:** For Lancelot's tool-output coercion and structured-output path (`lancelot.tool` + `@app.tool`), this maps to a `fast` model call for schema coercion — when the main agent produces JSON that doesn't match the tool's output schema, the apply-model attempts the coercion rather than failing. Not a byte-path primitive, but worth copying the separation of intent vs. apply.

---

## §11 User-rules precedence statement — verbatim copy text

From binary `strings-min100.txt:48890` (`ANTIGRAVITY.md §R2.9`):

```
The following are user-defined rules that you MUST ALWAYS FOLLOW WITHOUT ANY
EXCEPTION. These rules take precedence over any following instructions.
```

The rules source is `AGENTS.md` + `GEMINI.md` at workspace root (the `Manager.readGlobalRules` symbol confirms this). Conditional memories carry trigger predicates (e.g. "when editing TypeScript files, apply this rule") and only render when the predicate fires; unconditional memories always render.

The rule-preamble is injected by `UserRulesSection` mixin AFTER tool descriptions and BEFORE per-turn context — highest-priority position in the assembled prompt.

**Lancelot relevance:** Lancelot's `CLAUDE.md`/`LANCELOT.md` workspace file convention is the same mechanism. The key copy is the precedence phrasing — "take precedence over any following instructions" must be verbatim to work reliably with modern frontier models.

---

## §12 Safety-classifier copy text for destructive tools

Verbatim from `tool-arg-descriptions.txt`, applied to `run_command`, `execute_browser_javascript`, `send_input` (`ANTIGRAVITY.md §R2.17`):

```
"Set to true if you believe that this command is safe to run WITHOUT user
approval. A command is unsafe if it may have some destructive side-effects.
Example unsafe side-effects include: deleting files, mutating state,
installing system dependencies, making external requests, etc. Set to true
only if you are extremely confident it is safe. If you feel the command
could be unsafe, never set this to true, EVEN if the USER asks you to. It
is imperative that you never auto-run a potentially unsafe command."
```

The critical clause: "never set this to true, EVEN if the USER asks you to" — this is the permission override. The field is `SafeToAutoRun: bool` in the tool schema; the description IS the safety classifier prompt.

**Lancelot relevance:** Copy this as the `jsonschema_description` for any Lancelot tool that can make external requests or mutate state. The `lancelot.net` SSRF guard and `lancelot.webhook` sign/verify cover the transport-layer side; this covers the agent-reasoning side.

---

## §13 Three-state task checklist notation

From `planning-mode.txt` task.md spec (`ANTIGRAVITY.md §2.5`):

```
[ ]  uncompleted
[/]  in-progress (the key innovation — not in Manus todo.md)
[x]  completed
```

The `[/]` marker is unique to Antigravity. It lets the task view UI render live progress within a single task without needing to diff the markdown — the agent marks items `[/]` BEFORE working on them (showing intent) and `[x]` AFTER.

**Lancelot relevance:** For any Lancelot agent that produces a plan/checklist, use this 3-state convention. It's a trivial addition to any `plan.md` format but provides substantially better UX when streaming task progress to a UI or SSE client.

---

## §14 Multi-chunk edit metadata — importance + lint attribution

From `tool-arg-descriptions.txt` for `multi_replace_file_content` (`ANTIGRAVITY.md §R2.16`):

Each chunk ships 4 metadata fields:
- **`importance`**: `enum high|medium|low` — "Use 'high' for edits directly addressing the main request or fixing critical issues, 'medium' for supporting changes, 'low' for minor improvements."
- **`lint_ids`**: IDs of lint errors this edit fixes (only if the edit was influenced by lint feedback)
- **`explanation`**: "Brief, user-facing explanation. Focus on non-obvious rationale, design decisions. Don't just restate what the code does."
- **`complexity`** (on the outer tool, not per-chunk): 1-10, "A 1-10 rating of how important it is for the user to review this change."

These fields power the IDE's "this edit fixed lints X, Y, Z" badge + the auto-accept-low-complexity flow. No other agent framework has an explicit complexity rating on edits.

**Lancelot relevance:** When Lancelot generates tool output involving code changes (via `@app.tool` or the proposed agent layer), adding `importance + explanation` metadata to each change is a low-cost addition that substantially improves human-review UX. The `complexity: 1-10` could map to a `review_required: bool` field in Lancelot's HITL (human-in-the-loop) durable seam target.

---

## §15 Knowledge subagent stop_hook trigger pattern

From `ANTIGRAVITY.md §R2.6`: the KI background distillation fires on `stop_hook`. The `stop_hooks` array is configured in the hooks ABI and receives the finish reason + trajectory context.

The architectural pattern:
1. Main agent completes a task → emits `Finish` CortexStep
2. `stop_hook` chain fires
3. One hook dispatches a KNOWLEDGE SUBAGENT (`Model: fast`, restricted toolset) with the conversation log path
4. Subagent reads the logs, identifies new knowledge, writes `metadata.json` + `artifacts/` to the KI allowlist
5. Future conversations get the KI summaries pre-loaded in the context mixin

The trigger is LIGHTWEIGHT (stop_hook fires once per task completion, not per turn). The subagent is `Model: fast` (cheap). The write surface is restricted (can't contaminate the workspace). This is **safe background distillation**.

**Lancelot relevance:** Map `stop_hook` to `App.add_exception_handler` + lifecycle event pattern already in Lancelot. The knowledge distillation call goes through `TransportPool` with `Model: fast` routing. The KI allowlist is a simple directory check in `lancelot.secrets.FileBackend` territory.

---

## Anti-patterns to avoid

### Anti-pattern 1: Single-mode agentic loop (no state machine)
Running the agent in one mode where it can research, edit, and verify simultaneously leads to thrashing (re-planning without making progress) and scope creep (editing unrelated files). The mode gate is the fix. (`ANTIGRAVITY.md §2.2`)

### Anti-pattern 2: Static system prompt
A flat string system prompt cannot adapt to context budget, cannot inject conditional rules, and can't be updated per-turn without full prompt reconstruction. The mixin assembler with `ShouldInclude` + `DynamicContent` fallback is the fix. (`ANTIGRAVITY.md §R2.13`)

### Anti-pattern 3: Synchronous subagent dispatch (blocking parent)
If the parent agent blocks waiting for subagent results, concurrency is lost. The correct pattern is the async mailbox: parent fires subagent, goes idle (stops calling tools), gets woken by `send_message` OR a schedule-task timer. (`ANTIGRAVITY.md §R2.4 Prompt 2, §R2.7`)

### Anti-pattern 4: Replicating at `tool_use`/`parallel_tool_calls` level
OpenAI's binary `parallel_tool_calls` is per-request. The `waitForPreviousTools` per-tool flag is more granular — it lets the agent express DAG dependencies within a turn without re-serializing the whole turn. Don't collapse this to the coarser flag. (`ANTIGRAVITY.md §17.2`)

### Anti-pattern 5: KI distillation in the hot path
Running knowledge extraction synchronously in the main agent loop adds latency and context overhead per turn. The correct architecture is `stop_hook` + background subagent after task completion. (`ANTIGRAVITY.md §R2.6, §11.3`)

---

## Byte-path implication for Lancelot

Lancelot's byte path ALREADY provides the transport primitives that Antigravity's architecture depends on:

- **`TransportPool.spawn_child` + `fanout`** → maps to `invoke_subagent` with `Model: inherit/fast/heavy` routing. Lancelot's version adds mTLS, traceparent propagation, and Retry-After handling that Antigravity's harness lacks.
- **`lancelot.context` ContextVars** → `agent_id`, `request_id`, `traceparent` are already propagated; `task_mode` (PLANNING/EXECUTING/VERIFYING) would be one new ContextVar.
- **`lancelot.mcp.mount`** → Antigravity's `McpServerToolConfig` / `McpToolConfig` registry. Lancelot's minimal-return discipline (WARN >4 KB) corresponds to the `MCP_AUTH_PROVIDER_TYPE` + `background: off|always` flags.
- **`lancelot.batch` `@app.batch`** → no direct Antigravity analogue; Antigravity lacks request coalescing (it's a coding agent, not an API gateway).
- **`lancelot.upstream_metrics`** → maps to Antigravity's `RecordTrajectoryAnalytics` gRPC call + token usage extraction. Lancelot's provider detection already covers OpenAI/Anthropic/Google/etc.

What Lancelot LACKS that Antigravity has:
1. **Mode gate ContextVar** — `AgentMode` ContextVar + per-step dispatch check
2. **Mixin assembler** — `ContextBundle` with `ShouldInclude` + `DynamicContent` fallback
3. **Knowledge subagent** — `stop_hook` → `TransportPool` call with restricted tool allowlist
4. **`waitForPreviousTools` dispatch** — per-tool DAG dependency flag in `ToolSpec`
5. **Confidence scoring** — `ConfidenceScore` + `BlockedOnUser` on HITL notifications
6. **Schedule primitive** — one-shot timer + 5-field cron with auto-cancel-on-message

Items 1, 4, 5 are hours of work each. Item 6 is <200 LOC. Item 2 is the core of the ContextBundle design target. Item 3 combines existing primitives (TransportPool, stop_hook, FileBackend).

---

## API shape notes (worth stealing)

1. **`task_boundary(TaskName, TaskSummary, TaskStatus, Mode)`** — `TaskStatus` describes WHAT YOU WILL DO NEXT (future), not what you did. This prevents retrospective summaries that are useless for live UI rendering.

2. **`notify_user(PathsToReview, ConfidenceScore, ConfidenceJustification, BlockedOnUser)`** — `BlockedOnUser: false` is informational; `true` is a blocking gate. Separating these eliminates the "agent is stuck or just informing?" ambiguity.

3. **`invoke_subagent(Role: "2-5 word job title")`** — the Role field is a job-title, not a description, intentionally. A job-title is memorable and distinct; a description is verbose. "Database Debugger" vs "An agent that debugs databases". The short form fits in a UI chip.

4. **`multi_replace_file_content` chunk metadata: `importance: high|medium|low`** — tristate importance is more actionable than a 1-10 complexity scale for auto-accept decisions. Use tristate for user-facing controls; use 1-10 for nuanced sorting.

5. **`ScheduleTaskToolConfig` one-shot auto-cancel** — "If you receive any message from ANY task or subagent before the timer expires, the timer is cancelled silently." This is the correct default for a subagent orchestration timer — silence = something is wrong; any signal = task is alive.

6. **`SafeToAutoRun` in `jsonschema_description`** — embed the safety classifier logic in the tool's own schema description, not in the system prompt. This means the safety rule travels with the tool regardless of which system prompt is active.
