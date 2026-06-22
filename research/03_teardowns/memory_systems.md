# Memory Systems — Mechanism-Level Distillation
## Sources: Mem0, Graphiti/Zep, Supermemory, Cognee

**Sources read in full:**
- `teardowns/MEM0.md` (HEAD `3ac1c94`, `mem0ai/mem0`, verified against arXiv 2504.19413)
- `teardowns/GRAPHITI.md` (v0.29.2, `getzep/graphiti` HEAD `4a742cf`, verified against arXiv 2501.13956)
- `teardowns/SUPERMEMORY.md` (`supermemoryai/supermemory`, npm `supermemory@4.21.1`, April 2026)
- `teardowns/COGNEE.md` (`topoteretes/cognee` HEAD `a22320c`, dev-branch, June 2026)

**Lancelot lens:** Only mechanisms relevant to the agent half (typed memory, temporal state, retrieval ranking, feedback loops). Byte-path primitives Lancelot already owns are noted as such and not proposed to rebuild.

---

## 1. THE CRITICAL CORRECTION — Mem0 ADD-only at HEAD

**Every third-party write-up and the original arXiv paper (2504.19413) describe Mem0's signature mechanism as a two-call LLM arbitration that returns `[{id, text, event: ADD|UPDATE|DELETE|NOOP}]`.** That code path (`get_update_memory_messages`, `DEFAULT_UPDATE_MEMORY_PROMPT`) **is dead code at HEAD `3ac1c94`** — grep over the entire `mem0/` package returns zero live callers (`MEM0.md §0`).

The live default add path (`main.py:725` — "V3 phased batch pipeline") emits **only `event: "ADD"`**. UPDATE/DELETE fire only via the manual public API (`update(memory_id, data)` / `delete(memory_id)`), never from any LLM arbitration.

This is architecturally significant: Mem0 is now a pure append-only store. Its dedup is `md5(text)` exact-match plus the extraction prompt's own "skip if semantically equivalent" instruction. The model does conflict-detection implicitly in the single extraction call; it does not overwrite or delete stale facts. This makes it cheap (one LLM call per add) and lossy on history (the old value is gone from the vector store if you do call `update`; only the SQLite audit row survives).

---

## 2. THE FOUR-TIER DESIGN

The corpus validates a clean four-tier memory architecture, not a single unified store:

| Tier | Shape | Best use | Cost |
|---|---|---|---|
| **T0 — Short-term buffer** | last-10 messages per session scope (SQLite `messages` table, `storage.py:286`) | extraction prompt context; not retrieved, just forwarded | zero (plain table) |
| **T1 — Additive flat facts** | Mem0 V3 ADD-only | high-volume firehose (every message → facts), no conflict logic, `~7k tokens/conversation` (arXiv 2504.19413) | cheapest — 1 LLM call/add |
| **T2 — Bi-temporal fact edges** | Graphiti/Zep `EntityEdge` | facts that change over time; "what was true on date X?" | medium — 1 extract + dedup + invalidation LLM call |
| **T3 — Feedback-weighted graph** | Cognee `feedback_weight` + EMA | facts where user-rated answer quality is the ground truth | medium (add) + async (improve) |

**Rule of thumb for Lancelot:** T1 for firehose/casual observations; T2 for anything with a temporal lifecycle (account owners, contracts, team changes); T3 for facts where the agent's answer quality is directly observable (user preference, process correctness). These are not alternatives — a real system runs all three over one Postgres+pgvector, as all three converge on pgvector HNSW independently (`MEM0.md §6`, `GRAPHITI.md §14.1`, `COGNEE.md §6`).

---

## 3. GRAPHITI — BI-TEMPORAL EDGE SCHEMA (the most durable primitive)

### 3.1 The four-field schema (KNOWN, `edges.py:263-285`)

```python
# EntityEdge — two independent timelines on one fact triple
valid_at:    datetime | None   # T  (event time):  when this fact became true
invalid_at:  datetime | None   # T  (event time):  when this fact stopped being true
created_at:  datetime          # T' (system time): when written to the store
expired_at:  datetime | None   # T' (system time): when superseded in the store
```

A fact is **currently believed and currently true** iff `expired_at IS NULL AND invalid_at IS NULL`. Historical query: filter `expired_at IS NULL` (still believed) and `valid_at <= :t AND (invalid_at IS NULL OR invalid_at > :t)`. This is the capability no last-write-wins store can provide. (`GRAPHITI.md §0`)

