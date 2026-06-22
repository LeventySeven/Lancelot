# Mistral Agents API + Forge — Mechanism-Level Teardown

**Sources read in full:**
- `researchfms/teardowns/MISTRAL_AGENTS_API.md` — wire protocol RE from `mistralai/client-python` v2.4.5
- `researchfms/teardowns/MISTRAL_FORGE.md` — architectural RE (OpenAPI 610 KB, infrastructure fingerprinting, Temporal proof, LoRA recipe from open-source)
- `researchfms/teardowns/MISTRAL_FORGE_FUNCTIONS.md` — function-by-function traces, 30,800 total lines across backing notes

**Honesty labels:** KNOWN = confirmed from primary source code/OpenAPI/paper. INFERRED = high-confidence from multiple primary signals. DESIGNED = Mistral proprietary gap filled by RE reasoning.

---

## 1. The architectural insight: server-side loop + first-class handoffs as a wire primitive

Mistral is unique among major providers in running the agent loop **server-side** and emitting multi-agent transitions as typed SSE events. The client is a passive observer of handoffs; the server does the orchestration. This is what the wire protocol makes explicit that no other hosted API does.

The three durable primitives Mistral exposes over HTTP that nobody else does at wire level:

1. **Agent-as-a-resource** with Git-style versioning + named aliases (`production`, `staging` → atomic pointer swap to deploy/rollback)
2. **Typed handoff events** (`agent.handoff.started` / `agent.handoff.done`) in the SSE stream; client vs server execution mode
3. **Conversation fork** (`POST /v1/conversations/{cid}/restart?from_entry_id=...`) — branch from a historical message, original untouched

---

## 2. The open-union discriminator pattern (extensibility seam)

**Source:** `MISTRAL_AGENTS_API.md §2`, `models/agent.py:56`

Tool types registered via:

```python
parse_open_union(
    disc_key="type",
    variants=_AGENT_TOOL_VARIANTS,
    unknown_cls=UnknownAgentTool,
    union_name="AgentTool"
)
```

Same pattern on the SSE event side (`models/conversationevents.py:65–76`, `_CONVERSATION_EVENTS_DATA_VARIANTS`, `UnknownConversationEventsData`).

**The mechanism:** unknown `type` values roundtrip via `UnknownAgentTool(raw=..., is_unknown=True)` instead of raising. Server can add new tool types or event types without breaking older clients. This is the correct extensibility design for any discriminated union over a wire protocol.

**Lancelot implication:** `lancelot.tool` uses `@app.tool` + JSON schema but has no open-union discriminator on the wire. If Lancelot exposes a tool registry API (for `lancelot.mcp.mount` or a future agent tool protocol), the unknown-type shim is worth copying verbatim — no schema validation error on forward-compat additions.

---

## 3. Handoff implementation: synthetic tools, not a separate endpoint

**Source:** `MISTRAL_FORGE.md §7.5`, `MISTRAL_FORGE_FUNCTIONS.md §2.2 [§5 L1830]`

Mistral implements multi-agent handoffs by **injecting synthetic tools** into the agent's tool list at runtime:

```
agent.handoffs = [agent_b_id, agent_c_id]
→ augment tool set with: handoff_to_agent_b, handoff_to_agent_c (synthetic)
→ when LLM calls handoff_to_agent_b:
    1. emit: agent.handoff.started {previous_agent_*, next_agent_*}
    2. checkpoint conversation state (persist handoff row)
    3. swap conv.agent_id → agent_b, re-run Steps 4-11 with agent_b spec
    4. emit: agent.handoff.done
    5. continue streaming as agent_b
```

The `handoff_execution` flag (`"client" | "server"`) on `ConversationRequest` controls who acts: server mode = seamless continuation, client mode = server stops at the event and client decides whether to invoke next agent.

**Why this matters for Lancelot:** Handoff-as-synthetic-tool means the handoff primitive is not a special-case code path — it reuses the tool dispatch loop. A Lancelot agent framework can implement the same with zero new wire protocol surface: register `handoff_to_<name>` as a synthetic `@app.tool`, and the agent's ReAct loop dispatches it normally. The SSE events (`agent.handoff.started`, `agent.handoff.done`) are Lancelot stream emits — the byte path already handles them via `@app.stream`.

---

## 4. Alias deploy = atomic pointer swap with 2-stage Redis invalidation

**Source:** `MISTRAL_FORGE_FUNCTIONS.md §2.2 [§2 L450–540]`, FORGE.md §7.6

