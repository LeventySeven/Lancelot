# Perplexity AI — Teardown Distillation

*Sources: `teardowns/PERPLEXITY_DEEP.md` (GitHub RE + live API probe, April 2026); `teardowns/PERPLEXITY_COMPUTER.md` (SDK RE + leaked prompts + arXiv, April 2026)*

Focus per task instruction: **True-delta vs cumulative streaming convention**, **ToolChoice.NONE force-synthesis**, **BrowseSafe post-tool async scanner**, **MCP-eats-context lesson**.

---

## 1. True-Delta vs Cumulative Streaming — Pick ONE Convention

The single most dangerous gotcha for any framework that proxies or aggregates Perplexity's APIs: the two streaming endpoints use **opposite conventions**, and both differ from OpenAI's standard.

### The Exact Inversion (KNOWN — from SDK `types/response_stream_chunk.py` + live probe, April 2026)

| Endpoint | SSE field | Semantic | Client action |
|---|---|---|---|
| `/v1/responses` (Agent API) | `response.output_text.delta` → `delta` | **TRUE DELTA** — new text to APPEND | `buf += event.delta` |
| `/v1/chat/completions` (Sonar) | `choices[0].delta.content` / `choices[0].message.content` | **CUMULATIVE** — `message.content` = full text so far | Use `message.content` as current state; do NOT concatenate `delta.content` |

Perplexity **inverted OpenAI's convention between their two APIs** — OpenAI chat completions produce true deltas. Perplexity Sonar produces cumulative chunks. The LiteLLM issue #8455 confirmed this in production: "the content text sent always contains the complete content up to that point." Additionally, Perplexity's SSE object type for Sonar is `chat.completion` (not `chat.completion.chunk`), which is another signal that its "delta" field is not the standard partial-chunk it resembles.

### Wire Format Detail (KNOWN — `_streaming.py` SSEDecoder)

```
# Each event delimited by blank line (\n\n or \r\r or \r\n\r\n)
data: {"sequence_number":N,"type":"response.output_text.delta","delta":"token",...}\n
\n
# [DONE] terminator (Agent API):
data: [DONE]\n
\n
# Error: SSE field name, NOT in JSON data:
event: error
data: {...}
```

`sequence_number` is monotonically increasing per stream and present on ALL event types. Purpose: event ordering (detect out-of-order delivery on HTTP/2). NOT a cumulative content offset. NOT for deduplication.

### Lancelot stream.py implication

Lancelot's `@app.stream` pump emits true deltas (SSE `data:` lines, each chunk is new bytes). This is the **correct convention** — identical to the Agent API format. If Lancelot ever proxies or adapts Sonar chat completions, it must consume `message.content` (the cumulative field), not concatenate `delta.content` chunks. The existing stream transport is fine; the risk is in any future Sonar proxy adapter.

---

## 2. ToolChoice.NONE on Final Step — Force-Synthesis Pattern

### The Mechanism (KNOWN — `search_evals/deep_research.py`, Perplexity open source)

From the open-sourced eval agent `deep_research.py`:

```python
# Max 10 search steps per query
# On the FINAL step: ToolChoice.NONE forces the agent to synthesize (no more searching)
# Uses maybe_truncate() to manage context window — older search results are trimmed
```

The system prompt's explicit instruction for strategic pivot:
> "if no progress seems apparent... explicitly state that you are switching direction to a different hypothesis"

Retry: exponential backoff, 3 attempts, 1–10s wait.

### The Preset Budget Table (KNOWN — `docs.perplexity.ai/docs/agent-api/presets`)

| Preset | Model | Max Steps | Max Tokens |
|---|---|---|---|
| `fast-search` | Grok 4-1-fast | 1 | 3K |
| `pro-search` | GPT-5.1 | 3 | 3K |
| `deep-research` | GPT-5.2 | 10 | 10K |
| `advanced-deep-research` | Claude Opus 4.6 | 10 | 10K |

`max_steps=1` is the minimal search loop (one tool round). `max_steps=10` = up to 10 tool-call rounds before the model is forced to synthesize. The **final step always runs with tools disabled** (ToolChoice.NONE / `tools=[]`), making synthesis structurally guaranteed.

### Why This Matters for Lancelot's Agent Layer

The agent loop (`StopReason` + budget) is the principal build target in `00_LENS.md`. The durable pattern here is:

