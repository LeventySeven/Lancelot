export const meta = {
  name: 'founder-switch-verdict',
  description: 'Ten FastAPI-founder personas judge, strictly and honestly, which Lancelot features would actually make them migrate off FastAPI — and which are bullshit/non-reasons. Verified by an adversarial FastAPI-loyalist skeptic, then synthesized into a ranked build/keep/skip list.',
  phases: [
    { title: 'Founder Verdicts', detail: '10 founders evaluate Lancelot against their product byte path' },
    { title: 'Skeptic Challenge', detail: 'FastAPI-loyalist skeptic refutes each claimed switch reason' },
    { title: 'Synthesis', detail: 'aggregate + honesty audit into ranked build/keep/skip' },
  ],
}

// ---------------------------------------------------------------------------
// Shared, factual Lancelot capability brief. Sourced from lancelot/__init__.py
// and CLAUDE.md. HONEST — includes the limitations on purpose so founders can
// be skeptical. Do NOT let founders ask for AI-logic features (agent loops,
// vector stores, prompt orchestration) — those are deliberately out of scope.
// ---------------------------------------------------------------------------
const LANCELOT_BRIEF = [
  'LANCELOT — what it is: a backend framework like FastAPI/Robyn that optimizes the HTTP BYTE PATH',
  '(transport / serialization / streaming / resilience) for AI/cloud/web/startups. It is a Rust core',
  '(hyper/tokio/pyo3) with a Python surface. POSITIONING LOCK: AI logic stays on the USER side',
  '(LangGraph, agent loops, vector stores, prompt orchestration). Lancelot owns the byte path, the user',
  'owns the agent. It will NOT build agent frameworks, model gateways, vector stores, or prompt templating.',
  '',
  'DROP-IN COMPAT: `from lancelot import FastAPI` works; 99.6% FastAPI byte-parity (974/978 conformance).',
  'Runtime dep is pydantic>=2.7 only. abi3 wheels: `pip install lancelot` with NO Rust toolchain.',
  '',
  'VALIDATED WINS:',
  '- Const tier: fixed responses (health probes, static/cached) served prebaked from Rust, zero-GIL:',
  '  ~183k req/s, ~2.8x uvicorn+uvloop. (@app routes flagged const.)',
  '- Large-payload serialization: pydantic-core to_json beats Python json.dumps ~2.3x at 69 KB',
  '  (crossover ~1-4 KB; below that, uvicorn+uvloop wins).',
  '- Streaming (re-architected R3 pump): parity-to-1.3x vs Starlette, bounded backpressure, graceful',
  '  drain, client-disconnect -> upstream cancellation. SSE keepalive, [DONE] sentinel. TTFT metric',
  '  (lancelot.http.stream.ttft — time-to-first-chunk, the AI-streaming UX number).',
  '- @app.batch: prefix-aware request COALESCING — ~12x throughput / ~16x fewer upstream calls vs a',
  '  rate-limited LLM backend. No analogue in FastAPI/Robyn. (This is request grouping, NOT GPU batching.)',
  '- Cloud resilience+observability: body cap, connection cap, slow-loris/header timeout, per-route',
  '  timeout, graceful drain on SIGTERM, RED metrics, request-id echo, W3C traceparent propagation',
  '  (context.trace_id; TransportPool forwards it to the LLM call), /livez + /readyz, /fleet/status.',
  '- gzip compress middleware (Rust libdeflater, run OFF the event loop via executor): ~2.2x RPS,',
  '  ~12x better p99 under concurrency on a single worker; co-tenant tiny route 1.4 -> 230 RPS.',
  '',
  'AI/CLOUD TRANSPORT PRIMITIVES (Python surface):',
  '- lancelot.llm.TransportPool: outbound LLM HTTP — spawn_child (sub-agent ctx snapshot), fanout',
  '  (parallel-probe, first_n/deadline/cancel_laggards), mTLS, auto-injects x-request-id/x-agent-id/',
  '  traceparent, rate_limit token bucket, idle_guarded_chunks, SSRF guard (block_private_ips).',
  '- lancelot.upstream_metrics: provider/model/token/cost detection across OpenAI/Anthropic/Google/Groq/',
  '  DeepSeek/Mistral/Together/Fireworks; cost_usd ContextVar; X-Lancelot-Cost-USD response header.',
  '- lancelot.webhook: Standard Webhooks sign/verify (HMAC, replay window).',
  '- lancelot.secrets: 5 backends (Env/File/Vault/Infisical/AWS) + SIGHUP rotation + audit hook.',
  '- lancelot.mcp.mount(app): expose @app.tool tools as an MCP server; minimal-return discipline.',
  '- lancelot.tenant.TenantConcurrencyLimiter: per-tenant asyncio.Semaphore cap.',
  '- lancelot.memory_pool.MemoryPool: named-consumer OOM accounting with human-readable top-consumer.',
  '- lancelot.net: opt-in SSRF guard. lancelot.ratelimit: token-bucket limiter.',
  '- lancelot.canonical.dumps_canonical: byte-stable JSON for prompt/KV-cache prefix stability.',
  '- Response.set_session_affinity(key): KV-cache-aware routing hint for the LB.',
  '- lancelot.pagination: DoS-capped pagination + sort-allowlist dependencies.',
  '- Region-pinned routes (@app.get(..., region=...)). Idempotency-Key support.',
  '- auth / security / staticfiles / OpenAPI / WebSockets / BackgroundTasks.',
  '',
  'HONEST LIMITATIONS (be skeptical with these):',
  '- It does NOT provide AI logic (no agents, no vector store, no prompt orchestration). By design.',
  '- Tiny one-shot handler path (59k req/s, uvloop) BEATS FastAPI(41k)/Robyn(36k) but loses to RAW',
  '  Starlette+uvloop (67k). Never a blanket "faster server" claim.',
  '- Anti-scales with tokio threads (Python GIL) -> MUST deploy multiprocess (one loop per process).',
  '- SO_REUSEPORT load-balancing is Linux-only; on macOS you front N procs behind a proxy.',
  '- HTTP/1.1 only (designed to sit behind a TLS-terminating proxy). No HTTP/2, no brotli.',
  '- gzip middleware is slower at low concurrency (c=1 thread-hop) and shrinks behind a compressing',
  '  proxy or multi-worker uvicorn; the win is structural concurrency on a single worker.',
  '- The native Rust app.run() server has limits (historically no lifespan/DI on that path; sync',
  '  handlers only recently fixed). The battle-tested production path is Lancelot App as an ASGI app',
  '  under uvicorn, which gives full lifespan/DI.',
  '- @app.batch coalesces by request prefix/key; it is NOT GPU micro-batching of tensor inputs.',
].join('\n')

