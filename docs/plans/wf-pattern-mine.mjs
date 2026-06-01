export const meta = {
  name: 'pattern-mine',
  description: 'Mine the best FastAPI patterns (from 20+ production FastAPI projects) and the best Rust patterns (from DataFusion/Polars/LanceDB/Turbopuffer/Zed/Tauri/Ripgrep/Robyn/LakeSail) in researchfms, keeping ONLY byte-path-relevant patterns, then map each to Lancelot as already-have / build / adapt / skip-Tier-D.',
  phases: [
    { title: 'Mine', detail: '4 FastAPI clusters + 4 Rust clusters mined in parallel' },
    { title: 'Synthesize', detail: 'dedupe + rank each track' },
    { title: 'Map', detail: 'map every pattern to Lancelot applicability' },
  ],
}

const TD = '/Users/seventyleven/Desktop/researchfms/teardowns'
const FA_CLONE = '/Users/seventyleven/Desktop/researchfms/fastapi_projects/_clones'
const FA_PATTERNS = '/Users/seventyleven/Desktop/researchfms/fastapi/patterns'
const FA_DRAFT = '/Users/seventyleven/Desktop/researchfms/fastapi_projects'
const ROBYN_SRC = '/Users/seventyleven/Desktop/researchfms/robyn/Robyn/src'
const BLAZEIO_SRC = '/Users/seventyleven/Desktop/researchfms/blazeio/Blazeio'

// What Lancelot already has + its do-not-do list, so miners/synth classify
// correctly and never "discover" something already shipped or deliberately
// rejected. Condensed from CLAUDE.md.
const LANCELOT_STATE = [
  'LANCELOT MISSION: optimize the HTTP byte path (transport/serialization/streaming/resilience) for',
  'AI/cloud/web/startups. Rust core (hyper/tokio/pyo3) + Python surface. AI logic stays USER-side.',
  '',
  'ALREADY SHIPPED (do not re-propose as new): drop-in FastAPI compat (99.6%); const prebaked-bytes tier',
  '(183k rps); pydantic-core to_json large-payload serialization; re-architected streaming pump',
  '(on-loop _drain, bounded mpsc, backpressure, client-disconnect cancellation); SSE keepalive + [DONE];',
  'TTFT metric; @app.batch prefix coalescing; body/connection caps; slow-loris header timeout; per-route',
  'timeout; graceful SIGTERM drain; RED metrics; request-id; W3C traceparent propagation; /livez /readyz',
  '/fleet/status; gzip middleware (Rust libdeflater off-loop); TransportPool (spawn_child, fanout, mTLS,',
  'rate_limit, SSRF guard, header injection); upstream_metrics (provider/token/cost); webhook (Standard',
  'Webhooks); secrets (5 backends + SIGHUP); mcp.mount; tenant concurrency limiter; memory_pool (named',
  'OOM); canonical JSON; session-affinity hint; pagination DoS caps; region-pinned routes; Idempotency-Key;',
  'uvloop soft-dep; multiprocess deploy model; matchit O(1) routing; intern! hot-path; abi3 wheels.',
  '',
  'TIER-D — DELIBERATELY REJECTED (classify matches here as skip-tier-d, do NOT propose building):',
  'custom JSON serializer (orjson/ujson — pydantic-core already Rust-fast); per-chunk stream compression;',
  'optimizing @app.stream per-chunk overhead (LLM is upstream-rate-limited, not the framework); brotli;',
  'OpenAPI callbacks/webhooks spec; per-request dependency-override rebuild; built-in CLI runner;',
  'AI logic (agents/vector stores/prompt templates/model gateway/model routing); Robyn-style hard-kill',
  'shutdown; per-chunk Pydantic validation on streams; 1-process-many-threads deploy; claiming tiny-request',
  'throughput as the selling point (uvicorn+uvloop wins there).',
].join('\n')

const PATTERN_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['cluster', 'patterns'],
  properties: {
    cluster: { type: 'string' },
    patterns: {
      type: 'array',
      items: {
        type: 'object', additionalProperties: false,
        required: ['name', 'category', 'source_projects', 'what_it_does', 'why_it_matters', 'byte_path_relevant', 'lancelot_applicability', 'evidence'],
        properties: {
          name: { type: 'string' },
          category: { type: 'string', description: 'e.g. streaming, serialization, concurrency, backpressure, resilience, memory, FFI/GIL, routing, observability, deploy.' },
          source_projects: { type: 'array', items: { type: 'string' } },
          what_it_does: { type: 'string' },
          why_it_matters: { type: 'string', description: 'The concrete performance/operator/correctness win.' },
          byte_path_relevant: { type: 'boolean', description: 'True only if it touches transport/serialization/streaming/resilience/the operator experience — NOT app/business/AI logic.' },
          lancelot_applicability: { type: 'string', enum: ['already-have', 'build', 'adapt', 'skip-tier-d', 'skip-not-byte-path'] },
          evidence: { type: 'string', description: 'File path / function / teardown section that proves the pattern is real (not invented).' },
        },
      },
    },
  },
}

