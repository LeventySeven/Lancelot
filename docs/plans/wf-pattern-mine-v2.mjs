export const meta = {
  name: 'pattern-mine-v2',
  description: 'Round 2, deeper + broader: mine byte-path patterns from MORE FastAPI + Rust projects (incl. deep second-pass clusters on the richest corpora), recheck each mined pattern against what M19 ALREADY shipped + the v1 build-order doc (so nothing shipped/rejected is re-proposed), synthesize per track, map to Lancelot, then a completeness critic names what is STILL un-mined.',
  phases: [
    { title: 'Mine', detail: '5 FastAPI + 5 Rust clusters (incl. deep second-pass)' },
    { title: 'Recheck-vs-shipped', detail: 're-classify each pattern against M19 + v1 doc' },
    { title: 'Synthesize', detail: 'dedupe + rank each track' },
    { title: 'Map', detail: 'build / adapt / confirm / reject + completeness critic' },
  ],
}

const TD = '/Users/seventyleven/Desktop/researchfms/teardowns'
const FA_CLONE = '/Users/seventyleven/Desktop/researchfms/fastapi_projects/_clones'
const FA_PATTERNS = '/Users/seventyleven/Desktop/researchfms/fastapi/patterns'
const ROBYN_SRC = '/Users/seventyleven/Desktop/researchfms/robyn/Robyn/src'
const BLAZEIO_SRC = '/Users/seventyleven/Desktop/researchfms/blazeio/Blazeio'
const LREPO = '/Users/seventyleven/Desktop/Lancelot'
const V1_DOC = LREPO + '/docs/plans/2026-06-02-byte-path-build-order-from-founders-and-patterns.md'

// Lancelot state INCLUDING M19 (so the now-shipped v1 patterns are classified
// already-have, not re-proposed). Condensed from CLAUDE.md + the M19 merge.
const STATE = [
  'LANCELOT MISSION: optimize the HTTP byte path (transport/serialization/streaming/resilience) for',
  'AI/cloud/web/startups. Rust core (hyper/tokio/pyo3) + Python surface. AI logic stays USER-side.',
  '',
  'ALREADY SHIPPED — do NOT re-propose: drop-in FastAPI compat (~99.6%); const prebaked-bytes tier (183k);',
  'pydantic-core to_json large-payload serialization; on-loop _drain streaming pump w/ bounded backpressure +',
  'client-disconnect cancellation; SSE keepalive + [DONE]; @app.batch prefix coalescing; body/connection caps;',
  'slow-loris header timeout; per-route timeout; graceful SIGTERM drain; RED metrics; request-id; W3C',
  'traceparent; /livez /readyz /fleet/status; gzip off-loop (libdeflater); TransportPool (spawn_child, fanout,',
  'mTLS, rate_limit, SSRF guard); upstream_metrics; webhook (Standard Webhooks); secrets (5 backends+SIGHUP);',
  'mcp.mount; tenant concurrency limiter; memory_pool (named OOM); canonical JSON; session-affinity hint;',
  'pagination DoS caps; region routes; Idempotency-Key; uvloop; multiprocess deploy; matchit O(1) routing;',
  'intern! hot-path; abi3 wheels; defer_status eager-first-chunk; cancel_on_disconnect dual-task race.',
  '',
  'NEWLY SHIPPED in M19 (merged main 65ff172) — now ALSO already-have:',
  '- BaseHTTPMiddleware first-class: add_middleware(BaseHTTPMiddleware) streams + propagates scope[state]; AND',
  '  @app.middleware("http") rewritten to a pure-asyncio STREAMING task-group model (no dup content-type, no',
  '  buffering, app_exc exception transparency). [lancelot/middleware.py]',
  '- LANCELOT_MAX_INFLIGHT_BYTES: aggregate in-flight request-body budget, 503 load-shed (NOT queue-and-wait).',
  '  [src/service.rs] (so the "two-budget bytes-in-flight" pattern is now PARTIALLY shipped — the load-shed',
  '  variant; the min-in-flight WAITING admission was deliberately NOT built.)',
  '- TTFT on ANY streamed body at the ASGI send boundary (not just @app.stream). [lancelot/asgi.py]',
  '- max_stream_lifetime: absolute wall-clock SSE cap + reconnect frame. [lancelot/routing.py, asgi.py]',
  '- const-tier auth-bypass guardrail (LANCELOT_CONST_STRICT). [lancelot/app.py]',
  '- Runtime conformance matrix: custom get_route_handler, add_route_live, dynamic create_model, mount',
  '  lifespan PROVEN; duplicate STATIC path is last-wins (divergence). [tests/test_m19_runtime_surface_matrix.py]',
  '',
  'TIER-D — DELIBERATELY REJECTED (classify matches as skip-tier-d): custom JSON serializer (orjson/ujson);',
  'per-chunk stream compression; optimizing @app.stream per-chunk overhead; brotli; OpenAPI callbacks; per-request',
  'dep-override rebuild; built-in CLI; AI logic (agents/vector stores/prompt templates/model gateway/routing);',
  'Robyn-style hard-kill shutdown; per-chunk Pydantic validation on streams; 1-proc-many-threads deploy; claiming',
  'tiny-request throughput as the selling point; LanceDB min-in-flight QUEUE-AND-WAIT admission (we shed instead);',
  'fixing the duplicate-static-path first-wins divergence (documented, narrow edge case); cross-worker pub/sub bus;',
  'Arrow IPC / pickle binary serde / storage-engine spill machinery; runtime route registration fighting matchit.',
].join('\n')