### 3.2 The supersession algebra (EXACT, `edge_operations.py:538-573`)

When a new fact supersedes an old one:
```
old.invalid_at = new.valid_at      # event-time end of old = event-time start of new
old.expired_at = utc_now()         # system-time tombstone = now
```

When the new fact is itself born already-invalidated by a *newer* contradicting fact:
```
new.invalid_at = newer_candidate.valid_at
new.expired_at = now
```

Temporally-disjoint facts (old ended before new started, or vice-versa) are **not invalidated** — two facts coexist if their `[valid_at, invalid_at)` ranges do not conflict. This is a history, not a cache. (`GRAPHITI.md §6.4`)

### 3.3 The invalidation LLM call — TWO candidate sets, ONE call

Per new fact:
1. **Duplicate candidates** = same-endpoint existing edges + hybrid search filtered to those endpoints
2. **Invalidation candidates** = hybrid search with NO endpoint filter (semantically related, any endpoints) minus any already in set 1

Both lists are **continuous-indexed** (0..k-1 = duplicates, k.. = invalidation candidates) and sent in one small-model LLM call returning `EdgeDuplicate{duplicate_facts: int[], contradicted_facts: int[]}`. The worked example is the spec: `"Alice works at Acme as a software engineer"` vs `"...as a senior engineer"` → `contradicted_facts=[1]` (same relationship but updated title = contradiction, not duplicate). (`GRAPHITI.md §6.2`)

### 3.4 Entity resolution — cheapest-first, LLM last

`NODE_DEDUP_CANDIDATE_LIMIT=15`, `NODE_DEDUP_COSINE_MIN_SCORE=0.6` for semantic candidates. Then:
1. **Exact normalized-name match** → resolved, no LLM
2. **Entropy gate** (`_NAME_ENTROPY_THRESHOLD=1.5`, `_MIN_NAME_LENGTH=6`, `_MIN_TOKEN_COUNT=2`) — short/low-entropy names can't be fuzzy-matched reliably → LLM
3. **MinHash/LSH** (3-gram shingles, `_MINHASH_PERMUTATIONS=32`, `_MINHASH_BAND_SIZE=4`) → accept if Jaccard `≥ _FUZZY_JACCARD_THRESHOLD=0.9`
4. **LLM fallback** for the residue only

This three-stage design keeps the LLM out of the loop for the majority of entity resolution work. (`GRAPHITI.md §3`)

### 3.5 Temporally-grounded extraction prompt (VERBATIM key rules, `prompts/extract_edges.py`)

```
DATETIME RULES: Use ISO 8601 with 'Z' suffix (UTC).
  If the fact is ongoing (present tense), set valid_at to the timestamp of the episode.
  If a change/termination is expressed, set invalid_at.
  Leave both fields null if no explicit or resolvable time is stated.
  If only a date is mentioned (no time), assume 00:00:00.
  If only a year is mentioned, use January 1st at 00:00:00.
```

When `valid_at`/`invalid_at` come back null, a **separate lightweight small-model call** runs `extract_timestamps` — only for facts where timestamps are null and the episode has a `valid_at`. This two-path design means most facts get timestamps in the main call and only gaps cost a second cheap call. (`GRAPHITI.md §5`)

### 3.6 Constants

`SEMAPHORE_LIMIT=20` (per-episode fan-out), `RELEVANT_SCHEMA_LIMIT=10` (previous episodes in context), `DEFAULT_MIN_SCORE=0.6` (cosine floor), `MAX_SEARCH_DEPTH=3` (BFS hops), `extract_edges max_tokens=16384`, `MAX_SUMMARY_CHARS=1000`, `DEFAULT_TEMPERATURE=0` for all structured extraction. (`GRAPHITI.md §8`, `§15`)

### 3.7 Structured output on Anthropic = forced tool-use

Graphiti's Anthropic path: `_create_tool` builds a tool from `response_model.model_json_schema()` and forces `tool_choice={'type':'tool','name': tool_name}`. This is the exact pattern for any structured-output-requiring LLM call with Claude — the "structured output" is just a forced single-tool Claude call returning the Pydantic schema. (`GRAPHITI.md §9`)

