export const meta = {
  name: 'founder-recheck-v2',
  description: 'Round 2, deeper: 14 FastAPI-founder personas RE-EVALUATE migration to Lancelot now that M19 shipped (BaseHTTPMiddleware, bytes-in-flight cap, TTFT-on-any-body, max_stream_lifetime, const-auth-guardrail, runtime conformance matrix). Each verdict is challenged by a skeptic AND then code-grounded-verified against the ACTUAL shipped M19 files, then synthesized + audited.',
  phases: [
    { title: 'Recheck Verdicts', detail: '14 founders re-judge migration given M19' },
    { title: 'Skeptic', detail: 'FastAPI-loyalist refutes weak switch reasons' },
    { title: 'Code-Grounded Verify', detail: 'verify M19 claims against the real shipped files' },
    { title: 'Synthesis', detail: 'aggregate + honesty audit (what changed vs round 1)' },
  ],
}

const LANCELOT_REPO = '/Users/seventyleven/Desktop/Lancelot'
const TEARDOWN_DIR = '/Users/seventyleven/Desktop/researchfms/teardowns'
const CLONE_DIR = '/Users/seventyleven/Desktop/researchfms/fastapi_projects/_clones'

// Round-1 baseline brief (the validated wins + honest limits) — unchanged facts.
const BRIEF_BASE = [
  'LANCELOT — a backend framework like FastAPI/Robyn optimizing the HTTP BYTE PATH (transport /',
  'serialization / streaming / resilience) for AI/cloud/web/startups. Rust core (hyper/tokio/pyo3) +',
  'Python surface. POSITIONING LOCK: AI logic stays USER-side (agents, vector stores, prompt orchestration);',
  'Lancelot owns the byte path. Drop-in: `from lancelot import FastAPI`; ~99.6% byte-parity. Runtime dep',
  'pydantic only. abi3 wheels (pip install, no Rust).',
  '',
  'VALIDATED WINS: const prebaked-bytes tier (~183k rps, ~2.8x uvicorn+uvloop, health/static); large-payload',
  'serialization via pydantic-core to_json (~2.3x at 69KB, crossover ~1-4KB); re-architected streaming pump',
  '(parity-to-1.3x, bounded backpressure, graceful drain, client-disconnect->upstream cancel); @app.batch',
  'prefix coalescing (~12x throughput / ~16x fewer upstream calls vs a rate-limited LLM); resilience suite',
  '(body cap, conn cap, slow-loris timeout, per-route timeout, SIGTERM drain, RED metrics, request-id, W3C',
  'traceparent, /livez+/readyz+/fleet/status); gzip off-loop (Rust libdeflater); TransportPool (spawn_child,',
  'fanout, mTLS, rate_limit, SSRF guard); upstream_metrics (provider/token/cost); webhook (Standard Webhooks);',
  'secrets (5 backends+SIGHUP); mcp.mount; tenant limiter; memory_pool; canonical JSON; session-affinity hint.',
  '',
  'HONEST LIMITS: NOT AI logic (by design). Tiny one-shot beats FastAPI(41k)/Robyn(36k) at 59k but loses to',
  'raw Starlette+uvloop (67k). Anti-scales with threads -> deploy multiprocess. SO_REUSEPORT LB Linux-only.',
  'HTTP/1.1-only (behind a TLS proxy). gzip slower at c=1, shrinks behind a proxy/multi-worker. @app.batch is',
  'request coalescing, NOT GPU micro-batching.',
].join('\n')

