# Memory & Vector Storage — mechanism-level harvest

**Sources read in full:** TURBOPUFFER.md (12,860 lines), LANCEDB.md (32,183 lines), R2R.md (759 lines), MIROFISH.md (2,684 lines).
**Focus:** RRF k=60 default reranker · unified rank_by algebra (BM25+ANN in one query) · asymmetric embeddings · tunable k1/b · OCC retry with SlotBackoff · two-tool MCP pattern · GraphStorage abstract interface.

---

## 1. RRF k=60 — the durable constant

**What it is.**
Reciprocal Rank Fusion (Cormack-Clarke-Buettcher SIGIR 2009). Score per document = Σ 1/(rank_in_list_i + k). The k=60 constant is from the original paper: "experiments indicate k=60 was near-optimal, but the choice is not critical."

**LanceDB verbatim code** (`/tmp/lancedb/rust/lancedb/src/rerankers/rrf.rs:84-94`):
```rust
let mut rrf_score_map = BTreeMap::new();
let mut update_score_map = |(i, result_id)| {
    let score = 1.0 / (i as f32 + self.k);
    rrf_score_map.entry(result_id)
        .and_modify(|e| *e += score)
        .or_insert(score);
};
vector_ids.values().iter().enumerate().for_each(&mut update_score_map);
fts_ids.values().iter().enumerate().for_each(&mut update_score_map);

impl Default for RRFReranker {
    fn default() -> Self { Self { k: 60.0 } }
}
```
Source: LANCEDB.md §8.8 (`/tmp/lancedb/rust/lancedb/src/rerankers/rrf.rs:39-43`).

**turbopuffer empirical confirmation** (TURBOPUFFER.md R8.8): with `{"queries":[vectorQ, bm25Q], "rerank_by":["RRF"]}`, a doc ranked #1 in BOTH lists scores `$dist=0.032786883 = 2/61 = 2/(60+1)`. A doc in only one list scores `0.015873017 = 1/(60+3)`. The arithmetic proves k=60 is the **actual server default**, not just documentation. Source: TURBOPUFFER.md §R8.8.

**Why RRF, not score normalization:** RRF uses ranks, never absolute scores. This is **scale-invariant** — it doesn't matter if BM25 returns [8.4, 7.1, 3.2] and cosine returns [0.92, 0.87, 0.61]. No calibration needed. LanceDB also ships `normalize_scores` (min-max to [0,1], optional `invert` for distance→similarity) as an alternative path, but RRF is the default and the one both projects recommend.

**Column naming contract** (LANCEDB.md §8.11):
- FTS output column: `_score` (higher = better, BM25)
- Vector output column: `_distance` (lower = better, L2/cosine)
- Hybrid/RRF output column: `_relevance_score` (higher = better, RRF)

**Zep Cloud** (MIROFISH.md §10): `graph.search(reranker="rrf")` is the default. The alternative is `"cross_encoder"` (learned logit [0,1]) — used only by the deep `quick_search` tool where precision matters more than speed.

**Lancelot byte-path implication:** The ranking math is trivial and CPU-cheap (BTreeMap + float arithmetic). What Lancelot already provides is the **fan-out** (`TransportPool.fanout`) to run ANN and BM25 in parallel with a deadline, and the **streaming TTFT** metric on the first result chunk. Lancelot does not need to own RRF; it should return results in a shape that makes client-side or handler-side RRF easy (e.g. separate `ann_results` + `bm25_results` with rank indices, or provide a `lancelot.retrieval.rrf(ann_list, bm25_list, k=60)` utility). The 5-line implementation is copy-and-paste durable.

---

## 2. unified rank_by algebra — BM25+ANN in one query (no two-pass)

**turbopuffer's key design decision** (TURBOPUFFER.md R7.3.1, blog "Mixing numeric attributes into text search"):
The `rank_by` parameter is a **nested S-expression** evaluated INSIDE the same MAXSCORE window as BM25 — not a second-stage reranker. Example:
```json
{
  "rank_by": ["Sum", [
    ["Product", 0.7, ["vector", "ANN", [0.1, 0.2, 0.3]]],
    ["Product", 0.3, ["body", "BM25", "search terms"]]
  ]]
}
```
This is **single-stage hybrid scoring** (text + vector in one pass), distinct from two-stage RRF (which is a separate `rerank_by` mechanism). The MAXSCORE block-iterator carries an extra per-document accumulator that folds in the attribute-clause contribution before the top-k heap compare.

