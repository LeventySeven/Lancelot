# Claude Code — Lancelot Agent-Framework Distillation

> Source: `/Users/seventyleven/Desktop/researchfms/teardowns/CLAUDE_CODE.md`
> Rounds: Multi-agent (Floor), Round-2 (npm 2.1.132), Round-N (2.1.144), Round-3 (2.1.145)
> Focus: cache split, stop reasons, context introspection, deferred tools — exact mechanisms.

---

## 1. Static/Dynamic Cache Split — `SYSTEM_PROMPT_DYNAMIC_BOUNDARY`

**Mechanism (KNOWN, Round-3 §12, `sdk.d.ts:5591–5599`):**

```typescript
export declare const SYSTEM_PROMPT_DYNAMIC_BOUNDARY = "__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__";
```

The string is exported as a constant. Any custom `systemPrompt: string[]` that includes this literal as a standalone array element causes the harness to apply `cache_control: { type: "ephemeral", ttl: "1h" }` to every block *before* the marker, and `cache_control: { type: "ephemeral", ttl: "5m" }` to every block *after* it.

**Exact wire shape (Round-2 §13.4, `sdk.mjs`):**

```json
{
  "system": [
    { "type": "text", "text": "<static prefix>",
      "cache_control": { "type": "ephemeral", "ttl": "1h" } },
    { "type": "text", "text": "<dynamic suffix>",
      "cache_control": { "type": "ephemeral", "ttl": "5m" } }
  ]
}
```

The API accepts up to **4 cache breakpoints per request**. CC places them at:
1. End of static prefix → 1h
2. End of dynamic suffix (workdir, CLAUDE.md) → 5m
3. End of tool definitions block → 5m
4. End of last "stable" user message in long conversation → 5m

**Dynamic suffix contents** (Round-2 §3.8): working directory, OS/shell info, CLAUDE.md, auto-memory MEMORY.md index, today's date, git status, active skills, slash commands. These are *not* cross-session stable — hence 5m TTL or no cache.

**`excludeDynamicSections: true`** (Round-2 §3.8): strips the dynamic suffix from the system prompt and re-injects it as the first user message. Tradeoff verbatim: "the working-directory, memory-path, and git-status context is marginally less authoritative for steering the model. The first user message becomes slightly larger." Use for fleet-wide shared system prompts.

**Lancelot angle:** `lancelot.canonical.dumps_canonical` already provides stable JSON for KV-cache prefix stability. The `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` pattern is the transport-layer counterpart: mark exactly where prompt stability ends. When Lancelot's agent loop assembles a system prompt from a static policy section + per-request CLAUDE.md + current tools, insert the marker at the static/dynamic boundary. The static prefix gets the free 1h cache hit; only the dynamic tail burns tokens.

---

## 2. `TerminalReason` — 12 Stop Values

**Source: Round-3 §14, `sdk.d.ts:5636`.**

```typescript
export declare type TerminalReason = 
  'blocking_limit'        // safety system blocked
  | 'rapid_refill_breaker' // DOS circuit-breaker for rapid-fire requests
  | 'prompt_too_long'      // context exceeded after compaction failed
  | 'image_error'          // image input rejected
  | 'model_error'          // API-level model error
  | 'aborted_streaming'    // stream aborted mid-turn
  | 'aborted_tools'        // tool execution aborted
  | 'stop_hook_prevented'  // a Stop hook returned decision: 'block'
  | 'hook_stopped'         // a hook returned continue: false
  | 'tool_deferred'        // tool deferred and loop waited beyond budget
  | 'max_turns'            // hit maxTurns limit
  | 'completed';           // normal end_turn completion
```

Round-2 documented only 4 (`max_turns`, `completed`, `model_error`, `aborted_streaming`). The full 12 matter for the agent loop's error recovery logic.

**Complementary ResultMessage subtypes (Floor §7, `ResultMessage.subtype`):**

```python
subtype: "success" 
       | "error_max_turns" 
       | "error_max_budget_usd"
       | "error_during_execution"
       | "error_max_structured_output_retries"
```