const TEARDOWN_DIR = '/Users/seventyleven/Desktop/researchfms/teardowns'
const CLONE_DIR = '/Users/seventyleven/Desktop/researchfms/fastapi_projects/_clones'

// Each founder: their product, the angle/byte-path that defines them, the
// teardown markdown to read, and the source clone to spot-check.
const PERSONAS = [
  { key: 'polar', name: 'Polar', founder: 'Birk (Polar.sh)', teardown: 'POLAR.md', clone: 'polar',
    angle: 'Merchant-of-record billing/payments API. Stripe-style webhooks OUT to merchants, idempotent checkout/subscription endpoints, money correctness, low p99 on the checkout path.' },
  { key: 'bitcart', name: 'Bitcart', founder: 'MrNaif (Bitcart)', teardown: 'BITCART.md', clone: 'bitcart',
    angle: 'Self-hosted crypto payment processor. Outbound store webhooks on payment events, websocket/SSE payment-status push, must run lean on cheap self-hosted boxes.' },
  { key: 'langserve', name: 'LangServe', founder: 'LangChain team', teardown: 'LANGSERVE.md', clone: 'langserve',
    angle: 'Deploys LangChain runnables as REST. The whole value prop is /stream and /astream_events token streaming over SSE. Pure streaming play.' },
  { key: 'langflow', name: 'Langflow', founder: 'Langflow (DataStax)', teardown: 'LANGFLOW.md', clone: 'langflow',
    angle: 'Visual agent/flow builder. SSE streaming of flow execution, many small CRUD API calls for the builder UI, file uploads, long-running flow runs.' },
  { key: 'litserve', name: 'LitServe', founder: 'Lightning AI', teardown: 'LITSERVE.md', clone: 'litserve',
    angle: 'GPU model-serving engine. Their differentiator is server-side dynamic BATCHING of inference requests + worker scaling. Throughput on large tensors/payloads matters.' },
  { key: 'r2r', name: 'R2R', founder: 'SciPhi (R2R)', teardown: 'R2R.md', clone: 'r2r',
    angle: 'Production RAG engine. Streaming RAG answers (SSE), large retrieval/document JSON payloads, agentic retrieval calling many upstreams.' },
  { key: 'letta', name: 'Letta', founder: 'Letta (MemGPT)', teardown: 'LETTA.md', clone: 'letta',
    angle: 'Stateful agent-memory server. Streams agent reasoning steps (SSE), long-lived agent sessions, calls LLMs heavily, multi-tenant.' },
  { key: 'hayhooks', name: 'Hayhooks', founder: 'deepset (Haystack)', teardown: 'HAYHOOKS.md', clone: 'hayhooks',
    angle: 'Turns Haystack pipelines into REST + OpenAI-compatible + MCP endpoints. Streaming pipeline output, MCP server exposure, OpenAI-compat chat completions.' },
  { key: 'prefect', name: 'Prefect', founder: 'Prefect', teardown: 'PREFECT.md', clone: 'prefect',
    angle: 'Workflow orchestration. A FastAPI API server hit by a huge volume of small worker poll/heartbeat/status requests; mostly DB-bound; health/readiness probes everywhere.' },
  { key: 'fastapi-users', name: 'FastAPI-Users', founder: 'Francois Voron', teardown: 'FASTAPI_USERS.md', clone: 'fastapi-users',
    angle: 'An AUTH LIBRARY (not a server product) that plugs into FastAPI apps. Deeply coupled to FastAPI/Starlette internals (dependencies, Request, OAuth flows). Likely a HARD case for migration.' },
]

