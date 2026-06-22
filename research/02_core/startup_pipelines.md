# Startup AI Pipelines — Cost-Effective Production Playbook

**Source:** `researchfms/core/08_STARTUP_AI_PIPELINES.md` (678KB, 9090 lines, 2026-04-20)
**Focus:** Cost-effective AI pipeline primitives from production startups — the mechanisms operators have validated under real load.

---

## 1. The Core Cost Levers (ranked by impact)

### 1A. Semantic / Content-Addressed Cache — 40–95% Cost Reduction

**Mechanism (Tavily §11.3):** Three-tier cache keyed on query content:
- L1 Redis: 1% of keys → 30% of requests, sub-millisecond
- L2 S3 Express One Zone (same AZ): primary, single-digit ms
- L3 S3 Standard: backup durability
- Cache hit: skip all LLM stages. **95% cost reduction** from migrating to this from a document DB.

**Mechanism (Firecrawl §23.1):** Semantic index keyed on `SHA256(url + options)` with 14-day TTL → **~40% cache hit rate** for web scraping. Stored proc: `index_get_recent_4(p_url, p_team_id, p_origin, p_formats, p_only_main_content, ...)` [11 params]. Cache miss → fall through to 13-engine pipeline. Write: Supabase row + GCS blob, async.

**Lancelot byte-path:** `lancelot.canonical.dumps_canonical` is the KV-cache prefix stability primitive (stable sorted-key JSON bytes). This is **exactly** the prompt-cache prefix discipline these startups apply to their content-addressed caches. Lancelot does not yet expose an HTTP-layer semantic cache middleware — that would be the right Lancelot layer (an optional ASGI middleware keying on `dumps_canonical(request_body)` + route, with TTL). Upstream KV cache (Anthropic prompt cache) is the provider-side complement; `lancelot.canonical.dumps_canonical` enables the stable prefix for it.

---

### 1B. Tool Schema Compression — 98% Context Token Reduction

**Mechanism (Composio §1.1):** Instead of loading 1000+ tool schemas (~100K tokens) into every LLM context, expose exactly **6 meta-tools** (~2K tokens total):
1. `SEARCH_TOOLS` — semantic search over 2000+ tool schemas (Turbopuffer vector DB)
2. `GET_TOOL_SCHEMAS` — fetch full schema for N tools at runtime
3. `MULTI_EXECUTE_TOOL` — execute 1+ tools in one call
4. `MANAGE_CONNECTIONS` — OAuth/API key management
5. `REMOTE_WORKBENCH` — remote sandbox
6. `RECIPES` — learned multi-step execution plans

The agent discovers tools dynamically at runtime, loading only what it needs. Schema compression from ~100K → ~2K tokens = ~98% reduction in per-call tool-context cost.

**Lancelot byte-path:** `@app.tool` + `lancelot.mcp.mount` are Lancelot's tool primitives. The 6-meta-tool pattern is a **Lancelot agent-layer primitive** (not yet built): a `ToolRouter` that holds a schema index and exposes dynamic discovery tools. `lancelot.canonical.dumps_canonical` can key the search index for cache stability.

---

### 1C. Model Tiering — 70–80% Cost Reduction

**The validated production split (AgentQL/Mino §13.2, §13.11):**
- **Reasoning layer: 20–30% of steps** — frontier model (Gemini 3.0 Flash or GPT-4/Gemini Pro), used for planning, ambiguous decisions, novel situations
- **Execution layer: 70–80% of steps** — small task-specific fine-tuned models ("milliseconds not seconds"), used for mechanical execution of known patterns

**Codified learning (TinyFish §13.3):** First encounter → LLM resolves ambiguous UI decision (expensive, seconds). Second encounter → codified heuristic (versioned artifact, near-free, instant). More executions → cheaper and more reliable (compound improvement). Three tiers per decision node:
1. Deterministic code (transforms, schema validation) — cheapest
2. Model-backed choices with guards (ambiguous UI) — expensive once
3. Codified heuristics from previous encounters — near-free thereafter

