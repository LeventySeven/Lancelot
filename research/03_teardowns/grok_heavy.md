# Grok Heavy / Grok 4.20 Multi-Agent — Lancelot Agent Harvest

**Sources:** `teardowns/GROK_HEAVY.md` (R1 Jul-2025, R2 2026-05-20, R3 2026-05-26) + `teardowns/GROK_420_AGENTIC.md` (2026-04-13)
**Focus (per task brief):** Multi-agent with NO convergence metric; leader IS one of the N agents AND the synthesizer; `reasoning_content` as separate stream channel. Copy resource-bound termination + leader-is-synthesizer; refuse convergence-based termination.

---

## 1. The corrected architecture (R2 is the source of truth; R1 guesses are mostly wrong)

The central R2 finding that supersedes almost all of R1:

- **N ∈ {4, 16} only.** Proto `AgentCount` enum = `{AGENT_COUNT_4=1, AGENT_COUNT_16=2}`. Not 8/32. Not chosen by an orchestrator. Set by the **caller** at request time. `reasoning.effort low/medium → 4`; `high → 16` on the OpenAI-compat surface; native gRPC uses `agent_count` directly as an **independent** field from `reasoning_effort`. [GROK_HEAVY.md R2.4, R3.6]

- **No separate orchestrator. No separate judge.** The leader is agent #1 (named "Grok") — one of the N agents with the **same tools** as sub-agents. Sub-agents are Harper, Benjamin, Lucas (4-agent team); agents 5-16 names not confirmed. [GROK_HEAVY.md R2.4, R2.3]

- **Only the leader has render components.** Sub-agents call tools, reason, send messages via `chatroom_send`. Only the leader can emit `render_inline_citation`, `render_searched_image`, `render_generated_image`, `render_edited_image`, `render_file`. This **forces the leader to be the sole final-response emitter** — no duplicate UI from competing agents. [GROK_HEAVY.md R2.3]

- **Termination = `max_turns` ∧ 200s global ∧ 120s per-`wait` call.** No 75%-agreement convergence metric exists in the wire contract. R1's DESIGNED "75% convergence" is **wrong**. Termination is purely resource-bound. [GROK_HEAVY.md R3.7]

---

## 2. Primitives worth copying (mechanism-level, cited)

### 2.1 Resource-bound termination — `max_turns` ∧ 200s global ∧ 120s per-wait

The wire contract (`GetCompletionsRequest.max_turns`, `chat_pb2.pyi:188`):
```
while turns < max_turns and not final_answer and elapsed < 200s:
    step()
    # if step used 'wait', that wait is capped at 120s
```
"Defaults to server-side maximum." With parallel tool calls, multiple tool calls can happen in one turn, so `max_turns != total tool calls`. [GROK_HEAVY.md R3.7]

**Lancelot implication:** The agent loop's `StopReason` should combine ALL THREE: turn count, wall-clock budget, per-wait cap. Do not implement convergence-based stopping (no `while not converged` loop). This matches Lancelot's per-route `timeout=` and `LANCELOT_DRAIN_SECS` patterns — the 200s/120s caps map naturally onto `App(per_route_timeout=120)` for the individual agent steps and a wrapping budget for the entire run.

### 2.2 Leader-is-synthesizer pattern (no separate judge model)

The leader agent (system prompt `grok-expert.md`) is initialized with:
- Same base model as sub-agents
- Same tool registry as sub-agents
- **Plus**: render components (only the leader has them)
- **Plus**: `conversation_search` and `view_x_video` (leader-exclusive tools for memory access and video analysis)

Sub-agents communicate findings via `chatroom_send(message, to: enum{"Benjamin","Harper","Lucas","All"})`. The leader reads these via its context and emits the final synthesized response with citations. [GROK_HEAVY.md R2.3]

**The architectural insight:** differentiate leader vs sub-agent purely by what they can *output* (render/emit), not by which model they are or what tools they can call for gathering. All agents gather identically; only one synthesizes.

**Lancelot implication:** In a Lancelot multi-agent loop, the leader distinction can be implemented as a flag on the agent context: `is_leader: bool`. The leader calls `@app.stream` (or the SSE emitter); sub-agents write to a shared channel (analogous to `chatroom_send` → Lancelot `@app.batch` coalescing or an asyncio.Queue). The leader is NOT a different model class — it gets the same `ToolSpec` registry plus render-access.