const VERDICT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['product', 'byte_path_profile', 'switch_reasons', 'non_reasons', 'missing_blockers', 'overall_verdict', 'one_line'],
  properties: {
    product: { type: 'string' },
    byte_path_profile: { type: 'string', description: 'Concrete byte-path characteristics of THIS product drawn from the teardown: request shapes, payload sizes, streaming?, webhooks?, concurrency, scale, where time/cost actually goes.' },
    switch_reasons: {
      type: 'array',
      description: 'ONLY features that solve a REAL pain this product has AND that FastAPI+Starlette+a proxy does not already trivially cover.',
      items: {
        type: 'object', additionalProperties: false,
        required: ['feature', 'why_it_matters', 'evidence', 'strength'],
        properties: {
          feature: { type: 'string' },
          why_it_matters: { type: 'string' },
          evidence: { type: 'string', description: 'Specific evidence from THIS product/teardown that the pain is real.' },
          strength: { type: 'string', enum: ['blocker-solved', 'strong', 'nice-to-have'] },
        },
      },
    },
    non_reasons: {
      type: 'array',
      description: 'Lancelot features that are irrelevant to this product OR already covered by FastAPI/Starlette/proxy/their own stack — i.e. NOT a reason to switch.',
      items: {
        type: 'object', additionalProperties: false,
        required: ['feature', 'why_not'],
        properties: { feature: { type: 'string' }, why_not: { type: 'string' } },
      },
    },
    missing_blockers: {
      type: 'array',
      description: 'Things this product needs that Lancelot does NOT provide (and would or would not block migration). Respect the AI-logic boundary — do not list agent/vectorstore features as Lancelot gaps.',
      items: {
        type: 'object', additionalProperties: false,
        required: ['need', 'why', 'blocks_migration'],
        properties: { need: { type: 'string' }, why: { type: 'string' }, blocks_migration: { type: 'boolean' } },
      },
    },
    overall_verdict: { type: 'string', enum: ['would-switch', 'would-pilot-one-service', 'would-not-switch'] },
    one_line: { type: 'string', description: 'One brutally honest sentence the founder would actually say.' },
  },
}

const SKEPTIC_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['product', 'challenged_reasons', 'net_assessment', 'adjusted_verdict'],
  properties: {
    product: { type: 'string' },
    challenged_reasons: {
      type: 'array',
      items: {
        type: 'object', additionalProperties: false,
        required: ['feature', 'founder_claim', 'rebuttal', 'verdict'],
        properties: {
          feature: { type: 'string' },
          founder_claim: { type: 'string' },
          rebuttal: { type: 'string', description: 'The strongest case that FastAPI+Starlette+a proxy/library already covers this, or that it is not actually a bottleneck for this product.' },
          verdict: { type: 'string', enum: ['holds', 'weak', 'refuted'] },
        },
      },
    },
    net_assessment: { type: 'string' },
    adjusted_verdict: { type: 'string', enum: ['would-switch', 'would-pilot-one-service', 'would-not-switch'] },
  },
}

