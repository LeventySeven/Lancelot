# Manus — Context-Engineering Teardown

**Source:** `researchfms/teardowns/MANUS.md` (Tier-1A: leaked system prompt corpus x1xhlol/jujumilk3 + verbatim `tools.json` + OpenManus faithful clone + live-probed OpenAPI v2 + bundle constants)
**RE date:** 2026-05-20 (Round-1 + Round-2 deep pass)
**Focus (per task mandate):** KV-cache discipline, context-engineering lessons — the canonical source.

---

## 1. The six rules of Manus context-engineering discipline

These are verbatim from the leaked `Modules.txt` + `Agent loop.txt`, cross-confirmed by two independent leakers one day apart (0309 vs 0310 captures are 99% byte-identical). They are the durable layer — not Manus' specific tools or model, which are tactical.

### Rule 1 — One tool call per turn (deterministic, append-only trace)

`Agent loop.txt:31`: *"Choose only one tool call per iteration."*

Every turn produces exactly one `(LLM call → tool call → observation)` cycle appended to the event stream. There is no parallel tool dispatch inside a turn. The stream is append-only: the agent never removes or edits prior events. This is the foundation for KV-cache stability: the prefix up to the current turn is structurally identical to the prefix in the previous turn (only one new observation block was appended). Any cache hit on the shared prefix is guaranteed.

**Lancelot byte-path lens:** `@app.batch` and `TransportPool.fanout` run in parallel, but they operate on *outbound* LLM calls, not on the event stream the inner agent loop reads. The one-tool-per-turn discipline is about context shape, not transport. Not in conflict; just a different axis.

### Rule 2 — Mask tools; never remove them

`Modules.txt:201-206`: *"Events may originate from other system modules; only use explicitly provided tools. Carefully verify available tools; do not fabricate non-existent tools."*

The agent is prohibited from fabricating tool names. Tools are masked (removed from the schema list) only when they genuinely do not apply — never temporarily hidden for cost reasons. When you hide a tool to "save tokens" and later reveal it, the prefix before the revelation differs from the prefix after (the schema block changed), which busts every KV-cache entry after the mutation point. The discipline is: keep the schema block stable across turns; swap tool availability only at well-defined checkpoints (task transitions), not mid-task.

**Why it matters for caching:** provider KV-caches match on prefix bytes. If the tool-schema block at position N in the prompt changes between turn T and turn T+1, all tokens from position N onward are re-computed. The Manus rule (only use tools that are explicitly in the event stream) enforces schema-block immutability within a task. This is the same as Anthropic's "mask-don't-remove" discipline documented in the Anthropic caching guide.

**Lancelot byte-path lens:** `lancelot.canonical.dumps_canonical` already produces stable sorted-key JSON bytes for tool schemas and prompt prefixes. This is the byte-level mechanism for satisfying the mask-don't-remove rule: if your tool schema block is always serialized with `dumps_canonical`, the bytes are deterministic and the prefix is stable. **`dumps_canonical` is the implementation primitive for Rule 2.**

### Rule 3 — `todo.md` as external durable context (the filesystem-as-context pattern)

`Modules.txt:116-127` (verbatim):
```
- Create todo.md file as checklist based on task planning from the Planner module
- Update markers in todo.md via text replacement tool immediately after completing each item
- Rebuild todo.md when task planning changes significantly
- Must use todo.md to record and update progress for information gathering tasks
- When all planned steps are complete, verify todo.md completion and remove skipped items
```

This is Manus' answer to the context-window problem. The event stream `"may be truncated or partially omitted"` (`Modules.txt:33`). Rather than trying to fit the full task history in the context window, Manus externalizes task progress to a file (`todo.md`) that the agent can re-read at any point. On a context compaction/reset, the agent reads `todo.md` and reconstructs exactly where it is without relying on the truncated event-stream history.

**The two-level plan hierarchy:**
- Level 1: Planner module emits numbered pseudocode (coarse; owned by a separate service)
- Level 2: Agent expands each Planner step into a fine checklist in `todo.md` (agent-owned, on disk)