The two enum sets overlap but are at different levels: `TerminalReason` is query-loop-level (why the stream ended); `ResultMessage.subtype` is the SDK result-message-level (what the SDK returns after the loop terminates).

**SDKResultMessage `stop_reason` values (Round-2 §21, `sdk.d.ts`):**
- `"end_turn"` — normal
- `"tool_deferred"` — PreToolUse hook returned `"defer"` (non-interactive `-p` mode only)
- `"tool_deferred_unavailable"` — deferred tool no longer available on resume (MCP server disconnected)
- `"max_tokens"` — hit context limit
- `"refusal"` — model refused

**SessionEnd `reason` values (Round-3 §2, `sdk.d.ts:3716`):**
`'clear' | 'resume' | 'logout' | 'prompt_input_exit' | 'other' | 'bypass_permissions_disabled'`

**For Lancelot's agent loop:** A production loop must distinguish:
- `completed` → extract result and continue parent workflow
- `max_turns` / `error_max_turns` → budget exceeded, summarize + continue or escalate
- `stop_hook_prevented` → a quality-gate hook fired, read hook's reason, inject as next user message
- `hook_stopped` (`continue: false`) → hard stop, propagate error to caller
- `rapid_refill_breaker` → back off and retry with exponential delay
- `blocking_limit` → safety block, do NOT retry the same prompt
- `prompt_too_long` → compaction failed; must reduce context before retrying
- `tool_deferred` → HITL pause; resume with same session ID after user resolves

---

## 3. Context Introspection — `getContextUsage()`

**Source: Round-3 §3 / Round-N §RN.13, `sdk.d.ts:2214–2257`.**

```typescript
getContextUsage(): Promise<SDKControlGetContextUsageResponse>
```

Returns token breakdown by category:

```typescript
SDKControlGetContextUsageResponse = {
  totalTokens: number,
  maxTokens: number,
  rawMaxTokens: number,
  percentage: number,
  gridRows: { color, isFilled, categoryName, tokens, percentage, squareFullness }[][],
  model: string,
  memoryFiles: { path, type, tokens }[],
  mcpTools: { name, serverName, tokens, isLoaded? }[],
  deferredBuiltinTools: { name, tokens, isLoaded }[],
  systemTools: { name, tokens }[],
  systemPromptSections: { name, tokens }[],
  agents: { agentType, source, tokens }[],
  slashCommands: { totalCommands, includedCommands, tokens },
  skills: { totalSkills, includedSkills, tokens, skillFrontmatter[] },
  autoCompactThreshold: number,
  isAutoCompactEnabled: boolean,
  messageBreakdown: {
    toolCallTokens, toolResultTokens, attachmentTokens,
    assistantMessageTokens, userMessageTokens,
    redirectedContextTokens, unattributedTokens,
    toolCallsByType: { name, callTokens, resultTokens }[],
    attachmentsByType: { name, tokens }[]
  },
  apiUsage: { input_tokens, output_tokens,
              cache_creation_input_tokens, cache_read_input_tokens } | null
}
```

This is the exact shape behind `/usage` and `/cost` slash commands. The `isLoaded` field on `mcpTools` and `deferredBuiltinTools` reveals which tools are in the live prompt vs deferred behind ToolSearch.

**Companion: pre-flight token counting (Round-2 §3.2):** CC calls `POST https://api.anthropic.com/v1/messages/count_tokens` (beta: `token-counting-2024-11-01`) before building a turn. This is how the UI shows "this will use ~X tokens" without running the full API call.

**Cache creation tracking (Round-2 §2.17, AgentOutput):** The usage object tracks both TTLs separately:

```typescript
cache_creation: {
  ephemeral_1h_input_tokens: number,
  ephemeral_5m_input_tokens: number
}
```

Anthropic charges differently for 1h vs 5m writes; the agent layer must track both to report accurate costs.

**Lancelot angle:** `lancelot.context` already has `cost_usd`, `tokens_in`, `tokens_out` ContextVars, and `lancelot.upstream_metrics` tracks cache tokens. The `getContextUsage` shape is the model for a `/context` or `/usage` diagnostic endpoint that Lancelot's agent layer should expose: per-section token costs, deferred vs loaded tools, MCP tool load state. Implement as a `@app.get('/agent/context')` that reads the current turn's context assembly and returns the breakdown. The `systemPromptSections` sub-array is particularly useful — it makes visible which CLAUDE.md-equivalent files are consuming tokens.