### 2.3 `reasoning_content` as a separate stream channel (not interleaved with answer)

The Delta proto (`chat_pb2.pyi:283`) has **six parallel channels** in each SSE chunk:
```protobuf
message Delta {
  string content            = 1;  // visible answer text delta
  string reasoning_content  = 2;  // thinking-trace delta — SEPARATE channel
  MessageRole role          = 3;  // ROLE_ASSISTANT vs ROLE_TOOL
  repeated ToolCall tool_calls = 4;
  string encrypted_content  = 5;  // opaque blob for multi-turn re-hydration
  repeated InlineCitation citations = 6;  // opt-in via include=[inline_citations]
}
```
`reasoning_content` and `content` accumulate into separate client-side buffers (`chat.py:1093`, `_reasoning_content_buffers` keyed by output index). "Show thinking" is literally rendering the `reasoning_content` buffer. They never interleave. [GROK_HEAVY.md R3.5]

**Lancelot implication:** The existing `@app.stream` pump sends one async-gen to the Rust R3 pump → `StreamBody`. For a reasoning agent, the stream should be **typed chunks**, not raw strings: `{"type": "reasoning", "content": "..."}` vs `{"type": "answer", "content": "..."}`. The R3 pump handles this fine (it sends whatever bytes the gen yields); the typing is in the gen, not the pump. The `TTFT` metric (`lancelot.http.stream.ttft`) should record first **answer** chunk, not first reasoning chunk (reasoning chunks before the answer are "thinking time", not TTFT as an AI-UX metric).

### 2.4 Tool-call streaming via `IN_PROGRESS → COMPLETED / FAILED` status transitions

Each `ToolCall` on the wire carries `{id, type, status: IN_PROGRESS|COMPLETED|INCOMPLETE|FAILED, error_message, function: {name, arguments}}` (`chat_pb2.pyi:538`, `ToolCallStatus chat_pb2.pyi:77`). The activity panel the consumer app shows (spinner→checkmark) is driven by matching `tool_call.id` across chunks: first chunk `status=IN_PROGRESS` triggers spinner, later chunk same `id` with `status=COMPLETED` triggers checkmark, `FAILED` triggers error with `error_message`. [GROK_HEAVY.md R3.5]

**Lancelot implication:** `lancelot.tool` (`ToolSpec` + `@app.tool`) emits tool schemas but does not currently emit live execution status events. For an agentic product that streams a "what I'm doing" activity feed, add a `tool_status_stream` channel alongside the answer stream. This is SSE with typed events: `{"type":"tool_start","id":"t1","name":"web_search"}` then `{"type":"tool_done","id":"t1","status":"ok"}`.

### 2.5 `IncludeOption` as the verbose-streaming / citation-gate switch

8 include flags, any composable (`chat.py:27`, proto `chat_pb2.pyi:16`):
- `verbose_streaming` — emit sub-agent tool calls + intermediate steps in the stream (the "DeepSearch thinking" feed)
- `inline_citations` — populate structured `InlineCitation` objects (OFF by default; without it, only bare URL list `citations[]`)
- `web_search_call_output`, `x_search_call_output`, `code_execution_call_output`, `collections_search_call_output`, `attachment_search_call_output`, `mcp_call_output` — stream raw tool outputs back to caller

**DeepSearch is NOT a separate pipeline.** It is `multi_agent` + `include=["verbose_streaming","web_search_call_output","x_search_call_output","inline_citations"]`. [GROK_HEAVY.md R3.3]

**Lancelot implication:** For Lancelot's agent surface, adopt this include-flag pattern. A default request gets the clean answer; an `include=["trace","tool_outputs","citations"]` request gets the full transparency mode. This maps naturally onto Lancelot's existing request inspection architecture (the ASGI seam already reads headers/params; a `X-Lancelot-Include: trace,tool_outputs` header or query param can gate what the SSE stream emits).

### 2.6 Citation wire schema — character-span anchored, three source kinds