The agent updates `todo.md` via `file_str_replace` (exact-string find/replace, not overwrite), which is atomic and produces a minimal diff.

**Why this is the right compaction pattern:** Claude Code (this framework) uses the same pattern: persistent `CLAUDE.md` + chapter marks survive context resets. The Manus name for it is "filesystem-as-context." It works because: (a) file I/O is cheap relative to re-generating the full plan; (b) the file is inspectable by the user in real time; (c) the file survives VM hibernation and context truncation.

**Lancelot byte-path lens:** Lancelot does not currently provide a `ContextBundle` primitive that externalizes task state. This is one of the missing agent-layer primitives (per `00_LENS.md`). The Manus pattern is the reference implementation: durable file written via `file_write`, updated via `file_str_replace`, re-read on compaction. The Lancelot agent layer should expose a `TaskMemory` object that writes to a canonical location, exposes a `recitation()` method (the todo.md equivalent), and can be re-loaded from disk on resume.

### Rule 4 — Recitation / re-anchor to re-inject goal after compaction

`Modules.txt:57-60` (verbatim, Planner module rules):
```
- Each planning update includes the current step number, status, and reflection
- Must complete all planned steps and reach the final step number by completion
```

Every `Plan` event injected by the Planner module includes: current step number + status + reflection. The "reflection" field is the mechanism — it is the Planner's own re-statement of the goal as it stands at the current step, accounting for any objective drift. This is what Claude Code would call a "chapter mark" — a semantic anchor injected into the stream so the model can re-orient itself after truncation.

The agent is also prohibited from declaring completion until it has reached the final plan step, which forces it to re-read the plan periodically. Each re-read of the current step re-anchors the model to the goal.

**Concrete API shape (from `Modules.txt:55-59`):**
```
{step_number: N, status: "in_progress|done|failed", reflection: "<re-statement of current goal>"}
```

**Lancelot byte-path lens:** This is the `inspect()` method in the missing `ContextBundle` primitive — a structured snapshot of current goal + step + status that can be injected at any turn. The Manus version is injected by an *external service* (Planner), which is cleaner than asking the agent to recite its own goal (self-recitation is unreliable). For Lancelot's agent layer: a separate lightweight planning model (or even a deterministic state machine) that emits `PlanEvent` objects is the right pattern.

### Rule 5 — Stable prefix via append-only event stream + event-type taxonomy

`Modules.txt:32-41` (verbatim, the seven event types):
```
1. Message    — user input
2. Action     — tool calls
3. Observation — tool results
4. Plan       — planner events
5. Knowledge  — knowledge module injections
6. Datasource — data API documentation
7. Other      — misc system events
```

Every event has a deterministic type tag. The context window always starts with: (system prompt block) + (tool schema block) + (event stream). All three blocks are append-only within a task. The system prompt never changes. The tool schema block changes only at task boundaries. The event stream only appends.

The KV-cache efficiency falls out mechanically: the entire prefix from (system prompt start) to (last-event end at turn T) is eligible for cache hit at turn T+1, because only one new event was appended. For a 100-turn task, turns 2-100 each get a near-full cache hit on everything before the last observation.

**The three-block structure is the architecture.** It is not an accident: Manus explicitly separates user messages, tool call/result pairs, and system-generated module outputs (Plan/Knowledge/Datasource) so that each block has different cache-hit semantics:
- System prompt block: cached forever (never changes within a deployment)
- Tool schema block: cached for the task lifetime (changes only at task boundaries)
- Event stream block: cached up to the previous turn

**Lancelot byte-path lens:** `lancelot.canonical.dumps_canonical` handles the byte-stability of the system-prompt and tool-schema blocks. The event-stream block's append-only discipline is an agent-loop design rule, not a transport primitive — the Lancelot agent layer must enforce it. The key constraint: never mutate an already-emitted event; only append.