**Junie two-model split (§3.3):** Primary model (Sonnet/Opus/GPT-4o) for reasoning, code generation, complex decisions. Faster model (Haiku/Flash/GPT-4.1-mini) for context compression, task classification, memory extraction. Automatic selection by task complexity.

**Stagehand defaults (§21.2):** `openai/gpt-4.1-mini` for the SDK default (speed+cost), `openai/gpt-4.1-nano` for server-managed (even cheaper). o1/o3 downgraded to gpt-4o for verification steps (`// o1 is overkill for this task` — verbatim source comment).

**Lancelot byte-path:** TransportPool already handles multi-model dispatch. The tiering **policy** is the agent-layer primitive that's missing: a `ModelRouter` that classifies request complexity → routes to fast/standard/deep tier. Lancelot's `lancelot.upstream_metrics` already tracks error taxonomy and cost — it's the right foundation for a tiering policy that adapts on quota/cost signals.

---

### 1D. Iterative Distillation — Avoid Quadratic Token Growth

**Mechanism (Tavily Research Agent §11.2):**
- BAD: accumulate raw search results per iteration → O(n*m*(m+1)/2) tokens as iterations grow
- GOOD: after each iteration, distill results into short-term memory (compressed reflections), carry only distilled insights forward, provide raw sources ONLY at final synthesis for citations
- Result: **linear O(n*m) token growth**, **66% token reduction** vs Open Deep Research on equivalent quality

**Mechanism (Abridge / Lipton §15.10):** Verbatim production design pattern:
1. Prompt to a behavior (direct LLM call)
2. Layer multiple calls / different models in sequence (add guardrails for known failure modes)
3. Once the workflow becomes "Byzantine," **condense it into a single model call via distillation**
- Loop: "building → extending → refining → distilling → extending"
- The dict-distillation move (§15.3): 5,641 medication corrections compiled from 558 clinician-reported errors + synthetic data → regex lookup, applied BEFORE the LLM ever sees the transcript. Free at inference time.

**Lancelot byte-path:** Not currently a Lancelot primitive. A `ContextDistiller` that accepts an async generator of observations and emits rolling compressed summaries is the agent-layer primitive. Lancelot's streaming R3 pump (async-gen → bounded channel) is the structural foundation — the distillation step rides on top of it as a transform in the generator pipeline.

---

### 1E. Deterministic Preprocessing — Eliminate Expensive LLM Calls

**Mechanism (Abridge §15.3):** Cost gradient: free dict lookup → cheap recall-tuned gate → expensive LLM only on flagged spans.
- `clinicial_mistake_dict.js`: 558 clinician-reported ASR medication errors
- `synthetic_mistake_dict.js`: 4,096 synthetic medication error mappings
- `generic_names.js`: 987 drug name normalizations
- Total: 5,641 corrections applied via regex BEFORE transcript hits LLM
- 83% error reduction on new medications vs Google (pre-LLM, effectively free)

**Mechanism (Abridge MidFusion §15.9):** Post-hoc evidence extraction: classification framing beats span-extraction; generative LLMs terrible at task (GPT-3.5 F=26.8 vs MidFusion F=94). Finetune a small model on the expensive LLM's outputs once → run the small model forever.

**Mechanism (Firecrawl §23.2):** Schema routing before model selection: `hasRecursiveStructure(schema)` walks schema depth-5, checks for `$ref` → routes to gpt-4.1 (expensive). Otherwise gpt-4o-mini (cheap). Deterministic decision, zero LLM cost.

**Lancelot byte-path:** Not currently a Lancelot primitive. In the agent layer, pre-LLM deterministic gates belong in the ContextBundle assembly step. Lancelot's serialization speed (pydantic-core R5 R-swap) makes rapid schema introspection cheap.

---

### 1F. Deduplication at Ingest — Avoid Re-embedding

**Mechanism (Hyperspell §2.1):** SHA-256 content hash → Redis dedup check (TTL: 7 days) before any embedding or entity extraction. If duplicate: skip everything. Embedding cost: only for new content.

**Mechanism (Supermemory §17.2):** Content hash dedup via `contentHash` field. Cron imports every 4 hours deduplicate automatically.