`InlineCitation` proto (`chat_pb2.pyi:299`):
```
{id: str,               # display label ("1", "2") — string, not int
 start_index: int,      # char offset into response text (inclusive)
 end_index: int,        # char offset (exclusive)
 source: oneof {
   web_citation:         {url: str},
   x_citation:           {url: str},          # hydrate X card client-side via oEmbed
   collections_citation: {file_id, chunk_id, chunk_content: str, score: float, collection_ids[]}
 }
}
```
**Structured citations are opt-in** (`include=["inline_citations"]`). The legacy `citations[]` bare URL list is always present. The frontend highlights `response_text[start:end]` and attaches the source hover-card. `CollectionsCitation` includes the actual retrieved chunk text + score — enabling "show me the exact paragraph this came from." [GROK_HEAVY.md R3.4]

**Lancelot implication:** Lancelot's MCP tool layer (4 KB WARN on tool returns, `mcp-guidelines.md`) already implements the minimal-return discipline. The citation model above is the complement: when the agent cites a source, it emits `{start, end, url}` alongside the text — not embedded markdown links. The `lancelot.tool` `output_schema` could carry a `citations: list[Citation]` field alongside the main `content: str`. This is a non-breaking addition to the existing tool output surface.

### 2.7 Encrypted reasoning content for multi-turn continuity

`Delta.encrypted_content` (field 5) = opaque base64 blob of the full chain-of-thought. `chat.append(response)` auto-passes the encrypted blob back on the next turn (`chat.py:158-163`), giving multi-turn reasoning continuity without exposing raw CoT — and works for ZDR (zero-data-retention) customers who cannot use `previous_response_id`. [GROK_HEAVY.md R3.5]

**Lancelot implication:** For multi-turn agent conversations, Lancelot's `lancelot.context` ContextVars (`request_id`, `trace_id`) can carry a `reasoning_blob` ContextVar (encrypted, opaque) that the ASGI seam echoes back in a `X-Lancelot-Reasoning-State` response header. The client sends it back in the next request header; the ASGI seam injects it into the agent's context reconstruction. This pattern (encrypted-opaque-blob-as-continuation-token) is the production answer to "how do I resume agent state without storing it server-side."

### 2.8 Prefix cache + `cost_in_usd_ticks` calibration hook

`SamplingUsage` proto (`usage_pb2.pyi:31`) provides:
- `cached_prompt_text_tokens` — prefix-cache hit count in tokens, billed at 1/10 rate
- `cost_in_usd_ticks` — exact dollar cost of the request (1 tick = 1e-10 USD, `xai_sdk/cost.py:8`)
- `num_sources_used` — how many search sources fed the answer
- `server_side_tools_used[]` — which built-in tools ran

The shared multi-agent system prompt (~600 lines `grok-expert.md`) is prefix-cached across all 16 agents — that's why N=16 costs ~5× single-agent, not 16×. `DebugOutput.cache_read_count` / `cache_write_count` confirms the prefix cache exists at the serving layer. [GROK_HEAVY.md R3.6, R2.7]

**Lancelot implication:** `lancelot.canonical.dumps_canonical` (sorted-key JSON) is already the prompt-cache-stability primitive. For multi-agent runs with a shared system prompt, the leader and all sub-agents must receive **identical bytes** for the system prompt prefix — the `dumps_canonical` sorted serialization ensures this. Any per-agent personalization (e.g., agent name) must come AFTER the shared prefix (in the user message, not the system message), or it defeats prefix caching.

---

## 3. Anti-patterns (refuse to copy)

### 3.1 Convergence-based termination — DO NOT BUILD

R1's DESIGNED "75% agreement convergence threshold" does not exist in the production system. The actual termination is `max_turns` ∧ wall-clock timeout. A `while not converged:` loop based on agreement score will (a) run indefinitely on hard queries where instances never converge, (b) add latency on easy queries, (c) require a costly "compare all N answers" meta-call per round. The production system avoids all three by using resource bounds only. [GROK_HEAVY.md R3.7, correction of R1 §18]

### 3.2 Separate orchestrator + separate judge model — DO NOT BUILD

R1 proposed: "orchestrator decides N → fan-out → debate → separate judge picks best answer." R2 proved this is wrong: the leader is agent #1, the judge is the leader, N is set by the caller, no separate model is invoked. A separate judge doubles cost for the synthesis step with no architectural advantage (the leader already has all peer answers via `chatroom_send`). [GROK_HEAVY.md R2.4]