// NEW since round 1 — M19 (merged to main, commit 65ff172). Founders must RECHECK
// against these AND the code-verifier confirms them in the real files.
const BRIEF_M19 = [
  'M19 JUST SHIPPED (merged to main) — RE-EVALUATE your round-1 verdict against these. Verify in the repo at',
  LANCELOT_REPO + ' (files named below).',
  '',
  '1. BaseHTTPMiddleware NOW first-class (was the #1 cited migration blocker). `add_middleware(BaseHTTPMiddleware',
  '   subclass)` streams + injects response headers + PROPAGATES request.state -> ASGI scope[state] to the',
  '   Lancelot handler (the dishka / tenant-schema / CSRF per-request-scoping pattern). AND `@app.middleware',
  '   ("http")` was rewritten from sync full-buffer capture to a pure-asyncio STREAMING task-group model — fixes',
  '   a corrupted duplicated content-type and no longer buffers streams; exception transparency (Starlette',
  '   app_exc parity). Files: lancelot/middleware.py, tests/test_m19_basehttp_middleware.py,',
  '   tests/test_m19_http_middleware_streaming.py.',
  '2. `LANCELOT_MAX_INFLIGHT_BYTES` (default 0=off): aggregate in-flight request-body budget in the Rust',
  '   admission path, 503 load-shedding (NOT queue-and-wait). Bounds the 1024-conn x 16MiB aggregate-RAM DoS.',
  '   Files: src/service.rs, tests/test_m19_inflight_bytes.py.',
  '3. TTFT now attaches to ANY streamed body (user StreamingResponse / sse_starlette EventSourceResponse), not',
  '   only the @app.stream seam — honest "first-framework-byte incl. queue-wait", not "true LLM TTFT". Files:',
  '   lancelot/asgi.py (_ttft_send_wrapper), tests/test_m19_ttft_any_stream.py.',
  '4. `max_stream_lifetime`: per-route absolute wall-clock SSE cap + terminal `event: reconnect` frame (the',
  '   idle-silence watchdog could not catch a steadily-trickling stream). Files: lancelot/routing.py,',
  '   lancelot/asgi.py, tests/test_m19_stream_lifetime.py.',
  '5. const-tier auth-bypass guardrail: warn / `LANCELOT_CONST_STRICT` raise when a const_route shadows a path',
  '   with middleware (it would silently strip auth). Files: lancelot/app.py, tests/test_m19_const_auth_guardrail.py.',
  '6. Runtime/dynamic-surface conformance matrix published: custom APIRoute.get_route_handler, runtime',
  '   add_route_live, dynamic create_model body, mounted sub-app lifespan = PROVEN. KNOWN DIVERGENCE: duplicate',
  '   STATIC same-path routes are LAST-registered-wins (FastAPI is first-wins). File:',
  '   tests/test_m19_runtime_surface_matrix.py. Full status doc:',
  '   docs/plans/2026-06-02-byte-path-build-order-from-founders-and-patterns.md.',
  '',
  'ROUND-1 FINDING (what you are rechecking): all 10 founders judged would-NOT-switch — bottleneck is never the',
  'framework (DB / LLM upstream / GPU / Argon2), and the marquee Rust wins (const tier, native resilience) were',
  'unreachable under uvicorn where DI apps must run. The QUESTION for round 2: does M19 move any verdict, and',
  'where is Lancelot STILL not enough — be even more specific and code-grounded than round 1.',
].join('\n')