const SYNTH_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['build_now', 'keep_and_emphasize', 'bullshit_or_non_reasons', 'missing_worth_building', 'switch_table', 'headline'],
  properties: {
    build_now: {
      type: 'array', description: 'Features/improvements with real cross-founder demand that survived the skeptic. Ranked, highest demand first.',
      items: { type: 'object', additionalProperties: false, required: ['feature', 'demand_count', 'products', 'why_real'],
        properties: { feature: { type: 'string' }, demand_count: { type: 'integer' }, products: { type: 'array', items: { type: 'string' } }, why_real: { type: 'string' } } },
    },
    keep_and_emphasize: {
      type: 'array', description: 'Existing Lancelot features that ARE genuine switch reasons and should be led with in positioning.',
      items: { type: 'object', additionalProperties: false, required: ['feature', 'products', 'note'],
        properties: { feature: { type: 'string' }, products: { type: 'array', items: { type: 'string' } }, note: { type: 'string' } } },
    },
    bullshit_or_non_reasons: {
      type: 'array', description: 'Features/claims that founders + skeptic agree are NOT real reasons to switch (covered by FastAPI/proxy, not a bottleneck, or over-sold). Do not build/push these.',
      items: { type: 'object', additionalProperties: false, required: ['feature', 'why_not'],
        properties: { feature: { type: 'string' }, why_not: { type: 'string' } } },
    },
    missing_worth_building: {
      type: 'array', description: 'Genuine gaps multiple founders raised that respect the byte-path mission and AI-logic boundary. Ranked by leverage.',
      items: { type: 'object', additionalProperties: false, required: ['feature', 'requested_by', 'rationale', 'risk_or_caveat'],
        properties: { feature: { type: 'string' }, requested_by: { type: 'array', items: { type: 'string' } }, rationale: { type: 'string' }, risk_or_caveat: { type: 'string' } } },
    },
    switch_table: {
      type: 'array', description: 'One row per founder: final honest verdict.',
      items: { type: 'object', additionalProperties: false, required: ['product', 'verdict', 'top_reason', 'top_blocker'],
        properties: { product: { type: 'string' }, verdict: { type: 'string' }, top_reason: { type: 'string' }, top_blocker: { type: 'string' } } },
    },
    headline: { type: 'string', description: 'The single most important, honest takeaway for what to build next.' },
  },
}

const AUDIT_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['overclaims_caught', 'underweighted_signals', 'final_build_order', 'verdict'],
  properties: {
    overclaims_caught: { type: 'array', items: { type: 'string' }, description: 'Anything in the synthesis that over-sells a win or violates the no-bullshit / AI-logic-boundary rules.' },
    underweighted_signals: { type: 'array', items: { type: 'string' } },
    final_build_order: { type: 'array', items: { type: 'string' }, description: 'The honest, ranked list of what to actually build/emphasize next.' },
    verdict: { type: 'string' },
  },
}

const founderPrompt = (p) => [
  'You have FULL tool access (Read, Grep, Bash). The instructions below describe your OUTPUT, not a tool restriction — read files freely to gather evidence.',
  '',
  'ROLE: You are the founder/lead engineer of ' + p.name + ' (' + p.founder + '), a production system built on FastAPI.',
  'Your product byte-path angle: ' + p.angle,
  '',
  'TASK: Decide, strictly and with 100% honesty, which Lancelot features would give YOU a real reason to migrate ' + p.name + ' off FastAPI — and which would not. You are not a Lancelot salesperson and not a hater; you are a busy founder who only changes frameworks for a concrete win.',
  '',
  'STEP 1 — Know your own byte path. Read your teardown to ground every claim in reality:',
  '  Teardown (READ THIS): ' + TEARDOWN_DIR + '/' + p.teardown,
  '  Source clone to spot-check (grep for routers, streaming, websockets, middleware, uvicorn, BackgroundTasks): ' + CLONE_DIR + '/' + p.clone,
  '  Extract: actual request shapes, payload sizes, do you stream?, do you emit/consume webhooks?, concurrency model, scale, multi-tenant?, where does latency/cost ACTUALLY go (often DB or the LLM upstream, NOT the framework).',
  '',
  'STEP 2 — Evaluate Lancelot feature-by-feature against YOUR byte path, using this brief:',
  '----- LANCELOT BRIEF -----',
  LANCELOT_BRIEF,
  '----- END BRIEF -----',
  '',
  'STRICT RULES for counting a switch_reason:',
  '  - It must solve a REAL pain you have evidence for in your teardown.',
  '  - It must NOT be something FastAPI + Starlette + a normal reverse proxy (nginx/Cloudflare) already give you trivially (e.g. gzip at the proxy, health checks, request-id).',
  '  - "Faster in a microbenchmark" is NOT a reason unless the framework is actually your bottleneck (for most of these products it is the DB or the LLM upstream — be honest about that).',
  '  - Respect the boundary: Lancelot will NEVER provide agent loops/vector stores/prompt orchestration. Do not list those as gaps; list them only as "stays on our side".',
  '  - Migration cost is real: 99.6% drop-in is not 100%, and deep Starlette coupling counts against switching.',
  '',
  'Return the structured verdict. Be specific and cite your product. A "would-not-switch" with honest reasoning is a perfectly valid and valuable answer.',
].join('\n')