Full grammar (TURBOPUFFER.md §R8.8, §R6.5, OpenAPI spec):
- Leaves: `["<attr>", "ANN", [vec]]`, `["<attr>", "kNN", [vec]]`, `["<attr>", "SparseKNN", {token: weight}]`, `["<attr>", "BM25", "query"]`, `["<attr>", "BM25", ["t1","t2"], BM25Params]`
- Combinators: `["Sum", [...]]`, `["Max", [...]]`, `["Product", weight, subquery]`
- Score-shaping: `["Saturate", subquery, {midpoint, exponent}]` (sigmoid rising), `["Decay", subquery, {midpoint, exponent}]` (exponential decay)
- Attribute scoring: `["Dist", subquery, origin]`, `["Attribute", col]`

The `Sum/Product` algebra means you can do: title BM25 × 3 + body BM25 × 1 — weighted multi-field BM25 — without a second pass.

**Two-stage path** (turbopuffer `multiQuery`, TURBOPUFFER.md R8.8): `POST .../query?stainless_overload=multiQuery` with `{"queries":[...], "rerank_by":["RRF"]}` runs each sub-query independently then fuses with RRF. This is what LanceDB's `rerank_hybrid` trait does: run vector search and FTS search separately, then call `RRFReranker.rerank_hybrid(vector_results, fts_results)`. Both styles are valid — single-pass for tight coupling/speed, two-pass for flexibility/accuracy.

**BM25 tunable k1/b** (TURBOPUFFER.md §6.4, R8.8; LANCEDB.md §6.2):
- turbopuffer defaults: `k1=1.2`, `b=0.75` (textbook Robertson-Sparck-Jones)
- turbopuffer benchmark-tuned for MSMARCO: `k1=0.9, b=0.4` (confirmed TURBOPUFFER.md §R3.8.2 CORRECTION)
- LanceDB `scorer.rs:1550`: `K1=1.2, B=0.75` (Lucene-compatible defaults)
- LanceDB `Scorer` trait: `query_weight(idf) × doc_weight(tf, norm)` — two-method factoring makes it swappable per-column without touching the query engine
- Takeaway: **expose k1/b as operator knobs**, not hardcoded. The benchmarks show the difference matters (~15% MRR shift on MSMARCO). turbopuffer even exposes them per-attribute via `BM25WithParams`.

**Lancelot byte-path implication:** TransportPool's `fanout(first_n=1, deadline=...)` pattern already supports running two async retrieval calls (ANN + BM25) and taking the fastest or merging. The `rank_by` algebra itself is a query DSL for a vector DB (turbopuffer/LanceDB), not something Lancelot builds; Lancelot can expose the **response merge** primitive as a typed helper.

---

## 3. Asymmetric embeddings — source ≠ query encoding

**LanceDB `EmbeddingFunction` trait** (`lancedb/src/embeddings.rs:22830-22841`, LANCEDB.md §82.1):
```rust
pub trait EmbeddingFunction: std::fmt::Debug + Send + Sync {
    fn name(&self) -> &str;
    fn source_type(&self) -> Result<Cow<'_, DataType>>;
    fn dest_type(&self) -> Result<Cow<'_, DataType>>;
    fn compute_source_embeddings(&self, source: Arc<dyn Array>) -> Result<Arc<dyn Array>>;
    fn compute_query_embeddings(&self, input: Arc<dyn Array>) -> Result<Arc<dyn Array>>;
}
```
The **source/query split is first-class**: `compute_source_embeddings` is called at write time; `compute_query_embeddings` at search time. Some models MUST use different prompts: E5 prepends `"passage: "` for documents and `"query: "` for queries; BGE uses a similar asymmetric pattern. Source: LANCEDB.md §82.1 + §8.4.