### 3.8 Postgres DDL for the Graphiti port (DESIGNED in teardown, `GRAPHITI.md §14.1`)

```sql
CREATE TABLE fact_edges (
  uuid            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  group_id        text NOT NULL,                        -- tenant/channel partition
  source_node_id  uuid NOT NULL REFERENCES entity_nodes(uuid),
  target_node_id  uuid NOT NULL REFERENCES entity_nodes(uuid),
  name            text NOT NULL,                        -- relation, SCREAMING_SNAKE_CASE
  fact            text NOT NULL,                        -- NL sentence
  fact_embedding  vector(1024),                         -- Voyage voyage-3-large
  episode_ids     uuid[] NOT NULL DEFAULT '{}',         -- provenance
  created_at      timestamptz NOT NULL DEFAULT now(),   -- T' system
  expired_at      timestamptz,                          -- T' system tombstone
  valid_at        timestamptz,                          -- T  event start
  invalid_at      timestamptz,                          -- T  event end
  reference_time  timestamptz,
  attributes      jsonb NOT NULL DEFAULT '{}'
);
-- All four bi-temporal fields are indexed:
CREATE INDEX ON fact_edges USING hnsw (fact_embedding vector_cosine_ops);
CREATE INDEX fact_edges_fts ON fact_edges USING gin (to_tsvector('english', name || ' ' || fact));
CREATE INDEX ON fact_edges (group_id, valid_at, invalid_at, expired_at);
CREATE INDEX ON fact_edges (source_node_id);
CREATE INDEX ON fact_edges (target_node_id);
```

**Ingest must be serialized within a `group_id`** (one episode at a time) to preserve bi-temporal correctness — the supersession algebra races if two contradicting episodes are processed in parallel. Parallelize across `group_id`s freely. (`GRAPHITI.md §18`)

### 3.9 Cross-encoder trick worth stealing

Graphiti's `OpenAIRerankerClient` (for cross-encoder reranking) uses a boolean-logprobs trick: model `gpt-4.1-nano`, prompt "Respond with 'True' if PASSAGE is relevant to QUERY and 'False' otherwise", `temperature=0, max_tokens=1, logit_bias={'6432':1,'7983':1}` (True/False token ids), `logprobs=True, top_logprobs=2`; score = `exp(logprob)` of the True token. This is cheaper than running a real cross-encoder but produces a calibrated 0-1 relevance score. (`GRAPHITI.md §8`) **Note:** this is a depreciating tactical hack tied to specific token IDs in current model vocabularies — use Voyage `rerank-2.5` instead (a real cross-encoder, cheaper, and not vocabulary-dependent).

---

## 4. MEM0 — ADDITIVE EXTRACTION + HYBRID SCORING

### 4.1 The extraction prompt contract (KNOWN, `prompts.py:468-942`)

Key rules from `ADDITIVE_EXTRACTION_PROMPT`:
- **Temporal grounding rule**: resolve "yesterday/last week/recently" against `Observation Date`, NOT `Current Date`. "User went to Paris last week" → "the week of May 15, 2023." (`prompts.py:528-540`) — This is the mechanism that keeps time-relative facts meaningful forever.
- **"Contextually Rich, Not Atomic"**: capture transitions, not just states. "switched from almond to oat milk after an almond sensitivity" not just "prefers oat milk." (`prompts.py:606-621`)
- **"Preserve Specific Details — Never Generalize"**: keep proper nouns/quantities verbatim. (`prompts.py:640-666`)
- Output contract: `{"memory":[{"id":"0","text":"…","attributed_to":"user","linked_memory_ids":["uuid…"]}]}` — sequential string ids only, never real UUIDs.

### 4.2 Anti-hallucination ID mapping (CRITICAL mechanism, `main.py:743-747`)

Existing memories are presented to the LLM with **integer string ids "0","1",…** mapped from real UUIDs. The LLM can only echo back integer ids; the caller translates integer → real UUID before touching the store. The prompt enforces "Return IDs from the input IDs only; do not generate any new ID." This stops the model inventing a UUID and corrupting an unrelated row. **This must be replicated in any memory system that lets the LLM reference existing records.**

### 4.3 Hybrid scoring math (KNOWN, `scoring.py` + `main.py:1373`)