### 3.3 Fixed N outside {4, 16} — DO NOT BUILD

Exposing N as a continuous integer (1-32) is tempting but operationally counterproductive: it complicates pricing (unpredictable per-query cost), makes the consumer product dichotomy dishonest, and adds no measurable accuracy gain between, say, N=8 and N=16 that justifies the extra complexity. xAI's choice of {4, 16} is a deliberate product simplification. [GROK_HEAVY.md R2.8, R2.13]

### 3.4 Interleaving `reasoning_content` into the answer stream — DO NOT DO

The six-channel Delta is explicit: `reasoning_content` and `content` are separate fields, separate client buffers. Interleaving them forces the client to parse them apart (error-prone, breaks markdown rendering of the answer), and prevents selectively hiding the thinking trace. Keep them separate channels from the generator all the way to the client. [GROK_HEAVY.md R3.5]

### 3.5 Making MCP tool results deduplicated across agents — AVOID

In a 16-agent run where all agents have the same MCP server, if two agents call the same MCP tool with the same args, **two billable calls are made** — xAI does not deduplicate. This is the correct behavior for independent reasoning (each agent should see the result fresh), but a replica must account for this in cost estimates and avoid "share MCP results" as a cost optimization at the expense of agent independence. [GROK_HEAVY.md R2.6]

---

## 4. API shape notes (ergonomics worth stealing)

### 4.1 `agent_count` and `reasoning_effort` are INDEPENDENT fields (native gRPC)

On the native gRPC API, `agent_count` (4 or 16) and `reasoning_effort` (none/low/medium/high) are separate fields on `GetCompletionsRequest` (`chat_pb2.pyi:182,190`). The OpenAI-compat gateway maps `effort high → agent_count=16` as a convenience. A well-designed Lancelot agent API should expose both as independent params: `n_agents: Literal[1,4,16]` and `reasoning_budget: Literal["none","low","medium","high"]`. [GROK_HEAVY.md R3.6]

### 4.2 `max_turns` does not equal `total tool calls` (parallel tool calls in one turn)

A single "turn" in the multi-agent loop can include multiple parallel tool calls (enabled by default: `parallel_tool_calls: true`, `tool_choice: "auto"`). Budget the loop by turns, not by tool calls. [GROK_420_AGENTIC.md §5.4, GROK_HEAVY.md R3.7]

### 4.3 Tool calls returned atomically in streaming (not as deltas)

"With streaming, the function call is returned in whole in a single chunk, not streamed across chunks." [GROK_420_AGENTIC.md §5.5] This means an agent framework's tool-call parser never needs to handle partial tool-call JSON. Design accordingly: buffer tool-call chunks until they arrive complete (one chunk), then execute.

### 4.4 Custom tools pause execution; built-in tools do not interrupt client

Built-in tools (`web_search`, `x_search`, `code_execution`, MCP) execute autonomously server-side — the model internally calls them with no client round-trip. Custom function tools pause execution and **return control to the client** for execution. This is the server-side-vs-client-side tool distinction. [GROK_420_AGENTIC.md §4.2] In Lancelot terms: tools registered with `@app.tool` (server-side, Lancelot executes them) vs tools that return a `tool_call` event to the caller (client-side, the caller executes them). Both patterns are valid; make the distinction explicit in the API surface.

### 4.5 `server_label` prefix namespacing for MCP tools

When connecting multiple MCP servers, each gets a `server_label`. All tools from that server are prefixed `<server_label>.<toolname>` in tool-call events and citations. [GROK_420_AGENTIC.md §11.1-11.4] Lancelot's `lancelot.mcp.mount` does not currently namespace by server label. For multi-server MCP support, add `label: str` to `mount()` and prefix tool names.

---

## 5. Honest hype / durable vs. depreciating assessment

**Durable (3+ model-generation stability):**
- Leader-is-synthesizer pattern (architectural, not model-specific)
- Resource-bound termination (`max_turns` ∧ wall-clock) — the convergence-based alternatives are demonstrably worse
- `reasoning_content` as a separate stream channel — this naming will change but the separation won't
- Prefix-cache-aware prompt design (canonical shared prefix, personalization after the prefix)
- Character-span citation anchoring (beats paragraph-grain, more precise feedback loop)
- `IncludeOption` (opt-in verbosity gate) — clean API design pattern

