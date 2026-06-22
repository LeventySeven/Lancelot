# Retrieval agents — mechanism-level harvest

Sources: `TAVILY.md`, `TAVILY_R3_SEARCH_BACKEND.md`, `FIRECRAWL.md`, `EXA.md`
Focus: distilled-reflection memory · role-tiered models · highlights as token moat

---

## 1. Distilled-reflection memory (Tavily / GPT Researcher)

**The mechanism.** GPT Researcher keeps only compressed insight notes between iterations, not raw retrieved content. Raw sources are provided to the LLM once — at final synthesis. Across all research loops, the working context is `distilled_notes[]`, not raw pages.

**Why it matters.** Open Deep Research accumulates messages linearly: each step appends raw content → context grows as `n*(m+1)/2` where n = iterations, m = sources/iteration. GPT Researcher's model grows as `n*k` where k = compressed-note size. At 10 iterations × 5 sources/iteration that is 275 raw-content chunks vs ~10 distilled notes — a **66% token reduction** (source: `TAVILY.md §9`).

**Production constants (verbatim from GPT Researcher `config.py`, cited `TAVILY.md §12`):**
```
FAST_LLM       = gpt-4o-mini        # loop executor
SMART_LLM      = gpt-4.1            # complex reasoning steps
STRATEGIC_LLM  = o4-mini            # planning + final synthesis
TEMPERATURE    = 0.4
SIMILARITY_THRESHOLD = 0.42         # semantic dedup threshold
MAX_SCRAPER_WORKERS  = 15
BROWSE_CHUNK_MAX_LENGTH = 8192
MIN_CONTENT_LENGTH      = 100       # filter empty/junk pages
```

**The distillation prompt is called `process_research_results`** — it uses `o4-mini` with `reasoning_effort=High` to compress each batch of scraped content into a few bullet points before appending to `distilled_notes`. Only these bullets enter the next iteration's context. The verbatim system/user prompts are in `TAVILY.md §20.6`.

**Verdict on durability.** The "66% reduction" will track the model-context-window curve, not fight it. As context windows grow, the saving shrinks in absolute importance but the *principle* stays: don't pass raw retrieved text through the loop — compress first. This is a **durable context discipline primitive**, not a hack.

---

## 2. Role-tiered model assignment (Firecrawl SmartScrape + extract pipeline)

**The mechanism.** Firecrawl assigns different LLMs to planning vs. execution roles based on reasoning requirements and call frequency, never using one model for all tasks.

**Extract pipeline (concrete assignment, `FIRECRAWL.md §6`):**
- Schema analysis step: `gpt-4.1` (high-capability, once per request)
- URL reranking/planner: `gemini-2.5-pro` (thinking model, vertex backend, ~$1.3/$5 per M tokens) — runs once per extract job
- Primary extraction: `gpt-4o-mini` (cheap, runs once per scraped URL)
- Retry extraction on failure: `gpt-4.1` (escalation, rare)

**SmartScrape two-phase (concrete assignment, `FIRECRAWL.md §20`):**
- Phase 1 wrapper generation: `gpt-4.1` — wraps user schema with `{shouldUseSmartscrape, smartscrape_reasoning, smartscrape_prompt}`
- Phase 2 (planner): `gemini-2.5-pro` on Vertex AI — emits tool calls telling Flash what to scrape
- Phase 2 (tool executor): `gemini-2.0-flash` on Google API — performs the actual scrape tool calls (~10× cheaper than Pro)

**Deep research model assignment (`FIRECRAWL.md §27-28`):**
- Search query generation: `gpt-4o-mini` (runs every depth level × 3 queries)
- Gap analysis / should-continue: `gpt-4o-mini`
- Final synthesis: `o3-mini` explicitly (`getModel("o3-mini")`) — most expensive model, called ONCE

**Reranker threshold constants (`FIRECRAWL.md §21`):**
- Single-answer queries: threshold `0.6` (stricter, more likely a definitive source exists)
- Multi-entity queries: threshold `0.45` (lenient, many valid sources)
- System prompt instruction: "If unsure, give 1.0" — err toward inclusion not exclusion

**Verdict.** Role-tiering is a **durable structural primitive** — it survives model churn because the pattern (cheap-model-for-loops, expensive-model-for-once-per-run reasoning) holds regardless of which models fill each tier slot. The specific model names are tactical.

---

## 3. Highlights as token-efficiency moat (Exa)

**The mechanism.** Exa's `/search` returns `highlights[]` — sentence-level extracts from each result, scored by a cross-encoder relevance model. Default 500 chars replaces 8000-char full-page markdown in the LLM context: **16× fewer tokens at equivalent accuracy** (source: `EXA.md §7`).