The atomic deploy primitive:

```
agents.aliases.upsert(name="prod", agent_id=..., version=2)
```

Implementation (DESIGNED from RE):
- Postgres `SERIALIZABLE` transaction + optional CAS via `expected_version`
- Redis 5s LRU key `alias:{name}` → `(agent_id, version)` as hot cache
- Pub/Sub broadcast on alias write → all workers invalidate their local cache immediately

**In-flight rule (KNOWN from FORGE_FUNCTIONS §3 finding #7):** A conversation in progress keeps the OLD agent version for its current turn through completion. The next turn re-resolves the alias. Zero mid-turn corruption; zero downtime.

**Lancelot implication:** `lancelot.context` already has `agent_id` ContextVar. If Lancelot ever adds a local agent registry, the "alias → (agent_id, version) with 5s Redis TTL + Pub/Sub broadcast" is the correct deploy primitive. The in-flight pinning rule is non-obvious and critical to avoid mid-stream inconsistency.

---

## 5. Tools-first context ordering for KV-cache reuse

**Source:** `MISTRAL_FORGE_FUNCTIONS.md §1, finding #4`, `MISTRAL_FORGE.md §6.3`

Mistral V13 chat template puts `[AVAILABLE_TOOLS]` BEFORE user messages:

```
<s>
[SYSTEM_PROMPT]agent.instructions[/SYSTEM_PROMPT]
[AVAILABLE_TOOLS][...tool schemas...][/AVAILABLE_TOOLS]
[INST]user message[/INST]
```

**Why:** vLLM prefix caching reuses KV blocks for identical prefixes. Tools + system prompt are constant across all turns of a conversation. Putting them first means: 1× tool encoding cost per conversation instead of N× (once per turn). Cited as "10× savings on long convs" in FORGE_FUNCTIONS §1 Step 9.

**Lancelot implication:** `lancelot.canonical.dumps_canonical` already provides KV-cache prefix stability. When Lancelot builds its `ContextBundle` assembly (listed as missing in 00_LENS.md), the tool schema block MUST be placed at the front of the assembled context, not adjacent to the turn that calls it. This is the single highest-ROI context ordering decision.

---

## 6. Connector system_prompt concatenated at agent runtime

**Source:** `MISTRAL_FORGE_FUNCTIONS.md §3, finding #3`, `[§7 L1640]`

Each MCP connector can declare a `system_prompt` field (behavioral guidance: how the model should use this tool). Mistral concatenates this into the agent's system prompt at runtime, before inference. The agent author doesn't need to know about connector-level behavioral instructions; the tool ships them.

```
final_instructions = agent.instructions + "\n\n" + concat_connector_system_prompt(connector)
```

**Why this matters:** Tool authors can encode operational constraints ("when searching, always include date filters", "limit results to 5 items") without requiring every agent that uses the tool to copy-paste the guidance. This is a clean decoupling primitive.

**Lancelot implication:** `lancelot.mcp.mount` currently warns on tool returns > 4KB (CLAUDE.md). It does not currently support per-tool system_prompt injection. This is worth adding to the MCP mount interface: `mount(app, path='/mcp', system_prompt="...")` that auto-appends to the agent's context when the tool is registered.

---

## 7. Tool confirmation gate via `tool_confirmations[].override_args`

**Source:** `MISTRAL_FORGE_FUNCTIONS.md §3, finding #1 + #9`, `models/conversationappendrequest.py:32–58`, `extra/run/deferred.py:36–121`

Mistral does not have a separate `/confirm_tool/{call_id}` endpoint. Tool confirmations come back via the **standard** `POST /v1/conversations/{cid}/messages` (the append endpoint) with a `tool_confirmations` field:

```python
# Pause on: tool.execution.started + paused
# Client resumes via:
POST /v1/conversations/{cid}/messages
{
  "tool_confirmations": [
    {
      "tool_call_id": "...",
      "decision": "approved",       # or "rejected"
      "override_args": {"limit": 5} # MODIFY the model's chosen args, not just allow/deny
    }
  ]
}
```

`override_args` lets a human-in-the-loop **edit** the model's chosen tool arguments before execution, not just binary approve/deny. This is the correct HITL primitive.

**Lancelot implication:** When Lancelot adds HITL/confirmation to its tool system, the one-endpoint design (override via `append` rather than a new `/confirm` surface) keeps the API surface minimal. The `override_args` pattern is worth copying — allow/deny alone is insufficient for safety-critical operations.

---

## 8. Conversation fork (branch) as a durable primitive

**Source:** `MISTRAL_AGENTS_API.md §4.4`, `MISTRAL_FORGE.md §3.4`

```
POST /v1/conversations/{cid}/restart
{
  "from_entry_id": "<entry_id>",   # branch point
  "inputs": "different message"    # new path from this point
}
→ Returns new conversation ID; original untouched
```

This is a "what-if" exploration primitive: replay an agent run from a historical point with different input. Useful for: HITL correction ("go back to turn 3 and use this input instead"), offline eval ("branch from every production conversation and inject a test question"), debugging.

**ConversationEntry types in history (KNOWN, from MISTRAL_AGENTS_API.md §4.3):** `MessageInputEntry`, `MessageOutputEntry`, `ToolExecutionEntry`, `AgentHandoffEntry`, `FunctionResultEntry` — each with `id`, `created_at`, `object`, `type`.

**Lancelot implication:** Lancelot's `ContextBundle` (build target) needs a serializable entry ID scheme to support fork. This is the durable state seam: if `ContextBundle` entries carry stable IDs and the bundle is serializable, `fork(entry_id)` is straightforward. Design the IDs in from the start.

---

## 9. Reasoning effort as binary (not a 3-level ladder)

**Source:** `MISTRAL_FORGE.md §7.1`, OpenAPI verbatim

```yaml
reasoning_effort:
  type: string
  enum: ["none", "high"]
  default: "none"
```

Mistral explicitly chose binary, not OpenAI's `low|medium|high`. Reasoning: the `[THINK]` block is either emitted or not. Medium values add complexity without principled semantics.

**Lancelot implication:** If Lancelot exposes a reasoning mode toggle on agents or routes (thinking models), prefer binary `reasoning=True/False` over a 3-level string. The simpler API is more defensible and operators don't know what "medium" means in practice.

---

## 10. `store=False` ephemeral mode on conversations

**Source:** `MISTRAL_AGENTS_API.md §CE.13`, `conversationrequest.py:84`

`ConversationRequest.store: Optional[bool]` — default `True` (persisted server-side). Setting `store=False` runs the conversation ephemerally: no history retention, not queryable via explorer, not forkable. Used for privacy-sensitive one-shot tasks.

**Lancelot implication:** In Lancelot's context, this maps to whether a conversation's `ContextBundle` is checkpointed. The opt-in `store=False` default-on pattern is the right default for a framework (default durable, explicit opt-out for ephemeral) vs a default-stateless API that needs explicit opt-in for persistence.

---

## 11. Usage decomposition per tool (billing + observability)

**Source:** `MISTRAL_AGENTS_API.md §CE.14`, `models/conversationusageinfo.py`

`ConversationUsageInfo` (returned in `ResponseDoneEvent`):

```python
class ConversationUsageInfo:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    tool_executions: Optional[list[dict]]    # per-tool: tool_name → usage
    connector_usage: Optional[list[ConnectorToolResultMetadata]]
```

Per-tool usage in the done event lets downstream systems bill, gate, or observe tool invocations individually — not just aggregate tokens.

**Lancelot implication:** `lancelot.upstream_metrics` already accumulates `cost_usd`, `tokens_in/out`. The `tool_executions` breakdown — per-tool cost decomposition in the `ResponseDoneEvent` — is worth adding to Lancelot's own done-event emission when it builds a tool loop. `X-Lancelot-Cost-USD` (already exists) should eventually carry a per-tool breakdown in the body.

---

## 12. SSE heartbeat: blank line every 15s, typed `event:` terminator (NOT `[DONE]`)

**Source:** `MISTRAL_FORGE.md §3.14`, `MISTRAL_FORGE_FUNCTIONS.md §1 Step 11`

```
content-type: text/event-stream; charset=utf-8
cache-control: no-cache, no-store, no-transform
x-accel-buffering: no
```

Heartbeat: `:keep-alive\n\n` every 15s. Terminator: `event: conversation.response.done\ndata: {...}\n\n` — NO `[DONE]` sentinel (OpenAI-style). Mistral uses typed event discriminators throughout.

**Lancelot implication:** Lancelot's `@app.stream` already emits `[DONE]` SSE keepalive. The typed-event model (discriminated by `event:` field rather than magic strings in `data:`) is the more extensible design. For Lancelot's agent streaming surface, a typed done event (`event: response.done`) carries usage info and is forward-compatible vs a magic sentinel.

---

## 13. `FunctionTool.requires_confirmation = false` per-tool confirmation flag

**Source:** `MISTRAL_FORGE.md §7.2`, OpenAPI

```yaml
FunctionTool:
  properties:
    type: { const: "function" }
    function: FunctionDefinition
    requires_confirmation:
      type: boolean
      default: false
```

This is a per-tool-definition flag, not per-call. Operators declare at registration time which tools require human approval before execution (e.g., write operations, financial actions). The server enforces it.

**Lancelot implication:** `lancelot.tool`'s `@app.tool` decorator should eventually accept `requires_confirmation=True` (default False). This is the cleanest API: the tool author declares the confirmation requirement at registration; callers don't need to track it.

---

## 14. The Forge closed loop (completed observability → dataset → fine-tune → deploy)

**Source:** `MISTRAL_FORGE.md §9`, `MISTRAL_FORGE_FUNCTIONS.md §2.5`

```
production agent completions
  → auto-logged to ClickHouse observability.completions
  → LLM-as-judge campaign: judge_output_type ∈ {REGRESSION, CLASSIFICATION}
  → filter by score → Dataset
  → export as JSONL → /v1/fine_tuning/jobs
  → promote via alias swap → /v1/agents/aliases/prod
```

Judge schema (KNOWN, from OpenAPI):

```yaml
Judge:
  required: [name, model, prompt, output_type]
  properties:
    prompt: string           # template with {prompt}, {response}, {context}
    output_type:
      enum: [REGRESSION, CLASSIFICATION]
    output_schema:
      # REGRESSION: {min, max, units}
      # CLASSIFICATION: {labels: ["good", "bad", "needs_review"]}
```

**Verdict for Lancelot:** The full closed loop (serve → observe → judge → finetune → deploy) is Forge's defensible moat vs OpenAI fine-tuning. Lancelot's scope is the byte path, not the training loop. The durable-state seam (`lancelot.workflows` or similar) is out of scope per CLAUDE.md. However, the **observability substrate** is in scope: `lancelot.upstream_metrics` + ClickHouse logging of completions maps to the explorer's foundation. The judge/campaign primitives are user-side logic; `lancelot.mcp.mount` + `@app.tool` are how the user exposes their judge API.

---

## 15. What NOT to copy (hype / tactical / out-of-scope)

| Item | Why skip |
|---|---|
| Server-side code interpreter / sandbox | Infrastructure product, not byte path. Tier-D. |
| OAuth connector marketplace | Auth infrastructure, user's domain. |
| The Magistral GRPO recipe | Training, not serving. Forge's differentiator. Lancelot's scope is inference transport. |
| Built-in web_search / image_generation | Server-side hosted tools. Not framework primitives. |
| Workflow engine (Temporal-backed durable execution) | Out of scope per 00_LENS.md "durable seam" build target, not yet Lancelot scope. |
| YYMM model versioning scheme | Naming convention, not a framework primitive. |
| Full LoRA-SFT recipe (AdamW betas, FSDP FULL_SHARD) | Training, not serving. |

**Hype flag:** The "tools-first KV cache" claim of "10× savings" (MISTRAL_FORGE_FUNCTIONS §1 Step 9) is plausible but only applies when the token context is long and the user/system content is short relative to tool schemas. Lancelot should implement the ordering for correctness, not claim a specific multiplier.

---

## 16. Constants worth keeping

| Constant | Value | Source |
|---|---|---|
| Sandbox timeout for verifier/code execution | 4s, 300MB RAM | MISTRAL_FORGE.md §5.4 |
| SSE keepalive interval | 15s | MISTRAL_FORGE.md §3.14 |
| File upload cap (fine-tune) | 512 MB | MISTRAL_FORGE.md §2.1 |
| Alias Redis TTL | 5s | MISTRAL_FORGE_FUNCTIONS.md §3 finding #6 |
| Agent ID prefix | `ag_` | MISTRAL_FORGE.md §7.1 |
| Tool confirmation field | `tool_confirmations[].override_args` | MISTRAL_FORGE_FUNCTIONS.md §3 finding #9 |
| Connector credential scopes | `org`, `workspace`, `user` | MISTRAL_FORGE.md §3.8 |
| Moderation v2 categories count | 10 (including `jailbreaking`) | MISTRAL_FORGE.md §7.4 |
