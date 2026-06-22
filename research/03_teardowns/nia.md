# Nia — Architecture Teardown Distillation

> **Source corpus:** `teardowns/NIA.md` (2906 lines, 5-round live-probe teardown) · `NIA_R4_AGENTS_ORCH.md` · `NIA_R4_PROMPTS_MODELS.md` · `NIA_R4_INDEXING_QUERY.md`
> **Focus per task brief:** JSON-action ReAct `{action,args,reason}` self-parsed · `think({reflection})` sentinel · 4-tier memory with lineage · two-stage synthesis. Extract mechanism-level only.

---

## 1. JSON-Action ReAct Loop — Mechanism

### 1.1 The wire shape (KNOWN — dispositive from planner-error string)

**Source:** `NIA.md §7.1` (D4.0(1) planner-error verbatim), `NIA_R4_AGENTS_ORCH.md §R4.2.1`

When the model emits output that does NOT conform to the envelope, the server raises:

> *"Oracle planner failed before research completion: No valid action JSON returned by the model"*

This proves the loop is a **self-parsed JSON-action ReAct**, NOT Anthropic native `tool_use` content blocks. The model must output exactly:

```json
{
  "action": "<tool_name>",
  "args":   { /* tool-specific */ },
  "reason": "<one-sentence chain-of-thought>"
}
```

The opencode wrapper parses this JSON itself and dispatches. `--tools nia-oracle-tools --no-builtin-tools` replaces the opencode tool-dispatch layer entirely. Consequence: the `reason` field (per-iteration CoT) bleeds verbatim onto the SSE wire in every `tool_start` event — the agent's reasoning is observable per-turn by the client.

### 1.2 The agent loop structure (live-confirmed, `NIA_R4_AGENTS_ORCH.md §R4.2.1/§R4.2.3`)

```
connected
  ├── iteration 1: tool_start(list_sources) → tool_complete
  ├── iteration 2: iteration_start → tool_start(universal_search) → tool_progress → tool_complete
  ├── iteration N: think{reflection} → confidence gate ("Confidence: HIGH. Next step: finish")
  └── tool_start(finish, args:{}) → generating_report×2 → complete
```

**Termination is model-decided** — no hard iteration cap in observed runs (7–9 iterations). The `think` tool is the explicit confidence gate: the model calls `think({reflection:"…Confidence: HIGH…"})` and then emits `finish`. The loop only terminates when the model decides to call `finish`.

**Escalation ladder** (`NIA_R4_AGENTS_ORCH.md §R4.2.3`):
```
list_sources (cheapest, scoped)
  ↓ 0 matches
universal_search (indexed hybrid RAG)
  ↓ weak results
run_web_search (web fan-out)
  ↓ returns URLs
web_fetch × N (pull full content)
  ↓ enough evidence
think (consolidate, confidence gate)
  ↓
finish → synthesis
```

### 1.3 `finish` is a sentinel, not a real tool (KNOWN — `NIA_R4_AGENTS_ORCH.md §R4.2.1`)

`finish` emits a `tool_start(action:"finish")` but **never** a `tool_complete`. The opencode wrapper detects `action == "finish"` and routes the accumulated `tool_calls + summaries` to a separate synthesis LLM call. The synthesis LLM **never sees the original system prompt** — it only sees the tool-call summaries. This is why the system prompt is fundamentally unextractable (synthesis prompt and iteration prompt are decoupled).

**Lancelot byte-path implication:** Lancelot needs to detect the `finish` sentinel in the action dispatcher and branch to the synthesis path. This is a 3-line check in the loop dispatcher, not a library primitive. `TransportPool` already handles the two LLM calls (iteration + synthesis) as separate requests.

---

## 2. `think({reflection})` Sentinel — Mechanism

**Source:** `NIA.md §7.3 Tool 7`, `NIA_R4_AGENTS_ORCH.md §R4.2.1`

```json
// Call shape
{"action": "think", "args": {"reflection": "<free-form chain-of-thought>"}, "reason": "..."}

// Fixed return value (ALWAYS — verbatim across all runs)
{"summary": "Reflection recorded. Continue with next action."}
```

**`think` is a no-op pass-through.** The reflection content never reaches any tool server — it echoes "Reflection recorded." and the loop continues. The value is that it: (1) forces the model to emit an explicit `{action, args, reason}` frame for the CoT step (keeping the JSON-action invariant), (2) exposes the CoT verbatim on the `tool_start.args.reflection` wire field, and (3) implements the confidence gate — the agent calls `think` to explicitly self-assess before deciding to `finish` or continue.