**Mechanism (Tavily lgptr §11.7):** URL-keyed dict deduplication — `if doc['url'] not in search_results` (note: the `in` check against list is a silent no-op bug; dedup works via dict keying only — production gotcha, keep dict not list).

**Lancelot byte-path:** Not directly a Lancelot byte-path concern. In a Lancelot agent's memory layer, SHA-256 dedup at the ingestion boundary is the right primitive. `lancelot.canonical.dumps_canonical` provides the stable key for the hash.

---

## 2. Context Engineering Primitives

### 2A. Semantic + Structural Context Budget

**Mechanism (Crunched §5.4):** Context management in a 50-tab workbook with thousands of formulas:
1. User sends prompt with `workbook_state` (sheet list, total cells, active sheet, selected range)
2. Backend uses metadata to decide which areas to investigate
3. Backend sends `get_range` tool calls **in parallel** to gather context
4. Context management system selects the most relevant data
5. Selected context + finance-specific system prompt → LLM
6. Performance degrades after ~15 messages → start fresh (no persistent memory across threads — known limitation)

**Key pattern:** Send structured metadata first; gather relevant slices in parallel; let the context management layer select. Do NOT send the full document.

**Mechanism (Firecrawl deep research §23.5):**
- `generateSearchQueries` keeps only last 50 findings (sliding window `findings.slice(-50)`)
- Token limits enforced per stage: `generateFinalAnalysis=100k`, `analyzeAndPlan=120k`, `generateSearchQueries findings=10k`
- Exactly 3 search queries per iteration (`.slice(0,3)` hardcoded) — bounded fan-out

**Lancelot byte-path:** `lancelot.context` ContextVars (request_id, agent_id, tenant_id, trace_id) are the spine for context tracking. A `ContextBundle` primitive (the agent-layer missing piece) that tracks token budget, parallel fetches, and compaction state would sit on top.

---

### 2B. Hybrid Retrieval — Vector + BM25 Fusion

**Production constants across startups:**
- Hyperspell (§2.2): `0.7 * vector_score + 0.3 * graph_score`
- Nia/ZeroEntropy (§9.19): `alpha=0.7` (70% vector / 30% BM25), `top_k=20`
- Tavily HybridRAG (§11.6 + §11.12): Cohere `embed-english-v3.0` (1024d) + `rerank-english-v3.0`, MongoDB `$vectorSearch` with `numCandidates=max_local+3` (HARDCODED overfetch by 3)
- Perplexity (§12.1): BM25 + pplx-embed dense vector at chunk-level, NOT document-level → then 3-level reranking (L1 lexical+embedding fast, L2 cross-encoder expensive, L3 XGBoost ~0.7 threshold)

**Chunking constants:**
- Hyperspell: 512 tokens, 50-token overlap, tiktoken `cl100k_base`
- Perplexity: 512-token sliding window, 125-token overlap
- Supermemory: semantic boundary detection (NOT fixed-size); code: tree-sitter AST-aware

**Lancelot byte-path:** Retrieval is agent-layer. The constants (0.7/0.3 fusion weight, 512/50 chunking, +3 overfetch) are directly applicable to a Lancelot memory/retrieval primitive. `lancelot.canonical.dumps_canonical` provides the stable query key for retrieval caching.

---

### 2C. Observation Masking / Structured Injection vs. Raw Dump

**Mechanism (Supermemory withSupermemory §17.3):**
1. Extract last user message
2. Check LRU cache (max 100 entries, key: `containerTag:threadId:mode:normalizedMessage`)
3. If miss: call memory profile API
4. Deduplicate: static > dynamic > search (priority order)
5. Format as markdown sections: `## Static Profile`, `## Dynamic Profile`, search results
6. Inject into system prompt
7. After response: if `addMemory="always"`, save conversation

**At 80% context usage:** Trigger structured summarization preserving: literal requests, end goals, files touched, remaining work, **negative constraints** (things forbidden or that failed).

**Mechanism (MiroFish §4):** Entity context per batch truncated to 50,000 chars. Section generation shows previous sections truncated to 2,000 chars. Graph context capped at 50,000 chars. All explicit budget limits.