### Rule 6 — Save intermediate results to files; never rely on in-context intermediate state

`Modules.txt:116`: *"Actively save intermediate results and store different types of reference information in separate files."*
`file_rules` block (`Modules.txt:115-119`): *"Avoid string escape issues in shell commands — use file tools."*
`coding_rules` block (`Modules.txt:152-153`): *"Must save code to files before execution; direct code input to interpreter commands is forbidden."*

The rationale is the same for all three rules: intermediate state in the context window is lost on truncation. Intermediate state on disk persists. The agent is trained to externalize every piece of state that it will need to reference again: partial results, intermediate computations, draft sections of a long document. The anti-pattern is `python3 -c "..."` (one-shot computation in context, result seen once and forgotten) — Manus forbids it.

**The draft-then-append document pattern (`Modules.txt:175-176`):**
For long-form documents, the agent writes each section to a separate draft file, then appends them into a final file. Critically: *"no content should be reduced or summarized; the final length must exceed the sum of all individual draft files"*. This is the anti-compression rule — LLMs default to summarizing when combining drafts, which loses information. Manus explicitly forbids the summarization reflex.

**Lancelot byte-path lens:** Not a transport primitive. This is an agent-loop policy enforced by the system prompt. However, Lancelot's `lancelot.mcp.mount` (minimal-return >4KB WARN) is the server-side dual of this rule: the agent shouldn't stuff large blobs into its context window via MCP tool calls. The client side is: save large intermediates to disk/memory outside the context window, re-fetch only the slice needed.

---

## 2. The three-module architecture (separation of concerns that eliminates hallucination)

`Modules.txt:53-77` (Planner + Knowledge + Datasource verbatim):

The agent LLM does NOT write plans, recall best practices, or memorize API schemas. These are done by three external services that *inject* into the event stream as typed events:

| Module | Injected event type | Mechanism | What it prevents |
|---|---|---|---|
| Planner | `Plan` | Numbered pseudocode plan; separate model call | Agent losing track of goal over long tasks |
| Knowledge | `Knowledge` | Scope-predicated best-practice snippet | Agent hallucinating "best practice" (low-temp retrieval from curated store beats in-context recall) |
| Datasource | `Datasource` | API documentation injected when relevant | Agent fabricating API names/shapes |

The Planner module is a separate model call; INFERRED to be a smaller/faster model (Haiku/Flash tier) since planning is bounded structured output (`MANUS.md §10.1`).

The Knowledge module fires a *scope* predicate — each knowledge snippet has a condition that must be met before it's injected. This prevents knowledge pollution (injecting irrelevant snippets that push real content out of the context window).

The Datasource module is the hallucination guard for API calls: the agent is forbidden from calling a data API that was not first documented in a `Datasource` event (`Modules.txt:71`). Documentation is injected on-demand by a server-side service, not cached statically in the system prompt — so the schema block stays small.

**Three-tier information hierarchy (`Modules.txt:122`):**
1. Datasource API (managed, system-paid) — highest trust
2. Web search + browser visit (must visit source, not just rely on snippets)
3. Model's internal knowledge — lowest trust

The "snippets not valid sources" rule (`MANUS.md §15.2`) is the anti-hallucination guard for web search: the agent must retrieve and read the actual source page before citing it. Search-result snippets are explicitly forbidden as citations.

**Lancelot byte-path lens:** The Planner and Knowledge module injection pattern is an agent-orchestration design, not a transport primitive. However, `TransportPool.spawn_child` + `TransportPool.fanout` are the byte-path substrate for making parallel module-service calls and merging results. A Lancelot agent framework would have: a `Planner` service whose output is injected into the `ContextBundle`; a `Knowledge` retriever that fires on scope predicates; a `Datasource` registry that documents APIs on demand. All three use `TransportPool` for outbound calls.

---

## 3. The `todo.md` update pattern — exact mechanism

`Modules.txt:116-127` + tool schema (`tools.json`):