```python
# over-fetch: internal_limit = max(limit*4, 60)
# semantic: 1 - cosine_distance, threshold = 0.1 (gates before combining)
# bm25: sigmoid-normalized by lemma count: ≤3 → (mid=5.0,steep=0.7), ≤6 → (7.0,0.6), ≤9 → (9.0,0.5), ≤15 → (10.0,0.5), else → (12.0,0.5)
#   normalize_bm25 = 1/(1+exp(-steep*(raw-mid)))
# entity boost: similarity >= 0.5, ENTITY_BOOST_WEIGHT=0.5, memory_count_weight=1/(1+0.001*(num_linked-1)^2)
#   (entity linked to many memories is a weaker signal)
# combined = min((semantic + bm25 + entity_boost) / max_possible, 1.0)
#   max_possible = 1.0 (+1.0 if any bm25) (+0.5 if any entity)
```

The semantic `threshold=0.1` gates the **semantic score before combining** — BM25 or entity boost cannot rescue a below-threshold candidate. (`MEM0.md §4`)

### 4.4 The legacy two-call arbitration (still in file, dead at HEAD — useful for volatile facts)

`DEFAULT_UPDATE_MEMORY_PROMPT` (`prompts.py:176`) with `{"memory":[{"id","text","event":"ADD|UPDATE|DELETE|NONE","old_memory"}]}` is the classic design. Still the right choice for facts that *change* — it actively supersedes stale values. The V3 add-only path never does this. For a Lancelot memory tier, use the two-call path for volatile single-valued state ("current account owner") and the V3 additive path for high-volume append-y signal. (`MEM0.md §10.2-10.3`)

### 4.5 Constants

| Constant | Value | Source |
|---|---|---|
| Add-time existing-memory retrieval top_k | 10 | `main.py:738` |
| Search over-fetch pool | `max(limit*4, 60)` | `main.py:1386` |
| Default search top_k | 20 | `main.py:1156` |
| Semantic threshold | 0.1 (on `1-cos_dist`) | `main.py:1158` |
| Entity link similarity | ≥ 0.95 | `main.py:944` |
| Entity boost min similarity | ≥ 0.5 | `main.py:1533` |
| ENTITY_BOOST_WEIGHT | 0.5 | `scoring.py:57` |
| Recent-message buffer | 10 | `storage.py:286` |
| Last-k truncation | 300 chars | `prompts.py:965` |
| Message dedup | md5(text) | `main.py:825` |
| LLM temperature | 0.1 | `configs/llms/base.py:19` |
| LLM max_tokens | 2000 | `configs/llms/base.py:20` |

---

## 5. COGNEE — FEEDBACK EMA (the unique steal)

### 5.1 The complete feedback loop (KNOWN end-to-end)

1. `GraphCompletionRetriever.get_completion_from_context` stores `used_graph_element_ids = {node_ids:[...], edge_ids:[...]}` on the session QA entry at retrieval time (`graph_completion_retriever.py:333-343`).
2. User rates the answer 1..5 → stored as `feedback_score` on the QA entry.
3. `improve(session_ids=[...], feedback_alpha=0.1)` reads eligible QAs and runs EMA reweighting.
4. Next recall blends `feedback_weight` into the triplet score → higher-rated elements rank higher.

**Idempotency:** `memify_metadata["feedback_weights_applied"]` — each QA applied at most once. (`COGNEE.md §4.2`)

### 5.2 The EMA formula (VERBATIM, `apply_feedback_weights.py:43-59`)

```python
# Rating normalization: map 1..5 → [0, 1]
normalized_rating = (feedback_score - 1) / 4       # 1→0.0, 3→0.5, 5→1.0

# Streaming EMA update
updated     = previous_weight + alpha * (normalized_rating - previous_weight)
final_score = max(0.0, min(1.0, updated))           # clip
return round(final_score, 4)                        # FEEDBACK_WEIGHT_DECIMALS = 4

# Default: alpha = 0.1, initial weight = 0.5
# 5-star from 0.5: 0.5 + 0.1*(1.0-0.5) = 0.55
# 1-star from 0.55: 0.55 + 0.1*(0.0-0.55) = 0.495
# No single rating swings it hard — that's the point.
```

### 5.3 Recall scoring with feedback (VERBATIM, `CogneeGraph.py:480-539`)