const TEARDOWN = '/Users/seventyleven/Desktop/researchfms/teardowns'
const PERSONAS = [
  { key: 'polar', name: 'Polar', founder: 'Birk (Polar.sh)', teardown: 'POLAR.md', clone: 'polar', angle: 'Merchant-of-record billing; Stripe-style outbound webhooks, idempotent checkout, money correctness, 8-mixin APIRoute + makefun auth.' },
  { key: 'bitcart', name: 'Bitcart', founder: 'MrNaif', teardown: 'BITCART.md', clone: 'bitcart', angle: 'Self-hosted crypto payments; store webhooks, WS/SSE payment status, dishka DI, runs lean on cheap boxes.' },
  { key: 'langserve', name: 'LangServe', founder: 'LangChain', teardown: 'LANGSERVE.md', clone: 'langserve', angle: 'Deploys runnables as REST; /stream + /astream_events SSE token streaming; first-registered-wins route resolution.' },
  { key: 'langflow', name: 'Langflow', founder: 'DataStax', teardown: 'LANGFLOW.md', clone: 'langflow', angle: 'Visual agent builder; SSE flow-exec streaming, many small CRUD calls, uploads, BaseHTTPMiddleware.' },
  { key: 'litserve', name: 'LitServe', founder: 'Lightning AI', teardown: 'LITSERVE.md', clone: 'litserve', angle: 'GPU model serving; server-side dynamic batching, large tensors, the byte that matters is on a GPU worker behind a queue.' },
  { key: 'r2r', name: 'R2R', founder: 'SciPhi', teardown: 'R2R.md', clone: 'r2r', angle: 'Production RAG; streaming SSE answers, large retrieval JSON, tenant BaseHTTPMiddleware, dual-Security auth.' },
  { key: 'letta', name: 'Letta', founder: 'Letta (MemGPT)', teardown: 'LETTA.md', clone: 'letta', angle: 'Stateful agent-memory server; SSE agent steps, Granian+nginx already, orjson already, Redis-resumable streams.' },
  { key: 'hayhooks', name: 'Hayhooks', founder: 'deepset', teardown: 'HAYHOOKS.md', clone: 'hayhooks', angle: 'Haystack pipelines -> REST + OpenAI-compat + MCP; runtime route registration, app.routes mutation, mounted MCP sub-apps.' },
  { key: 'prefect', name: 'Prefect', founder: 'Prefect', teardown: 'PREFECT.md', clone: 'prefect', angle: 'Orchestration API; huge volume of small worker poll/heartbeat requests, mostly DB-bound, CSRF BaseHTTPMiddleware, custom get_route_handler.' },
  { key: 'fastapi-users', name: 'FastAPI-Users', founder: 'Francois Voron', teardown: 'FASTAPI_USERS.md', clone: 'fastapi-users', angle: 'Auth LIBRARY; makefun runtime-signature Security introspection, Argon2 hot path, deep Starlette coupling.' },
  // NEW in round 2 (mine more):
  { key: 'dispatch', name: 'Dispatch', founder: 'Netflix OSS', teardown: 'DISPATCH.md', clone: 'dispatch', angle: 'Incident-management; heavy CRUD API, Slack/webhook integrations, BaseHTTPMiddleware for per-request org/tenant scoping, plugin system.' },
  { key: 'opal', name: 'OPAL', founder: 'Permit.io', teardown: 'OPAL.md', clone: 'opal', angle: 'Realtime open-policy admin; SSE/WebSocket policy push, cross-worker pub/sub fan-out (EventBroadcaster), fastapi BaseHTTPMiddleware-heavy.' },
  { key: 'bentoml', name: 'BentoML', founder: 'BentoML', teardown: 'BENTOML.md', clone: 'bentoml', angle: 'Model-serving framework; adaptive batching in a runner, large payloads, run_in_threadpool offload, health/readiness, graceful drain.' },
  { key: 'fullstack-template', name: 'Full-Stack-FastAPI-Template', founder: 'Tiangolo (template maintainer)', teardown: 'FULL_STACK_FASTAPI_TEMPLATE.md', clone: 'full-stack-fastapi-template', angle: 'The canonical GREENFIELD FastAPI starter (SQLModel + JWT + CRUD). The key question: would you recommend `from lancelot import FastAPI` as the base for new projects built on this template?' },
]