**Critical design note:** the `think` reflection is visible in the private iteration LLM context (it appears in the conversation as a tool result). It is NOT passed to the synthesis LLM. CoT accumulates in the running transcript, and the `reason` field of each subsequent `tool_start` references findings from prior tools — this is the observe→reflect→act cycle.

**Lancelot:** `think` is a trivial tool — it requires no external call, just a fixed string return and a no-op handler. Add it to the tool registry with `handler: lambda reflection: "Reflection recorded. Continue with next action."`.

---

## 3. Memory Architecture (4-tier with lineage)

Nia's full memory model, mechanistically:

### Tier 1 — Session context (ephemeral, in-loop)

Per-iteration accumulation of `{action, args, reason, result_summary}` tuples. The synthesis call receives this tuple array as its sole input. Stored in-process (Hatchet/Daytona worker memory). TTL = job lifetime.

**Lineage tracking:** each `tool_complete` emits `result_summary` (truncated on wire to ~250 chars; full content in the iteration LLM's private context). The `citations[]` array on the final result is the lineage record — each `{source_id, tool, args, summary}` traces exactly which tool call sourced each factual claim. Wire verbatim from `NIA_R4_PROMPTS_MODELS.md §R4.2.C`:

```json
{"source_id": 1, "tool": "universal_search", "args": {"query": "…", "top_k": 10},
 "summary": "<truncated result text>"}
```

`[Source N]` tags in the final report resolve to index N in the `citations[]` array. Full traceability: final_report claim → source_id → tool_name → args → result.

### Tier 2 — Session persistence (Oracle sessions)

Oracle creates both a `job` and a `session`. A session persists forever (`oracle_sessions` is append-only, no TTL — `NIA.md §7.8`). Chat continuation rebuilds the agent loop from the DB on each follow-up turn:

```
// Chat continuation prompt template (verbatim — NIA.md §7.7):
<original_research_query>…</original_research_query>
<existing_report_excerpt>…</existing_report_excerpt>
<existing_citations>…</existing_citations>
<followup_question>…</followup_question>
```

**Key mechanism:** each chat turn is a **stateless rebuild from DB**, not an accumulating message log. Only the last `report_excerpt` carries forward — this caps context growth and prevents cross-turn context explosion. Model_version is stored as `null` (never echoed back — `NIA_R4_AGENTS_ORCH.md §R4.2.6`).

### Tier 3 — Indexed source memory (TurboPuffer namespaces)

The global cross-tenant dedup pool: `public-repo-<16hex>`, `public-docs-<16hex>`, etc. (`NIA.md §3.3`). Any tool call to `universal_search` hits this. Content-addressed by `sha256(url+branch)[:16]`. N subscribers share one namespace — index once, serve N. This IS the agent's long-term factual memory about code/docs.

### Tier 4 — Vault (agent-maintained wiki, Postgres-backed)

**Source:** `NIA_R4_PROMPTS_MODELS.md §R4.3`

`RemoteVaultFs` — a Postgres-backed FUSE-like FS with a Unix toolset (`cat/grep/find/tree/pipes/redirects`). Layout:
```
/schema.md          user-owned conventions, never overwritten
/index.md           auto-maintained catalog
/log.md             append-only chronological log
/concepts/*.md      LLM-generated concept pages
/entities/*.md      LLM-generated entity pages
/notes/*.md         user-curated, ALWAYS respected by sync
```

**Page dual-section format** (verbatim, `NIA_R4_PROMPTS_MODELS.md §R4.2.A`):
```
{Compiled Truth block — rewritten on each sync}
---
{Timeline block — append-only evidence trail}
```

**8-type typed wikilink vocabulary:**
```
[[Title|relates_to]] [[Title|works_at]] [[Title|founded]] [[Title|uses]]
[[Title|extends]] [[Title|contradicts]] [[Title|supports]] [[Title|part_of]]
```

**Lineage on vault writes:** `provenance.last_human_edit` Postgres column — set when a human edits a page. Sync workflow skips pages with this stamp. A `/notes/*.md` page is always user-owned. `/schema.md` is always user-owned. An edited `/concepts/` or `/entities/` page is "frozen" until deleted.

**Lancelot:** `lancelot.memory_pool.MemoryPool` is byte-OOM reservation, NOT agent memory. Lancelot needs a separate `lancelot.agent.memory` module. The Tier 4 vault pattern (Postgres-backed FUSE-like FS + typed wikilinks + Compiled Truth/Timeline dual-section) is the most novel and copyable pattern. The key constant: **`provenance.last_human_edit` as a freeze gate** ensures the agent never overwrites human edits.

---

## 4. Two-Stage Synthesis (decoupled from the iteration prompt)

**Source:** `NIA.md §7.1`, `NIA_R4_AGENTS_ORCH.md §R4.2.5`, `NIA_R4_PROMPTS_MODELS.md §R4.2.C`

The synthesis phase is architecturally separate from the agent loop:

```
[Stage A — tool-call summary aggregation, ~instant]
  input:  tool_calls[] = [{tool, args, result_summary}]
  output: ordered evidence set with source_ids assigned

[Stage B — markdown report rendering, ~52s]
  input:  evidence set + [Source N] citation index
  output: final_report (markdown with [Source N] inline citations)
```

The double `generating_report` event on the SSE wire (`"Synthesizing research findings into final report"` then `"Creating comprehensive research report"`) corresponds to these two sub-stages.

**Key property: the synthesis LLM NEVER sees the original system prompt.** Its input is only the tool-call summaries. This is both a security property (unextractable prompt) and an architectural property (synthesis can be a different model/provider from the iteration LLM).

**Output contract** (verbatim from `NIA_R4_PROMPTS_MODELS.md §R4.2.C`):
- H2 lead (`## Title`)
- Bold-emphasis claims: `**thin delegating wrapper**`
- Every factual sentence: `[Source N]` citation token
- Fixed section scaffold: *Implementation → How It Fits → Why This Design Works → Practical Implications (markdown table) → Key Insight → Takeaway*
- `citations[]` array: `{source_id, tool, args, summary}` — tool output `summary` echoed verbatim

**Lancelot:** `TransportPool.fanout` handles the parallel tool calls. The synthesis call is a single `TransportPool.request()`. The `dumps_canonical(tool_calls)` → stable synthesis prompt prefix → KV-cache prefix stability (Lancelot's existing `lancelot.canonical.dumps_canonical` is the right primitive here).

---

## 5. Additional Mechanism-Level Findings Worth Copying

### 5.1 SSE transport contract (KNOWN — `NIA_R4_AGENTS_ORCH.md §R4.0`)

- `text/event-stream`, `cache-control: no-cache, no-transform`, **`x-accel-buffering: no`** (nginx proxy would otherwise buffer — this header is load-bearing for keepalive delivery)
- `data:`-only frames — no `event:`, no `id:`, no `retry:` fields. Discriminator is the JSON `"type"` key, not the SSE `event:` line.
- No Last-Event-ID resume: a reconnect re-subscribes from scratch. Completed jobs re-emit only the terminal event.
- Distinct `x-correlation-id` (workflow lifetime) vs `x-request-id` (HTTP request) pair.

**Lancelot:** Lancelot's `@app.stream` + SSE already provides the byte path. The `x-accel-buffering: no` header should be auto-set by `@app.stream` when serving SSE to a reverse-proxy environment. The `x-correlation-id` / `x-request-id` split maps directly to Lancelot's `context.trace_id` (workflow) vs `context.request_id` (per-HTTP).

### 5.2 Dual heartbeat pattern (`NIA_R4_AGENTS_ORCH.md §R4.2.4`)

Two independent keepalive emitters:
- **Workflow-layer heartbeat** (`+session_id`): strict fixed **5.0 s** timer from the Hatchet/Daytona worker. Rock-steady.
- **Relay-layer heartbeat** (no `session_id`): opportunistic gap-filler — fires during dead-air windows (notably the ~52 s synthesis phase when no workflow events flow). Irregular inter-arrival: 5–30 s.

**Why two:** neither leg idle-times-out if the other goes quiet. During synthesis, the workflow is silent for ~52 s — only the relay heartbeat keeps the TCP connection alive. The relay fires to prevent proxy/LB from closing the connection, not on a fixed timer.

**Lancelot:** `@app.stream` + `sse_keepalive` already provides one keepalive emitter. The dual pattern is the insight: add a second SSE keepalive at the application layer (not just the framework layer) during phases where the framework is silent (e.g. long upstream LLM calls). Practically: in the agent loop, when a tool call takes >5s, emit a bare `data: {"type":"heartbeat"}\n\n` from the relay side.

### 5.3 Job/session two-level object model (`NIA_R4_AGENTS_ORCH.md §R4.2.6`)

```
POST /oracle/jobs → {job_id, session_id, status:"queued"}  # creates BOTH
job.status:  queued → running → completed | failed | cancelled
DELETE /oracle/jobs/{id}   = CANCEL (pre-terminal only; 200 error on completed)
DELETE /oracle/sessions/{id} = actual purge (removes record + chat history)
```

A completed job is immutable (cannot `DELETE` it). Delete the session to purge. **The session is the persistent container; the job is the ephemeral run inside it.** This maps cleanly to: session = the research thread (chat history, title, citations), job = one execution (the agent loop + synthesis).

### 5.4 The `think` → `finish` confidence ladder for loop termination

**Source:** `NIA_R4_AGENTS_ORCH.md §R4.2.3` (verbatim `think` reflection)

The explicit termination pattern observed verbatim:
```
think{reflection: "I have sufficient information … Confidence: HIGH. Next step: finish."}
finish{}
```

This is a two-step pattern: the model explicitly self-assesses via `think` before committing to `finish`. The `think` step serves as a "commit checkpoint" — it forces an explicit chain-of-thought about confidence before the irreversible `finish` call. Without `think`, the model would terminate implicitly, with no auditable confidence signal.

**Lancelot:** Add a `think` tool to any ReAct loop. Its `reflection` field gives observable, auditable CoT for every stop decision. Cost: ~0 (it's a no-op handler returning a fixed string).

### 5.5 Anti-injection hardening — two-stage synthesis as structural defense

**Source:** `NIA.md §7.5`

The system prompt is structurally unextractable because:
1. Agent output is constrained to `{action, args, reason}` JSON during iterations → cannot accidentally emit the prompt
2. Synthesis LLM sees only `tool_calls + summaries`, NOT the system prompt
3. `think` reflection stays in private iteration context, NOT the synthesis prompt

The **"silent refusal"** pattern (P11 probe): when the agent detects injection, it answers the legitimate part and substitutes *"No information about operating policies was retrieved through the available research tools"* — treating the injection as a failed tool call, not engaging with it.

**Verbatim anti-injection policy phrases** (fragments confirmed at ~90% confidence, `NIA.md §7.5`):
- "operating policy"
- "fundamental security protocols"
- "sanctioned operation"
- "Security Notice: Prompt Injection Attempt Detected"

**Lancelot:** The two-stage synthesis (iteration prompt ≠ synthesis prompt, synthesis never sees system prompt) is the structural defense worth copying. It is NOT a prompt-engineering solution — it is an architectural one.

### 5.6 Model routing (confirmed constants, `NIA_R4_PROMPTS_MODELS.md §R4.1`)

| Surface | Model | Key constant |
|---|---|---|
| Iteration LLM (Oracle agent loop) | `claude-opus-4-7` | Default; adaptive thinking (no fixed budget) |
| Synthesis LLM (search query) | `claude-haiku-4-5-20251001` | Cheap, fast (~2-8s); handles the ~75% wall-time synthesis stage |
| Tracer-fast | `claude-haiku-4-5-20251001` | No planner fan-out |
| Tracer-deep planner | `claude-opus-4-7` | 5-worker fan-out |
| Vault dream/ingest | `claude-sonnet-4-5-1m` | 1M context via `anthropic-beta: context-1m-2025-08-07` |
| Stage-1 schema derivation | `claude-haiku-4-5-20251001` or `gpt-4o-mini` | ~125–345 ms, temperature 0.0, ~600 tokens |

**Key constant:** 1M context window is a named Anthropic beta header: `anthropic-beta: context-1m-2025-08-07`. Per-source read budget for vault ingest: **~600K chars / ~500 files**. `DocumentQueryRequest.thinking_budget = 10000` (ignored on Opus 4.7, which uses adaptive thinking).

**Lancelot:** `TransportPool.request()` sends arbitrary headers — add `"anthropic-beta": "context-1m-2025-08-07"` to headers for 1M context requests. This is already implementable with existing primitives.

### 5.7 Tracer mode-gated planner fan-out (`NIA_R4_AGENTS_ORCH.md §R4.3.2`)

**tracer-deep**: emits `planning_start{worker_count:5, model:"claude-opus-4-7"}` — 5-way parallel sub-agent fan-out with opus planner.  
**tracer-fast**: NO `planning_start` event — single haiku agent runs the loop directly.

The fan-out is a **mode gate**, not a default. Model and mode are coupled: `haiku ↔ fast`, `opus ↔ deep` (enforced — mismatch is rejected by the API).

**Lancelot:** `TransportPool.fanout(coros, first_n=5, deadline=…)` is the byte-path analogue. Lancelot already has this primitive. The mode gate pattern (cheap-single-agent vs expensive-fan-out based on user-supplied tier) is worth copying as an API design.

### 5.8 Oracle `waitForJob` constants (KNOWN — binary verbatim, `NIA_R4_AGENTS_ORCH.md §R5.2`)

```js
poll interval:  2000 ms  (client-side REST poll)
job timeout:    600000 ms (10 min)
terminal set:   new Set(["completed","failed","cancelled"])
status enum:    ["queued","running","completed","failed","cancelled"]
```

**Cancel semantics:** `DELETE /oracle/jobs/{id}` = cancel (pre-terminal only). Cancel ack ~800 ms. **In-flight LLM calls still complete and bill** — cancel only saves future-phase tokens.

### 5.9 `x-accel-buffering: no` as the SSE load-balancing primitive

**Source:** `NIA_R4_AGENTS_ORCH.md §R4.0`

This header is on every SSE response, proving an nginx-compatible proxy sits in front. Without it, the proxy would buffer chunks and the client would receive heartbeats in batches (defeating the keepalive purpose). Any Lancelot deployment behind nginx/Caddy/Traefik needs this header on SSE responses.

**Lancelot:** should auto-set `x-accel-buffering: no` on `@app.stream` SSE responses. This is a 1-line addition to the stream response headers.

---

## 6. What NOT to copy (anti-patterns confirmed by the teardown)

### 6.1 Deep search as own agent logic

`mode=deep` is a thin proxy to Exa.ai — Nia owns zero research logic there. It's 150 lines: Stage-1 schema derivation + ULID-task poll relay + fallback to universal. The agent loop, sub-query planning, and synthesis all run inside Exa. **Do not build a deep-research pipeline; build the relay + fallback pattern.**

**The relay pattern** (DESIGNED, `NIA.md §6.5`):
```
stage1 = derive_output_schema(query, output_format)  # ~300ms, cheap model, temp=0.0
task_id = POST upstream /research/tasks {query, schema}
while True:
  sleep(~3s)
  status = GET upstream /research/tasks/{task_id}
  if n % 10 == 0: emit polling(status, elapsed_s, poll_count=n)
  if status.done: break
```

### 6.2 Negation-blind semantic caches

Nia's L2 semantic cache (cosine-threshold=0.92 over query embedding) is negation-blind: `"X"` and `"NOT X"` have cosine ~0.93–0.97 → same cache slot → wrong answer. Still unmitigated as of 2026-05-29. Fix: a negation regex (`not / n't / without / except / no_std / unlike`) that forces a cache miss when present.

**Source:** `NIA.md §4.3`, `NIA_R4_INDEXING_QUERY.md §R4.3.3` (re-confirmed live)

### 6.3 `think` CoT in the synthesis prompt

The `think` reflection stays in the iteration LLM's private context. It must NOT be forwarded to the synthesis LLM — the synthesis prompt inputs are only `tool_calls[]` summaries. Forwarding the raw CoT to synthesis would: (a) bloat the synthesis prompt, (b) expose intermediate thoughts the user doesn't need, and (c) potentially leak system-prompt language via CoT.

### 6.4 Using `initial_score` raw in fusion (the `g(init)^1.85` superlinearity)

Nia's final fusion is NOT `w·init + (1-w)·rerank`. The `initial_score` undergoes a super-linear normalization: `g(init) = init^1.85` (empirically fitted, `NIA_R4_INDEXING_QUERY.md §R5.2`). Using raw linear initial_score in a two-stage blend undervalues top-ranked retrieval results. When `init=1.0`, `g(init)=1.0` — the formula collapses to `w + (1-w)·rerank`, exact to machine precision. The non-linearity only matters for non-maximal retrieval scores.

---

## 7. API Shape Notes (ergonomics worth stealing)

### 7.1 Discriminated-union mode endpoint

Single `POST /v2/search` with `mode ∈ {query, web, deep, universal}` as Pydantic discriminated union. Each arm has its own schema. The 422 error leaks the exact allowed tags: `"Input tag '<x>' found using 'mode' does not match any of the expected tags: 'query', 'web', 'deep', 'universal'"`.

**For Lancelot:** `@app.route(path)` with a `Discriminated[Union[...], "type"]` body is the pattern. Pydantic v2 discriminated unions generate correct OpenAPI `oneOf` schemas automatically.

### 7.2 Chat-continuation as `messages[]` not `query: str`

`mode=query` takes `messages: list[{role, content}]` — a chat history array, not a bare string. This enables multi-turn RAG natively. The request shape is identical to the OpenAI chat-completion envelope, which makes it natural for agents that already manage a message history.

### 7.3 `output_format` as NL steering hint, not a JSON schema

`output_format: str` on deep search is a natural-language description of the desired output shape ("name and one-line tradeoff for each option"). The Stage-1 schema-derivation LLM converts this NL hint into a proper JSON Schema. The user-facing API never exposes raw JSON Schema. This is a significant ergonomic improvement over requiring users to write JSON Schema.

### 7.4 `result_summary` truncation on wire, full content in LLM context

`tool_complete.result_summary` is truncated to ~250 chars on the SSE wire. The iteration LLM receives the full result in its private context. This decouples observability (short summary visible to the client) from LLM context (full result visible to the agent). Prevents the SSE stream from becoming a firehose for large tool results.

### 7.5 `retrieval_weight` returned on every result (scoring transparency)

`mode=universal` returns per-result: `{score, initial_score, reranker_score, retrieval_weight, pre_rerank_rank, reranked}`. The entire fusion algebra is observable from the wire. This enables clients to build their own re-ranking or filtering on top of the raw scores. Nia uses this internally to make the scoring formula fully reverse-engineerable.

---

## 8. Byte-Path Implications Summary

| Nia primitive | Lancelot status | Gap / action |
|---|---|---|
| JSON-action ReAct dispatch | NOT built | Build: parse `{action,args,reason}` in agent loop; dispatch to tool registry |
| `think` no-op tool | NOT built | Trivial: fixed-string handler, no external call |
| `finish` sentinel → synthesis branch | NOT built | 3-line check in loop dispatcher |
| Two-stage synthesis (iteration ≠ synthesis prompt) | NOT built | Two separate `TransportPool.request()` calls; synthesis sees only `tool_calls[]` |
| SSE `x-accel-buffering: no` header | Partial | Add to `@app.stream` response headers |
| Dual heartbeat (workflow + relay) | Partial (`sse_keepalive`) | Add application-layer heartbeat during long tool calls (>5s) |
| `x-correlation-id` (workflow) vs `x-request-id` (HTTP) | Partial (`context.trace_id` + `context.request_id`) | Emit both on SSE responses |
| Job/session two-level model | NOT built | Design: session = persistent container, job = ephemeral execution |
| 1M context via beta header | NOT built, trivial | `TransportPool.request(headers={"anthropic-beta":"context-1m-2025-08-07"})` |
| Vault (Postgres-backed wiki + typed wikilinks) | NOT built | Out of scope (Tier-D: agent logic) but the dual-section page format is worth the wiki design |
| `provenance.last_human_edit` freeze gate | NOT built | Part of vault; durable state layer |
| Fan-out mode gate (fast=single, deep=fan-out) | BUILT (`TransportPool.fanout`) | Just needs the mode-coupling API design |
| Memory lineage `{source_id, tool, args, summary}` | NOT built | Build as part of the citation system in the agent loop |
| `dumps_canonical(tool_calls)` for KV-cache prefix | BUILT (`lancelot.canonical.dumps_canonical`) | Use it on synthesis prompt construction |
| Negation guard on semantic cache | NOT built (Lancelot has no semantic cache yet) | When building the cache: add negation regex before the ANN lookup |

---

## 9. Honest Flag: What is Hype / Depreciating

- **The specific model IDs** (`claude-opus-4-7`, `claude-haiku-4-5-20251001`, etc.) will churn on the ~1-2yr model clock. The **routing pattern** (cheap model for synthesis, expensive model for iteration, 1M for vault ingest) is durable.
- **The `anthropic-beta: context-1m-2025-08-07`** beta header will be superseded when 1M context becomes GA. The mechanism (a header flag) is durable; the exact header value is not.
- **Exa.ai as deep-search upstream** — this is a tactical vendor dependency. The relay pattern (Stage-1 schema derivation + external research task poll + fallback) is the durable architecture; Exa is a replaceable upstream.
- **The exact `p≈1.85` exponent in `g(init)=init^1.85`** — fitted from 52 data points, not analytically derived. Close enough for a replica but not a first-principles constant.
- **Daytona sandbox** — the micro-sandbox cold-start (~4 min) is the biggest UX problem in the system. This is a vendor/infrastructure dependency, not an architectural primitive. The durable lesson: **sandbox cold-start is the dominant cost; agent loop itself is fast (~57s post-cold-start)**.