```python
# Base distance per graph element:
distance = (2 - importance_weight) * cosine_distance    # importance_weight default 0.5

# Blend with feedback (only if feedback_influence > 0):
normalized_distance = distance / 2.0                    # cosine distance ∈ [0,2] → [0,1]
blended = (1 - fi) * normalized_distance + fi * (1 - feedback_weight)
effective_distance = blended * 2.0

# triplet_score = effective_d(node1) + effective_d(node2) + effective_d(edge)
# keep k SMALLEST scores (heapq.nsmallest)
# feedback_influence default = 0.0 — feedback is OFF until opted in
```

**`feedback_influence=0.0` is the default** — feedback reweighting must be explicitly opted in by the caller (`get_search_type_retriever_instance.py:67`). The weight is stored on every node and edge from day one (default `0.5`), but it has no effect until `feedback_influence > 0`. (`COGNEE.md §4.5`)

### 5.4 Where `feedback_weight` lives

On the base `DataPoint` model: `feedback_weight: float = 0.5` (`DataPoint.py:62`). Read from graph via `coalesce(n.feedback_weight, 0.5)` — missing = 0.5 is the clean default. Written back by `set_node_feedback_weights`/`set_edge_feedback_weights` in the Neo4j adapter (`adapter.py:885-920`). The abstract interface (`graph_db_interface.py:341-371`) requires all four methods (read/write for nodes and edges). (`COGNEE.md §4.4`)

### 5.5 KG-extraction prompt (VERBATIM key rules, `generate_graph_prompt.txt:1-34`)

```
Every edge should include a description when the text supports relevant information
about the endpoints. The description must use the endpoint names, stay dry and efficient,
and may include useful qualifiers from the source text. Do not add outside knowledge.
  Good: Alice works at Acme as a platform engineer on the search team.
  Bad: This edge describes an employment relationship.

Consistency: Ensure you use basic or elementary types for node labels.
  - Always label a person as "Person". Avoid "Mathematician"/"Scientist".
  - Don't use too generic terms like "Entity".
  - Node IDs: Never use integers. Use names or human-readable identifiers.
```

Response schema: `KnowledgeGraph{nodes: list[Node{id,name,type,description}], edges: list[Edge{source_node_id,target_node_id,relationship_name,description}]}` via Instructor structured output. Edges with a source/target not in the node set are dropped (hallucination guard, `extract_graph_from_data.py:181-188`). (`COGNEE.md §3.2`)

### 5.6 Node summary quality rules (VERBATIM, `prompts/snippets.py:summary_instructions`)

These 10 rules are the actionable load-bearing ones — applicable to any memory summarization, not just Cognee:
> "1. Output only factual content. Never explain what you're doing. 3. STATE FACTS DIRECTLY IN UNDER 1000 CHARACTERS. 4. Preserve all materially relevant names, roles, places, dates, counts, and temporal qualifiers. 6. When the durable fact is the content of what was said, state the content directly instead of narrating that it was said. 8. Never use filler verbs like 'mentioned', 'described', 'stated', 'reported', 'noted', 'discussed', 'referenced', or 'indicated' unless the communication act itself is the fact. 10. Begin with the entity name or a direct fact, not with 'A', 'An', 'The', or 'This is'."

BAD: "The context shows John ordered pizza. Due to length constraints, other details are omitted."
GOOD: "John ordered pepperoni pizza from Mario's at 7:30 PM and had it delivered to the office."