The update mechanism is `file_str_replace` (exact-string find/replace), not `file_write` (overwrite). This is deliberate:

```python
# CORRECT (Manus pattern)
file_str_replace(file="todo.md", old_str="[ ] Step 3: Analyze results", new_str="[x] Step 3: Analyze results")

# WRONG (overwrite pattern — loses history, busts cache)
file_write(file="todo.md", content="..full rewritten content..")
```

The `file_str_replace` approach produces a minimal diff, preserves completed-item history, and avoids re-reading the full file to reconstruct state. It also means the LLM only needs to generate the changed line (the `old_str`/`new_str` pair), not the entire file contents.

The `file_str_replace` tool signature in `tools.json`:
```json
{
  "name": "file_str_replace",
  "parameters": {
    "file": {"type": "string", "description": "absolute path"},
    "old_str": {"type": "string", "required": true},
    "new_str": {"type": "string", "required": true},
    "sudo": {"type": "boolean", "optional": true}
  }
}
```

This mirrors Anthropic's `str_replace_editor` tool exactly (`MANUS.md §3.2`), suggesting Manus copied or was influenced by Anthropic's tool design.

---

## 4. `structured_output_schema` — post-task extraction (Round-2 finding)

`MANUS.md §R2.4`, verbatim from `task.create` schema:

```json
"structured_output_schema": {
  "type": "object",
  "properties": {...},
  "required": [...all properties...],
  "additionalProperties": false
}
```

After the agent loop completes, a separate LLM call extracts a JSON object matching this schema from the final transcript. Result shape: `{success: bool, value: <schema>, error: string|null}`. When `success: false`, `value` is populated with schema-conforming zero-values — consumers never need to handle null.

**Why this matters for context engineering:** the structured extraction is a *separate* LLM call, not part of the agent loop. The agent loop remains uncontaminated by output formatting concerns. This is cleaner than asking the final agent turn to emit structured JSON (which forces the agent to change its output format mid-stream, busting the tool-call discipline of "plain text responses are forbidden").

**Lancelot byte-path lens:** This is an agent-loop completion pattern. Lancelot's `@app.tool` + auto `output_schema` derivation (`lancelot.tool.ToolSpec`) is the server-side analog — it auto-derives the JSON schema from the return type annotation. The Manus pattern adds: (a) post-task extraction as a separate API call (not just a route attribute); (b) zero-value fallback on failure. Both ideas are worth copying.

---

## 5. The notify/ask split — non-blocking vs blocking message tools

`Modules.txt:108-112` (verbatim):
- `message_notify_user`: non-blocking, fires and continues
- `message_ask_user`: blocking, waits for user reply
- `suggest_user_takeover: "browser"` on `message_ask_user`: tells the UI to hand browser control back to user

The discipline: minimize `ask` calls (each one pauses the entire task loop). Use `notify` for progress updates. Ask only for genuinely blocking decisions (login, payment, captcha).

**OpenManus equivalent:** `Terminate().name` in `special_tool_names` — when called, the loop stops (`MANUS.md §15.8`).

**Lancelot byte-path lens:** The Lancelot agent layer needs this split natively. In the HTTP seam: `notify` maps to a streaming SSE event (`@app.stream` with `lancelot.http.stream.ttft`); `ask` maps to a blocking HTTP request to the user's endpoint with a webhook callback (the `lancelot.webhook` pattern). The notify/ask split directly maps onto Lancelot's streaming vs webhook primitives.

---

## 6. Shell-session multiplexing — named parallel bash sessions

`tools.json` `shell_exec` schema: `id: string` identifies the bash session.

The agent maintains multiple concurrent named bash sessions: `"dev"` (running server), `"test"` (running tests), `"logs"` (tailing logs). Each `shell_exec` call with a given `id` runs in the same persistent subprocess — env vars and cwd persist across calls. Kill is per-session via `shell_kill_process(id=...)`.

This is the architectural primitive for "build and test a running server" tasks in one agent loop. No other documented agent (Devin partial, Jules no) matches this cleanly per `MANUS.md §15.12`.