const PATTERN_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['cluster', 'patterns'],
  properties: {
    cluster: { type: 'string' },
    patterns: { type: 'array', items: {
      type: 'object', additionalProperties: false,
      required: ['name', 'category', 'source_projects', 'what_it_does', 'why_it_matters', 'byte_path_relevant', 'lancelot_applicability', 'evidence'],
      properties: {
        name: { type: 'string' }, category: { type: 'string' },
        source_projects: { type: 'array', items: { type: 'string' } },
        what_it_does: { type: 'string' }, why_it_matters: { type: 'string' },
        byte_path_relevant: { type: 'boolean' },
        lancelot_applicability: { type: 'string', enum: ['already-have', 'build', 'adapt', 'skip-tier-d', 'skip-not-byte-path'] },
        evidence: { type: 'string', description: 'file/function/teardown section proving it is real, not invented.' },
      } } },
  },
}

const SYNTH_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['track', 'ranked_patterns', 'newly_already_have_post_m19', 'dropped'],
  properties: {
    track: { type: 'string' },
    ranked_patterns: { type: 'array', items: {
      type: 'object', additionalProperties: false,
      required: ['name', 'category', 'source_projects', 'summary', 'lancelot_applicability', 'leverage', 'new_vs_round1'],
      properties: { name: { type: 'string' }, category: { type: 'string' }, source_projects: { type: 'array', items: { type: 'string' } }, summary: { type: 'string' }, lancelot_applicability: { type: 'string', enum: ['already-have', 'build', 'adapt', 'skip-tier-d', 'skip-not-byte-path'] }, leverage: { type: 'string', enum: ['high', 'medium', 'low'] }, new_vs_round1: { type: 'boolean' } } } },
    newly_already_have_post_m19: { type: 'array', items: { type: 'string' }, description: 'Patterns that round 1 marked build/adapt but M19 has since shipped.' },
    dropped: { type: 'array', items: { type: 'string' } },
  },
}

