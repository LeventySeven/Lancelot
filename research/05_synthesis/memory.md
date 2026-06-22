# Lancelot Memory Layer — Synthesis Brief

**Layer:** `lancelot.agent.memory`
**Altitude:** "just build agents, brutal." The smallest memory primitive that earns its keep — anti-overkill (no graph DB, no 4 LLM-calls-per-ingest, no bi-temporal supersession by default) AND anti-underkill (typed tiers + temporal grounding + bounded-by-default, because last-write-wins + flat append both lose).
**Cardinal rule:** runtime dep is `pydantic>=2.7` only. `sqlite-vec` and provider embedding SDKs are **lazy-imported**, exactly like `lancelot.secrets`' Vault/Infisical/boto3 backends (`lancelot_serve_tools.md §7`).

---

## 0. The one-paragraph decision

Build **one `Memory` object** with a **4-tier typed store** (scratchpad / episodic / fact / procedural), each tier with its own TTL and its own write policy, fronted by **memory-as-tools** (`remember` / `recall` / `recall_as_of`) that the agent calls explicitly. Default storage is a **single SQLite file** with the `sqlite-vec` extension lazy-loaded; the store sits behind a **`MemoryBackend` Protocol** so Postgres+pgvector / Turbopuffer / Zep / in-RAM all satisfy the same 4 methods. Retrieval is **composite-scored** (vector + recency-decay + importance) for the cheap default, with **RRF k=60** as the opt-in path when a caller already has two ranked lists. Temporal facts are **ADD-only with `valid_at`/`invalid_at` (never hard-delete)**; full bi-temporal supersession (Graphiti's algebra) is a documented opt-in, NOT the default. A **virtual-context page-table** string (Letta's `<memory_metadata>`) is injected so the model knows what it can't see. Consolidation is a **background hook** (Letta sleep-time / Codex 2-phase), frequency-gated, routed to a cheap model. Everything rides the byte path Lancelot already owns: `dumps_canonical` for dedup-hash + cache-stable embedding input, `TransportPool.fanout` for parallel embed calls, `@app.batch` for embed/extract coalescing, `cost_usd` ContextVar for automatic memory-call cost attribution, `tenant_id`/`agent_id` as the scope keys.

**Why this and not more:** the strongest convergent finding in the corpus is that LangChain's *memory-class proliferation was the anti-pattern itself* — 9 memory classes + 30 backends, all deprecated; "the right abstraction was always messages-list + compaction-hook; LangChain discovered this four years in" (`langchain.md:115-126`). The strongest finding *for* typing is that flat-append and last-write-wins each lose: Mem0 went pure ADD-only and is "lossy on history" (`memory_systems.md:20`); last-write-wins "cannot answer who led Acme in Q1" (`memory_systems.md:355`). So: typed tiers (not one blob, not nine classes) + ADD-only-with-tombstone (not delete, not overwrite). That is the earn-its-keep middle.

---

## 1. MUST BUILD

### 1.1 The 4-tier typed store (the spine)

The corpus converges on **exactly four tiers** from five independent sources — this is the single most cross-validated memory primitive in the harvest:

| Tier | Cross-source name | Shape | TTL default | Write policy |
|---|---|---|---|---|
| **scratchpad** | T0 short-term (`memory_systems.md:30`); "Short-term / in-context" (`transcripts_dlai.md:218`); Nia Tier-1 working memory (`nia.md:91`) | per-conversation `{action, args, reason, result_summary}` tuples + recent messages | **job/session lifetime** (Nia: "TTL = job lifetime", `nia.md:91`) | append, no LLM |
| **episodic** | "Long-term episodic / action-sequence log" (`transcripts_dlai.md:220`) | `{task_name, task_description, outcome, embedding, created_at}` | **90 days** | ADD-only |
| **fact** | "Long-term semantic" (`transcripts_dlai.md:219`); Mem0 facts (`memory_systems.md:31`) | `{content, importance, embedding, tags, valid_at, invalid_at, created_at}` | **none (durable)** | ADD-only + tombstone |
| **procedural** | "Long-term procedural / system-prompt store" (`transcripts_dlai.md:221`); Parahelp policy files (`transcripts_yc_root.md:165`) | `{content, version, update_history[]}` | **none (durable)** | versioned overwrite (human-editable) |