1. Loop runs up to `max_steps` with tools enabled.
2. On step `max_steps`, emit the request with **`tool_choice="none"`** (OpenAI/Anthropic term) or the equivalent `tools=[]` for the final completion call. This prevents the model from attempting another tool call when the budget is exhausted and forces a natural-language synthesis of what was gathered.
3. `maybe_truncate()` manages context: older search results are trimmed to preserve budget for the final synthesis. The oldest observations are compacted or dropped when context approaches limit.

This is a concrete stopping mechanism — distinct from "the model decides to stop" (which can loop forever on tool-happy models). It's a **hard budget + forced synthesis** pattern.

---

## 3. BrowseSafe — Post-Tool Async Scanner

### Architecture (KNOWN — arXiv:2511.20597, HuggingFace `perplexity-ai/browsesafe`)

```
Agent calls fetch_url / browser_task / get_full_page_content
         |
         +---> Tool returns raw HTML result to agent
         |
         +---> BrowseSafe async task STARTS (concurrent with LLM planning phase)
                   |
                   v
              Qwen3-30B-A3B-Instruct-2507 fine-tuned
              Input: raw HTML (NOT extracted text — degrades F1 significantly)
              Output: single token: "yes" (VIOLATES) or "no" (SAFE)
              Latency: < 1 second
                   |
                   v
              If VIOLATES: REPLACE tool output with placeholder (NOT append warning)
              If SAFE: agent sees tool output normally
              Uncertain: escalate to frontier LLM (GPT-5 ~2s, Claude Sonnet 4.5 ~23s)
```

Key implementation points (arXiv §4.3):

> "the system initiates asynchronous classification executing in parallel with language model planning, hiding security overhead behind the agent's execution time."

The gate fires **before the LLM acts on the result** (before the next tool call), not before the LLM sees the result in its context.

### The Block Decision (KNOWN — arXiv §4.7, verbatim)

> "we replace the tool call that returned malicious content with a placeholder tool call, explaining to the model what happened so that it can change course and inform the user. Critically, we **avoid including details on the malicious content** to avoid the agent inadvertently falling victim to the malicious content while trying to demonstrate the danger to the user."

**The entire tool output is replaced** — not tagged, not appended to. This is the correct mechanism: appending a warning `[BLOCKED: ...]` after the malicious content still exposes the malicious instructions to the model's attention window.

### Chunking for Long HTML (KNOWN — model card, `meta-llama/llama-cookbook/prompt_guard/inference.py`)

```
If HTML > T_w tokens (likely ~14K–15K given 16,384 context):
  Split into non-overlapping chunks at token boundaries
  Classify each chunk in PARALLEL
  Aggregation: conservative OR — ANY "yes" → entire document blocked
  Latency: sublinear with document length (parallel chunks ≈ 1-chunk latency)
```

### Model Specification (KNOWN — HuggingFace `perplexity-ai/browsesafe`)

```
Base:     Qwen3-30B-A3B-Instruct-2507 (MoE: 31B total, ~3B active at inference)
Context:  16,384 tokens
F1:       0.904 (BrowseSafe-Bench, 3,680 test samples)
Precision: 0.978
Recall:   0.841
Latency:  < 1 second
Refusals: 0 — always classifies
```

**Chosen specifically for MoE efficiency**: "primarily due to its balance between size and inference profile, having only 3B active parameters at inference time." A 31B-total MoE classifier running at 3B-effective is the right architecture for a post-tool safety gate — fast, cheap, no GPU idle.

### What Triggers BrowseSafe (KNOWN — arXiv + Comet prompt)

Exclusively tools that retrieve **external web content**:
- `fetch_url` / `get_full_page_content` — primary triggers
- `browser_task` / CDP automation returning page content