const VERDICT_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['product', 'byte_path_profile', 'round1_verdict_guess', 'm19_impact', 'switch_reasons', 'remaining_blockers', 'overall_verdict', 'verdict_changed_from_round1', 'one_line'],
  properties: {
    product: { type: 'string' },
    byte_path_profile: { type: 'string', description: 'Concrete byte-path facts from the teardown: request shapes, payload sizes, streaming?, webhooks?, middleware?, concurrency, where time/cost actually goes.' },
    round1_verdict_guess: { type: 'string', enum: ['would-switch', 'would-pilot-one-service', 'would-not-switch'], description: 'Your honest read of what the round-1 verdict would have been (pre-M19).' },
    m19_impact: {
      type: 'array', description: 'For each M19 feature relevant to YOU: did it remove a real blocker, help a little, or not matter? Be specific + cite your product.',
      items: { type: 'object', additionalProperties: false, required: ['m19_feature', 'effect', 'detail'],
        properties: { m19_feature: { type: 'string' }, effect: { type: 'string', enum: ['removes-a-blocker', 'helps-marginally', 'irrelevant'] }, detail: { type: 'string' } } },
    },
    switch_reasons: {
      type: 'array', description: 'Post-M19 REAL reasons to switch (solves a real pain AND not trivially covered by FastAPI+Starlette+proxy). Empty is a valid honest answer.',
      items: { type: 'object', additionalProperties: false, required: ['feature', 'why_it_matters', 'evidence', 'strength'],
        properties: { feature: { type: 'string' }, why_it_matters: { type: 'string' }, evidence: { type: 'string' }, strength: { type: 'string', enum: ['blocker-solved', 'strong', 'nice-to-have'] } } },
    },
    remaining_blockers: {
      type: 'array', description: 'What STILL blocks or de-motivates migration after M19 (respect the AI-logic boundary — do not list agent/vectorstore features).',
      items: { type: 'object', additionalProperties: false, required: ['need', 'why', 'blocks_migration'],
        properties: { need: { type: 'string' }, why: { type: 'string' }, blocks_migration: { type: 'boolean' } } },
    },
    overall_verdict: { type: 'string', enum: ['would-switch', 'would-pilot-one-service', 'would-not-switch'] },
    verdict_changed_from_round1: { type: 'boolean' },
    one_line: { type: 'string' },
  },
}

const SKEPTIC_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['product', 'challenged', 'overcredited_m19', 'net_assessment', 'adjusted_verdict'],
  properties: {
    product: { type: 'string' },
    challenged: { type: 'array', items: { type: 'object', additionalProperties: false, required: ['feature', 'founder_claim', 'rebuttal', 'verdict'],
      properties: { feature: { type: 'string' }, founder_claim: { type: 'string' }, rebuttal: { type: 'string' }, verdict: { type: 'string', enum: ['holds', 'weak', 'refuted'] } } } },
    overcredited_m19: { type: 'array', items: { type: 'string' }, description: 'M19 effects the founder over-credited (e.g. crediting a fix that does not actually reach their deploy/path).' },
    net_assessment: { type: 'string' },
    adjusted_verdict: { type: 'string', enum: ['would-switch', 'would-pilot-one-service', 'would-not-switch'] },
  },
}

const VERIFY_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['product', 'checks', 'confirmed_helpful_m19', 'debunked_claims', 'final_verdict'],
  properties: {
    product: { type: 'string' },
    checks: {
      type: 'array', description: 'For each M19 feature the founder/skeptic credited or disputed, what the ACTUAL code/tests show.',
      items: { type: 'object', additionalProperties: false, required: ['m19_feature', 'file_checked', 'reality', 'matches_claim'],
        properties: { m19_feature: { type: 'string' }, file_checked: { type: 'string' }, reality: { type: 'string' }, matches_claim: { type: 'string', enum: ['confirms', 'partially', 'refutes'] } } },
    },
    confirmed_helpful_m19: { type: 'array', items: { type: 'string' } },
    debunked_claims: { type: 'array', items: { type: 'string' } },
    final_verdict: { type: 'string', enum: ['would-switch', 'would-pilot-one-service', 'would-not-switch'] },
  },
}