const skepticPrompt = (p, v) => [
  'You have FULL tool access (Read, Grep, Bash). Use it to check claims against FastAPI/Starlette behavior if useful.',
  '',
  'ROLE: You are a skeptical senior FastAPI/Starlette engineer doing devil\'s advocate on a migration proposal for ' + p.name + '. Your job is to REFUTE weak reasons to switch frameworks. Default to skepticism: most framework migrations are not worth it.',
  '',
  'The founder of ' + p.name + ' produced this verdict (JSON):',
  JSON.stringify(v),
  '',
  'For EACH claimed switch_reason, make the strongest honest case that it is NOT a real reason: either FastAPI+Starlette+a reverse proxy already covers it, or it targets something that is not actually this product\'s bottleneck (DB / LLM upstream / network), or it is over-sold relative to Lancelot\'s own stated limitations (GIL multiprocess, HTTP/1.1-only, native-server lifespan limits, gzip slow at low concurrency, loses tiny path to raw Starlette).',
  'Mark each: holds / weak / refuted. Then give a net assessment and an adjusted verdict.',
  'Be fair: if a reason genuinely holds (e.g. @app.batch coalescing or Standard-Webhooks signing with no clean FastAPI equivalent), say it holds. 100% honesty both directions.',
  '----- LANCELOT BRIEF (for grounding) -----',
  LANCELOT_BRIEF,
  '----- END BRIEF -----',
].join('\n')

const synthPrompt = (rows) => [
  'You are the head of product for Lancelot. Below are 10 FastAPI-founder verdicts, each already challenged by an adversarial FastAPI-loyalist skeptic. Synthesize them into a ranked, brutally honest action list. Weight the SKEPTIC-survived reasons most. Honor the cardinal rule: no bullshit, no useless logic — every "build" must change the byte path or the operator experience, and nothing may violate the AI-logic boundary (no agents/vector stores/prompt orchestration).',
  '',
  'Verdicts + skeptic challenges (JSON array of {product, verdict, skeptic}):',
  JSON.stringify(rows),
  '',
  'Produce: build_now (real cross-founder demand that survived the skeptic), keep_and_emphasize (existing features that ARE genuine switch reasons), bullshit_or_non_reasons (what to NOT build/push), missing_worth_building (honest gaps within the mission), a per-founder switch_table, and one headline takeaway.',
  '----- LANCELOT BRIEF -----',
  LANCELOT_BRIEF,
  '----- END BRIEF -----',
].join('\n')

const auditPrompt = (synth, rows) => [
  'You are a no-bullshit honesty auditor for Lancelot (the framework whose cardinal rule is: every change must move the byte path or operator experience; never over-sell perf; AI logic stays user-side). Audit this synthesis for over-claims, mission drift, and anything the underlying founder/skeptic data does not actually support.',
  '',
  'Synthesis (JSON):',
  JSON.stringify(synth),
  '',
  'Underlying founder+skeptic data (JSON):',
  JSON.stringify(rows),
  '',
  'Catch every over-claim and every under-weighted real signal, then give the final honest ranked build order.',
].join('\n')

// ---------------------------------------------------------------------------
// Run: founder -> skeptic per persona (pipeline, no barrier between stages),
// then a single synthesis + honesty audit over all of it.
// ---------------------------------------------------------------------------
log('Dispatching ' + PERSONAS.length + ' FastAPI-founder personas + adversarial skeptics...')

const evaluated = await pipeline(
  PERSONAS,
  (p) => agent(founderPrompt(p), { label: 'founder:' + p.key, phase: 'Founder Verdicts', schema: VERDICT_SCHEMA }),
  (verdict, p) => agent(skepticPrompt(p, verdict), { label: 'skeptic:' + p.key, phase: 'Skeptic Challenge', schema: SKEPTIC_SCHEMA })
    .then((skeptic) => ({ product: p.name, verdict, skeptic })),
)

const clean = evaluated.filter(Boolean)
log(clean.length + '/' + PERSONAS.length + ' founder verdicts completed. Synthesizing...')

const synthesis = await agent(synthPrompt(clean), { label: 'synthesis', phase: 'Synthesis', schema: SYNTH_SCHEMA })
const audit = await agent(auditPrompt(synthesis, clean), { label: 'honesty-audit', phase: 'Synthesis', schema: AUDIT_SCHEMA })

return { founders: clean, synthesis, audit }