**turbopuffer's production use** (TURBOPUFFER.md §R6.4, `turbogrep`):
```typescript
.post("https://api.voyageai.com/v1/embeddings")
// ...
"model": "voyage-code-3",
"input_type": embedding_type.as_str(),  // "query" or "document"
```
turbopuffer's own code-search CLI (`turbogrep`) sets `input_type: "query"` for user queries and `"document"` for indexed code chunks. This is Voyage AI's asymmetric retrieval: **same model, different encoding mode**. Source: TURBOPUFFER.md §R6.4 (`turbogrep` Rust source, file:line cited in teardown).

**Why it matters for Lancelot's tool layer:**
`lancelot.tool` / `@app.tool` generates JSON schemas for tool inputs. If Lancelot ships a memory retrieval primitive, the tool's `embed` call must use `"query"` mode, not the same call as the `ingest` path uses for `"document"` mode. The asymmetry belongs in the `EmbeddingDefinition` per column (LanceDB's `EmbeddingRegistry` pattern): register once with `source_fn` + `query_fn`, call the right one automatically at read/write time. This is a 5-line distinction that most implementations get wrong (using the same embedding call for both).

**Lancelot already has `lancelot.canonical.dumps_canonical`** for KV-cache prefix stability — this is complementary. The asymmetric embedding model means: even if you stabilize the JSON prefix, the embedding call itself must still distinguish document vs query context.

---

## 4. OCC retry with SlotBackoff — contention-aware exponential

**LanceDB verbatim code** (`/tmp/lance/rust/lance-core/src/utils/backoff.rs:111-146`, LANCEDB.md §3.11):
```rust
pub struct SlotBackoff {
    base: u32,         // = 2
    unit: u32,         // = 50 ms initially; set to first_attempt_duration * 1.1 on first conflict
    starting_i: u32,   // = 2  → 4 slots initially
    attempt: u32,
    rng: rand::rngs::SmallRng,
}

impl SlotBackoff {
    pub fn next_backoff(&mut self) -> Duration {
        let num_slots = self.base.saturating_pow(self.attempt + self.starting_i);
        let slot_i = self.rng.random_range(0..num_slots);
        self.attempt += 1;
        Duration::from_millis((slot_i * self.unit) as u64)
    }
}
```

**The math:** attempt 0 → random slot in 0..4; attempt 1 → 0..8; attempt N → 0..2^(N+2). The `unit` is set to `first_attempt_elapsed * 1.1` on the first conflict — it **learns the unit from how long one operation takes**, so each slot is approximately one operation's worth of time. This is strictly better than jitter-exponential for OCC because: (a) the slot space grows exponentially so concurrent retriers desynchronize, (b) the unit is self-calibrating rather than a magic constant. Source: LANCEDB.md §3.11, citing `/tmp/lance/rust/lance-core/src/utils/backoff.rs:106-110`.

**LanceDB also has `commit_retries=20`** (default inner OCC retry count, LANCEDB.md §35.4 / §R3): there are TWO independent retry counters — high-level `merge_insert` retries (3 default) and low-level manifest version conflict retries (20 default). The two-level design avoids conflating retriable-transient with retriable-conflict.

**turbopuffer's jitter pattern** (TURBOPUFFER.md §8987): TypeScript SDK uses multiplicative discount jitter — `delay = base × random_in([0.75, 1.0])`. The worst case is exactly `base` (upper bounded); best case is 75% of it. Unlike additive jitter `base ± (random × 0.25 × base)`, this is always at or below the base, giving a hard upper bound. Source: TURBOPUFFER.md §R5 (`backoff.ts`, cited in teardown).

**Lancelot byte-path implication:** `TransportPool` already has `Retry-After` honoring and exponential backoff for upstream LLM calls. The SlotBackoff pattern applies specifically to **manifest-level OCC** (if Lancelot ever ships a durable-state layer or a shared write-ahead log). For the current scope (HTTP transport), the turbopuffer jitter pattern (`[0.75×base, 1.0×base]`) is the right copy — it gives a hard upper bound that's useful for capacity planning.

---

## 5. Two-tool MCP pattern — `execute` + `search_docs`

