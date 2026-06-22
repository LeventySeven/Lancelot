# Devin + Warp — mechanism-level teardown

**Sources:** `researchfms/teardowns/DEVIN.md` (Tier-1A: leaked system prompt `x1xhlol/Devin AI/Prompt.txt` + Cognition Rust binary strings + ACP open-protocol schema) · `researchfms/teardowns/WARP.md` (Tier-1A: AGPL Rust source `crates/ai/src/` + leaked server-resolved system prompt + primary blog posts). Both rated Tier-1A in their own files; cited as `DEVIN.md:§...` / `WARP.md:§...` below.

**Focus:** cross-model failover · in-place compaction · 13-trigger think · capability-bitmap prompt · planning gate.

---

## 1. Cross-model failover — the single most load-bearing routing fact

**Source:** `WARP.md:§2.6` ("The cross-model FAILOVER CHAINS") — stated verbatim in four primary warp.dev blog posts.

**The finding in one sentence (KNOWN, verbatim):** *"We originally attempted to retry with the same model, and found that this often produced repeat failures"* → switched to **cross-model** fallback (`WARP.md:blog.md:225-227`).

### Failover triggers (verbatim, `WARP.md:blog.md:169`)
> "In the rare case a request fails – be it an **outage, a rate limit, a malformed tool call**, etc. – we use a fallback mechanism to retry the request with a different model."

Three independent failure modes that ALL trigger the same cross-model hop:
1. Provider outage
2. Rate limit / quota exhaustion
3. Malformed tool call / `invalid` response (schema violation, truncated JSON, etc.)

### The concrete failover ladders (KNOWN, verbatim, `WARP.md:§2.6`)

**Chain 2 (SWE-bench Verified 71%, the clearest published chain):**
```
1. Anthropic Claude 4 Sonnet
   ↓ (outage / rate-limit / malformed tool call / invalid)
2. Anthropic Claude 3.7 Sonnet
   ↓
3. Google Gemini 2.5 Pro
   ↓
4. OpenAI GPT-4.1
```

**Chain 1 (Terminal-Bench v0.1.1, split-model architecture):**
```
PLAN: Claude Opus 4  →  EXEC: Claude Sonnet 4
                               ↓ (failure)
                 fallback: Gemini 2.5 Pro | OpenAI GPT-4.1  (~2% of runs)
```

**Architecture stability note (`WARP.md:blog.md:750`):** the primary model in the ladder head keeps moving (Claude Sonnet 4 → GPT-5 → GPT-5.2 → `auto` over GPT-5.2–5.5 + Claude 4.5–4.7), but **the mechanism is durable**: same-agent, cross-provider ladder, automatic restoration on provider recovery.

### Three orthogonal retry layers (`WARP.md:§3.4`, `§3.5`, `§2.6`)

These are separate, not alternatives:

| Layer | Where | Mechanism | When used |
|---|---|---|---|
| (a) Client whole-stream retry | Client | `MAX_RETRIES=3`, whole stream | ONLY if `!has_received_client_actions` (no partial work yet) |
| (b) Server per-tool-call retry | Server (black box) | `blog.md:223` "server-side retries of failed calls" | individual tool-call failure |
| (c) Server cross-model failover | Server | the ladder above | request-level failure (outage/rate-limit/malformed) |

Client uses **conversation-resume** (not fresh-start) after partial work: `should_resume_conversation_after_stream_finished = true` when `has_received_client_actions`; bounded by `AUTO_RESUME_TIMEOUT = 120s` (`WARP.md:agent_sdk/driver.rs:132`).

### Automatic restoration on provider recovery (`WARP.md:§2.3`)

Client-side: `DisableReason::should_clear_preference(has_byok_key)` (`WARP.md:llms.rs:88-94`):
- `ProviderOutage` / `OutOfRequests` → **keep** the user's chosen model (transient, restore when provider comes back)
- `AdminDisabled` / `Unavailable` → clear preference
- `RequiresUpgrade` → clear unless BYOK key present

This means the failover is **fully transparent** to the user: their configured model is preserved during an outage; the server silently routes to the fallback; it returns when the primary recovers.

### Devin's equivalent routing

Devin has **two distinct routers** (`DEVIN.md:§R4.5`):
1. **Adaptive model router (shipped)** — prompt-complexity classifier → tier (simple→fast, complex→capable); billing abstracted at a flat per-token rate so the underlying model swap is invisible.
2. **Agent Router (upcoming)** — routes to the best agent across the ACP registry (Devin/Codex/Claude Agent/…).