NOT triggered for: `bash`, `write`, `read`, `grep`, `glob`, `search_web` results (those go through Perplexity's own pipeline and are pre-sanitized).

### Honesty about bypass rate

Lasso Security red team achieved **36% bypass rate** using encoding attacks (NATO phonetic alphabet, Pig Latin, Base32). The stated recall 0.841 means ~16% of direct attacks pass through even without obfuscation. Not production-perfect; runtime cost of a missed injection is high in browser/credential-touching contexts.

### Lancelot lancelot.mcp mount / tool implication

Lancelot already has `lancelot.mcp.mount` with the `>4KB WARN` discipline (minimal-return). The BrowseSafe pattern adds a **post-tool async safety hook**: after any tool returning external web content, fire a classifier in a background task concurrent with the agent's next reasoning step, gate the result before the agent can act on it, and **replace** (not annotate) the tool output on detection. This is the correct async pattern — hidden behind inference latency, not blocking.

Lancelot's `@app.stream` pump runs on the same asyncio loop, so a post-tool hook fits naturally as: `asyncio.ensure_future(classify_tool_result(tool_output))` immediately after tool return, then `await classification_fut` before the next tool dispatch.

---

## 4. MCP-Eats-Context Lesson

### The Finding (KNOWN — Denis Yarats, Perplexity CTO, Ask 2026 dev conference)

> "MCP's tool definitions consume context window tokens — every tool description, parameter schema, and response format eats into the model's working memory."

For Computer (which makes many tool calls across long sessions), accumulated MCP schemas compound into significant token overhead. Perplexity's fix: move internal tool orchestration **away from MCP toward direct APIs** where tools are defined once server-side and not re-sent per request.

They still keep MCP for external/user-facing connector discovery and auth. The internal execution layer went direct-API.

### The Concrete Pattern

```
BEFORE (MCP per-request):
  Every request payload includes full tool definitions
  40+ tools × average ~200 tokens/schema = ~8K tokens per step
  10-step deep-research = 80K tokens just in tool schemas

AFTER (server-side tools):
  POST /v1/agent with just { model, input, preset }
  Tools defined once at backend; never re-sent
  40+ tools = 0 tokens of tool-schema overhead per request
```

The public Agent API endpoint (`/v1/responses`) reflects this architecture: the `tools` field accepts only 3 client-specifiable types (`web_search`, `fetch_url`, `function`). All internal Computer tools (`bash`, `write`, `run_subagent`, `confirm_action`, etc.) are server-injected, never in the client payload.

### Lancelot lancelot.mcp.mount implication

Lancelot's existing `>4KB WARN` minimal-return discipline is the output-side of this lesson. The input-side lesson is: for agent frameworks that have large tool registries, **server-side tool registration** (register schemas once at mount time, not per-request) dramatically reduces per-call token overhead. This maps directly to `@app.tool` + `tool_schemas()` in Lancelot's design — the schema is registered at decoration time, not serialized into every request payload.

---

## 5. Additional High-Signal Primitives

### 5a. The Agent Loop Budget Model (KNOWN — presets + search_evals)

The full loop pattern:

```python
for step in range(max_steps):
    if step == max_steps - 1:
        tool_choice = "none"   # force synthesis
    else:
        tool_choice = "auto"
    
    response = call_model(context, tools=tools, tool_choice=tool_choice)
    
    if response.stop_reason == "end_turn":
        break  # model satisfied
    
    if tool_choice == "none":
        break  # synthesis step complete
    
    # execute tool calls, append results to context
    context = maybe_truncate(context, max_tokens=budget)
    # maybe_truncate: trim oldest observations first to preserve synthesis budget
```

The `max_steps` + `ToolChoice.NONE` + `maybe_truncate` triad is the minimal durable deep-research loop. No magic.

### 5b. Two-API Streaming Architecture: SSE vs Socket.IO (KNOWN — robots.txt + `socket.io-client-swift` fork)

Perplexity uses **SSE for the API layer** and **Socket.IO for the web frontend** — they translate between them server-side. This is the correct split: SSE is stateless, cacheable, simpler for API consumers; Socket.IO supports reconnection and fallback polling for browser reliability. Lancelot's SSE streaming is already the right choice for the API layer.

### 5c. `<think>` Tag Strip Discipline (KNOWN — MCP server `content.replace(/<think>[\s\S]*?<\/think>/g, '').trim()`)

The MCP server's `stripThinkingTokens` function strips CoT wrapped in `<think>...</think>` before returning to callers. This is the DeepSeek-R1 / Sonar-reasoning-pro convention. The strip is opt-in: `strip_thinking: bool` parameter on the `perplexity_research` and `perplexity_reason` tools. If context is tight, strip thinking tokens; if the caller needs the reasoning trace, leave them.

### 5d. Preset Architecture as Agent Configuration Table

The preset design is directly copyable for Lancelot's agent layer:

```python
PRESETS = {
    "fast":    Preset(model="...", max_steps=1, max_tokens=3_000, tools=["search"]),
    "pro":     Preset(model="...", max_steps=3, max_tokens=3_000, tools=["search", "fetch"]),
    "deep":    Preset(model="...", max_steps=10, max_tokens=10_000, tools=["search", "fetch"]),
    "advanced":Preset(model="...", max_steps=10, max_tokens=10_000, reasoning_effort="high"),
}
```

The preset is a named bundle of `(model, max_steps, max_tokens, tools, reasoning_effort)`. User picks a preset; the framework fills in the budget parameters. This is a cleaner API than exposing all knobs separately — prevents the combinatorial explosion of "what if I set max_steps=7 with fast-search?" and lets the framework enforce sensible defaults.

### 5e. Async API for Long-Running Agents (KNOWN — SDK `async_/chat/completion_create_params.py`)

```
POST /v1/async/sonar      → submit, returns request_id
GET  /v1/async/sonar/{id} → poll result
TTL: 7 days, polling only (no callbacks)
```

Perplexity went with **polling-only async** (no webhook callbacks) for their initial async API. This simplifies the server contract: no need to track callback URLs, retry delivery, or handle webhook failures. Deep research tasks run 460–1800 seconds. The polling model is fine for human-in-the-loop workflows; for automated pipelines, the framework can wrap polling in a `while status != "completed"` with backoff.

### 5f. Thought Signature Passthrough (KNOWN — `types/response_create_params.py`)

```python
class FunctionCallInput(TypedDict):
    thought_signature: str   # base64 opaque blob for thinking models
```

When a thinking model (Claude extended thinking, o1-style) makes a tool call, the response includes a `thought_signature` base64 blob. This must be passed back in the next turn's `function_call_output`. It carries the thinking chain state across the tool round-trip. Frameworks that strip or lose `thought_signature` will break extended-thinking models' tool loops.

### 5g. Observation Compaction: `maybe_truncate()` Convention (KNOWN — `search_evals/deep_research.py`)

Older search results are trimmed first when context approaches the budget. The correct trim order is oldest-first (FIFO eviction), not newest-first. This preserves the most recent context (the results the agent was just reasoning about) while recovering budget for the synthesis step. This is the `observation_masking compaction` primitive that `00_LENS.md` lists as missing from Lancelot's agent layer.

---

## What to Skip (Tier-D for Lancelot)

- **MoE CUDA kernels** (pplx-garden, pplx-kernels): GPU inference infrastructure. Entirely off-thesis for Lancelot's byte-path mission.
- **Vespa.ai integration**: search index infrastructure. Perplexity's real moat. Not replicable and not relevant to Lancelot's layer.
- **pplx-embed training pipeline**: Embedding model training. Tier-D for a framework project.
- **Speculative decoding internals**: Inference optimization. Off-thesis.
- **Socket.IO vs SSE frontend split**: Lancelot already has SSE; no change needed.
- **`search_context_size: "high"|"medium"|"low"` knob**: Search quality tuning. Pure content concern, not transport.
- **Perplexity Computer's 40+ tool catalog**: The specific tool set is Perplexity-product-specific. The patterns (parallel tasks array, ID system, ephemeral sessions) are more durable but not immediately needed for Lancelot's agent loop.
- **`response_format: regex` constrained decoding**: Grammar-constrained decoding at the inference engine level. 10–30s startup cost. Not a framework primitive.

---

## Honesty Flags

- **BrowseSafe 36% bypass rate**: The classifier is not production-perfect. Encoding attacks (NATO alphabet, Base32) bypass it with 36% success per independent red team. It is "good enough for production" as a fast first layer, not a security guarantee.
- **True-delta/cumulative inversion**: This is a genuine gotcha that breaks integration code silently — the stream appears to work (you see text) but content repeats if you're concatenating cumulative chunks. It is NOT documented prominently by Perplexity.
- **MCP context cost is real but varies**: At 40+ tools × 200 tokens/schema ≈ 8K tokens per request, MCP overhead is real for large tool registries. For Lancelot's `@app.tool` (schema registered at decoration, not per-request), this problem doesn't manifest — but it would if a future adaptor re-serialized schemas per call.
- **`maybe_truncate()` is not disclosed at code level**: The search_evals teardown describes it by name and behavior (trim oldest results first) but the implementation in `deep_research.py` is not open-sourced. The behavior is KNOWN; the exact implementation is INFERRED from the description.