const SYNTH_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['track', 'ranked_patterns', 'dropped'],
  properties: {
    track: { type: 'string' },
    ranked_patterns: {
      type: 'array', description: 'Deduped, ranked by byte-path leverage for Lancelot. Highest first.',
      items: {
        type: 'object', additionalProperties: false,
        required: ['name', 'category', 'source_projects', 'summary', 'lancelot_applicability', 'leverage'],
        properties: {
          name: { type: 'string' }, category: { type: 'string' },
          source_projects: { type: 'array', items: { type: 'string' } },
          summary: { type: 'string' },
          lancelot_applicability: { type: 'string', enum: ['already-have', 'build', 'adapt', 'skip-tier-d', 'skip-not-byte-path'] },
          leverage: { type: 'string', enum: ['high', 'medium', 'low'] },
        },
      },
    },
    dropped: { type: 'array', items: { type: 'string' }, description: 'Patterns dropped as not-byte-path or already-have, with one-line why.' },
  },
}

const MAP_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['build_candidates', 'adapt_candidates', 'confirms_existing', 'rejected', 'headline'],
  properties: {
    build_candidates: {
      type: 'array', description: 'Net-new patterns worth building, ranked. Each must move the byte path and respect Tier-D + the AI-logic boundary.',
      items: { type: 'object', additionalProperties: false, required: ['pattern', 'source', 'how_to_apply_to_lancelot', 'leverage', 'risk'],
        properties: { pattern: { type: 'string' }, source: { type: 'string' }, how_to_apply_to_lancelot: { type: 'string' }, leverage: { type: 'string' }, risk: { type: 'string' } } },
    },
    adapt_candidates: {
      type: 'array', description: 'Existing Lancelot areas that a mined pattern would harden/improve.',
      items: { type: 'object', additionalProperties: false, required: ['pattern', 'source', 'what_to_change'],
        properties: { pattern: { type: 'string' }, source: { type: 'string' }, what_to_change: { type: 'string' } } },
    },
    confirms_existing: { type: 'array', items: { type: 'string' }, description: 'Patterns that validate a choice Lancelot already made.' },
    rejected: { type: 'array', items: { type: 'string' }, description: 'Mined patterns correctly skipped (Tier-D or not byte-path), with why.' },
    headline: { type: 'string' },
  },
}

// ----- FastAPI mining clusters -----
const FA_CLUSTERS = [
  { key: 'fa-streaming', title: 'FastAPI: LLM serving & streaming',
    teardowns: ['LANGSERVE.md', 'LANGFLOW.md', 'R2R.md', 'HAYHOOKS.md'],
    clones: ['langserve', 'langflow', 'r2r', 'hayhooks'],
    focus: 'SSE/streaming response patterns, StreamingResponse usage, async generators, client-disconnect handling, OpenAI-compat endpoints, MCP exposure, backpressure, keepalive.' },
  { key: 'fa-serving', title: 'FastAPI: model serving & batching',
    teardowns: ['LITSERVE.md', 'BENTOML.md', 'RAY_SERVE.md', 'VLLM.md'],
    clones: ['litserve', 'bentoml', 'ray', 'vllm', 'modal-client'],
    focus: 'dynamic request batching, worker pools, async-vs-threadpool offload of blocking inference, payload/tensor serialization, health/readiness, graceful drain, GPU queue management.' },
  { key: 'fa-payments', title: 'FastAPI: payments, webhooks & production templates',
    teardowns: ['POLAR.md', 'BITCART.md', 'FULL_STACK_FASTAPI_TEMPLATE.md', 'FASTAPI_USERS.md', 'DISPATCH.md', 'OPAL.md'],
    clones: ['polar', 'bitcart', 'full-stack-fastapi-template', 'fastapi-users', 'dispatch', 'opal'],
    focus: 'webhook signing/verification & delivery, idempotency, middleware stacks, error-handling/exception handlers, settings/secrets, rate limiting, request validation, auth dependency patterns, project structure that scales.' },
  { key: 'fa-infra', title: 'FastAPI: orchestration, observability & distilled patterns',
    teardowns: ['PREFECT.md', 'LETTA.md', 'SENTRY_FASTAPI.md', 'CISCO_FASTAPI.md', 'MICROSOFT_FASTAPI.md'],
    clones: ['prefect', 'letta', 'sentry-python'],
    extra_reads: [FA_PATTERNS + '/FASTAPI_PRODUCTION_PATTERNS.md', FA_PATTERNS + '/VALIDATION_FINDINGS.md', FA_DRAFT + '/_patterns_draft.md', FA_DRAFT + '/VALIDATION_FINDINGS.md'],
    focus: 'high-volume small-request handling, connection pooling, lifespan/startup-shutdown, observability/RED metrics/tracing integration, ASGI middleware ordering, async DB session management, blocking-in-async-loop pitfalls. ALSO read the already-distilled pattern docs and BUILD ON them, do not duplicate.' },
]