**Depreciating (1-2yr model-clock churn risk):**
- Specific agent names (Harper, Benjamin, Lucas) — these are prompt-level persona strings, likely to change with each model generation. Do not hardcode them in Lancelot.
- xAI-specific tool schemas (the verbatim JSON schemas for `x_keyword_search` etc.) — useful for replica fidelity but not portable to other providers.
- `reasoning.effort` mapping to `agent_count` — the OpenAI-compat gateway mapping (`low/medium → 4`, `high → 16`) is a convenience translation that may change; use native `agent_count` directly.
- The specific 200s/120s timeout values — these are xAI's infrastructure constants, not universal. Calibrate your own.

**Hype:**
- "Continuously updated — no strict knowledge cutoff" (Grok 4 leaked system prompt). R3 shows the docs now state an explicit November 2024 cutoff. The marketing line is gone. [GROK_HEAVY.md R3.6 correction]
- HLE 50.7% as evidence of multi-agent superiority — HLE benefits specifically from ensemble reasoning on diverse hard questions. On saturating benchmarks (GPQA ~85-90%), the Heavy advantage shrinks to noise. The ensemble wins where individual accuracy is moderate and questions are independent. [GROK_HEAVY.md §11]

---

## 6. Lancelot byte-path lens (what TransportPool/stream/context already provides)

| Grok mechanism | Lancelot analog | Gap |
|---|---|---|
| `max_turns` ∧ 200s ∧ 120s/wait | `per_route_timeout=`, `LANCELOT_DRAIN_SECS` (drain), `max_concurrent_requests` | **Gap:** no turn counter or per-step wall-clock budget at agent-loop level; needs a new `BudgetContext` |
| `chatroom_send(message, to=enum)` | `asyncio.Queue` + `@app.batch` coalescing | **Partial:** `@app.batch` coalesces requests by key; for inter-agent messaging, a named channel per agent (keyed queue) is the pattern; `fanout()` sends outbound, but incoming messages from peers need a separate primitive |
| Leader-only render components | `@app.stream` (only the leader calls it) | **Minimal gap:** already enforced by who holds the `@app.stream` generator reference; no framework change needed |
| `reasoning_content` separate channel | `@app.stream` with typed chunk dicts | **Gap:** need a typed chunk convention (`{"type":"reasoning","content":"..."}` vs `{"type":"answer","content":"..."}`); R3 pump handles it, the convention needs standardizing |
| `encrypted_content` multi-turn blob | `lancelot.context` ContextVar (opaque bytes) | **Gap:** no encrypted-blob ContextVar today; could add `reasoning_state: bytes` ContextVar echoed via `X-Lancelot-Reasoning-State` response header |
| `inline_citations` opt-in include | `lancelot.mcp` minimal-return discipline | **Gap:** MCP already warns on >4KB returns; per-sentence char-span citations are not yet a first-class primitive; could be added as an optional output type on `ToolSpec` |
| `SamplingUsage.cost_in_usd_ticks` | `lancelot.context.cost_usd` + `X-Lancelot-Cost-USD` trailer | **Already built.** `lancelot.upstream_metrics` calculates cost per LLM call and accumulates into `cost_usd` ContextVar. |
| Prefix cache via `dumps_canonical` | `lancelot.canonical.dumps_canonical` | **Already built.** Use canonical shared prompt prefix; personalization after the prefix. |
| `IncludeOption` verbosity gate | Not yet present | **Gap:** a `X-Lancelot-Include: trace,tool_outputs` header or query param to gate what the SSE stream emits; clean addition to ASGI seam |
| Tool-call `IN_PROGRESS/COMPLETED/FAILED` status events | Not in `lancelot.tool` | **Gap:** `ToolSpec` output carries result but not live status; add `tool_status_stream` channel for agentic products with activity feeds |

---

*End of note. Sources: `teardowns/GROK_HEAVY.md` (R1/R2/R3) + `teardowns/GROK_420_AGENTIC.md`. All claims cited to section/round above.*