---

## 4. Deferred MCP Tools — ToolSearch Pattern

**Source: Round-2 §8.4, `sdk.d.ts:940, 1045, 1056`.**

```typescript
// McpServerConfig (any transport)
alwaysLoad?: boolean;
// When true: all tools from this server are always included in the prompt and never deferred.
// Default: tools are deferred when tool search is enabled.
// Side effect: blocks startup until connected (capped at 5s connect timeout) even when
// MCP_CONNECTION_NONBLOCKING=1, since tools must be present at turn-1.
```

When many MCP servers are connected, loading all tool schemas inline burns ~1KB × N_tools tokens. CC's solution: a `ToolSearch` meta-tool that the model calls to discover and load specific tool schemas on demand. Only `alwaysLoad: true` tools (safety-critical / high-frequency) are inlined at startup. This is precisely the `ToolSearch` deferred-tool system visible in the current session's system reminder.

**The ToolSearch tool (from this session's system-reminder):** Takes a `query` string matching against tool names/descriptions, returns up to `max_results` fully-expanded JSONSchema definitions. The model calls it when it needs to use a tool it can see by name but not by schema.

**Deferred tool token accounting (Round-2 §3.9):** `deferredBuiltinTools: { name, tokens, isLoaded }[]` — "tools deferred behind tool search." Isloaded = false means the tool schema hasn't been fetched yet this turn; loading it will cost `tokens` tokens.

**MCP connect timeout: 5 seconds** (verbatim, `sdk.d.ts:1056`). `MCP_CONNECTION_NONBLOCKING=1` lets connections happen async, but `alwaysLoad` overrides it (tools must be present at turn 1).

**Lancelot angle:** `lancelot.mcp.mount` already implements MCP server mounting with a >4KB minimal-return warning. The deferred-tool pattern extends this: when mounting many MCP servers, expose only lightweight "registration" stubs in the system prompt, with lazy full-schema loading on first use. For Lancelot's agent `@app.tool` surface, tools used infrequently should be registered in a "lazy" catalog rather than inlined in every system prompt. The implementation hook: `tool_schemas()` could return a two-tier response — a compact name+description catalog (always sent) and a full JSONSchema available via a `resolve_tool_schema(name)` call.

---

## 5. Subagent Context Isolation and Output Contract

**Source: Floor §9, §2.17 (`AgentOutput`), Round-2 §12.**

The canonical "only the final message returns" contract (verbatim, `Claude Code 2.0.txt:735–737`):

> "Each agent invocation is stateless. You will not be able to send additional messages to the agent, nor will the agent be able to communicate with you outside of its final report. Therefore, your prompt should contain a highly detailed task description for the agent to perform autonomously and you should specify exactly what information the agent should return back to you in its final and only message to you."

AgentOutput shape (the parent's view):

```typescript
AgentOutput = {
  agentId: string;
  content: { type: "text"; text: string }[];
  totalToolUseCount: number;
  totalDurationMs: number;
  totalTokens: number;
  usage: {
    input_tokens, output_tokens,
    cache_creation_input_tokens, cache_read_input_tokens,
    cache_creation: { ephemeral_1h_input_tokens, ephemeral_5m_input_tokens } | null,
    service_tier: "standard" | "priority" | "batch" | null
  };
  toolStats: { readCount, searchCount, bashCount, editFileCount,
               linesAdded, linesRemoved, otherToolCount };
  status: "completed";
}
// OR for background launch:
| { status: "async_launched"; agentId: string; description: string; }
```

Parent only receives the aggregate — intermediate tool calls stay in the subagent's context. **Service tiers `priority`/`batch`** tracked per subagent so cost attribution works across mixed-tier runs.

**Per-agent-type memory directories (Round-N §RN.11, `sdk.d.ts:81–85`):**

```typescript
memory?: 'user' | 'project' | 'local';
// user  → ~/.claude/agent-memory/<agentType>/
// project → .claude/agent-memory/<agentType>/
// local   → .claude/agent-memory-local/<agentType>/
```

Specialized subagents accumulate domain knowledge across invocations in their own memory tree, separate from the parent agent's CLAUDE.md. This is the mechanism for building specialists that improve over time without polluting parent context.

**Lancelot angle:** `lancelot.llm.TransportPool.spawn_child` already implements the "snapshot parent traceparent + request_id" primitive. The `AgentOutput.toolStats` breakdown and `usage.cache_creation` tracking are the metrics an agent coordinator needs to report accurate costs and detect runaway subagents (e.g., `totalToolUseCount > threshold` before stopping a looping subagent). The per-agent-type memory pattern maps directly to Lancelot's missing "typed memory" tier — a `~/.lancelot/agent-memory/<agentType>/` directory that the agent's system prompt auto-loads.

---

## 6. Hooks — Quality-Gate Patterns

**Source: Round-2 §7, Round-3 §2.**

The most load-bearing hook for agent quality gates is **`Stop`** with `decision: "block"`:

```json
// PostToolBatch output — kill switch for runaway loops
{ "decision": "block", "reason": "Cost threshold exceeded: $0.50" }

// Stop hook output — forces Claude to keep generating
{ "decision": "block", "reason": "Tests failed: 3 failures in auth suite" }
```

**`PostToolBatch`** fires after ALL parallel tool calls in a batch complete, before the next API call. This is the correct point to inject cost-gate, rate-gate, or quality-gate logic — it fires once per model turn, not once per tool.

**`asyncRewake`** (Round-2 §7.7): a command hook can run async without blocking Claude, but if it eventually returns exit code 2, the harness wakes the model mid-task with the error. This enables slow validators (CI run, integration test) to gate the agent loop without adding latency to fast paths.

**`UserPromptSubmit` with `suppressOriginalPrompt`** (Round-3 §2, `sdk.d.ts:5750–5758`): hook can inject `additionalContext` AND suppress the original user prompt — allowing prompt pre-processing/sanitization before the model sees it.

**Hook precedence** (verbatim, Round-2 §11.9):
`deny rules > ask rules > blocking hook (exit 2) > allow rules > non-blocking hook decisions`

**29 events total** (Round-3 §2). For Lancelot agent layer the critical subset:
- `PreToolUse` — allow/deny/mutate input before any tool executes
- `PostToolBatch` — aggregate gate after each model turn's tools
- `Stop` — quality-gate when model thinks it's done
- `SubagentStop` — inspect subagent transcript path + final message before returning to parent
- `PreCompact` — save/transform context before summarization

**Lancelot angle:** Lancelot already has per-route timeout and body-cap at the HTTP layer. The agent layer needs the same primitives at the turn layer: a `max_tool_calls_per_turn`, `max_cost_per_turn`, `max_turns` enforced via a `PostToolBatch` hook equivalent. The `asyncRewake` pattern is worth replicating exactly — it decouples validator latency from agent latency, which matters when validation (e.g., running a test suite) takes 30s+ but the agent should not stall.

---

## 7. Task Budget — Self-Pacing via `output_config.task_budget`

**Source: Round-N §RN.8, `sdk.d.ts:1474–1483`.**

```http
POST /v1/messages
anthropic-beta: task-budgets-2026-03-13
{
  "output_config": { "task_budget": { "total": 100000 } }
}
```

**Behavioral contract (verbatim):** "the model is made aware of its remaining token budget so it can pace tool use and wrap up before the limit."

This is distinct from the harness-side budget controls:
- `taskBudget` → **model self-pacing** (trained behavior, free, Claude-only)
- `maxBudgetUsd` → **harness circuit breaker** (stops with `error_max_budget_usd`, provider-agnostic)
- `maxTurns` → **turn counter** (stops after N user-assistant pairs)

The three form defense-in-depth for agent cost control: model paces itself first, harness cuts it if it exceeds USD budget, and turn counter provides a hard fallback regardless of cost or model behavior.

**Lancelot angle:** `lancelot.context.cost_usd` already accumulates per-request cost. The `output_config.task_budget` field can be set by `TransportPool.request()` — it's just an extra JSON field in the messages request. Implement as `pool.request(..., task_budget_tokens=N)` kwarg that adds the beta header + field when N is set. This gives Claude agents on Lancelot free self-pacing at zero extra implementation cost. For non-Anthropic models, the harness-side `max_turns` + `max_budget_usd` are the fallback.

---

## 8. Three-Mode Compaction (Client → Server Migration)

**Source: Round-2 §9, Round-3 §6.**

**Client-side auto-compaction trigger (Round-2 §9.3):**
- Threshold: `autoCompactWindow` (default ~95% of context)
- Override: `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=50` for earlier triggers
- Three modes: full compact, suffix-preserving (keep recent N messages), prefix-preserving

**Server-side compaction (Round-3 §6, `sdk.mjs:228112`):**

```python
# New: server-side in a single API call
edits = [{ "type": "compact_20260112" }]

# Old (deprecated): client-side — a second API call to a compaction model
# compactionControl = { enabled: True, contextTokenThreshold: 165_000,
#                       model: compact_model, summaryPrompt: DEFAULT_COMPACT_PROMPT }
```

The date suffix `20260112` is Anthropic's versioning convention for beta primitives (like `prompt-caching-2024-07-31`). The server-side variant collapses the client's two-step (check threshold → call compaction model) into one directive.

**Skills survive compaction (Round-2 §5.5):** After compaction, re-attached skills get:
- 5,000 tokens kept per skill (head only)
- 25,000 tokens combined budget across all skills
- Filled newest-first

**`compact` `load_reason` (Round-2 §10.4):** CLAUDE.md is re-loaded after compaction. This means compaction triggers a `SessionStart(source: "compact")` hook — the correct place to re-inject context that summarization would drop.

**Lancelot angle:** Lancelot's streaming and context layers don't currently manage agent-level context compaction. When building the agent loop's context management, the `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` marker (§1) handles the static/dynamic split; compaction handles the turn history. For Lancelot agents using Anthropic: the `compact_20260112` edit type is the correct lever. For other models: implement the client-side threshold check + a second compaction call. In both cases, a `PreCompact` hook equivalent should save any in-flight tool state before summarization.

---

## 9. Per-Agent-Type Subagent Routing — `description` Field

**Source: Floor §3, Round-2 §12, `AgentDefinition`.**

The `description` field is the **routing mechanism** — how the orchestrator decides which subagent to call:

```python
@dataclass
class AgentDefinition:
    description: str   # REQUIRED: natural language routing ("When to use this agent")
    prompt: str        # REQUIRED: the agent's system prompt
    tools: list[str] | None = None
    model: Literal["sonnet", "opus", "haiku", "inherit"] | None = None
    skills: list[str] | None = None
    memory: Literal["user", "project", "local"] | None = None
```

**Model resolution order (Floor §3):**
1. `CLAUDE_CODE_SUBAGENT_MODEL` env var
2. Per-invocation `model` parameter
3. Agent definition `model` frontmatter
4. Main conversation's model

`inherit` (default) means the subagent uses the same model as the parent. For cost-sensitive deployments, set specialized subagents to `haiku`/`sonnet` and reserve `opus` for orchestrator decisions only.

**`criticalSystemReminder_EXPERIMENTAL`** (Round-2 §18, TypeScript only): an experimental field in `AgentDefinition` that injects a critical reminder into the subagent's system prompt. Not in Python SDK as of April 2026.

**Lancelot angle:** `lancelot.llm.TransportPool.spawn_child` handles the transport layer. The `description`-based routing is a model-side concern — the orchestrator Claude sees the description in the tools list and decides which subagent to call. For a Lancelot agent layer, register available subagents in the system prompt using the same `description` field pattern; the model routes to them without explicit if/else logic in application code.

---

## 10. Anti-Patterns to Skip

**1. Inlining all MCP tool schemas in the system prompt** — kills prompt budget. Use the deferred/ToolSearch pattern; only `alwaysLoad` for the 3-5 most-used tools.

**2. Per-chunk validation on streams** — already in Lancelot's Tier-D. Confirmed by CC's `@app.stream` pattern: validate input once at request time, not per chunk.

**3. Claiming `bypassPermissions` mode is safe without an OS sandbox** — CC documents this explicitly: sandbox is opt-in, so `Read(./.env)` deny rules don't block `cat .env` in Bash. At the agent layer, hook-based permission rules are advisory; OS-level enforcement requires a separate sandbox primitive.

**4. `compactionControl` client-side loop** (deprecated as of 2026-01) — use `edits: [{ type: "compact_20260112" }]` or the threshold-and-call pattern. The old two-step approach adds an unnecessary second API round-trip.

**5. Single-process multi-threaded Rust agent server** — anti-scales (GIL). Already in Lancelot's Tier-D. CC's lesson confirms: the binary is multiprocess per-core, not threaded.

---

## 11. API Shape Notes

**`TerminalReason` enum** (§2) — use exactly these 12 string values in Lancelot's `StopReason` type. The names are stable across SDK versions (Round-3 corrects Round-2's incomplete 4-value list).

**Two-TTL cache annotation** — the `ephemeral_1h` / `ephemeral_5m` split maps directly to Lancelot's static system-prompt (agent identity + tools, 1h) and dynamic per-turn context (workdir, memory, date, 5m). Implement as a sentinel constant `LANCELOT_PROMPT_DYNAMIC_BOUNDARY = "__LANCELOT_PROMPT_DYNAMIC_BOUNDARY__"` following the same array-element convention.

**`getContextUsage()` shape** — the `systemPromptSections`, `mcpTools.isLoaded`, `deferredBuiltinTools.isLoaded` fields are the minimum needed for a Lancelot `/agent/context` diagnostic endpoint. The rest (gridRows, etc.) are UI chrome.

**`AgentOutput.usage.cache_creation`** — track `ephemeral_1h_input_tokens` and `ephemeral_5m_input_tokens` separately. Anthropic charges different rates; `lancelot.upstream_metrics` should add these two fields to its token-extraction logic for Anthropic provider responses.

**`output_config.task_budget`** — add to `TransportPool.request()` as an optional kwarg. Wire: one extra JSON key + one `anthropic-beta` header. Zero complexity, free self-pacing for Claude models.

**Hook output protocol (verbatim):**

```json
{
  "continue": true,
  "stopReason": "shown when continue:false",
  "suppressOutput": false,
  "systemMessage": "warning shown to user",
  "hookSpecificOutput": {
    "hookEventName": "EventName",
    "additionalContext": "context appended to Claude"
  }
}
```

Exit code 2 = blocking error (no JSON parsed). Exit 0 = JSON from stdout. Other = non-blocking error.

---

## 12. Byte-Path Implications

| CC Primitive | Lancelot Already Has | Gap / Action |
|---|---|---|
| `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` | `lancelot.canonical.dumps_canonical` (stable prefix) | Add the sentinel constant + 1h/5m TTL split to `TransportPool.request()` |
| `TerminalReason` (12 values) | None | Define as `StopReason` enum in agent loop |
| `getContextUsage()` per-section breakdown | `lancelot.context` ContextVars | Expose as `/agent/context` GET endpoint |
| Deferred MCP tools / ToolSearch | `lancelot.mcp.mount` (full schema inline) | Add lazy-load catalog tier |
| `AgentOutput.cache_creation` dual TTL | `lancelot.upstream_metrics` (tokens_in/out) | Add `cache_1h_tokens` + `cache_5m_tokens` fields |
| `output_config.task_budget` | `lancelot.context.cost_usd` | Add `task_budget_tokens` kwarg to `TransportPool.request()` |
| Per-agent-type memory dirs | `lancelot.mcp.mount` >4KB warn | Not yet — add `~/.lancelot/agent-memory/<agentType>/` auto-load |
| Server-side compaction (`compact_20260112`) | None | Context management primitive in agent loop |
| `PostToolBatch` cost gate | Per-route timeout | Turn-level cost gate hook |
| `asyncRewake` (slow validator, async exit-2 wake) | Webhook + graceful drain | Agent-level quality gate hook |

---

*All KNOWN items cite the exact source line or section from the teardown. Items marked INFERRED come from the source map leak (Round-1 §20) and are flagged there.*