**turbopuffer's Stainless-hosted MCP** (`turbopuffer.stlmcp.com`, TURBOPUFFER.md §R5.10, R6.10):

The MCP `tools/list` response (TURBOPUFFER.md §R6.10.4, verbatim):

| Tool | Description | Annotations |
|------|-------------|-------------|
| `execute` | "Runs JavaScript code. Define an async function named 'run' that takes an initialized SDK client." | NOT readOnly — can mutate |
| `search_docs` | "Search SDK documentation to find methods, parameters, and usage examples." | `readOnlyHint: true` |

The `search_docs` input schema:
```json
{
  "query": "string",
  "language": "enum[http,python,go,typescript,javascript,terraform,ruby,java,kotlin]",
  "detail": "enum[default,verbose] (optional)"
}
```

**Why two tools, not one:** The split follows MCP's `readOnlyHint` annotation semantic — documentation search has no side effects and can be optimistically parallelized by the client. Code execution is a mutation. The agent pattern:
1. Call `search_docs` to understand the API shape
2. Call `execute` with the generated code

**Sandbox architecture** (TURBOPUFFER.md §R6.10.5, live probe 2026-05-24):
- Deno 2.7.7 + V8 14.6.202.9 + TypeScript 5.9.2 on x86_64 Linux
- `mainModule: "file:///workspace/$deno$stdin.mts"` — runs from stdin
- **25-second hard execution timeout**
- Network access denied by default except through the SDK client
- `--allow-run="tsgo"` only (no `jq`, no shell exec)
- "Variables do not persist between calls" — each `execute` invocation is stateless

**Instructions embedded in `initialize` response** (TURBOPUFFER.md §R6.10.3, ~6KB of TypeScript examples in `serverInfo.instructions`). The LLM receives usage examples at MCP handshake time, before any tool call. Key instructions:
- "Always truncate output with `substring()` to avoid token limits"
- "Do not add try-catch; the tool handles errors"
- "Variables do not persist between calls"
- "Cast attributes before using string methods"
- Check namespace schema before querying

**The jq filter** (TURBOPUFFER.md §R5.10.4): a `jq`-style expression post-processes tool output server-side before returning to the client. Applied at the MCP-layer (not inside the sandbox), so the LLM can request only specific fields from a large JSON response — reduces token consumption on large result sets. Example: `".rows[].id"` on a query response returns only IDs.

**Lancelot byte-path implication:** Lancelot has `lancelot.mcp.mount` which already enforces the 4KB minimal-return discipline (WARN log). The two-tool pattern (execute + search_docs) maps directly onto Lancelot's existing `@app.tool` surface: one tool with `readOnly: False` for write/compute operations, one with `readOnly: True` for documentation/schema introspection. The 25-second timeout maps to Lancelot's per-route `timeout=` kwarg. The `serverInfo.instructions` embed (usage examples in MCP handshake) is an underused pattern that Lancelot's `lancelot.mcp.mount` could surface as an optional `instructions=` kwarg.

---

## 6. GraphStorage abstract interface (Zep Cloud)

**From MIROFISH.md §10 (Zep Cloud Python SDK, KNOWN-ZEP):**

The Zep graph API is the closest real-world example of a **GraphStorage abstract interface** used in production by an agent system.

`graph.add()` — episode ingestion:
```python
def add(
    *,
    data: str,
    type: GraphDataType,    # "text" | "json" | "message" | "fact_triple"
    created_at: Optional[str] = OMIT,
    graph_id: Optional[str] = OMIT,
    metadata: Optional[Dict[str, Any]] = OMIT,   # max 10 keys, scalar values only
    source_description: Optional[str] = OMIT,
    user_id: Optional[str] = OMIT,
) -> Episode
```
Returns immediately with `processed=False` and `task_id`. Processing is async (NER → entity extraction → graph node/edge creation → embedding). Poll until `processed=True` (MiroFish polls every 3 seconds, 600-second timeout).