The MongoDB taxonomy (`transcripts_dlai.md:212-241`) is the cleanest statement and gives the exact schema keys above. Letta's tiers (core/recall/archival, `letta.md:12-18`) and Nia's 4-tier-with-lineage (`nia.md:85-149`) cross-validate it. `arch_catalog.md:315-326` independently lists the same four ("Auto/Session/Auto Dream/...").

**The brutal cut:** these are **rows in one table discriminated by a `tier` column**, not four subsystems. One SQLite file, one `memories` table, `tier` enum column. Tier dictates TTL + write policy + whether it's in-prompt (scratchpad/procedural) or retrieved (episodic/fact). This is the anti-LangChain stance: typed *data*, not a typed *class hierarchy*.

### 1.2 Memory-as-tools — `remember` / `recall` / `recall_as_of` (ADD-only)

Every production system exposes memory to the agent **as tool calls it makes explicitly**, never as silent magic: Letta ("every write is the agent's explicit choice — via tool call. Nothing is automatic", `letta.md:21`); Agno's MemoryManager calls `add/update/delete_memory` as tools (`agno.md:95`); Anthropic's `memory_20250818` tool with 6 ops (`agentic_deep.md:259`); Mastra's auto-registered `updateWorkingMemory` tool (`mastra.md:201`).

Lancelot already has the tool substrate — `ToolSpec` + `app.tool_schemas()` + `app.call_tool()` with Pydantic validation on input/output (`lancelot_serve_tools.md §2`). The memory layer registers three tools on top:

- `remember(content, tier="fact", tags=[], importance=None)` → ADD. Default ADD-only (Mem0 V3 live path is ADD-only and that's the cheap-and-correct default, `memory_systems.md:16-20`).
- `recall(query, tier=None, k=10)` → composite-scored retrieval (§1.4).
- `recall_as_of(query, at: datetime, ...)` → temporal query over `valid_at`/`invalid_at` (§1.5). This is the capability "no last-write-wins store can provide" (`memory_systems.md:51`).

**Two mandatory safety mechanisms copied verbatim:**

1. **Integer-id mapping, never real UUIDs to the model.** When existing memories are shown to the LLM for reference, present them with integer string ids `"0","1",…` mapped from real UUIDs in the caller; the model can only echo integers. "This stops the model inventing a UUID and corrupting an unrelated row… mandatory" (`memory_systems.md:155, 357`).
2. **`str_replace`-exact on procedural edits.** A procedural-block edit must raise if `old_content not in current_value` — "no silent overwrite… the key safety property" (`letta.md:54`, copied from Anthropic computer-use `edit.py:154`). Mastra learned the same: full-document rewrites "caused the LLM to alter fields that should be unchanged… overwrite only that field" (`transcripts_yc_root.md:100`).

### 1.3 Pluggable `MemoryBackend` Protocol (sqlite-vec dev default)

A `typing.Protocol` with the minimal method set every backend in the corpus implements — Zep's `add`/`search` (`memory_vector.md:194-222`), Cognee's `graph_db_interface` 4 methods (`memory_systems.md:243`), MiroFish proving the interface is backend-swappable (Zep→Neo4j, `memory_vector.md:241-258`):

```python
class MemoryBackend(Protocol):
    def add(self, rec: MemoryRecord) -> str: ...          # returns id
    def search(self, q: Query) -> list[Scored]: ...        # hybrid, backend-implemented
    def get_as_of(self, q: Query, at: datetime) -> list[Scored]: ...
    def tombstone(self, id: str, invalid_at: datetime) -> None: ...
```

**Default `SqliteVecBackend`:** one file, `sqlite-vec` extension **lazy-loaded on first `add`** (cardinal rule — exactly the `lancelot.secrets` lazy-SDK pattern, `lancelot_serve_tools.md §7`). Cited as the dev-tier choice in the task brief; OpenAI Agents ships a SQLite session store as its default (`openai_agents.md:170`), Codex stores memory in `memories_1.sqlite` (`codex.md:298`) — SQLite-as-default-memory is the validated production-startup choice. Postgres+pgvector / Turbopuffer / Zep are user-supplied backends, NOT a Lancelot dependency. **Do NOT mandate Postgres** — that is Letta's explicit anti-pattern ("Mandatory PostgreSQL + pgvector… overkill", `letta.md:251`).

### 1.4 Composite retrieval scoring (the cheap default) + RRF k=60 (opt-in)

**Default = composite score, single-stage, no second LLM call:**
```python
composite = 0.5*vector_similarity + 0.3*exp(-age_days/30) + 0.2*importance
```
Verbatim from `transcripts_dlai.md:223-231`. The load-bearing discipline: **importance is LLM-generated at *creation* time and stored as metadata** — "Pre-compute importance at creation time, not at retrieval time (avoids per-document LLM call on every retrieval)" (`transcripts_dlai.md:231`). Recency-decay default 30 days. This gives recency + salience + similarity in pure float math, zero retrieval-time LLM cost.

**Opt-in = RRF k=60** when the caller already has two ranked lists (vector + BM25). The single most durable retrieval constant in the entire corpus — confirmed as the *actual server default* (not just docs) by three independent sources: LanceDB `impl Default { k: 60.0 }` (`memory_vector.md:25-28`), turbopuffer arithmetic proof `2/61` (`memory_vector.md:31`), Exa production unchanged (`retrieval_agents.md:116-120`), LangChain `c=60` (`langchain.md:299`), Zep default reranker (`memory_vector.md:40`). The implementation is "5-line, copy-and-paste durable" (`memory_vector.md:42`). Ship it as a **utility** `lancelot.agent.memory.rrf(lists, k=60)`, not as the default scorer — because the default `SqliteVecBackend` returns one fused list and doesn't need it.

**Asymmetric embedding is first-class.** `compute_source_embeddings` (write) ≠ `compute_query_embeddings` (read) — E5/BGE/Voyage need different prompts (`"passage:"` vs `"query:"`, `input_type:"document"` vs `"query"`). "A 5-line distinction that most implementations get wrong" (`memory_vector.md:83-105`); the MiroFish offline fork got it wrong (`memory_vector.md:258`). The `EmbeddingDefinition` carries both fns; the right one fires automatically at read/write. `dumps_canonical` provides the cache-stable, dedup-stable bytes for the embedding *input* (`memory_systems.md:348`).

### 1.5 Temporal facts: ADD-only + tombstone (never hard-delete); bi-temporal opt-in

Default fact-tier policy: **append a new fact, set `old.invalid_at = new.valid_at` on the superseded one, never `DELETE`.** This is the unanimous corpus position: Graphiti "a fact is never deleted… history not a cache" (`memory_systems.md:51, 67`); Zep "old edge gets `invalid_at` set — never deleted" (`memory_vector.md:224`); Supermemory version-chain `isLatest=false` (`memory_systems.md:288`). The dual timestamp — `valid_at` (event time) vs `created_at` (system time) — is the bi-temporal insight (`memory_systems.md:43-49`, `memory_vector.md:303-312`).

**Temporal grounding at extraction (verbatim rule to copy):** resolve "yesterday/last week" against the **observation date, not the current date** — "User went to Paris last week → the week of May 15, 2023… keeps time-relative facts meaningful forever" (`memory_systems.md:148`); same ISO-8601 rules in Graphiti's `extract_edges` prompt (`memory_systems.md:90-96`).

**What is OPT-IN, not default (the anti-overkill line):** Graphiti's full supersession *algebra* — the two-candidate-set invalidation LLM call (`memory_systems.md:69-75`), entity-resolution MinHash/LSH cascade (`memory_systems.md:79-85`), serialized-per-`group_id` ingest (`memory_systems.md:135`). That is ~3-4 LLM calls per ingest vs 1 (`memory_systems.md:344`). Worth it for "account owners, contracts, team changes" (`memory_systems.md:36`); overkill for a casual chatbot. Ship the **schema** (the four bi-temporal columns are always present) but gate the **LLM arbitration** behind `Memory(temporal_arbitration=True)`. The cheap default just stamps `valid_at` and tombstones on explicit supersede.

### 1.6 Virtual-context page-table (the "what you can't see" inject)

Letta's `<memory_metadata>` block, rebuilt each turn, telling the model how much context lives *outside* its window — "N previous messages stored in recall, M memories in archival, available tags: […]… it is the literal page-table" (`letta.md:31-43`). Trivial to build: a `Memory.page_table() -> str` that str-replaces a `{MEMORY_METADATA}` placeholder before the call. Hermes injects recalled memory with an explicit `<memory-context>[System note: recalled memory, NOT new user input]` framing (`hermes_core.md:129-134`) — copy that anti-injection wrapper.

**Memory-freeze invariant (cache-preservation, copy verbatim).** Hermes' most important memory primitive: writes hit disk immediately, but **the model's *view* of memory stays frozen until the next session** — "this is what preserves the Anthropic prefix cache; system-prompt bytes identical across every turn" (`hermes_core.md:110-119`). And the corollary discipline: **dynamic recall goes in the *user* turn, not the system prompt** (`hermes_core.md:174`). This is the bridge to Lancelot's existing cache thesis (`dumps_canonical` byte-stable prefix → >80% cache hit) — `dumps_canonical` stabilizes the snapshot serialization (`hermes_core.md:149`).

### 1.7 Bounded-by-default everywhere

Every tier has a hard cap; this is non-negotiable per the corpus and Lancelot's cardinal "bounded" rule:
- Procedural / index file: **≤200 lines / ~25KB** — the single most repeated constant: v0 `MEMORY.md` truncated ~200 lines (`vercel_ai_sdk.md:296, 354`), Claude Code autoDream "keep MEMORY.md under 200 lines / ~25KB" (`arch_catalog.md:339`), Hermes `MEMORY.md=2200 chars` (`hermes_core.md:123`). "The 200-line truncation is the key constraint — keeps it always-loaded but forces hierarchical organization" (`vercel_ai_sdk.md:301`).
- Scratchpad: last-**10** messages (Mem0 `storage.py:286`, Mastra `lastMessages` default 10 — `memory_systems.md:185`, `mastra.md:175`).
- Recall `k`: default **10** (Mem0 add-retrieval, Zep search, Cognee — `memory_systems.md:380`, `memory_vector.md:217`); Mastra semantic recall `topK=4` with **±1 message-range expansion** (`grep -C 1` around each hit — `mastra.md:177-178`, validated independently `transcripts_yc_root.md:106`).
- Result-summary on wire: truncate ~**250 chars**, full content stays in the iteration's private context (Nia, `nia.md:93`).
- Extraction selectivity: "Only extract information useful in *future* conversations. Do NOT extract every fact… over-extraction pollutes retrieval precision" (`transcripts_dlai.md:233-235`).

### 1.8 Consolidation hook (background, frequency-gated, cheap model)

Not a runtime primitive — a **background task fired after the response**, frequency-gated, tools restricted to memory-only, routed to a **cheaper model**. Three independent implementations agree:
- Letta sleep-time agents: fire every `sleeptime_agent_frequency` turns, `asyncio.create_task` post-response, restricted to `[memory_replace, memory_insert, memory_rethink, memory_finish_edits]` (`letta.md:121-132`).
- Codex 2-phase: cheap extraction model (Phase 1) → medium consolidation sub-agent rewrites `MEMORY.md` (Phase 2), **rate-gated `≥25% quota remaining`**, runs **only if git-tracked changes** (`codex.md:287-311, 419`).
- Claude Code autoDream three-gate: idle + change + file-lock; "merge overlapping, convert relative dates to absolute, delete contradicted facts… 913 sessions consolidated in ~9 min" (`arch_catalog.md:328-341`).

Lancelot wiring: `@app.lifetime_task` for the supervised daemon (`lancelot_serve_tools.md §11`) OR `asyncio.create_task` post-stream; the consolidation LLM call routes through `TransportPool` to a cheap model — cost auto-accrues to `cost_usd` (`agno.md:132`). Copy the two production guards verbatim: **rate-gate (don't burn quota when tight)** and **only-if-changed (don't thrash on every session)**.

The `memory_rethink` active-consolidation tool — "rewrite the entire block integrating new information, organized/readable/comprehensive" (`letta.md:52`) — and the 10 summary-quality rules (state facts directly, never "mentioned/described/noted", <1000 chars, begin with the entity name — `memory_systems.md:262-268`) are the consolidation prompt contract.

---

## 2. REUSE from the byte path (do NOT rebuild)

| Need | Byte-path primitive | Cite |
|---|---|---|
| Dedup hash input + cache-stable embedding input | `lancelot.canonical.dumps_canonical` (`md5(text)` dedup is Mem0's mechanism) | `memory_systems.md:348`, `lancelot_model_io.md §4` |
| Parallel embedding / parallel ANN+BM25 calls | `TransportPool.fanout(first_n, deadline, cancel_laggards, concurrency)` — use instead of a `ThreadPoolExecutor` (no GIL re-acquire for I/O) | `agno.md:87, 395`, `lancelot_model_io.md §1.7` |
| Coalesce high-volume embed/extract calls to a rate-limited backend | `@app.batch(size_fn, max_size)` — token-budget flush; ~12×/~16× | `lancelot_model_io.md §5`, `lancelot_serve_tools.md §11` |
| Memory-call cost attribution | `cost_usd` ContextVar + `upstream_metrics` (background MemoryManager cost accrues automatically if dispatched via TransportPool) | `agno.md:132`, `lancelot_model_io.md §2.4` |
| Scope keys (= `user_id`/`group_id` in all 4 systems) | `lancelot.context.agent_id` + `tenant_id` | `memory_systems.md:348`, `lancelot_model_io.md §3` |
| Per-credential secret access for embedding/provider SDKs | `lancelot.secrets.get(...)` (lazy backends, SIGHUP rotate) — the lazy-import template for `sqlite-vec` too | `lancelot_serve_tools.md §7` |
| Provider/token shape detection on memory-LLM responses | `upstream_metrics.detect_provider` + `extract_tokens` (3 shapes) | `lancelot_model_io.md §2` |
| Memory tools surfaced as MCP | `lancelot.mcp.mount` + `app.tool_schemas()` (4KB minimal-return WARN already validates the handle-and-fetch discipline) | `lancelot_serve_tools.md §3`, `transcripts_yc_root.md:58` |
| Outbound rate cap on shared embed API key | `TransportPool` `_TokenBucket` (`rate_limit=`) | `lancelot_model_io.md §1.9` |
| OCC retry IF a shared write log is ever added | turbopuffer jitter `[0.75×, 1.0×]base` (hard upper bound); SlotBackoff for manifest-level | `memory_vector.md:113-139` |
| SSRF guard on any memory-fetch-from-URL tool | `lancelot.net.assert_safe_url` | `lancelot_serve_tools.md §6` |

**Critical reuse note:** `lancelot.memory_pool.MemoryPool` is **byte-OOM reservation, NOT agent memory** — flagged in every single teardown that mentions it (`agno.md:132`, `mastra.md:310`, `nia.md:149`, `letta.md:285`, `memory_vector.md:236`, `transcripts_stanford_palantir.md:460`). The new module must be `lancelot.agent.memory`, a different layer. Do not overload the name.

---

## 3. REFUSE (off-thesis / depreciating / overkill)

1. **A memory-class hierarchy.** LangChain's 9 classes (`ConversationBufferMemory`, `…SummaryMemory`, `…EntityMemory`, `…KGMemory`, …) + 30 backends are *all deprecated* — "every memory class was a different ad-hoc truncation strategy; the right abstraction was always messages-list + compaction-hook" (`langchain.md:120-126`). One `Memory` object, a `tier` column, one `compaction` hook. Hard refuse.

2. **`ConversationEntityMemory`-style per-entity LLM extraction.** "N entities → N summarization LLM calls per user turn. The cost cliff is invisible to users" (`langchain.md:124, 315`). Mem0 *removed* its per-add arbitration to one call (`memory_systems.md:16-20`). Default to ONE extraction call per add, importance pre-computed at creation.

3. **Mandatory graph DB / bi-temporal arbitration as the default.** Graphiti/Cognee's 3-4-LLM-calls-per-ingest + serialized-per-group ingest + MinHash entity-resolution is correct for temporal-lifecycle facts but overkill for the common case (`memory_systems.md:344, 358`). Schema present, arbitration opt-in (§1.5). Refuse it as the default.

4. **Mandatory Postgres + pgvector + Alembic.** Letta's explicit anti-pattern (`letta.md:251`). SQLite default, Protocol for the rest.

5. **GBNF grammar-constrained generation** for non-native-tool models. "Depreciating tactical hack… frontier models have native tool calls… the model clock is against it. Do not build" (`letta.md:226-228`).

6. **Hard-delete on supersede / last-write-wins.** Loses history; "cannot answer who led Acme in Q1" (`memory_systems.md:355`). ADD-only + tombstone is the floor.

7. **The boolean-logprobs cross-encoder hack** (`logit_bias` on True/False token ids). "Depreciating, tied to specific token IDs in current model vocabularies — use a real reranker instead" (`memory_systems.md:139`).

8. **Eager/synchronous consolidation on every turn.** Burns quota, thrashes files. Background + frequency-gate + rate-gate + only-if-changed (`codex.md:419`, §1.8).

9. **Resumable-SSE-over-Redis / durable background-run memory.** Real (Letta `redis_stream_manager`, `letta.md:148-163`; Codex `state_5.sqlite`) but **defer** — it's the *durable-execution* layer's job, not memory's. "Defer until background-run support is a real requirement" (`letta.md:163`). Flag for the durable-seam brief, not here.

10. **Putting raw retrieved content / full tool outputs into memory or the loop.** "Compress first (distilled-reflection)… raw HTML in the loop is quadratic context growth" (`retrieval_agents.md:202`). Cache the 500-char cross-encoder highlight, not the 8000-char page (`retrieval_agents.md:205`). Store insights, not logs — "logs ≠ memories" (`transcripts_dlai.md:241`).

11. **Pickle across process boundaries** for any consolidation sub-process result. Letta fixed this to JSON at HEAD (`letta.md:255`). Use pydantic-core `to_json`.

---

## 4. Recommended API sketch (the shape users touch)

```python
from lancelot.agent.memory import Memory, EmbeddingDefinition

mem = Memory(
    backend=None,                       # None → SqliteVecBackend (sqlite-vec lazy-loaded) at default path
    scope=("agent_id", "tenant_id"),    # pulled from lancelot.context automatically
    embed=EmbeddingDefinition(          # asymmetric: source ≠ query encoding
        source_fn=embed_documents,      # "passage:" / input_type="document"
        query_fn=embed_query,           # "query:"  / input_type="query"
        dim=1024,
    ),
    # per-tier TTL (seconds); 0 = no expiry
    ttl={"scratchpad": 0, "episodic": 90*24*3600, "fact": 0, "procedural": 0},
    importance_decay_days=30,           # recency half-life in composite score
    procedural_max_lines=200,           # the 200-line index cap
    temporal_arbitration=False,         # opt-in Graphiti supersession; default = tombstone-on-explicit-supersede
)

# 1. Register the three memory tools on the app (rides ToolSpec / tool_schemas / call_tool)
mem.install_tools(app)                  # adds remember / recall / recall_as_of to app.tool_schemas()

# 2. In the agent loop — explicit, agent-driven
hits = await mem.recall("what DB does the user prefer?", tier="fact", k=10)
await mem.remember("switched from almond to oat milk after a sensitivity",
                   tier="fact", tags=["diet"], importance=0.8)
past = await mem.recall_as_of("who led Acme?", at=datetime(2025, 1, 1), tier="fact")

# 3. Virtual-context page-table for the system prompt (frozen-per-session view)
system_prompt = template.replace("{MEMORY_METADATA}", mem.page_table())

# 4. Background consolidation — frequency + rate + only-if-changed gated
@app.lifetime_task
async def consolidate():
    await mem.consolidate(model="claude-haiku-4.5",       # cheap model via TransportPool
                          every_turns=5, min_quota_pct=25, only_if_changed=True)

# 5. Bring-your-own backend — same Protocol, no Lancelot dep change
mem = Memory(backend=PgVectorBackend(dsn), embed=...)     # or Turbopuffer / Zep / RAM
```

`recall()` returns `list[Scored]` carrying the **full fusion algebra on the wire** (`{score, vector_sim, recency, importance, valid_at}`) — Nia's scoring-transparency pattern so callers can re-rank/filter (`nia.md:347-349`).

---

## 5. Exact default constants (all cited)

| Constant | Default | Source |
|---|---|---|
| Composite score weights | `0.5*vector + 0.3*recency + 0.2*importance` | `transcripts_dlai.md:223` |
| Recency decay half-life | **30 days** | `transcripts_dlai.md:225` |
| Fact/procedural TTL | **none (durable)** | §1.1 |
| Episodic TTL | **90 days** (auto-delete if not accessed) | `transcripts_dlai.md:237` |
| Scratchpad TTL | **session/job lifetime** | `nia.md:91` |
| Memory-creation / compaction trigger | **75%** context fill (compaction band 70–80%) | `transcripts_dlai.md:33-37` |
| RRF k (opt-in fusion) | **60** | `memory_vector.md:25`, `retrieval_agents.md:116` |
| Recall default `k` | **10** | `memory_systems.md:380` |
| Semantic-recall `topK` + range expansion | **4**, **±1** message | `mastra.md:177-178` |
| Scratchpad / recent-message buffer | **10** | `memory_systems.md:185` |
| Search over-fetch pool | `max(k*4, 60)` | `memory_systems.md:381` |
| Semantic similarity floor (gate before combine) | **0.1** on `1-cos_dist` (Mem0) / **0.6** cosine (Graphiti/turbopuffer/Supermemory) | `memory_systems.md:382, 393` |
| Procedural / index file cap | **≤200 lines / ~25KB** | `vercel_ai_sdk.md:354`, `arch_catalog.md:339` |
| Result-summary wire truncation | **~250 chars** | `nia.md:93` |
| Node/memory summary cap | **≤1000 chars** | `memory_systems.md:378` |
| Extraction/consolidation LLM temperature | **0** (structured) / **0.1** (Mem0) | `memory_systems.md:379, 190` |
| Consolidation frequency gate | every **N turns** (`turns % N == 0`) | `letta.md:124` |
| Consolidation rate-gate | **≥25%** quota remaining | `codex.md:405` |
| Consolidation model | **cheap** (Haiku 4.5 class, ~10× cheaper) | `letta.md:140`, `codex.md:294` |
| Compaction (if used): trigger / keep | **90%** trigger, keep **70%**, snap-to-assistant-boundary, summary at index 1 | `letta.md:136-143` |

---

## 6. Open questions (flagged, not blocking)

1. **Default embedding provider.** Lancelot ships no rates and no provider SDK. The `EmbeddingDefinition` is required from the user (no default `embed_fn`) — confirm that's the right ergonomic vs shipping a `sqlite-vec`-local-model convenience. Leaning: require it (cardinal rule; no hidden model dep).
2. **Where the SQLite file lives.** Codex uses `~/.codex/`, Claude Code `~/.lancelot/agent-memory/<agentType>/` was the suggested map (`claude_code.md:230`). Per-agent-type subdir vs single file — defer to first build; the Protocol makes it backend-internal.
3. **Multi-worker SQLite.** The MCP session-set has the same documented single-worker gap (`lancelot_serve_tools.md §3`). SQLite memory is single-writer; for multi-worker, the Protocol routes to Postgres/Turbopuffer. Document the same gap; don't solve it in the default backend.
4. **Should `episodic` carry lineage/citation by default?** Nia's `{source_id, tool, args, summary}` lineage (`nia.md:93, 369`) is powerful but couples memory to the loop's tool-call records. Lean: optional `provenance` field on `MemoryRecord`, populated by the loop layer, not the memory layer.
5. **`provenance.last_human_edit` freeze gate** for procedural blocks (Nia/Parahelp: agent never overwrites human edits — `nia.md:149`, `transcripts_yc_root.md:167`). Likely belongs in §1.2's `str_replace`-exact guard; confirm during build.

---

*Sources synthesized: `03_teardowns/{memory_systems,memory_vector,letta,agno,mastra,nia,hermes_core,codex,claude_code,manus,vercel_ai_sdk,openai_agents,google_adk,langchain,langgraph,retrieval_agents,strands,character_ai}.md`; `02_core/{transcripts_dlai,transcripts_yc_root,transcripts_a16z,transcripts_stanford_palantir,agentic_deep,arch_catalog,bad_guide_build_positioning}.md`; `04_byte_path/{lancelot_model_io,lancelot_serve_tools}.md`.*