**Lancelot byte-path:** The `withSupermemory` LRU cache (100 entries, content-addressed) is directly reusable as a pattern for any per-turn context injection layer in Lancelot agents.

---

### 2D. Semantic Cache Bypass Field (Anti-Cheat)

**Mechanism (ZeroEntropy/Nia §9.11):** `bypass_semantic_cache: bool` parameter explicitly lets the caller skip the semantic cache when they know the query is unique/time-sensitive. This is a first-class API field, not a workaround.

**Mechanism (Tavily §11.2):** `exact_match: bool` (Feb 2026 addition) — bypasses semantic search, implies custom inverted index exists behind the semantic layer.

**Lancelot byte-path:** Any Lancelot semantic cache layer should expose this bypass knob. Maps to a route-level parameter or request header.

---

## 3. Streaming and Latency Patterns

### 3A. TTFT as the Signal, Not Total Throughput

**Mechanism (Nia §9.20):** Tracer SSE event sequence: `connected → started → planning_complete → synthesis_start → synthesis_complete → complete`. Client can display progress at each stage. `planning_complete` fires after thinking (tracer-deep) or after subagent dispatch (tracer-fast) — enables TTFT measurement at the planning boundary.

**Mechanism (Grok 4.20 §20.11):** Tool calls returned **atomically in single chunk** (NOT streamed per-token). This is deliberate — prevents the client from acting on partial tool call JSON.

**Mechanism (Abridge §15.13):** Socket.IO for app events/subscriptions (pingInterval 25000ms, pingTimeout 20000ms, maxPayload 1MB). Self-hosted LiveKit WebRTC SFU for low-latency dictation (migrated off LiveKit Cloud — `use-self-hosted-live-kit` flag). Assist tokens stream over SSE (`/abridge-assist/streamChunk`). Two planes: events vs audio.