**Lancelot byte-path lens:** Not a Lancelot primitive. The Lancelot agent layer could expose `subprocess_pool` session management, but this is sandbox infrastructure, not HTTP transport.

---

## 7. Three-tier information hierarchy (anti-hallucination)

From `MANUS.md §15.2` (`Modules.txt:122-128` verbatim):

1. **Datasource API** (managed, system-paid, schema injected as `Datasource` event) — highest trust. Only call APIs documented in the event stream.
2. **Web search + browser visit** — must navigate to and read the actual source page. Search-result snippets are explicitly forbidden as citations.
3. **Model's internal knowledge** — lowest trust. Never cite without verification.

Search discipline: search for each attribute of an entity separately ("CEO of X", then "founded year of X") — not combined ("X CEO founded year headquarters"). Each attribute search produces one clean result.

**Lancelot byte-path lens:** This is a system-prompt policy rule, not a transport primitive. The Lancelot agent layer should encode this as a `SearchDiscipline` rule block in its default system prompt.

---

## 8. Avoid/anti-patterns

| Anti-pattern | Mechanism | Source |
|---|---|---|
| Mutating the tool schema block mid-task | Busts all KV-cache entries from that position onward | `Modules.txt:201-206` |
| Fabricating tool or API names | Hallucination; "only use explicitly provided tools" | `Modules.txt:201`, `Modules.txt:71` |
| Parallel tool calls within a turn | Makes trace non-deterministic; harder to replay | `Agent loop.txt:31` |
| Shell-based file ops (`cat`, `sed`, `echo`) | String escape bugs; forbidden | `Modules.txt:115` |
| `python3 -c "..."` inline code | Result lost on context truncation | `Modules.txt:152-153` |
| Citing search snippets without visiting the source | Hallucination anchor | `Modules.txt:124` |
| Summarizing/compressing when merging drafts | Loses information | `Modules.txt:175-176` |
| Calculating in context (mental arithmetic) | Wrong answers; must use `bc` or Python file | `Modules.txt:149` |
| Claiming task done before all Planner steps complete | Premature termination | `Modules.txt:57-59` |

---

## 9. API surface notes worth copying

**From `tools.json` (verbatim schemas):**

- `message_ask_user` has `suggest_user_takeover: enum["none", "browser"]` — a typed enum, not a free-text field. Forces the UI interaction to be machine-readable.
- `shell_exec` has `id: string` (session name) + `exec_dir: string` (abs path) — both required. Session isolation and reproducible working directory are first-class.
- `file_read` has `start_line` + `end_line` (0-based, exclusive) — enables line-sliced file reads, which is the mechanism for reading large files without flooding the context window with the full content.
- `info_search_web` has `date_range: enum[all, past_hour, past_day, past_week, past_month, past_year]` — matches Tavily's `time_range` enum, suggesting Tavily as the backend.
- `deploy_apply_deployment` has `type: enum["static", "nextjs"]` — only two deployment types. Simple, explicit, no footgun.

**From `task.create` OpenAPI v2 schema (`MANUS.md §R2.4`):**

- `interactive_mode: bool` — when false, the agent runs without asking clarifying questions (batch mode). When true, the agent may pause and ask. This is the `ask`/`notify` split at the API level.
- `hide_in_task_list: bool` — background tasks (subtasks spawned by the agent) are hidden from the user-visible task list. Maps to Lancelot's `agent_subtask` task type.
- `structured_output_schema: JsonSchema` — post-task extraction schema. `additionalProperties: false` + all properties in `required` enforced.
- Verbose mode on `task.listMessages` toggles `tool_used`, `plan_update`, `new_plan_step`, `explanation` events — clean separation of "chatbot mode" (compact) vs "trajectory mode" (full observability).