const SYNTH_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['verdict_movement', 'm19_that_moved_verdicts', 'm19_that_didnt', 'next_build_now', 'still_bullshit', 'switch_table', 'headline'],
  properties: {
    verdict_movement: { type: 'string', description: 'How many of the 14 moved (and which way) vs round 1; the honest net.' },
    m19_that_moved_verdicts: { type: 'array', items: { type: 'object', additionalProperties: false, required: ['feature', 'products', 'why'],
      properties: { feature: { type: 'string' }, products: { type: 'array', items: { type: 'string' } }, why: { type: 'string' } } } },
    m19_that_didnt: { type: 'array', items: { type: 'object', additionalProperties: false, required: ['feature', 'why_not'],
      properties: { feature: { type: 'string' }, why_not: { type: 'string' } } } },
    next_build_now: { type: 'array', description: 'Ranked next builds that WOULD move still-stuck founders — survived skeptic + code-verify, in-mission, respect Tier-D.',
      items: { type: 'object', additionalProperties: false, required: ['feature', 'demand_count', 'products', 'why_real', 'risk'],
        properties: { feature: { type: 'string' }, demand_count: { type: 'integer' }, products: { type: 'array', items: { type: 'string' } }, why_real: { type: 'string' }, risk: { type: 'string' } } } },
    still_bullshit: { type: 'array', items: { type: 'object', additionalProperties: false, required: ['feature_or_claim', 'why_not'],
      properties: { feature_or_claim: { type: 'string' }, why_not: { type: 'string' } } } },
    switch_table: { type: 'array', items: { type: 'object', additionalProperties: false, required: ['product', 'round1', 'round2', 'top_reason_or_blocker'],
      properties: { product: { type: 'string' }, round1: { type: 'string' }, round2: { type: 'string' }, top_reason_or_blocker: { type: 'string' } } } },
    headline: { type: 'string' },
  },
}

const AUDIT_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['overclaims_caught', 'underweighted_signals', 'final_build_order', 'verdict'],
  properties: {
    overclaims_caught: { type: 'array', items: { type: 'string' } },
    underweighted_signals: { type: 'array', items: { type: 'string' } },
    final_build_order: { type: 'array', items: { type: 'string' } },
    verdict: { type: 'string' },
  },
}

const founderPrompt = (p) => [
  'You have FULL tool access (Read, Grep, Bash). Read files freely to gather evidence — the schema describes OUTPUT only.',
  '',
  'ROLE: founder/lead engineer of ' + p.name + ' (' + p.founder + '), a production system on FastAPI.',
  'Your byte-path angle: ' + p.angle,
  '',
  'This is ROUND 2 of an honest migration assessment. Lancelot just shipped M19 (below). Re-evaluate, 100% honest.',
  '',
  'STEP 1 — Ground in your own byte path. READ your teardown: ' + TEARDOWN + '/' + p.teardown,
  '  Spot-check the source clone (grep routers/streaming/middleware/uvicorn/webhooks): ' + CLONE_DIR + '/' + p.clone,
  'STEP 2 — Read the M19 brief AND verify the relevant claims in the actual Lancelot repo (' + LANCELOT_REPO + '): the',
  '  brief names the exact files. Decide, per M19 feature, whether it removes a REAL blocker for YOU, helps a little,',
  '  or is irrelevant — cite your product.',
  'STEP 3 — Net verdict: did M19 change your (round-1) would-not-switch read? What STILL blocks you?',
  '',
  '----- LANCELOT BASE BRIEF -----', BRIEF_BASE,
  '----- M19 (RECHECK + VERIFY IN CODE) -----', BRIEF_M19,
  '----- END -----',
  '',
  'STRICT: a switch_reason must solve a real pain you can cite AND not be trivially covered by FastAPI+Starlette+a',
  'reverse proxy. "Faster in a microbench" is not a reason unless the framework is your actual bottleneck (for most',
  'of these it is the DB / LLM upstream / GPU — say so). Respect the AI-logic boundary. A would-not-switch with',
  'honest reasoning is fully valid.',
].join('\n')

const skepticPrompt = (p, v) => [
  'You have FULL tool access (Read, Grep, Bash). Check claims against the Lancelot repo (' + LANCELOT_REPO + ') and FastAPI behavior.',
  'ROLE: skeptical senior FastAPI/Starlette engineer doing devil\'s advocate on ' + p.name + '\'s round-2 migration verdict. REFUTE weak reasons; especially catch where the founder OVER-CREDITS an M19 feature that does not actually reach their deploy/path (e.g. native-server-only wins under uvicorn, a fix that needs a knob they would not set, a metric they have no pipeline for).',
  'Founder verdict (JSON): ' + JSON.stringify(v),
  'Mark each challenged reason holds/weak/refuted; list over-credited M19 effects; give an adjusted verdict. Be fair both ways.',
  '----- M19 -----', BRIEF_M19, '----- END -----',
].join('\n')