**Lancelot byte-path:** `lancelot.http.stream.ttft` (time-to-first-chunk) is already built and validated. The patterns here confirm TTFT is the right metric. Tool call atomicity (return the full call, don't chunk mid-JSON) is the right streaming behavior for the Lancelot agent layer.

---

### 3B. Polling Constants (Research-class endpoints)

Operators universally use exponential backoff polling for long-running AI jobs:

| System | Initial | Backoff | Cap | Max |
|--------|---------|---------|-----|-----|
| Tavily Research (§11.12) | 2000ms | 1.5× | 10000ms | 15min (pro)/5min (mini) |
| Firecrawl extract (§23.4) | — | — | — | 24h TTL |
| Mino/TinyFish (§13.7) | retry 0.5s | 0.5× multiplier | 8s | 600s |
| Aemon (§10) | — | — | — | ~300-1000 iterations |

**Lancelot byte-path:** `lancelot.ratelimit` handles inbound. For outbound long-running job management (agent-layer), the polling constants above are the right reference. `TransportPool` with `retry_hint` (T4.9, Turbopuffer pattern) already handles Retry-After header honoring.

---

## 4. Batch Coalescing Patterns

### 4A. Request Batching at the API Boundary

**Mechanism (Tavily lgptr §11.7):** `/extract` called in batches of 20 URLs with `asyncio.gather` — concurrent, not sequential. MAX_SEARCH_QUERIES=4.

**Mechanism (Firecrawl §23.4):** Extract in chunks when content exceeds token budget. Schema analysis and extraction separated.

**Mechanism (MiroFish §3):** `asyncio.Semaphore(128)` caps LLM concurrency within a simulation. `asyncio.gather(*[LLMAction() for each active agent])` — all agents act in parallel, capped by semaphore.

**Lancelot byte-path:** `@app.batch` is Lancelot's prefix-aware request coalescing primitive (~12× throughput, ~16× fewer upstream calls vs rate-limited LLM backend). The patterns above confirm this is the right abstraction — particularly for fan-out to the same LLM endpoint across many user requests.

---

### 4B. Parallel Context Gathering Before LLM Call

**Mechanism (Crunched §5.4):** Backend sends `get_range` tool calls **in parallel** to gather context. Not sequential.

**Mechanism (Firecrawl deep research §23.5):** `generateSearchQueries` generates 3 queries → parallel scrape of top URLs → transformer stack → context assembly → single synthesis call.

**Mechanism (Tavily get_company_info §11.13):** Fires **3 parallel searches** across `("news","general","finance")` simultaneously.

**Lancelot byte-path:** `TransportPool.fanout(coros, first_n, deadline, cancel_laggards, concurrency)` is the exact primitive for parallel context gathering. This is the byte-path complement to context assembly in the agent layer.

---

## 5. Multi-Tier Research Agent Architecture

### 5A. Mini / Pro / Auto Tiers

**Mechanism (Tavily §11.2, §11.9):**
- `mini`: Planning → WebSearch(es) → Generating (simple pipeline)
- `pro`: Planning → WebSearch(es) → **ResearchSubtopic cycles** (nested sub-topics, parallel) → Generating
- `auto`: server decides based on query complexity
- Timeouts: mini=5min, pro=15min, auto=15min
- Credits: mini 4-110, pro 15-250

SSE tool names by tier (verbatim):
- mini: `Planning`, `WebSearch`, `Generating`
- pro: `Planning`, `Reflection`, `ResearchSubtopic`, `WebSearch`, `Generating`

**Mechanism (ZeroEntropy/Nia §9.18):**
- `tracer-fast`: `claude-haiku-4-5-20251001` + parallel Haiku subagents (fan-out, server-side)
- `tracer-deep`: `claude-opus-4-7` single-agent with extended thinking
- Default: `tracer-deep` (quality-first default is intentional for a research product)

**Lancelot byte-path:** The mini/pro/auto tier pattern is a Lancelot agent-layer primitive (not yet built). The tiers map directly to: `TransportPool` model selection + `fanout()` (pro tier uses fan-out subagents). SSE event names for each stage are the right streaming protocol shape.

---

## 6. Context-as-Memory Primitives

### 6A. Four-Tier Memory with TTLs

**Mechanism (Nia/ZeroEntropy §9.21):**
- `scratchpad`: 1 hour TTL — session-scoped working memory
- `episodic`: 7 days (default) — conversation history, recent context
- `fact`: permanent — user-stated facts ("I prefer Python")
- `procedural`: permanent — learned workflows ("when I ask X, do Y")

Memory writes include a `lineage` field: `{source_ids, confidence, derived_from, tool_calls, model_version}` — provenance graph over stored memories, enabling "how did you know this?" queries.

**Mechanism (Supermemory §17.1):** MemoryEntry relations (3 types):
- `updates`: supersedes old info → old gets `isLatest: false`
- `extends`: enriching context without contradiction
- `derives`: second-order inferences from combining memories

Three embedding slots per chunk (primary, migration, Matryoshka). `isForgotten`, `forgetAfter`, `isStatic` temporal controls.

**Lancelot byte-path:** `lancelot.memory_pool.MemoryPool` is byte-OOM tracking, NOT agent memory. The 4-tier TTL memory structure is a **Lancelot agent-layer primitive** (not yet built). The scratchpad/episodic/fact/procedural distinction is durable across model generations.

---

### 6B. Experimental DB as Memory (Anti-Hallucination)

**Mechanism (Periodic Labs §22.5):** Context assembled from DB, not conversation:
1. Top-N performing compositions (ranked by target metric)
2. Last-M failures (prevents repetition)
3. Relevant literature passages (field-filtered RAG)
4. Current simulation prediction
5. Pattern analysis of element combinations in top performers

NOT carried in LLM context: full conversation history. The experimental DB IS the memory. "You can look for aberrations. You can look for patterns." Each call is semi-stateless — LLM gets a fresh context assembled from structured DB records.

**Why this matters for Lancelot:** The expensive anti-pattern is stuffing conversation history into every call. The correct pattern is assembling structured context from an indexed store per call. This is the ContextBundle assembly pattern.

---

## 7. Cost-Effective Model Selection in Production

### 7A. Validated Production Model Assignments

| Task | Validated Model | Source | Why |
|------|----------------|--------|-----|
| High-volume agent execution (mutation) | Gemini Flash | Aemon §10.2 | Best cost at scale (~$0.01-0.05/iter, 300 iters ≈ $3-15) |
| Breakthrough mutations (rare, ~15% of steps) | Sonnet/Pro | Aemon §10.2 | Quality when it matters |
| Agent planning/reasoning | Gemini 3.0 Flash | Mino §13.11 | 20-30% of steps |
| Agent mechanical execution | Fine-tuned 7B task-specific | Mino §13.11 | 70-80% of steps, "milliseconds not seconds" |
| Extract/scrape (simple schemas) | gpt-4o-mini | Firecrawl §23.2 | Default — cheapest capable |
| Extract (recursive schemas with `$ref`) | gpt-4.1 | Firecrawl §23.2 | Routed by `hasRecursiveStructure()` |
| Deep research synthesis | o3-mini | Firecrawl §23.5 | Quality for final answer |
| Entity extraction (memory ingestion) | Llama 3.1 8B via Groq | Hyperspell §2.1 | Temperature=0, structured JSON — open-source cost |
| SDK default for tool-use agents | gpt-4.1-mini | Stagehand §21.2 | Speed + cost balance |
| Server-managed (multi-tenant) | gpt-4.1-nano | Stagehand §21.2 | Even cheaper for managed |
| Verification steps | gpt-4o (downgrade from o1) | Stagehand §21.14 | "o1 is overkill for this task" (verbatim) |

**Firecrawl model cost table (KNOWN from source §23.5):**
```
"openai/o3-mini":     input $1.10/M, output $4.40/M
"gpt-4o-mini":        input $0.15/M, output $0.60/M
"openai/gpt-4o":      input $2.50/M, output $10.00/M
"gemini-2.5-pro":     ≤200k input $1.25/M, output $10.00/M
```

---

### 7B. The "Ferrari Engine in a Toyota" Anti-Pattern

**Verbatim from source (§7982, unnamed product teardown):** "model is a commodity engine, the system is the moat (Ferrari engine in a Toyota)." gpt-4 (temp 0.3, max_tokens 800) → gpt-3.5-turbo (600, 0.3) **only on context overflow**. The system (context assembly, retrieval, tool design) determines quality; the model is interchangeable within a quality tier.

**Lancelot byte-path implication:** This confirms that the most defensible investments are in byte-path primitives (streaming, batching, context assembly, caching), not in model selection. Lancelot's architecture is correctly positioned.

---

## 8. Observed Anti-Patterns (Do Not Copy)

### 8A. Per-Chunk LLM Calls (Streaming Anti-Pattern)

**Source:** The streaming pump regression in Lancelot's own history (M3) was a 16-26× regression. Every production system confirms: the expensive pattern is per-chunk bridging. The correct pattern: assemble context once, stream via a single LLM call, don't re-call the LLM per chunk.

Stagehand §21.5: `extract()` chunking uses 2 LLM calls per chunk (extract + metadata). Metadata call decides if extraction is complete → early termination when complete. This is the minimum viable chunked extract. Still more expensive per page than a streaming call.

**Lancelot byte-path:** The R3 pump (one bridge crossing/stream) is already the right architecture.

---

### 8B. Sequential Tool Calls for Context Gathering

**Source (Crunched §5.4):** Correct pattern: `get_range` tool calls issued **in parallel**. The anti-pattern is sequential tool-call chains to gather context.

**Lancelot byte-path:** `TransportPool.fanout()` is the right primitive for parallel context gathering.

---

### 8C. Quadratic Token Accumulation (Research Loops)

**Source (Tavily §11.2):** Raw accumulation of search results across iterations → O(n*m*(m+1)/2) tokens. The fix: distill after each iteration, carry only compressed reflections forward. Validated 66% token reduction.

---

### 8D. MCP Tool Overhead vs CLI

**Source (AgentQL/Mino §13.24):** MCP overhead: 500–2,000 tokens per call vs ~200 data payload. CLI alternative: **7× better success rate**, **40% fewer tokens** on identical tasks. MCP adds structured metadata overhead that degrades agent performance when the underlying data payload is small.

**Lancelot byte-path:** `lancelot.mcp.mount` already includes the >4KB WARN discipline (Dex Horthy pattern). This data point confirms: keep MCP tool responses minimal.

---

### 8E. Context History Inflation

**Source (Stagehand §21.3):** Anthropic CUA client: `compressConversationImages()` before each step to prevent context overflow. Images in conversation history inflate context rapidly — compress/evict before each call.

**Source (Crunched §5.4):** Performance degrades after ~15 messages. No persistent memory across threads — known limitation. Start fresh. This is the honest product design: reset context rather than degrade.

---

## 9. Operational Constants (Production-Validated)

```
# Chunking
tiktoken cl100k_base, max_chunk=512 tokens, overlap=50 tokens  [Hyperspell §2.1]
Semantic boundary detection (not fixed-size) for code          [Supermemory §17.2]

# Cache TTLs
Semantic index: 14 days [Firecrawl §23.1]
Entity dedup (Redis): 7 days [Hyperspell §2.1]
Memory: scratchpad=1h, episodic=7d, fact=permanent [Nia §9.21]
LRU context injection: max 100 entries [Supermemory §17.3]
Model gateway cache: 48h [Browserbase §21.9]

# Retrieval
Hybrid weight: 0.7 vector / 0.3 BM25 [Hyperspell §2.2, Nia §9.19]
numCandidates overfetch: max_local + 3 [Tavily §11.12]
top_k: 20 [Nia §9.19]
min_score (cosine): 0.6 [Supermemory §17.4]

# Agent loop limits
max steps (tool agent): 20 [Stagehand §21.9]
max steps (CUA): 10 [Stagehand §21.9]
Agent tool timeout: 45,000ms [Stagehand §21.9]
Max tool schemas per call: 200 [Grok §20.9]
Max debate budget: ≤180 tokens total [Grok §20.9]

# Context budgets
AriaTree max tokens: 70,000 [Stagehand §21.9]
MiroFish entity context per batch: 50,000 chars [§4]
Section generation previous sections: 2,000 chars truncation [MiroFish §4]
Sliding findings window (deep research): 50 items [Firecrawl §23.5]

# Research polling
Initial: 2000ms, backoff: 1.5×, cap: 10000ms, mini=5min, pro=15min [Tavily §11.12]

# Semaphore / concurrency
LLM semaphore for batch simulation: 128 [MiroFish §3]

# Token counting (client-side)
gpt-3.5-turbo encoding via tiktoken — NOT a backend model signal, only for input estimation [Tavily §11.5]
```

---

## 10. Byte-Path Implications for Lancelot (Summary)

| Primitive | Lancelot Status | Action |
|-----------|----------------|--------|
| KV-cache stable prefix | `lancelot.canonical.dumps_canonical` — BUILT | Use as cache key for HTTP semantic cache middleware |
| Request batch coalescing | `@app.batch` — BUILT, validated ~12×/~16× | Directly applicable to fan-out LLM calls |
| Parallel fanout | `TransportPool.fanout()` — BUILT | Use for parallel context gathering |
| TTFT metric | `lancelot.http.stream.ttft` — BUILT | Already aligned with production signals |
| MCP minimal-return discipline | `lancelot.mcp.mount` >4KB WARN — BUILT | Confirmed by MCP overhead data |
| Model tier routing | NOT BUILT | Agent-layer ModelRouter: fast/standard/deep |
| Iterative distillation | NOT BUILT | Agent-layer ContextDistiller over streaming |
| 6-meta-tool schema compression | NOT BUILT | Agent-layer ToolRouter with semantic search |
| 4-tier memory with TTLs | NOT BUILT | Agent-layer Memory (scratchpad/episodic/fact/procedural) |
| HTTP semantic cache | NOT BUILT | Optional ASGI middleware keying on `dumps_canonical` + route |
| SHA-256 content dedup | NOT BUILT | Memory ingestion layer primitive |

---

*All mechanism claims are cited to their source section in `08_STARTUP_AI_PIPELINES.md`. KNOWN = confirmed from source code / RE. INFERRED = high-confidence inference from available signals. DESIGNED = author-designed for replica, not observed in production.*