Devin Local's **`system_prefix_len`** is the prompt-cache boundary: "Most tasks take up to 30% fewer tokens … primarily through enhanced prompt caching" + "Staying on the same model across turns enables caching" → **switching models busts the cache** (`DEVIN.md:§R4.3`). This is why the Adaptive router "takes prompt caching into account when routing" — it is penalized for switching unless the quality delta is worth it.

**Hype flag:** The Devin Adaptive router's "best result" claim is an INFERRED mechanism (cheap classifier → tier). The actual routing signal is not disclosed. The durable primitive is the concept, not Cognition's specific classifier.

---

## 2. In-place compaction — window freed, not expanded

**Source:** `WARP.md:§4` ("Context-window management / compaction") and `WARP.md:§8` (Part-3 client trigger). `DEVIN.md:§R4.3` (Devin Local `AsyncFileCompactor`).

### The defining design choices (`WARP.md:§4`, KNOWN)

Warp's compaction is **automatic, structured, model-consistent**:

1. **Same model summarizes** — `SummarizeConversation` is a normal conversation input using the same `ModelConfig.base` as the conversation (`WARP.md:mod.rs:2473-2476`). No cheap side-model, no model switch.
2. **Structured payload carries TODO + rules deterministically** — rules and TODO state ride in `context: Arc<[AIAgentContext]>`, NOT in the free-text history. They are preserved exactly, not left to LLM discretion (`WARP.md:app/src/ai/agent_sdk/driver/output.rs:40,318,794,1018`).
3. **In-place replacement, not append** — earlier messages are **replaced** by the summary message in the message list (`supports_summarization_via_message_replacement = FF::SummarizationViaMessageReplacement`, `WARP.md:impl.rs:103-104`). The window is actually freed.
4. **Most-recent turn preserved verbatim** — the summary replaces *earlier* turns; the latest turn stays intact for immediate context continuity.
5. **File context already minimized before summarization** — `should_preserve_file_content_in_history: true` + "use `read_files`, not `cat`" means file content enters history as edited-region ±k lines, never as raw `cat` dumps. So the summarization input is already token-lean.

### Wire shape (`WARP.md:mod.rs:2473-2476`, KNOWN verbatim)

```rust
SummarizeConversation {
    prompt: Option<String>,              // structured prompt (server-templated when None)
    context: Arc<[AIAgentContext]>,      // carries ProjectRules + TODO blocks
}
```

Client trigger: only the `/summarize` (or `/compact`) slash command emits `AIAgentInput::SummarizeConversation` (`WARP.md:slash_command.rs:243-245`). **Auto-compaction is server-driven**, not client-timer-driven. The client only advertises the capability flag; the server decides when to compact.

### Two compaction types (`WARP.md:agent/mod.rs:1649-1653`, KNOWN)

```rust
enum SummarizationType {
    ConversationSummary,      // full-conversation compaction
    ToolCallResultSummary,    // single large tool result summarized inline
}
```

`ToolCallResultSummary` is the in-place compaction of a single oversized tool result — this is how, e.g., a 10,000-line `read_files` result is compressed to what matters without summarizing the whole conversation.

### Monotonic flag (`WARP.md:conversation.rs:543-544,1787-1789`, KNOWN)

`AIConversation.conversation_usage_metadata.was_summarized` is **false→true only, never reverts**. Once compacted, the conversation is permanently marked as having been compacted.

### Context-usage indicator constants (`WARP.md:§4.3`, docs, KNOWN)

| Threshold | Behavior |
|---|---|
| < 20% used | No indicator shown |
| 20%–80% | Usage bar progresses |
| ≥ 80% | Indicator turns red |

This is **UI only** — no behavioral auto-compact threshold. The server compacts when the context saturates, not at 80%.

### Fresh-conversation auto-segmentation (`WARP.md:§4.5`, docs, KNOWN)

Warp starts a *new* conversation (rather than summarizing) in three cases:
1. Shell command → immediately followed by an AI query
2. 3-hour idle timeout
3. Topic-shift detected by `SuggestNewConversation` tool (suggested, not forced)

### Devin Local equivalent (`DEVIN.md:§R4.3`, KNOWN)