const verifyPrompt = (p, v, s) => [
  'You have FULL tool access (Read, Grep, Bash). This is a CODE-GROUNDED verification step — READ THE ACTUAL FILES in ' + LANCELOT_REPO + '.',
  'ROLE: implementation auditor. The founder of ' + p.name + ' and a skeptic disagree about what M19 actually does. Resolve it against the SHIPPED CODE + TESTS.',
  'Founder verdict: ' + JSON.stringify(v),
  'Skeptic: ' + JSON.stringify(s),
  'For each M19 feature in dispute, OPEN the named file(s) (lancelot/middleware.py, lancelot/asgi.py, src/service.rs, lancelot/routing.py, lancelot/app.py, tests/test_m19_*.py) and state what the code REALLY does — does it confirm, partially support, or refute the founder\'s credit for THIS product\'s use case? Then give the final, code-grounded verdict.',
  '----- M19 (file pointers) -----', BRIEF_M19, '----- END -----',
].join('\n')

const synthPrompt = (rows) => [
  'You are head of product for Lancelot. Below are 14 ROUND-2 founder rechecks, each challenged by a skeptic and then CODE-GROUNDED-VERIFIED against the shipped M19 files. Synthesize honestly: did M19 move verdicts? Which M19 features genuinely moved which founders (only count code-verified ones)? Which did not and why? What is the ranked NEXT build that would move the still-stuck founders (in-mission, respects Tier-D + the AI-logic boundary)? What is still bullshit? Give a per-founder round1->round2 switch table + one headline.',
  'Data (JSON [{product, verdict, skeptic, verify}]): ' + JSON.stringify(rows),
  '----- M19 -----', BRIEF_M19, '----- END -----',
].join('\n')

const auditPrompt = (synth, rows) => [
  'You are a no-bullshit honesty auditor for Lancelot (every change must move the byte path or operator experience; never over-sell perf; AI logic stays user-side). Audit this round-2 synthesis for over-claims (esp. crediting M19 effects the code-verify step did not confirm) and under-weighted real signals; give the final honest ranked build order.',
  'Synthesis: ' + JSON.stringify(synth),
  'Underlying data: ' + JSON.stringify(rows),
].join('\n')

log('Round 2: ' + PERSONAS.length + ' founders re-judging against M19, with code-grounded verification...')

const evaluated = await pipeline(
  PERSONAS,
  (p) => agent(founderPrompt(p), { label: 'recheck:' + p.key, phase: 'Recheck Verdicts', schema: VERDICT_SCHEMA }),
  (v, p) => agent(skepticPrompt(p, v), { label: 'skeptic:' + p.key, phase: 'Skeptic', schema: SKEPTIC_SCHEMA }).then((s) => ({ p, v, s })),
  (bundle) => agent(verifyPrompt(bundle.p, bundle.v, bundle.s), { label: 'verify:' + bundle.p.key, phase: 'Code-Grounded Verify', schema: VERIFY_SCHEMA })
    .then((verify) => ({ product: bundle.p.name, verdict: bundle.v, skeptic: bundle.s, verify })),
)

const clean = evaluated.filter(Boolean)
log(clean.length + '/' + PERSONAS.length + ' rechecks done. Synthesizing...')

const synthesis = await agent(synthPrompt(clean), { label: 'synthesis', phase: 'Synthesis', schema: SYNTH_SCHEMA })
const audit = await agent(auditPrompt(synthesis, clean), { label: 'honesty-audit', phase: 'Synthesis', schema: AUDIT_SCHEMA })

return { founders: clean, synthesis, audit }