`graph.search()` — hybrid retrieval:
```python
def search(
    *,
    query: str,
    reranker: Optional[Reranker] = OMIT,       # "rrf" (default) | "cross_encoder"
    scope: Optional[GraphSearchScope] = OMIT,   # "edges" | "nodes" | "episodes" | "communities" | "auto"
    limit: Optional[int] = OMIT,               # default 10, max 50
    search_filters: Optional[SearchFilters] = OMIT,
    bfs_origin_node_uuids: Optional[Sequence[str]] = OMIT,  # BFS traversal anchor
    mmr_lambda: Optional[float] = OMIT,        # maximal marginal relevance
    ...
)
```

**Temporal fact model** (MIROFISH.md §10 Temporal Fact Model): Every edge has `valid_at`, `invalid_at`, `expired_at`. When a contradicting fact is ingested, the old edge gets `invalid_at` set — it is **never deleted**. History is preserved. This is the correct model for agent memory: facts change over time, and the agent should know that "as of T1 X was true, as of T2 X changed." MiroFish's `panorama_search` tool explicitly queries both active (`invalid_at IS NULL`) and historical (`invalid_at IS NOT NULL`) facts.

**Ontology schema** (MIROFISH.md §4 OntologyGenerator): The graph is typed — entity types (PascalCase) + edge types (UPPER_SNAKE_CASE) with explicit `source_targets` constraints per edge. The `edge_types` key is not `relation_types` (Zep SDK-specific; wrong key → silent fail). Reserved attribute names (`name`, `uuid`, `group_id`, `created_at`, `summary`) must not be used in custom attributes.

**Paging constants** (MIROFISH.md §8, `backend/app/utils/zep_paging.py`):
```python
DEFAULT_PAGE_SIZE = 100
MAX_NODES = 2000      # hard cap on pagination
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_DELAY = 2.0   # seconds, doubles each retry (exponential)
```
Retries only on transient errors: `ConnectionError`, `TimeoutError`, `OSError`, `InternalServerError`.

**Lancelot byte-path implication:** `lancelot.memory_pool.MemoryPool` is a byte-OOM primitive, not agent memory. The Zep interface above is what a Lancelot agent memory layer would need to implement against. Lancelot does NOT need to build the graph storage itself — it needs the `GraphStorage` protocol type (an abstract `add(episode)` + `search(query, scope, reranker)` trait that a Zep, Neo4j, or in-memory backend can satisfy). MiroFish's offline fork (`MiroFish-Offline`, MIROFISH.md §14) replaces Zep with Neo4j — confirming the interface is backend-agnostic. The hybrid search in `graph.search` (BM25 + semantic, re-ranked by RRF) is implemented by the backend, not by Lancelot.

---

## 7. MiroFish offline fork — Neo4j as pluggable GraphStorage backend

**MIROFISH.md §14 (KNOWN-FORK, `nikmcfly/MiroFish-Offline`):**

The offline fork replaces Zep Cloud with Neo4j + local embedding service. This is the proof that GraphStorage is an exchangeable backend:

Neo4j schema (Cypher):
```cypher
CREATE INDEX FOR (n:Entity) ON (n.name)
CREATE INDEX FOR ()-[r:RELATES_TO]-() ON (r.created_at)
```

**Hybrid search implementation** in the offline fork (MIROFISH.md §14):
1. BM25 via Neo4j fulltext index (`CALL db.index.fulltext.queryNodes(...)`)
2. Semantic via local embedding model + cosine similarity on vector property
3. Fusion: manual RRF merge in Python (same k=60 pattern)

**Embedding service** (offline fork): Sentence-transformers `all-MiniLM-L6-v2` via FastAPI, `batch_size=32`. The source/query distinction is NOT preserved in the offline fork — it uses the same embedding call for both document ingestion and query encoding. This is the asymmetric-embeddings anti-pattern: for real production use, `compute_source_embeddings` and `compute_query_embeddings` should diverge (E5/BGE style).

---

## 8. Anti-patterns to avoid

**From R2R.md §R2-R1 (KNOWN, verbatim source code):**