(`GRAPHITI.md §16` — same rules appear in Graphiti's `summarize_nodes.py`)

### 5.7 Cost-saving shortcut for node summaries

Graphiti's **edge-fact-append-before-LLM shortcut** (`node_operations.py:863-885`): if a node already has a short summary, concatenate its current edge facts. If `len(summary_with_edges) <= MAX_SUMMARY_CHARS * 2` (≤ 2000 chars), use the concatenation **without an LLM call**. Only when the combined text overflows does it compress via LLM. This is a direct token saver for any memory system that maintains entity summaries. (`GRAPHITI.md §16`)

---

## 6. SUPERMEMORY — WHAT IS WORTH STEALING (AND WHAT IS NOT)

Supermemory's production backend is closed-source. The tear-down is based on SDK types, schemas, incident reports, and middleware source. What is confirmed from open source:

### 6.1 Three relation types (KNOWN, `schemas.ts`)

```typescript
MemoryRelationEnum:
  "updates"  — new info supersedes old; old.isLatest = false; creates version chain
  "extends"  — enriching info, no contradiction; additive
  "derives"  — second-order inference from combining multiple memories; isInference = true
```

Every memory carries: `version` (int), `isLatest` (bool), `parentMemoryId`, `rootMemoryId`. The "updates" chain is the analog of Graphiti's supersession but without the bi-temporal timestamps — it's version-chain-with-pointer, not event-time-stamped. (`SUPERMEMORY.md §3`, `§16`)

### 6.2 Three embedding slots (KNOWN, `schemas.ts`)

```
embedding / embeddingModel        -- primary
embeddingNew / embeddingNewModel  -- secondary/migration
matryokshaEmbedding / matryokshaEmbeddingModel  -- Matryoshka variable-dimension
```

Plus `summaryEmbedding` at document level and `memoryEmbedding` at memory entry level. **This is the zero-downtime embedding migration infrastructure**: both old and new embeddings live in the same row during migration. For Lancelot's memory tier, two embedding columns per entity (current + new) is the right pattern for model transitions.

### 6.3 Dual-layer temporal grounding (KNOWN, from `updateMemory.temporalContext`)

```typescript
temporalContext: {
  documentDate: string   // when the conversation happened
  eventDate: string[]    // when the described events occurred
}
```

Two separate timestamps: system time of ingestion vs. event time of described events. This is the same bi-temporal insight as Graphiti (`valid_at` = event time, `created_at` = system time) but applied at the memory-entry level rather than the edge level. (`SUPERMEMORY.md §15`)

### 6.4 Context compaction at 80% — the critical threshold (`SUPERMEMORY.md §13.2`)

OpenCode plugin triggers structured summarization at **80% context usage**. The summarization prompt requires exactly five components:
1. User Requests (as-is, verbatim)
2. Final Goal
3. Work Completed
4. Remaining Tasks
5. **MUST NOT Do (Critical Constraints)** — the constraint preservation is the non-obvious one

At 80% (not 100%) is the right trigger — waiting for 100% leaves no room to build the summary itself. The constraint-preservation item is load-bearing: it prevents the agent from re-discovering forbidden paths after compaction.

### 6.5 Signal-keyword extraction for coding agents (`SUPERMEMORY.md §13.1`)

Claude Code plugin `signalExtraction` mode: only captures turns containing keywords ("remember", "architecture", "decision", "bug", "fix", "solved", "pattern"). Captures N turns before signal (default 3) for context. This is a cheap filter that multiplies memory S/N ratio without LLM cost — apply before any extraction call.

### 6.6 What is NOT worth stealing from Supermemory

- The core memory processing pipeline is closed-source — the `MemoryEntry` schema and relation types are readable, but the actual extraction/indexing pipeline is not.
- The ASMR system (98.6% accuracy with 6 reader agents + 3 search agents + 8-12 answering agents per query) is explicitly "a big social experiment" / "parody" per the founder (`SUPERMEMORY.md §14`). Not production-viable compute budget.
- Three embedding slots is a migration pattern, not a retrieval innovation.
- The Cloudflare Workers architecture is infra-specific to their deployment; not applicable to Lancelot.

---

## 7. GRAPH-VS-VECTOR DECISION

The corpus converges on a consistent answer:

**Use vector-only (Mem0 V3 additive) when:** fact shape is flat, facts don't change (or you don't need history), volume is high, and cost per add must be minimal. The Mem0 V3 add path benchmarks at ~7k tokens/conversation and 91% lower p95 latency vs full-context (arXiv 2504.19413).

**Use graph (Graphiti/Cognee) when:** facts have relationships (entity A EMPLOYS entity B), facts change over time (and you need "as of when" queries), or you need multi-hop traversal ("who at Acme knows Alice"). The graph adds cost (~3-4 LLM calls per ingest vs. 1) but enables the invalidation/supersession logic and BFS-neighbor retrieval that vector-only cannot provide.

**The hybrid sweet spot:** vector seed (wide top-k = 100) → graph neighborhood projection → triplet scoring. Both Mem0's entity-boost and Cognee's `brute_force_triplet_search` use this pattern: start wide in the vector index, narrow via graph structure. (`MEM0.md §4`, `COGNEE.md §5.3`)

**Lancelot's byte path note:** `lancelot.context.agent_id` and `tenant_id` are already the scoping keys that map to `user_id`/`group_id` in all four systems. `lancelot.canonical.dumps_canonical` already provides the stable serialization needed for `md5(text)` dedup and cache-stable embedding inputs. These are not memory primitives — they are the plumbing the memory layer rides on.