const MAP_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['build_candidates', 'adapt_candidates', 'confirms_existing', 'rejected', 'completeness_gaps', 'headline'],
  properties: {
    build_candidates: { type: 'array', items: { type: 'object', additionalProperties: false, required: ['pattern', 'source', 'how_to_apply', 'leverage', 'risk', 'new_vs_round1'],
      properties: { pattern: { type: 'string' }, source: { type: 'string' }, how_to_apply: { type: 'string' }, leverage: { type: 'string' }, risk: { type: 'string' }, new_vs_round1: { type: 'boolean' } } } },
    adapt_candidates: { type: 'array', items: { type: 'object', additionalProperties: false, required: ['pattern', 'source', 'what_to_change'],
      properties: { pattern: { type: 'string' }, source: { type: 'string' }, what_to_change: { type: 'string' } } } },
    confirms_existing: { type: 'array', items: { type: 'string' } },
    rejected: { type: 'array', items: { type: 'string' } },
    completeness_gaps: { type: 'array', items: { type: 'string' }, description: 'What is STILL un-mined — modalities/projects/pattern-families not yet covered after two rounds; the next mining targets.' },
    headline: { type: 'string' },
  },
}

const FA_CLUSTERS = [
  { key: 'fa-streaming', title: 'FastAPI: LLM serving & streaming', teardowns: ['LANGSERVE.md', 'LANGFLOW.md', 'R2R.md', 'HAYHOOKS.md'], clones: ['langserve', 'langflow', 'r2r', 'hayhooks'],
    focus: 'SSE/streaming, async-gen, client-disconnect, OpenAI-compat, MCP exposure, backpressure, keepalive, resume cursors, reconnect.' },
  { key: 'fa-serving', title: 'FastAPI: model serving & batching', teardowns: ['LITSERVE.md', 'BENTOML.md', 'RAY_SERVE.md', 'VLLM.md'], clones: ['litserve', 'bentoml', 'ray', 'vllm', 'modal-client'],
    focus: 'dynamic batching, worker pools, blocking-offload, tensor/payload serialization, health/readiness, drain, GPU queue, liveness watchdog.' },
  { key: 'fa-payments', title: 'FastAPI: payments / webhooks / templates / auth', teardowns: ['POLAR.md', 'BITCART.md', 'FULL_STACK_FASTAPI_TEMPLATE.md', 'FASTAPI_USERS.md', 'DISPATCH.md', 'OPAL.md'], clones: ['polar', 'bitcart', 'full-stack-fastapi-template', 'fastapi-users', 'dispatch', 'opal'],
    focus: 'webhook signing/delivery, idempotency, middleware stacks (BaseHTTPMiddleware per-request scoping), exception handlers, settings/secrets, rate limiting, request validation, scalable project structure, transient-error->retryable status.' },
  { key: 'fa-infra', title: 'FastAPI: orchestration / observability / distilled', teardowns: ['PREFECT.md', 'LETTA.md', 'SENTRY_FASTAPI.md', 'CISCO_FASTAPI.md', 'MICROSOFT_FASTAPI.md'], clones: ['prefect', 'letta', 'sentry-python'],
    extra_reads: [FA_PATTERNS + '/FASTAPI_PRODUCTION_PATTERNS.md', FA_PATTERNS + '/VALIDATION_FINDINGS.md'],
    focus: 'high-volume small-request handling, connection pooling, lifespan, observability/RED/tracing, ASGI middleware ordering, async DB sessions, blocking-in-async pitfalls. Build on the distilled docs, do not duplicate.' },
  { key: 'fa-deep2', title: 'FastAPI: DEEP second pass — patterns round 1 MISSED', teardowns: ['LANGFLOW.md', 'R2R.md', 'PREFECT.md', 'OPAL.md', 'DISPATCH.md', 'POLAR.md'], clones: ['langflow', 'r2r', 'prefect', 'opal', 'dispatch'],
    extra_reads: [V1_DOC],
    focus: 'Round 1 already mined the obvious patterns (see the v1 build-order doc — READ IT and do NOT repeat its findings). Go DEEPER: subtle byte-path/operator patterns the first pass skipped — error-taxonomy nuances, header/trailer tricks, connection lifecycle, cancellation edge cases, multi-tenant isolation on the wire, deploy/ordering gotchas. Only NET-NEW patterns not in the v1 doc.' },
]