**Production behavior:**
- Highlights run **per-request** (not cached) — each query gets freshly scored snippets
- A cross-encoder scorer ranks candidate spans against the query — this is not simple keyword windowing
- `DEFAULT_MAX_CHARACTERS = 10000` in Exa SDK constants (the fallback if highlights not used)
- Highlights are returned inside `results[i].highlights[]` and `results[i].highlightScores[]`

**The token math behind it.** A 10-result search without highlights: 10 × 8000 chars ≈ 80,000 chars of context. With highlights (500 chars each): 10 × 500 = 5,000 chars. The loop agent can run ~16 iterations before filling the context that a single un-highlighted search would consume. For iterative research agents (Tavily-style), this is the structural reason deep-research can run 20-50 iterations without hitting context limits.

**Exa Websets decomposer — schema-constrained single call.** Exa decomposes a natural-language research question into `{criteria[], enrichments[], previewItems[]}` via a single LLM call with JSON-schema-constrained output (source: `EXA.md §11`). This is the plan-step of a plan-and-execute pattern, not a tool call inside a ReAct loop.

**Verdict.** Highlights are a **durable retrieval primitive** — the cross-encoder scoring layer is what makes them accurate at 500 chars. The 16× number is a property of the cross-encoder-vs-full-page tradeoff, not model-generation-dependent.

---

## 4. Plan-and-execute architecture (Exa /research/v1)

**Event taxonomy (verbatim from Exa SDK `ResearchEventDtoClass`, `EXA.md §10`):**
```
research-definition   → plan emitted
plan-definition       → planner-controlled task graph
plan-operation        → planner deciding next step
plan-output           → final plan structure
task-definition       → one task (think | search | crawl)
task-operation        → task in progress
task-output           → task complete with results
research-output       → final research result
```

**This is NOT ReAct.** The planner LLM emits `tasksInstructions[]` up front. Parallel task workers execute independently. The planner controls termination, not the loop counting iterations.

**Stop-reason taxonomy (`EXA.md §12 /agent/runs`):**
```
schema_satisfied   → LLM determined all required fields are filled
budget_reached     → costDollars.total >= budget
error              → unrecoverable failure
cancelled          → caller cancelled
```

**Cost transparency.** Each run returns `costDollars: {total, numPages, numSearches, reasoningTokens}` — operators can set a `budget` and the agent self-terminates at `budget_reached` before the loop degrades into random searches.

**Persistent agent memory via `previousRunId` (`EXA.md §12`).** The server stores the full event log. A continuation run loads `previousRunId` as its initial context — the prior run's entire event stream is replayed as context. This is event-sourced persistent memory without any application-layer checkpoint logic.

**Poll constants for production integrations (`EXA.md §20`):**
```
Research poll interval  = 1000ms
Research poll timeout   = 600000ms  (10 minutes)
Max consecutive fails   = 5
```

---

## 5. Hybrid retrieval fusion constant: RRF k=60 (Exa)

**The mechanism.** Exa fuses dense vector search with BM25 sparse search using Reciprocal Rank Fusion. The canonical constant `k=60` prevents high-rank documents from dominating when one signal produces a clear winner (source: `EXA.md §20 constants table`).

**Why k=60.** This is the academic standard from the original RRF paper (Cormack et al., 2009). At `k=60`, rank 1 scores 1/61 ≈ 0.016, rank 10 scores 1/70 ≈ 0.014 — a gentle enough slope that ranks 1-5 from both signals contribute meaningfully. Values much lower (k=1) over-weight top-1; much higher (k=200) makes the fusion uniform. Exa production uses `k=60` unchanged from the paper.

**This is a durable primitive.** The constant will not change with model generations.

---

## 6. Context budget discipline (Firecrawl deep research)

**Findings rolling window.** Firecrawl caps findings at 50 items via `slice(-50)` (`FIRECRAWL.md §28`). This is not a quality cutoff — it is a context budget guardrail. New findings evict old ones: LRU-style recency bias.

**Other budget constants (production-verified, `FIRECRAWL.md §28`):**
```
max search queries per depth  = 3  (LLM outputs 3-5, sliced to 3)
search result cache maxAge    = 4 * 60 * 60 * 1000 ms  (4 hours)
max failed LLM attempts       = 3  (before erroring the job)
totalExpectedSteps            = maxDepth * 5            (5 steps per depth)
```

**Stop conditions on main loop:**
```
currentDepth >= maxDepth
OR urlsAnalyzed >= maxUrls
OR timeElapsed >= timeLimit * 1000
OR shouldContinue == false  (planner LLM self-terminates with 1 minute remaining)
```

The `shouldContinue` self-termination is the most important: the planner model sees remaining time and sets the flag — this is a **budget-aware planner gate** not just a loop counter.

---

## 7. Three-tier cache: semantic → S3 Express → S3 Standard (Tavily)

**The mechanism.** Tavily routes incoming queries through three cache tiers before firing a live search (`TAVILY.md §11`):
1. Redis semantic-similarity cache (covers ~1% of keys but ~30% of requests — the long tail of repeated high-frequency queries)
2. S3 Express One Zone (millisecond-latency object store for recent results)
3. S3 Standard (cold storage for results older than the Express window)