---

## 8. ANTI-PATTERNS

1. **Claiming Mem0's LLM arbitration (ADD/UPDATE/DELETE) is the default** — it is dead code at HEAD. The live path is ADD-only. (Source: `MEM0.md §0`, verified by grep)
2. **Using last-write-wins for facts with a temporal lifecycle** — any system where "Dana now leads Acme" silently overwrites the old fact without `old.invalid_at = new.valid_at` loses the history. Cannot answer "who led Acme in Q1." (Source: `GRAPHITI.md §0`)
3. **Running `feedback_influence > 0` without recorded provenance** — Cognee's loop is inert if `used_graph_element_ids` is not stored on the QA. Provenance must be captured at retrieval time, not at improve time. (`COGNEE.md §4.1`)
4. **Embedding the ID map as real UUIDs** — letting the LLM emit real UUIDs during any add/update/delete operation is a hallucination injection vector. The integer-string id map (`"0","1",…` → real UUIDs in the caller) is mandatory. (`MEM0.md §2.3`)
5. **Serializing episodes globally for bi-temporal correctness** — Graphiti's async worker is intentionally serialized (one episode at a time per `group_id`) to prevent the supersession algebra from racing. Parallelizing ingest within a group breaks invalidation. (`GRAPHITI.md §18`)
6. **Taking ASMR (98.6% benchmark) as a production target** — it is explicitly a research demonstration ("big social experiment"/"parody"). The actual production Supermemory system achieves 85.2%. (`SUPERMEMORY.md §14`)
7. **Rebuilding what Lancelot already has** — `lancelot.batch` is a 12× throughput / 16× fewer upstream calls coalescer for rate-limited LLM backends (exactly the embedding/extraction bottleneck). `TransportPool.fanout` is the fan-out primitive for parallel embedding calls. `lancelot.canonical.dumps_canonical` is the dedup hash input. These are not memory concerns.

---

## 9. KEY CONSTANTS CONSOLIDATED

| Constant | Value | Source |
|---|---|---|
| Graphiti semaphore (per-episode fan-out) | 20 | `helpers.py:38` |
| Graphiti BFS max depth | 3 | `search_utils.py:68` |
| Graphiti cosine floor | 0.6 | `search_utils.py:66` |
| Graphiti MMR lambda default | 0.5 | `search_utils.py:67` |
| Graphiti entity dedup cosine min | 0.6 | `node_operations.py:65` |
| Graphiti entity dedup candidate limit | 15 | `node_operations.py:64` |
| Graphiti LSH Jaccard threshold | 0.9 | `dedup_helpers.py:271` |
| Graphiti MinHash permutations / band size | 32 / 4 | `dedup_helpers.py:97,117` |
| Graphiti name entropy threshold | 1.5 | `dedup_helpers.py:83` |
| Graphiti extract_edges max_tokens | 16384 | `edge_operations.py:141` |
| Graphiti summary max chars | 1000 | `node_operations.py` |
| Graphiti LLM temperature | 0 | `llm_client/client.py:34` |
| Mem0 add retrieval top_k | 10 | `main.py:738` |
| Mem0 search over-fetch | max(limit*4, 60) | `main.py:1386` |
| Mem0 semantic threshold | 0.1 (1-cos_dist) | `main.py:1158` |
| Mem0 entity link similarity | ≥ 0.95 | `main.py:944` |
| Mem0 entity boost weight | 0.5 | `scoring.py:57` |
| Mem0 message buffer | 10 | `storage.py:286` |
| Cognee feedback alpha | 0.1 | `improve.py:130` |
| Cognee feedback weight default | 0.5 | `DataPoint.py:62` |
| Cognee recall wide_search_top_k | 100 | `get_search_type...:65` |
| Cognee recall top_k | 10 | `get_search_type...:59` |
| Cognee triplet distance penalty | 6.5 | `CogneeGraph.py:38` |
| Cognee feedback influence default | **0.0** (opt-in) | `get_search_type...:67` |
| Supermemory context compaction trigger | 80% | OpenCode plugin |
| Supermemory search default similarity cutoff | 0.6 | `ai-sdk.ts` (`chunkThreshold: 0.6`) |