// ----- Rust mining clusters -----
const RU_CLUSTERS = [
  { key: 'ru-query', title: 'Rust: query & data engines',
    teardowns: ['DATAFUSION.md', 'POLARS.md', 'LAKESAIL.md'],
    focus: 'memory pools / reservation & spill (TrackConsumers), vectorized/zero-copy buffers, streaming execution & backpressure, Arrow IPC serialization, tokio task scheduling, bounded channels, cooperative cancellation.' },
  { key: 'ru-vectordb', title: 'Rust: vector & storage DBs',
    teardowns: ['LANCEDB.md', 'TURBOPUFFER.md'],
    focus: 'async IO & tokio runtime tuning, connection/transport pooling, retry/backoff & retry-hint, zero-copy reads (mmap/Bytes), write batching, request coalescing, object-store streaming, graceful degradation under load.' },
  { key: 'ru-systems', title: 'Rust: editor/desktop & systems',
    teardowns: ['ZED.md', 'TAURI.md', 'RIPGREP.md'],
    focus: 'low-overhead IPC/serialization, async runtime structure, parallelism (rayon/work-stealing), buffer reuse / arena allocation, panic isolation, hot-path interning, SIMD/byte scanning, avoiding allocations in the hot loop.' },
  { key: 'ru-server', title: 'Rust: web server & Python-FFI bridges',
    teardowns: ['ROBYN.md', 'BLAZEIO.md'],
    extra_reads: [ROBYN_SRC, BLAZEIO_SRC],
    focus: 'pyo3 GIL discipline (release across await, GIL-across-FFI hazards), const/prebaked routing, request proxy laziness, asyncio bridging from Rust, shared-socket/SO_REUSEPORT multiprocess, the EXACT mistakes Robyn/Blazeio made that Lancelot should avoid or already beat. READ THE ROBYN RUST SOURCE.' },
]

const faMinePrompt = (c) => [
  'You have FULL tool access (Read, Grep, Bash). "Mine patterns" describes your OUTPUT — read source and teardowns freely.',
  '',
  'ROLE: You are a framework architect mining PRODUCTION FastAPI patterns for the cluster: ' + c.title + '.',
  'Mine ONLY byte-path-relevant patterns — transport, serialization, streaming, backpressure, resilience, observability, deploy, connection/worker management. IGNORE business/AI/app logic (agent loops, prompt building, vector math, domain models).',
  '',
  'FOCUS for this cluster: ' + c.focus,
  '',
  'SOURCES (teardowns are pre-distilled — start there; grep the clones for the actual code that proves a pattern):',
  '  Teardowns: ' + c.teardowns.map((t) => TD + '/' + t).join('  |  '),
  '  Clones: ' + c.clones.map((d) => FA_CLONE + '/' + d).join('  |  '),
  (c.extra_reads ? '  Also READ these already-distilled docs and build on them (do not duplicate what they cover): ' + c.extra_reads.join('  |  ') : ''),
  '',
  'For each pattern: name it, cite the real source (file/function/teardown section) as evidence, say what it does and the concrete win, mark byte_path_relevant, and classify lancelot_applicability against this state:',
  '----- LANCELOT STATE -----',
  LANCELOT_STATE,
  '----- END STATE -----',
  '',
  'Be rigorous: no invented patterns, no padding. 8-15 strong patterns beats 40 weak ones. If something is clearly already-have or Tier-D, still report it but mark it so.',
].join('\n')