`AsyncFileCompactor` with a split-point, triggered when "System prefix exceeds compaction threshold". Three things **always preserved verbatim** regardless of what the LLM chooses to summarize:
- Skill messages (verbatim — skills are the persistence substitute since Devin Local has no cross-session memory)
- The todo list
- Rules flagged `preserve_on_compaction`

`MessageNodeMetadata{ reason, profile, model_override, summarized_from, num_tokens_preceding }` — links each summary back to its source node range (provides audit trail; the `summarized_from` field).

Manual: `/compact` command. Hook event: `core/compaction` fires on the typed event/hook bus (agents can hook into compaction to perform custom bookkeeping).

**Key mechanism difference vs Warp:** Devin Local uses a SQLite **message-forest DAG** (`message_nodes(node_id, parent_node_id, …)`) where each turn is a node and fork = new child of an old node. Compaction writes a summary node into the chain, with `summarized_from` pointing at the replaced range. The Warp wire protocol uses in-place message replacement (`supports_summarization_via_message_replacement`).

---

## 3. The 13-trigger think tool — Devin's mandatory reflection mechanism

**Source:** `DEVIN.md:§2.3` (`Prompt.txt:50-73`, KNOWN verbatim).

### The tool signature

`<think>freeform text</think>` — no other args, private scratchpad, user does NOT see the content. Rendered as a `devin_thoughts` collapsed section in the web UI (expandable).

### 3 mandatory triggers (`Prompt.txt:54-59`, verbatim)

1. Before critical git/GitHub decisions (branch choice, PR create/update)
2. When transitioning from exploration to code edits ("have you gathered all necessary context, found all locations, inspected references, types?")
3. **Before reporting completion** — "critically examine your work and ensure you completely fulfilled the user's request"

### 10 "should use" triggers (`Prompt.txt:61-72`, verbatim)

1. No clear next step
2. Clear next step but unclear details that matter
3. Facing unexpected difficulties
4. Tried multiple approaches, nothing working
5. Decision critical for task success
6. Tests / lint / CI failed — what to do about it
7. Possible environment-setup issue — decide whether to report to user
8. Unclear which repo is correct
9. **Opening an image or browser screenshot** — "spend extra time thinking about what you see and what it really means"
10. In planning mode, search found no matches — think about alternative search terms

### Why this list is production-grade signal (`DEVIN.md:§2.3`)

"Compared to Claude Code (which has `<thinking>` blocks but no specific trigger conditions), Cursor (which uses extended thinking but doesn't enumerate triggers), or Aider (no explicit think step), Devin's prompt forces reflection at 13 specific decision points — 3 mandatory, 10 'should use'."

The enumeration is **the labelling schema for a structured-reasoning fine-tune**: Cognition almost certainly trained on this trigger taxonomy (`DEVIN.md:§R2.3`). The 13 conditions are the discrete states where the model's raw instinct underperforms and structured reflection is consistently worth the token cost.

**Hype flag / depreciation risk:** this is a **prompting-only primitive** — it works until models natively do this reflection without prompting. Still durable for at least 2–3 model generations based on current model behavior.

---

## 4. Capability-bitmap prompt — the server templates from what the client sends

**Source:** `WARP.md:§5.2` (`impl.rs:12-152`, KNOWN verbatim) + `WARP.md:§1` (diagram, KNOWN).

### The mechanism

**The system prompt is NOT in the client** (`WARP.md:impl.rs:1048-1053`). The client sends a `Settings` capability bitmap; the server templates the actual "You are Agent Mode…" system-prompt text from these fields. Every capability flag that is true adds a corresponding prompt section server-side.

### The full bitmap (KNOWN verbatim, `WARP.md:impl.rs:1031-1110`)

```rust
Settings {
    model_config: ModelConfig { base, coding, cli_agent, computer_use_agent, base_model_context_window_limit },
    rules_enabled,                              // gates AGENTS.md / WARP.md injection
    warp_drive_context_enabled,
    web_context_retrieval_enabled: true,        // hard-coded
    supports_parallel_tool_calls: true,         // hard-coded
    use_anthropic_text_editor_tools: false,     // hard-coded off
    planning_enabled,
    supports_create_files: true,
    supported_tools,                            // the per-request computed tool list
    supports_long_running_commands: true,
    should_preserve_file_content_in_history: true,
    supports_todos_ui: true,
    supports_linked_code_blocks,               // FF::LinkedCodeBlocks
    supports_started_child_task_message: true,
    supports_suggest_prompt: true,
    supports_read_image_files,                 // FF::ReadImageFiles
    supports_reasoning_message: true,          // hard-coded
    api_keys,                                  // BYOK
    autonomy_level,                            // Supervised (default) | Unsupervised
    isolation_level,                           // None (default) | Sandbox
    web_search_enabled,
    supported_cli_agent_tools,
    supports_v4a_file_diffs,                   // FF::V4AFileDiffs
    supports_summarization_via_message_replacement,
    supports_bundled_skills,
    supports_research_agent,
    supports_orchestration_v2,
    custom_model_providers,
    // ...
}
```