const RU_CLUSTERS = [
  { key: 'ru-query', title: 'Rust: query & data engines', teardowns: ['DATAFUSION.md', 'POLARS.md', 'LAKESAIL.md'],
    focus: 'memory pools/reservation/spill, zero-copy buffers, streaming exec + backpressure, tokio scheduling, bounded channels, cooperative cancellation, allocator choice.' },
  { key: 'ru-vectordb', title: 'Rust: vector & storage DBs', teardowns: ['LANCEDB.md', 'TURBOPUFFER.md'],
    focus: 'async IO & tokio tuning, transport pooling, retry/backoff/retry-hint, zero-copy reads, write/request batching, object-store streaming, graceful degradation, two-budget admission.' },
  { key: 'ru-systems', title: 'Rust: editor/desktop & systems', teardowns: ['ZED.md', 'TAURI.md', 'RIPGREP.md'],
    focus: 'low-overhead IPC/serialization, async runtime structure, parallelism, buffer reuse/arena, panic isolation, interning, SIMD/byte scanning, no-alloc hot loops, drop-guard cleanup.' },
  { key: 'ru-server', title: 'Rust: web server & Python-FFI bridges', teardowns: ['ROBYN.md', 'BLAZEIO.md'], extra_reads: [ROBYN_SRC, BLAZEIO_SRC],
    focus: 'pyo3 GIL discipline (release across await, GIL-across-FFI hazards), const/prebaked routing, lazy request proxy, asyncio bridging from Rust, SO_REUSEPORT multiprocess, the EXACT mistakes Robyn/Blazeio made. READ the Robyn Rust source.' },
  { key: 'ru-deep2', title: 'Rust: DEEP second pass — richest corpora', teardowns: ['DATAFUSION.md', 'LANCEDB.md', 'TURBOPUFFER.md'], extra_reads: [V1_DOC],
    focus: 'These are the largest teardowns (DataFusion 407KB, LanceDB 1.17MB, Turbopuffer 686KB). Round 1 skimmed them — READ the v1 build-order doc and do NOT repeat its findings. Grep/skim DEEPER for hot-path/memory/cancellation/FFI/backpressure/retry patterns the first pass missed that transfer to an HTTP server. Only NET-NEW vs the v1 doc.' },
]

const minePrompt = (c, isRust) => [
  'You have FULL tool access (Read, Grep, Bash). "Mine patterns" describes OUTPUT — read source/teardowns freely.' + (isRust ? ' These teardowns are large; GREP/skim for perf/tokio/GIL/memory/serialization/backpressure/retry/alloc sections.' : ''),
  '',
  'ROLE: ' + (isRust ? 'Rust systems engineer' : 'framework architect') + ' mining BYTE-PATH patterns for cluster: ' + c.title + '.',
  'Mine ONLY transport/serialization/streaming/backpressure/resilience/observability/deploy' + (isRust ? '/memory/FFI/cancellation' : '/connection-mgmt') + ' patterns. IGNORE business/AI/app-domain logic.',
  'FOCUS: ' + c.focus,
  '',
  'SOURCES:',
  '  Teardowns: ' + c.teardowns.map((t) => TD + '/' + t).join('  |  '),
  (c.clones ? '  Clones: ' + c.clones.map((d) => FA_CLONE + '/' + d).join('  |  ') : ''),
  (c.extra_reads ? '  Also READ: ' + c.extra_reads.join('  |  ') : ''),
  '',
  'Classify each pattern\'s lancelot_applicability against this state (so you do NOT re-propose anything already',
  'shipped — including the M19 additions — or anything Tier-D):',
  '----- LANCELOT STATE (incl. M19) -----', STATE, '----- END -----',
  '',
  'Rigorous, no invented patterns, cite real evidence (file/function/section). 8-15 STRONG patterns. If a',
  'pattern is already-have or Tier-D, still report it but mark it so.',
].join('\n')