const ruMinePrompt = (c) => [
  'You have FULL tool access (Read, Grep, Bash). "Mine patterns" describes your OUTPUT — read teardowns/source freely. These teardowns are large; GREP/skim for perf, hot-path, tokio, GIL, memory, serialization, streaming, backpressure, retry, allocation sections rather than reading end to end.',
  '',
  'ROLE: You are a Rust systems engineer mining hot-path / server / runtime patterns for the cluster: ' + c.title + '.',
  'Mine ONLY patterns that could transfer to Lancelot\'s Rust core (hyper/tokio/pyo3 HTTP byte path). IGNORE patterns specific to the source domain (query planning, editor UI, vector indexing math) UNLESS the underlying technique (memory pool, backpressure, zero-copy, cancellation, FFI discipline) is reusable for an HTTP server.',
  '',
  'FOCUS for this cluster: ' + c.focus,
  '',
  'SOURCES:',
  '  Teardowns: ' + c.teardowns.map((t) => TD + '/' + t).join('  |  '),
  (c.extra_reads ? '  Source to read directly: ' + c.extra_reads.join('  |  ') : ''),
  '',
  'For each pattern: name it, cite real evidence (teardown section / file), what it does, the concrete win, byte_path_relevant, and lancelot_applicability against this state:',
  '----- LANCELOT STATE -----',
  LANCELOT_STATE,
  '----- END STATE -----',
  '',
  'Rigorous, no invented patterns. Flag GIL-across-await / panic-across-FFI / unbounded-memory hazards explicitly when a source got them wrong — those are anti-patterns worth recording too. 8-15 strong patterns.',
].join('\n')

const synthPrompt = (track, results) => [
  'You are consolidating mined ' + track + ' patterns for Lancelot (HTTP byte-path framework; Rust core + Python surface; AI logic stays user-side). Dedupe across clusters, drop anything that is not byte-path or is already-have/Tier-D (record those in "dropped"), and rank the rest by byte-path leverage for Lancelot.',
  '',
  'Mined results (JSON array of {cluster, patterns}):',
  JSON.stringify(results),
  '',
  '----- LANCELOT STATE (for correct classification) -----',
  LANCELOT_STATE,
  '----- END STATE -----',
].join('\n')

const mapPrompt = (faSynth, ruSynth) => [
  'You are the Lancelot tech lead. Below are the ranked FastAPI-pattern and Rust-pattern syntheses. Produce ONE actionable map: which mined patterns are genuine BUILD candidates (net-new, byte-path, respect Tier-D + AI-logic boundary), which would ADAPT/harden an existing Lancelot area, which merely CONFIRM an existing choice, and which are correctly REJECTED. Be ruthless about the no-bullshit rule: a "build" must change the byte path or operator experience.',
  '',
  'FastAPI synthesis (JSON):',
  JSON.stringify(faSynth),
  '',
  'Rust synthesis (JSON):',
  JSON.stringify(ruSynth),
  '',
  '----- LANCELOT STATE -----',
  LANCELOT_STATE,
  '----- END STATE -----',
].join('\n')

// ---------------------------------------------------------------------------
// Mine all 8 clusters in parallel (barrier — synth needs the whole track), then
// synthesize each track, then map both into one actionable list.
// ---------------------------------------------------------------------------
log('Mining 4 FastAPI clusters + 4 Rust clusters in parallel...')

const faMined = await parallel(FA_CLUSTERS.map((c) => () =>
  agent(faMinePrompt(c), { label: 'mine:' + c.key, phase: 'Mine', schema: PATTERN_SCHEMA })))
const ruMined = await parallel(RU_CLUSTERS.map((c) => () =>
  agent(ruMinePrompt(c), { label: 'mine:' + c.key, phase: 'Mine', schema: PATTERN_SCHEMA })))

const faClean = faMined.filter(Boolean)
const ruClean = ruMined.filter(Boolean)
log('FastAPI clusters: ' + faClean.length + '/4, Rust clusters: ' + ruClean.length + '/4. Synthesizing each track...')

const [faSynth, ruSynth] = await parallel([
  () => agent(synthPrompt('FastAPI', faClean), { label: 'synth:fastapi', phase: 'Synthesize', schema: SYNTH_SCHEMA }),
  () => agent(synthPrompt('Rust', ruClean), { label: 'synth:rust', phase: 'Synthesize', schema: SYNTH_SCHEMA }),
])

log('Mapping patterns to Lancelot applicability...')
const mapped = await agent(mapPrompt(faSynth, ruSynth), { label: 'map:lancelot', phase: 'Map', schema: MAP_SCHEMA })

return { fastapi_track: faSynth, rust_track: ruSynth, mapped, raw: { faClean, ruClean } }