**The APISIX response envelope (`MANUS.md §R2.4`):**
```json
{"ok": true,  "request_id": "...", "<resource>": {...}}
{"ok": false, "request_id": "...", "error": {"code": "...", "message": "..."}}
```
Every response, success or failure, has `request_id` at the top level. This is a Lancelot alignment: `lancelot.context.request_id` + `X-Request-ID` echo already does this at the HTTP layer. The `ok: bool` envelope pattern is worth adopting for Lancelot's agent-facing APIs.

---

## 10. Hype / depreciating tacticals to skip

- **Manus's specific data API catalog** (`WeatherBank/get_weather`, etc.): platform-specific, churns as Manus expands their catalog. Not replicable without the proprietary gateway.
- **The iOS build pipeline detail** (Apple ID + TWOFA flows, macOS EC2): complex, Apple-TOS-constrained, distinctive to Manus's product but not a generalizable agent primitive for Lancelot.
- **Agent profile naming** (`manus-1.6-lite/standard/max`): branding, not architecture.
- **Manus Pages (MDX-based shareable deliverables)**: product feature; the *principle* (materialize agent output as a durable artifact) is generalizable, but the MDX+CDN implementation is tactical.
- **KIMI K2 routing inference**: INFERRED only, not leaked. Skip.

---

## 11. Durable primitives vs depreciating tacticals

| Primitive | Durability | Claim |
|---|---|---|
| Append-only event stream for KV-cache stability | Durable | Every LLM provider caches on prefix; append-only is structural |
| `todo.md` / filesystem-as-context | Durable | Same pattern as CLAUDE.md, Cursor `.cursorrules`, Devin recap |
| Mask-don't-remove tool schema | Durable | Works on any provider with prefix caching (Anthropic, OpenAI, Google) |
| Recitation/re-anchor via Planner events | Durable | Model-agnostic goal-anchoring mechanism |
| Separate module services (Planner/Knowledge/Datasource) | Durable | Separation of concerns; agent LLM only reacts, doesn't plan |
| Draft-then-append for long docs | Durable | Context-window workaround that works on any model |
| `file_str_replace` instead of `file_write` for incremental updates | Durable | Minimal-diff update discipline |
| Three-tier information hierarchy | Durable | Anti-hallucination grounding applicable to any agent |
| notify/ask split (non-blocking vs blocking user messages) | Durable | UX discipline; maps to stream vs webhook |
| Named shell sessions for parallel execution | Durable | Multi-session bash is an OS primitive |
| search-step-by-step (one attribute per query) | Durable | Decomposed search produces better individual results |

---

## Summary — What to copy for Lancelot's agent layer

1. **`todo.md` / `TaskMemory`:** an agent-owned durable file that externalizes task progress. Updated via `file_str_replace` (minimal diff). Re-read on context compaction. The single most important context-engineering primitive from Manus.

2. **Append-only event stream with typed event classes:** `Message | Action | Observation | Plan | Knowledge | Other`. Never mutate an emitted event. The three-block structure (system prompt / tool schema / event stream) maps directly onto Anthropic's prompt-cache prefix discipline.

3. **`dumps_canonical` for schema stability:** `lancelot.canonical.dumps_canonical` is the byte-level mechanism for enforcing mask-don't-remove. Use it for every tool-schema serialization.

4. **Separate Planner service:** a lightweight planning model that emits `PlanEvent(step_n, total, status, reflection)` into the event stream. The agent LLM consumes the plan; it does not generate it. This is the architectural separation that makes Manus reliable where AutoGPT failed.

5. **`structured_output_schema` as a post-task extraction step:** separate from the agent loop, uses strict-JSON-schema mode, populates zero-values on failure. Lancelot's `ToolSpec.output_schema` is the server-side analog; the post-task extraction pattern is additive.

6. **notify/ask split:** `notify` → SSE stream event (`@app.stream`); `ask` → webhook callback (`lancelot.webhook`). The discipline: minimize `ask` calls per task.

7. **`interactive_mode` flag on task creation:** toggles whether the agent uses `ask` at all (batch mode vs interactive mode). First-class API parameter, not a prompt-level setting.