const recheckPrompt = (c, mined) => [
  'You have FULL tool access (Read, Grep, Bash). Verify against the Lancelot repo (' + LREPO + ') + the v1 doc (' + V1_DOC + ').',
  'ROLE: recheck auditor for the mined cluster "' + c.title + '". For EACH mined pattern: (1) confirm it is real (the evidence holds), (2) RE-CLASSIFY its lancelot_applicability against the CURRENT state — crucially, demote to "already-have" anything M19 shipped (BaseHTTPMiddleware streaming, inflight-bytes load-shed, TTFT-on-any-body, max_stream_lifetime, const-auth-guardrail) or that the v1 doc already covers; (3) ADD any genuinely byte-path pattern the miner MISSED on a second look. Drop padding and not-byte-path items.',
  'Mined patterns (JSON): ' + JSON.stringify(mined),
  'Return the REFINED, re-classified pattern set (same schema).',
  '----- LANCELOT STATE (incl. M19) -----', STATE, '----- END -----',
].join('\n')

const synthPrompt = (track, results) => [
  'Consolidate mined+rechecked ' + track + ' patterns for Lancelot (HTTP byte-path; Rust core + Python; AI logic user-side). Dedupe across clusters, drop not-byte-path / already-have / Tier-D (record already-have-post-M19 separately), rank the rest by byte-path leverage, and flag which are NET-NEW vs round 1.',
  'Rechecked results (JSON [{cluster, patterns}]): ' + JSON.stringify(results),
  '----- LANCELOT STATE (incl. M19) -----', STATE, '----- END -----',
].join('\n')

const mapPrompt = (faSynth, ruSynth) => [
  'You are the Lancelot tech lead. Given the ranked FastAPI + Rust syntheses (round 2, post-M19), produce ONE actionable map: BUILD candidates (net-new, byte-path, respect Tier-D + AI boundary — flag new_vs_round1), ADAPT candidates (harden an existing area), CONFIRMS (validated choices incl. M19), REJECTED (Tier-D / not byte-path), and COMPLETENESS_GAPS (what is STILL un-mined after two rounds — the next targets). Be ruthless re no-bullshit: a build must move the byte path or operator experience and must not already be shipped (incl. M19).',
  'FastAPI synthesis: ' + JSON.stringify(faSynth),
  'Rust synthesis: ' + JSON.stringify(ruSynth),
  '----- LANCELOT STATE (incl. M19) -----', STATE, '----- END -----',
].join('\n')

log('Round 2 mine: 5 FastAPI + 5 Rust clusters, each rechecked vs shipped+M19...')

const faMined = await pipeline(
  FA_CLUSTERS,
  (c) => agent(minePrompt(c, false), { label: 'mine:' + c.key, phase: 'Mine', schema: PATTERN_SCHEMA }),
  (mined, c) => agent(recheckPrompt(c, mined), { label: 'recheck:' + c.key, phase: 'Recheck-vs-shipped', schema: PATTERN_SCHEMA }),
)
const ruMined = await pipeline(
  RU_CLUSTERS,
  (c) => agent(minePrompt(c, true), { label: 'mine:' + c.key, phase: 'Mine', schema: PATTERN_SCHEMA }),
  (mined, c) => agent(recheckPrompt(c, mined), { label: 'recheck:' + c.key, phase: 'Recheck-vs-shipped', schema: PATTERN_SCHEMA }),
)

const faClean = faMined.filter(Boolean)
const ruClean = ruMined.filter(Boolean)
log('FastAPI ' + faClean.length + '/5, Rust ' + ruClean.length + '/5 rechecked. Synthesizing...')

const faSynth = await agent(synthPrompt('FastAPI', faClean), { label: 'synth:fastapi', phase: 'Synthesize', schema: SYNTH_SCHEMA })
const ruSynth = await agent(synthPrompt('Rust', ruClean), { label: 'synth:rust', phase: 'Synthesize', schema: SYNTH_SCHEMA })

log('Mapping + completeness critic...')
const mapped = await agent(mapPrompt(faSynth, ruSynth), { label: 'map:lancelot', phase: 'Map', schema: MAP_SCHEMA })

return { fastapi_track: faSynth, rust_track: ruSynth, mapped, raw: { faClean, ruClean } }