**The hard-coded constants** (never user-toggled, `WARP.md:impl.rs`):
- `web_context_retrieval_enabled = true`
- `supports_parallel_tool_calls = true`
- `use_anthropic_text_editor_tools = false`
- `supports_reasoning_message = true`
- `supports_create_files = true`
- `supports_long_running_commands = true`
- `should_preserve_file_content_in_history = true`
- `supports_todos_ui = true`

**The replica build target (verbatim, `WARP.md:§5.2`):** "The server must receive these exact capability flags and tool advertisements to produce equivalent prompt behaviour."

### Four independent model slots per request (`WARP.md:§2.1`, KNOWN)

```rust
ModelConfig {
    base:                        // orchestrator model
    coding:                      // file edits model (server-resolved, unused client-side)
    cli_agent:                   // CLI/terminal sub-agent model
    computer_use_agent:          // computer-use sub-agent model
    base_model_context_window_limit,
}
```

These can be set to `"auto"` (server picks per-turn), a specific opaque `LLMId` string, or a BYOK-keyed custom endpoint. The SQL schema proves this: `ai_queries` table has `model_id`, `planning_model_id`, `coding_model_id` columns added over 2025-01 → 2025-05 (`WARP.md:§5.2` migrations).

---

## 5. Planning gate — plan before acting, two-model split

**Source:** `WARP.md:§2.6` Chain 1; `DEVIN.md:§2.2` (planning mode gate).

### Warp's two-model execution split (`WARP.md:§2.6`, KNOWN verbatim)

Terminal-Bench v0.1.1 (52% SOTA): **planning model = Claude Opus 4** (strategy), **execution model = Claude Sonnet 4** (speed). The `planning_enabled` flag in the capability bitmap (`WARP.md:Settings.planning_enabled`) enables this split; when false, the single base model handles everything.

**Verbatim rationale:** "leveraging the intelligence of Opus to provide a high-level strategy to the problem and then relying on the speed of Sonnet to execute on said strategy."

**Negative result (KNOWN, `WARP.md:blog.md:171`):** tried Opus 4 as the sole base model. "Could not reproduce a benefit; even Opus 4 with parallel tool calls was slightly *worse* than the Sonnet 4 baseline." Bigger model ≠ better for execution; speed dominates. The win comes from using the larger model *only for planning*.

**SQL evidence for the split:** `ai_queries.planning_model_id` added 2025-02-24, `ai_queries.coding_model_id` added 2025-05-07 — the multi-model split was a deliberate evolution, not the initial design.

Also discovered at the feature-flag level: `FeatureFlag::AgentModePrimaryXML` AND `FeatureFlag::AgentModePrePlanXML` — **two distinct XML system prompts** (a primary agent prompt + a separate pre-plan prompt, `WARP.md:§8.D`).

### Devin's planning mode gate (`DEVIN.md:§2.2`, KNOWN verbatim)

Devin has a two-mode state machine:
- **Planning mode** (default entry): agent explores and produces a plan (`<suggest_plan/>`); no code edits
- **Standard mode**: user approved the plan; agent executes

"The agent enters planning mode by default at the start of each user request; the agent never enters standard mode on its own — the user must toggle it."

This is a **hard gate**: Devin cannot bypass it, and the user cannot be forced through it. Cognition's bet: the cost of the planning turn is worth it for user trust and task success.

---

## 6. Key constants (not to guess)

All KNOWN from verbatim source:

| Constant | Value | Source |
|---|---|---|
| `MAX_RETRIES` (whole-stream) | 3 | `WARP.md:response_stream.rs:274` |
| `AUTO_RESUME_TIMEOUT` | 120s | `WARP.md:agent_sdk/driver.rs:132` |
| Cross-model failover rate | ~2% of runs hit non-primary fallback | `WARP.md:blog.md:170` |
| `SECTION_MATCH_THRESHOLD` (Jaro-Winkler fuzzy edit) | 0.9 | `WARP.md:diff_validation/mod.rs:933` |
| Context-usage indicator red threshold | 80% | `WARP.md:usage/mod.rs:8-49` |
| Context-usage indicator hidden | < 20% | `WARP.md:docs` |
| Fresh-conversation idle | 3 hours | `WARP.md:docs` |
| Devin rules cap | 32 KiB each | `DEVIN.md:§R4.3` |
| Devin session expiry | 30 days | `DEVIN.md:§R4.3` |
| Devin CI failure retry limit before "report to user" | 3 attempts | `DEVIN.md:Prompt.txt:403` |
| Devin think trigger count | 3 mandatory + 10 should-use = 13 total | `DEVIN.md:Prompt.txt:54-72` |
| Devin output cap per model | Opus: 128k tokens, Sonnet: 64k tokens | `DEVIN.md:§R4.3` |
| Devin prompt-cache token savings | ~30% | `DEVIN.md:§R4.3` (switching models busts it) |

---

## 7. Lancelot byte-path lens

### What TransportPool already provides

- **Retries with exp-backoff + Retry-After honoring** — covers layer (a) above (client retry on transient). This is the "same model retry" Warp found inadequate. **Lancelot needs cross-provider failover on top of this, not instead of it.**
- **`rate_limit` token bucket** — handles rate-limit prevention but not rate-limit failover (when the bucket is already exhausted on the primary provider).
- **`fanout(first_n, deadline, cancel_laggards)`** — this is cross-provider race, not failover. Useful for latency optimization, not correctness recovery.
- **Error taxonomy `ok/quota/timeout/permanent/transient/upstream_5xx`** — the taxonomy is right; the failover action (hop to next provider) is not yet wired.

### What Lancelot does NOT yet have

1. **Cross-model failover chain** — a `ProviderProfile` list that the `TransportPool` walks on `quota/timeout/upstream_5xx`. The `upstream_metrics` error taxonomy already classifies these; the missing piece is the "on classification X, retry with pool[i+1]" logic.

2. **`ProviderProfile` / model-slot abstraction** — Warp's `LLMId` opaque string + 4-slot `ModelConfig` (base / coding / cli_agent / computer_use). Lancelot needs at minimum: `planning_model` vs `execution_model` slots, each with their own `TransportPool` (or a single pool with a per-slot model override).

3. **In-place compaction** — Lancelot has no `ContextBundle` / conversation-message management at all (this is on the "what Lancelot lacks" list in the lens). The Warp mechanism (same model, structured payload carries non-summarizable items deterministically, in-place replacement, ToolCallResultSummary for single large results) is the design blueprint.

4. **Capability bitmap** — the `Settings` struct pattern is directly portable. An agent's `InferenceRequest` to Lancelot's serving layer would carry a bitmap of capabilities; the server (or the harness) templates the system prompt from it. This separates prompt text from capability negotiation.

5. **`system_prefix_len` prompt-cache boundary** — Lancelot already has `dumps_canonical` for KV-cache prefix stability. The Devin finding that "switching models busts the cache" means the failover chain must try same-family (Anthropic→Anthropic) before cross-provider (Anthropic→Google), or at minimum invalidate the cached prefix on a provider switch. `TransportPool` sends `traceparent` and `x-request-id`; the cache boundary tracking is a separate concern.

6. **`ToolCallResultSummary` compaction** — when a tool result is oversized (> model budget), summarize just that result inline rather than triggering full conversation compaction. The `lancelot.mcp.mount` >4KB WARN exists; the in-place summarization-of-tool-result is not yet wired.

### What is Tier-D / do not build

- **Same-model retry as the primary recovery mechanism** — Warp explicitly found this "often produced repeat failures." Lancelot's current exp-backoff retry is correct for transient blips but should not be the primary mechanism for quota/provider errors.
- **Prompt compression by LLM discretion** — Warp's lesson: TODO state and rules must be carried in the structured payload (`context: Arc<[AIAgentContext]>`), not left to the LLM to echo into its own summary. Any compaction that trusts the model to remember structured state will lose it.
- **Multi-model orchestration with shared mutable context** — Devin's `dont-build-multi-agents` finding (§R3.22, §§R4.3): "Workers are stateless tools whose output the parent summarizes, never peers with their own durable context." Fan-out (planning→execution, Warp's Opus-plan/Sonnet-execute) is fine; multi-agent shared context is not.