`users_router.py:1598` — blocking `requests.post` inside `async def` OAuth callback:
```python
async def google_callback(code: str, state: str) -> WrappedLoginResponse:
    token_data = requests.post("https://oauth2.googleapis.com/token", ...).json()  # BLOCKING
```
**Effect:** parks the OS thread running the event loop for the full network round-trip (100–800ms typically). Under any concurrency this turns OAuth login into a head-of-line-blocking DoS vector. Fix: `httpx.AsyncClient` or `run_in_executor`. This pattern is common — the symptom is `async def` routes that inadvertently use synchronous HTTP libraries.

**Hardcoded CSRF state** (R2R.md §R2-R1, `users_router.py:1651`):
```python
state = "some_random_string_or_csrf_token"  # HARDCODED — never verified in callback
```
Defeats the entire purpose of OAuth `state`. Fix: `secrets.token_urlsafe(32)`, persist in signed cookie, verify in callback.

**ALLOW_ORIGINS=`["*"]` + `allow_credentials=True`** (R2R.md §1.7): CORS spec-invalid combination. Browsers reject credentialed wildcard requests. Accepted because SDK/curl clients don't enforce CORS, but breaks browser-based clients.

**Byte-slicing UTF-8 SSE frames** (R2R.md §R2-R4): `chunk[i:i+1024]` splits multi-byte UTF-8 sequences across yields. Works because SSE bytes are concatenated client-side before decode, but fragile if any intermediary decodes per-chunk.

---

## 9. Turbopuffer production limits (from `skills/` repo + R9 live probe)

These are the hard numbers turbopuffer publishes for LLM agents in their `skills/` repo (TURBOPUFFER.md §R6):
- 16-concurrent-queries semaphore per namespace (confirmed live TURBOPUFFER.md §R9.1: "requires 17 permits, max is 16")
- 800ms wait timeout before queue overflow
- 2GB unindexed backpressure before writes stall
- p50=14ms / p90=57ms FTS hot-cache latency
- 500M docs / 2TB max per namespace
- 10k writes/s @ 32 MB/s per-namespace write cap
- Max FTS query length: 1,024 chars
- Max dense vector dims: 10,752; max sparse dims: 1,024

**The concurrency enforcement model** (TURBOPUFFER.md §R9.1): a per-namespace `asyncio.Semaphore`-equivalent of 16 permits. Separate requests beyond 16 are **queued** (server_total_ms > query_execution_ms); a single multiQuery request demanding >16 permits returns `400 💔 requires N permits, max is 16`. Lancelot's `lancelot.tenant.TenantConcurrencyLimiter` is the direct analogue of this pattern.

---

## API shape notes worth copying

**turbopuffer `rank_by` as S-expression array** (TURBOPUFFER.md §R6.5): the DSL is a JSON-native nested array: `["Sum", [["Product", 0.7, ["vector","ANN", vec]], ["Product", 0.3, ["body","BM25","query"]]]]`. Each node is `[head, ...args]`. This is easy to serialize/parse (no AST objects) and survives schema evolution via string head constants. All multi-language SDK codegen (`turbopuffer-apigen`) generates typed wrappers that emit this wire format.

**`$dist` column for hybrid RRF scores** (TURBOPUFFER.md §R8.8): RRF scores are returned in the same `$dist` field as ANN distances. The field is polysemantic — its meaning depends on `rank_by`/`rerank_by`. LanceDB uses a separate `_relevance_score` column for hybrid output. The LanceDB approach is clearer.

**SSE event taxonomy from R2R** (R2R.md §1.6, KNOWN verbatim):
`message` (assistant delta) / `thinking` (reasoning delta) / `tool_call` / `tool_result` / `search_results` / `citation` (first-occurrence only, `payload=null` on repeats) / `final_answer` / `error` / `done` (`data: [DONE]`). The citation dedup optimization — full payload only on first occurrence, null on repeats — is a clean bandwidth win for long RAG responses.

**`model_fields_set` for "omitted vs default"** (R2R.md §1.5, `retrieval_router.py:392-393`): Pydantic's `model_fields_set` distinguishes "client set this field" from "this is just the default value." R2R uses this to inject a configured model only when the client genuinely omitted it: `if "model" not in rag_generation_config.model_fields_set: rag_generation_config.model = self.config.app.quality_llm`. Same mechanism drives `merge_search_settings(exclude_unset=True)`.