**Cost impact.** Tavily claims **95% cost reduction** from this stack. The Redis tier is what makes it 95% not 60% — repeated AI search workloads generate high-frequency identical or near-identical queries (e.g. agents probing the same company across many user sessions).

**Operator knob:** `use_cache: boolean` on the API (server-side parameter). Default appears to be `true` for `/search`, unset for `/research` (where fresh results matter more).

**Durability.** The tier topology (semantic cache → fast object store → cold object store) is a durable pattern for any high-repetition AI retrieval workload. The specific vendors (Redis, S3 Express) are tactical.

---

## 8. Production SSE tool-call hierarchy (Tavily Pro research)

**Tavily Pro model SSE event sequence (`TAVILY.md §21.1`):**
```
Planning                          → emit top-level tool_call
  └─ ResearchSubtopic             → emit subtopic tool_call
       └─ WebSearch (1..N)        → emit child tool_call (parent_tool_call_id links to subtopic)
Reflection                        → emit tool_call
content_chunk                     → streamed text
sources                           → final citation list
done                              → stream end
```

This multi-level hierarchy (Planning → ResearchSubtopic → WebSearch) vs. the mini model's flat Planning → WebSearch is the observable structural difference between tiered model behavior. The `parent_tool_call_id` field links child tool calls to their parent subtopic — a **tool call DAG**, not a flat list.

**Auto-parameters classifier (`TAVILY.md §21.4`):** Tavily inspects the query text and sets `topic` (finance/news/general) and `time_range` automatically — sub-50ms overhead, no user action required. Finance = stock/crypto/earnings signals. News = sports/political/breaking signals. Time range is absent when no temporal signal detected.

---

## 9. Lancelot byte-path implications

| Mechanism | Lancelot impact |
|---|---|
| Distilled-reflection memory | Not a byte-path primitive. Pure context-engineering logic. Lancelot's `lancelot.canonical.dumps_canonical` is a prerequisite (stable bytes for cache-stable compressed notes). |
| Role-tiered models | `TransportPool` already handles multi-provider dispatch. The tier assignment is a **policy** layer on top — a `ModelRouter(tier → provider_profile)` sitting above `TransportPool`. Not transport, not built yet. |
| Highlights (cross-encoder scoring) | Entirely upstream of Lancelot. Lancelot streams highlight responses via `@app.stream` with backpressure. No new primitive needed on the byte path. |
| RRF k=60 | Pure Python retrieval math. Lancelot's `upstream_metrics` measures cost of retrieval calls; the fusion math is user-side. |
| 50-item rolling window / budget gate | Loop state — belongs in the agent loop primitive (`StopReason`, budget tracking). Lancelot byte path has `lancelot.context.cost_usd` ContextVar which accumulates cost — a `budget_reached` check is one comparison away. |
| Three-tier cache | `lancelot.canonical.dumps_canonical` provides the cache key stability needed for Redis semantic lookup. The cache tiers themselves are infra, not framework code. |
| `previousRunId` event-log replay | This is the durable seam / checkpoint primitive Lancelot currently lacks. The mechanism (server-stored event log, continuation run reloads it) maps directly to the event-sourced checkpoint design. |
| Stop-reason taxonomy | `schema_satisfied / budget_reached / error / cancelled` — this is the `StopReason` enum Lancelot's agent loop needs. Exa's names are the right canonical vocabulary. |
| Plan-and-execute (not ReAct) | For retrieval-heavy agents, a planner-emit-tasks-then-parallel-execute pattern fits `TransportPool.fanout` — the planner emits the task list, fanout executes in parallel. The byte-path primitive already exists; the planner harness does not. |

---

## 10. Anti-patterns to avoid

- **Passing raw retrieved content through the agent loop.** Compress first (distilled-reflection). Raw HTML / full-page markdown inside the loop context is quadratic context growth.
- **Single model for all steps.** Using the same model for query generation (should be cheap, runs N times), reasoning (should be capable, runs once), and final synthesis (should be best, runs once) wastes money where it doesn't matter and skimps where it does.
- **Treating stop conditions as loop counters only.** Firecrawl and Exa both have planner-controlled self-termination (`shouldContinue`, `schema_satisfied`) — a loop that only stops at `max_iterations` will keep searching past the point of diminishing returns.
- **Caching full retrieved pages instead of compressed highlights.** Cache the cross-encoder-scored highlight, not the full page. The 500-char span is what the LLM sees; the 8000-char page is what you paid to retrieve. The cache should match the consumption unit.
- **Building a one-size-fits-all retrieval primitive.** Tavily's auto-classifier (finance/news/general) shows that query intent should gate retrieval strategy. Trying to build one retrieval call shape that covers all intents degrades accuracy on each.
