export const meta = {
  name: 'lancelot-bytepath-mine',
  description: 'Comprehensive SOURCE-LEVEL mine of Robyn/FastAPI/Blazeio + AI-serving + Lancelot current-state, then 9 domain synthesizers + 1 findings-ranker → prioritized additive byte-path build list',
  phases: [
    { title: 'Mine: Robyn', detail: 'Rust src + Python bridge + teardown (closest peer)' },
    { title: 'Mine: FastAPI', detail: 'framework internals + production patterns' },
    { title: 'Mine: FastAPI projects', detail: '24 real production codebases by archetype' },
    { title: 'Mine: Blazeio', detail: 'async protocol core + production modules' },
    { title: 'Mine: serving infra', detail: 'vLLM/LitServe/BentoML/Ray/Modal/SGLang/TGI' },
    { title: 'Mine: Lancelot state', detail: 'current-state baseline so findings are additive' },
    { title: 'Synthesize', detail: '9 byte-path domains, additive gap analysis', model: 'opus' },
    { title: 'Rank', detail: 'prioritized buildable findings list', model: 'opus' },
  ],
}

const L = '/Users/seventyleven/Desktop/Lancelot/research/bytepath_mine/00_BYTEPATH_LENS.md'
const RF = '/Users/seventyleven/Desktop/researchfms'
const TD = `${RF}/teardowns`
const FA = `${RF}/fastapi`
const LC = '/Users/seventyleven/Desktop/Lancelot'
const RES = `${LC}/research/bytepath_mine`

const MINER = {
  type: 'object', additionalProperties: false,
  required: ['source', 'one_liner', 'mechanisms', 'note_path'],
  properties: {
    source: { type: 'string' },
    one_liner: { type: 'string' },
    mechanisms: {
      type: 'array', description: 'byte-path mechanisms, mechanism-level',
      items: {
        type: 'object', additionalProperties: false,
        required: ['name', 'mechanism', 'byte_path_relevance'],
        properties: {
          name: { type: 'string' },
          mechanism: { type: 'string', description: 'exact knob/constant/file/fn — not vibes' },
          byte_path_relevance: { type: 'string', description: 'why it matters for Lancelot byte path / operator exp' },
          lancelot_check_hint: { type: 'string', description: 'where to check if Lancelot already has it' },
          cite: { type: 'string' },
        },
      },
    },
    perf_or_resilience_notes: { type: 'string' },
    note_path: { type: 'string' },
  },
}

const SYNTH = {
  type: 'object', additionalProperties: false,
  required: ['domain', 'summary', 'must_build', 'have', 'refuse', 'brief_path'],
  properties: {
    domain: { type: 'string' },
    summary: { type: 'string' },
    must_build: {
      type: 'array',
      items: {
        type: 'object', additionalProperties: false,
        required: ['win', 'why', 'lancelot_state', 'buildability'],
        properties: {
          win: { type: 'string' },
          why: { type: 'string' },
          source_cite: { type: 'string' },
          lancelot_state: { type: 'string', description: 'have | partial | missing' },
          buildability: { type: 'string', description: 'S | M | L' },
          gate_risk: { type: 'string', description: 'conformance/clippy/perf risk' },
        },
      },
    },
    improve: { type: 'array', items: { type: 'string' } },
    have: { type: 'array', items: { type: 'string' }, description: 'already shipped in Lancelot — explicitly NOT to build' },
    refuse: {
      type: 'array',
      items: {
        type: 'object', additionalProperties: false,
        required: ['item', 'why'],
        properties: { item: { type: 'string' }, why: { type: 'string' } },
      },
    },
    brief_path: { type: 'string' },
  },
}

const T = (label, phase, dir, file, sources, focus) => ({ label, phase, dir, file, sources, focus })

const TARGETS = [
  // ---------------- ROBYN (closest peer: Rust+Python) ----------------
  T('robyn_rust_core', 'Mine: Robyn', '01_robyn', 'rust_core.md',
    [`${RF}/robyn/Robyn/src/lib.rs`, `${RF}/robyn/Robyn/src/server.rs`, `${RF}/robyn/Robyn/src/runtime.rs`, `${RF}/robyn/Robyn/src/asyncio.rs`, `${RF}/robyn/Robyn/src/blocking.rs`, `${RF}/robyn/Robyn/src/callbacks.rs`],
    'Read these Rust files IN FULL. How Robyn handles requests on hyper/tokio: the runtime/worker model, the asyncio↔Rust bridge (asyncio.rs — how it drives Python coroutines from Rust, the KEY comparison to Lancelot R2/R3), GIL release around handler calls (callbacks.rs — Python::allow_threads?), the blocking pool, const/prebaked routes if any. Mechanism-level: exact fns, how the GIL is held/dropped, how handlers are dispatched. This is Lancelot\'s closest architectural mirror — extract every byte-path mechanism + every divergence from Lancelot\'s approach.'),
  T('robyn_rust_routing', 'Mine: Robyn', '01_robyn', 'rust_routing.md',
    [`${RF}/robyn/Robyn/src/routers`, `${RF}/robyn/Robyn/src/types`, `${RF}/robyn/Robyn/src/conversion.rs`, `${RF}/robyn/Robyn/src/shared_socket.rs`, `${RF}/robyn/Robyn/src/websockets`, `${RF}/robyn/Robyn/src/io_helpers`, `${RF}/robyn/Robyn/src/executors`],
    'ls then read these IN FULL. Robyn\'s routing engine, Rust↔Python type conversion (conversion.rs — request/response marshaling cost), SO_REUSEPORT shared socket (shared_socket.rs — Lancelot\'s multiprocess scaling), websocket handling, io_helpers, executors. Mechanism-level: the route match algo, how request/response bytes cross the FFI boundary, the socket sharing model. Compare to Lancelot matchit + R2 lazy request.'),
  T('robyn_py_bridge', 'Mine: Robyn', '01_robyn', 'python_bridge.md',
    [`${RF}/robyn/Robyn/robyn/__init__.py`, `${RF}/robyn/Robyn/robyn/router.py`, `${RF}/robyn/Robyn/robyn/processpool.py`, `${RF}/robyn/Robyn/robyn/robyn.pyi`, `${RF}/robyn/Robyn/robyn/types.py`, `${RF}/robyn/Robyn/robyn/status_codes.py`],
    'Read IN FULL. Robyn\'s Python app API, the pyo3 surface (.pyi), the MULTIPROCESS model (processpool.py — how it spawns/manages workers; Lancelot also deploys multiprocess), router registration, the request/response Python types. Mechanism-level: the worker spawn + socket handoff, the app/run API ergonomics. Compare to lancelot.app + LANCELOT_WORKERS.'),
  T('robyn_py_dx', 'Mine: Robyn', '01_robyn', 'python_dx.md',
    [`${RF}/robyn/Robyn/robyn/dependency_injection.py`, `${RF}/robyn/Robyn/robyn/authentication.py`, `${RF}/robyn/Robyn/robyn/openapi.py`, `${RF}/robyn/Robyn/robyn/ai.py`, `${RF}/robyn/Robyn/robyn/mcp.py`, `${RF}/robyn/Robyn/robyn/pydantic_support.py`, `${RF}/robyn/Robyn/robyn/responses.py`, `${RF}/robyn/Robyn/robyn/_param_utils.py`, `${RF}/robyn/Robyn/robyn/ws.py`],
    'Read IN FULL. Robyn\'s DX layer: DI, authentication, OpenAPI gen, ai.py (their AI helper — what byte-path AI features they expose), mcp.py (MCP support), pydantic_support, response helpers, param utils, websockets. Mechanism-level. What DX/AI byte-path features Robyn ships that Lancelot lacks; what Lancelot does better.'),
  T('robyn_teardown', 'Mine: Robyn', '01_robyn', 'teardown.md', [`${TD}/ROBYN.md`],
    'Read the 187KB Robyn teardown (page/grep). The distilled RE analysis: architecture, the const-router, the GIL story, perf claims, the known weaknesses. Cross-check Lancelot\'s existing research/robyn/*.md + research/deep/04,05 (read them) to be ADDITIVE — what did prior Lancelot mining capture, and what did it MISS? Surface the gaps.'),

  // ---------------- FASTAPI (the parity target + internals) ----------------
  T('fastapi_core', 'Mine: FastAPI', '02_fastapi', 'core.md',
    [`${FA}/fastapi/fastapi/applications.py`, `${FA}/fastapi/fastapi/routing.py`, `${FA}/fastapi/fastapi/datastructures.py`, `${FA}/fastapi/fastapi/requests.py`, `${FA}/fastapi/fastapi/responses.py`, `${FA}/fastapi/fastapi/concurrency.py`, `${FA}/fastapi/fastapi/background.py`],
    'Read the FastAPI framework source IN FULL. App + routing + request lifecycle, the route handler wrapper (get_request_handler — the hot path), datastructures, response classes, the run_in_threadpool concurrency seam, background tasks. Mechanism-level. Lancelot targets 99.6% byte-parity — find lifecycle/response behaviors Lancelot may still diverge on or could absorb.'),
  T('fastapi_di_params', 'Mine: FastAPI', '02_fastapi', 'di_params.md',
    [`${FA}/fastapi/fastapi/dependencies`, `${FA}/fastapi/fastapi/params.py`, `${FA}/fastapi/fastapi/param_functions.py`, `${FA}/fastapi/fastapi/_compat.py`, `${FA}/fastapi/fastapi/encoders.py`],
    'ls dependencies/ then read IN FULL. The DI system (solve_dependencies, the dependency cache, yield-deps + AsyncExitStack), params, pydantic v1/v2 compat shim (_compat.py), and jsonable_encoder (encoders.py — the serialization fallback). Mechanism-level. Lancelot is pydantic-only + Rust-serialize — what FastAPI DI/encoder behaviors matter for parity and which to absorb vs refuse.'),
  T('fastapi_openapi_sec', 'Mine: FastAPI', '02_fastapi', 'openapi_security.md',
    [`${FA}/fastapi/fastapi/openapi`, `${FA}/fastapi/fastapi/security`, `${FA}/fastapi/fastapi/exceptions.py`, `${FA}/fastapi/fastapi/exception_handlers.py`, `${FA}/fastapi/fastapi/middleware`],
    'ls then read IN FULL. OpenAPI schema generation, the security utilities (OAuth2/APIKey/HTTP schemes), exception handling, middleware shims. Mechanism-level. What of FastAPI\'s OpenAPI/security surface Lancelot should match for drop-in adoption (DX/conformance), what is bloat.'),
  T('fastapi_patterns', 'Mine: FastAPI', '02_fastapi', 'production_patterns.md',
    [`${RF}/core/FASTAPI_PRODUCTION_PATTERNS.md`, `${FA}/patterns/VALIDATION_FINDINGS.md`, `${FA}/patterns/_patterns_draft.md`, `${TD}/FASTAPI.md`, `${FA}/INDEX.md`],
    'Read these synthesis/teardown docs. The cross-project FastAPI production patterns + validation findings (the blocking-in-async hazard, the real anti-patterns) + the FastAPI teardown. Cross-check Lancelot research/fastapi/*.md + research/deep/01,02,03 (read them) — be ADDITIVE. Surface production byte-path patterns Lancelot should support/optimize that it doesn\'t yet.'),

  // ---------------- FASTAPI PRODUCTION PROJECTS (real byte-path usage, by archetype) ----------------
  T('faproj_serving', 'Mine: FastAPI projects', '02_fastapi', 'proj_serving.md',
    [`${FA}/code/litserve`, `${FA}/code/bentoml`, `${FA}/code/ray_serve`, `${FA}/code/modal`, `${TD}/LITSERVE.md`, `${TD}/BENTOML.md`, `${TD}/RAY_SERVE.md`, `${TD}/MODAL.md`],
    'ls each code dir (read _INDEX.md + key files + deep/), and the teardowns. The ML/LLM SERVING archetype: LitServe (mp-queue broker + dynamic batching + ZMQ response routing), BentoML (CorkDispatcher LEARNED adaptive batching, create_model-from-signature, make_safe_connect SSRF guard), Ray Serve (@serve.batch, Power-of-Two-Choices router, ASGI-over-actors), Modal (ASGI-over-gRPC, WS bridge, lifespan ClientSession). Mechanism-level. THESE ARE LANCELOT\'S TARGET USERS — what byte-path serving features (batching beyond @app.batch, request collectors, broker patterns) should Lancelot offer?'),
  T('faproj_inference', 'Mine: FastAPI projects', '02_fastapi', 'proj_inference.md',
    [`${FA}/code/vllm`, `${TD}/VLLM.md`],
    'ls vllm code dir (read _INDEX.md + openai/ + launcher.py + utils.py + deep/) + the VLLM teardown. The inference-server archetype: the OpenAI-compatible server, @with_cancellation dual-task race (request.is_disconnected broken under ASGI middleware — the cancellation pattern), RequestOutputCollector, mid-stream error chunks (finish_reason:error on HTTP 200), the watchdog. Mechanism-level. Lancelot has R3 cancel + C2 — what does vLLM do that Lancelot should absorb for AI streaming/cancellation correctness?'),
  T('faproj_dbweb', 'Mine: FastAPI projects', '02_fastapi', 'proj_dbweb.md',
    [`${FA}/code/full-stack-fastapi-template`, `${FA}/code/dispatch`, `${FA}/code/polar`, `${FA}/code/prefect`, `${FA}/code/bitcart`, `${TD}/FULL_STACK_FASTAPI_TEMPLATE.md`, `${TD}/POLAR.md`, `${TD}/PREFECT.md`],
    'ls the code dirs (read _INDEX.md + key route/DI files) + teardowns. The DB-backed web-app archetype: custom APIRoute classes (PrefectAPIRoute response-scoped AsyncExitStack, Polar AutoCommitAPIRoute, the route-class extension pattern), DI patterns (CommonParameters, DBInjector, version-gate DI), tx-boundary-via-generator. Mechanism-level. Lancelot has custom APIRoute (M2-push4) — what route-class/DI/tx patterns from real monoliths should it support?'),
  T('faproj_auth_obs_ai', 'Mine: FastAPI projects', '02_fastapi', 'proj_auth_obs_ai.md',
    [`${FA}/code/fastapi-users`, `${FA}/code/sentry_fastapi`, `${FA}/code/opal`, `${FA}/code/langserve`, `${FA}/code/r2r`, `${FA}/code/hayhooks`, `${TD}/FASTAPI_USERS.md`, `${TD}/SENTRY_FASTAPI.md`, `${TD}/LANGSERVE.md`],
    'ls the code dirs + teardowns. Three lenses: AUTH (fastapi-users: transport×strategy auth, makefun-synthesized security deps), OBSERVABILITY (sentry_fastapi: how the SDK instruments via monkeypatching, two-phase tx naming — what hooks Lancelot must expose for instrumentation), AI-APP serving (langserve/r2r/hayhooks: how AI apps stream over FastAPI, the /invoke /stream /batch shapes). Mechanism-level. What auth/observability/AI-serving byte-path seams should Lancelot ship?'),

  // ---------------- BLAZEIO (async-Python, perf-focused) ----------------
  T('blazeio_core', 'Mine: Blazeio', '03_blazeio', 'core.md',
    [`${RF}/blazeio/Blazeio/Blazeio/__init__.py`, `${RF}/blazeio/Blazeio/Blazeio/Parsers/min_http_parser.py`, `${RF}/blazeio/Blazeio/Blazeio/Utils/socket_ops.py`, `${RF}/blazeio/Blazeio/Blazeio/Dependencies/__init__.py`, `${RF}/blazeio/Blazeio/Blazeio/Dependencies/alts.py`],
    'ls the Blazeio package then read these IN FULL. The async server/protocol core, the minimal HTTP parser (min_http_parser.py — their hand-rolled parse, the perf trick), socket ops, the dependency/alts layer. Mechanism-level: how Blazeio achieves its ~59k tiny-response number in pure Python (the parser, the buffer strategy, zero-copy tricks, the event loop usage). Lancelot ties Blazeio on tiny — what async tricks could close the gap to raw Starlette+uvloop or improve large-payload?'),
  T('blazeio_modules', 'Mine: Blazeio', '03_blazeio', 'modules.md',
    [`${RF}/blazeio/Blazeio/Blazeio/Modules/server_utilities.py`, `${RF}/blazeio/Blazeio/Blazeio/Modules/safeguards.py`, `${RF}/blazeio/Blazeio/Blazeio/Modules/reasons.py`, `${RF}/blazeio/Blazeio/Blazeio/Other/proxy.py`, `${RF}/blazeio/Blazeio/Blazeio/Other/asynctemplate.py`, `${TD}/BLAZEIO.md`],
    'Read IN FULL (+ the BLAZEIO teardown 67KB, page it). Blazeio production modules: server_utilities, safeguards (their resilience — slow-loris/caps/timeouts), reasons (status), the proxy (streaming relay — compare to lancelot.llm.relay), async templating. Cross-check Lancelot research/blazeio/*.md + research/deep/06 (read them) — be ADDITIVE. Mechanism-level: streaming relay + safeguards Lancelot should absorb.'),

  // ---------------- SERVING / INFERENCE INFRA (byte-path through the AI lens) ----------------
  T('serving_frontier', 'Mine: serving infra', '04_serving', 'serving_frontier.md',
    [`${TD}/SGLANG.md`, `${TD}/TGI.md`, `${TD}/RAY_SERVE.md`, `${TD}/GROQ.md`],
    'Read these teardowns (page large ones). The inference-serving frontier: continuous/dynamic batching, KV-cache + prefix sharing, request scheduling, the streaming-cancel + watchdog patterns, three-state health. Mechanism-level. Lancelot owns the byte path for AI serving — which serving-grade patterns (adaptive batching, prefix-aware scheduling, health states) translate into Lancelot byte-path features WITHOUT becoming a model server (stay user-owns-the-model)?'),
  T('serving_gateways_deploy', 'Mine: serving infra', '04_serving', 'gateways_deploy.md',
    [`${TD}/LITELLM.md`, `${TD}/PORTKEY.md`, `${TD}/KONG_AI_GATEWAY.md`, `${TD}/RAILWAY.md`, `${TD}/RENDER.md`],
    'Read these teardowns (page large). OUTBOUND model gateways (LiteLLM/Portkey/Kong — failover, caching, the 4-method Config, cost) — what does lancelot.llm.TransportPool already do vs these, and what thin additions matter. DEPLOYMENT (Railway/Render — container/cloud byte-path: cold start, health, secrets, multi-region). Cross-check Lancelot research/external/*.md (read them). Mechanism-level, additive.'),

  // ---------------- LANCELOT CURRENT-STATE BASELINE (so synthesis is additive) ----------------
  T('lc_state_core', 'Mine: Lancelot state', '05_lancelot_state', 'core.md',
    [`${LC}/lancelot/app.py`, `${LC}/lancelot/asgi.py`, `${LC}/lancelot/routing.py`, `${LC}/lancelot/requests.py`, `${LC}/lancelot/responses.py`, `${LC}/lancelot/params.py`, `${LC}/lancelot/di.py`, `${LC}/lancelot/validation.py`, `${LC}/lancelot/middleware.py`, `${LC}/lancelot/openapi.py`, `${LC}/lancelot/docs.py`],
    'Read Lancelot\'s OWN source IN FULL. Inventory EXACTLY what Lancelot has for routing/app/request/response/DI/params/validation/middleware/openapi. This is the BASELINE the synthesizers diff peer findings against. Be precise: list the public API + the mechanisms already implemented + any TODOs/limitations in comments. Output a clean "Lancelot HAS / Lancelot LACKS" inventory for the core HTTP path.'),
  T('lc_state_bytepath', 'Mine: Lancelot state', '05_lancelot_state', 'bytepath.md',
    [`${LC}/lancelot/stream.py`, `${LC}/lancelot/batch.py`, `${LC}/lancelot/canonical.py`, `${LC}/lancelot/compress.py`, `${LC}/lancelot/concurrency.py`, `${LC}/lancelot/pool.py`, `${LC}/lancelot/background.py`, `${LC}/lancelot/llm.py`, `${LC}/lancelot/upstream_metrics.py`, `${LC}/lancelot/context.py`],
    'Read Lancelot\'s OWN source IN FULL. Inventory the byte-path/AI primitives: streaming (R3 pump), batch coalescing, canonical JSON, compression, concurrency/gather, pool, background tasks, TransportPool (outbound), upstream_metrics, context ContextVars. Precise "HAS / LACKS" inventory for streaming/serialization/concurrency/AI-transport. Note the perf envelope claims from CLAUDE.md.'),
  T('lc_state_resilience', 'Mine: Lancelot state', '05_lancelot_state', 'resilience.md',
    [`${LC}/lancelot/security.py`, `${LC}/lancelot/auth.py`, `${LC}/lancelot/net.py`, `${LC}/lancelot/secrets.py`, `${LC}/lancelot/tenant.py`, `${LC}/lancelot/ratelimit.py`, `${LC}/lancelot/fleet.py`, `${LC}/lancelot/status.py`, `${LC}/lancelot/settings.py`, `${LC}/lancelot/metrics.py`, `${LC}/lancelot/telemetry.py`, `${LC}/lancelot/websockets.py`, `${LC}/lancelot/staticfiles.py`, `${LC}/lancelot/uploads.py`],
    'Read Lancelot\'s OWN source IN FULL. Inventory resilience/cloud/observability/DX: security, auth, net (SSRF), secrets, tenant, ratelimit, fleet, status (/livez/readyz), settings, metrics (RED), telemetry, websockets, staticfiles, uploads. Precise "HAS / LACKS" inventory. Note operator knobs (env vars).'),
  T('lc_state_research_plans', 'Mine: Lancelot state', '05_lancelot_state', 'research_plans_ledger.md',
    [`${LC}/CLAUDE.md`, `${LC}/research/deep/07-stability-failure-mode-catalog.md`, `${LC}/research/deep/09-ultimate-buildable-wbs.md`, `${LC}/research/design/03-versatility-competitive-surface.md`, `${LC}/docs/plans/2026-06-02-byte-path-build-order-from-founders-and-patterns.md`, `${LC}/docs/plans/2026-05-31-deferred-candidates-panel.md`, `${LC}/docs/plans/M5-PLAN-PROVE-VS-PEERS-AND-ADOPTABLE.md`, `${LC}/docs/plans/M6-PLAN-SPEED-STABILITY-ADOPTION.md`],
    'Read these IN FULL. Build the "already mined / already built / already DEFERRED / explicitly REFUSED" ledger from Lancelot\'s CLAUDE.md (the Tier-D do-not-do list, the milestone state, the perf envelope) + the prior research notes + the build-order/deferred-candidates plans. This is the GUARDRAIL: the synthesizers must not propose anything on the Tier-D refuse list or already-built. Output: a clear DEFERRED-list + REFUSED-list + a pointer to what M1-M19 already shipped.'),
]

// ---------------- domain synthesizers ----------------
const SL = (label, file, focus) => ({ label, file, focus })
const LAYERS = [
  SL('routing_request', 'routing_request.md', 'Routing engine + request/response lifecycle + param extraction + const tier + lazy request. Diff Robyn (matchit/router, FFI marshaling) + FastAPI (route handler hot path, lifecycle) vs Lancelot (matchit, R2 lazy request, const tier).'),
  SL('serialization', 'serialization.md', 'Serialization: pydantic-core to_json, canonical/cache-stable JSON, FastAPI jsonable_encoder fallback, large-payload, streaming JSON. Diff peers vs Lancelot (pydantic-core, canonical). The crossover + the encoder-parity surface.'),
  SL('streaming', 'streaming.md', 'Streaming: the R3 pump, SSE/WS, backpressure, client-disconnect→cancel, TTFT, AI token streaming, mid-stream error chunks, request collectors, the @with_cancellation race. Diff vLLM/LitServe/Blazeio-relay vs Lancelot R3+C2.'),
  SL('concurrency_workers', 'concurrency_workers.md', 'Concurrency/workers/GIL: the asyncio↔Rust bridge, GIL release discipline, multiprocess + SO_REUSEPORT, blocking pool, tokio runtime. Diff Robyn (asyncio.rs/callbacks.rs/processpool.py) + Blazeio (async core) vs Lancelot (uvloop, LANCELOT_WORKERS, anti-scales-with-threads). The residual-FFI-cost gap to raw Starlette.'),
  SL('resilience', 'resilience.md', 'Resilience/cloud-native: body/conn caps, timeouts, graceful drain, health states, slow-loris, idempotency, backpressure, SSRF, rate-limit, tenant isolation. Diff Blazeio safeguards + serving watchdogs + FastAPI prod patterns vs Lancelot (caps/drain/livez/readyz/net/tenant/ratelimit). Validate-under-load mandate.'),
  SL('observability', 'observability.md', 'Observability: RED metrics, streaming TTFT, traceparent propagation, cost accounting, request-id, OTel, the instrumentation seams (how Sentry monkeypatches — what HOOKS Lancelot must expose). Diff vs Lancelot metrics/telemetry/upstream_metrics.'),
  SL('ai_bytepath', 'ai_bytepath.md', 'The AI byte-path positioning wins: TransportPool (outbound failover/cost/cache), @app.batch + serving-grade adaptive/dynamic batching (BentoML CorkDispatcher, LitServe broker), prefix-aware scheduling, MCP, prompt-cache-stable serialization. Diff serving infra + gateways vs Lancelot AI primitives. STAY user-owns-the-model (no model server).'),
  SL('dx_conformance', 'dx_conformance.md', 'DX + conformance + adoption: FastAPI byte-parity gaps, OpenAPI/security surface, DI/route-class ergonomics (custom APIRoute patterns from Prefect/Polar), error shapes, testclient, drop-in migration. Diff FastAPI internals + prod projects vs Lancelot (99.6% parity, custom APIRoute). What closes adoption friction.'),
  SL('deployment', 'deployment.md', 'Deployment/packaging: multiprocess model, wheels/abi3, container cold-start, multi-region, cloud platforms (Railway/Render/Modal), secrets, config-from-env. Diff Robyn processpool + deployment teardowns vs Lancelot (LANCELOT_WORKERS, wheels CI, deploy.md). The operator experience.'),
]

// ============ run ============
log(`Byte-path mine: ${TARGETS.length} source-level miners (Sonnet) → ${LAYERS.length} domain synthesizers + ranker (Opus).`)

const minePrompt = (t) => `You are mining ONE source cluster for the Lancelot BYTE-PATH harvest — comprehensive, SOURCE-LEVEL, mechanism-level.

STEP 1 — read the lens (mission, Lancelot positioning, cardinal rules, quality bar, additive mandate): ${L}
STEP 2 — read the source(s) IN FULL. For directories: \`ls\` first, then read the key files completely; grep huge files to the load-bearing parts: ${t.sources.join('  ·  ')}
STEP 3 — FOCUS: ${t.focus}

Extract every byte-path mechanism at the level of the exact knob / constant / file / function — never vibes. Cite (file:line + section). Be honest: real byte-path win vs micro-opt vs non-bottleneck. Where you can, note where Lancelot would check whether it already has this (lancelot_check_hint).

STEP 4 — WRITE your mechanism-level cited note to: ${t.file ? `${RES}/${t.dir}/${t.file}` : `${RES}/${t.dir}/note.md`}
STEP 5 — RETURN the structured digest.

You have FULL tool access (Read, Grep, Bash). "Research output" restricts only your reply FORMAT, not your inputs — READ THE FULL SOURCE; do not guess.`

const mined = await parallel(TARGETS.map(t => () =>
  agent(minePrompt(t), { label: t.label, phase: t.phase, schema: MINER, model: 'sonnet' })
))
const okMined = mined.filter(Boolean)
log(`Mined ${okMined.length}/${TARGETS.length} notes. Synthesizing ${LAYERS.length} byte-path domains (additive gap analysis).`)

const synthPrompt = (l) => `You are the **${l.label}** byte-path domain synthesizer for Lancelot ("just build, brutal" — no overkill, every win must change the byte path or operator experience).

STEP 1 — read the lens: ${L}
STEP 2 — read the FULL mine + the Lancelot current-state baseline in one shot:
   cat ${RES}/01_robyn/*.md ${RES}/02_fastapi/*.md ${RES}/03_blazeio/*.md ${RES}/04_serving/*.md ${RES}/05_lancelot_state/*.md
   (05_lancelot_state/ is Lancelot's HAVE/LACKS baseline + the DEFERRED/REFUSED ledger — diff against it.)
STEP 3 — synthesize the **${l.label}** domain: ${l.focus}

Additive gap analysis: for each peer mechanism, classify Lancelot's state (have / partial / missing). Surface ONLY new or improvable wins (must_build / improve). Tag already-shipped ones as HAVE so nothing is redundant. RESPECT the Tier-D refuse list + the cardinal rules (pydantic-only runtime; validate-under-load; don't oversell throughput) — anything that violates them goes in \`refuse\`. Every win: mechanism + source cite + Lancelot state + buildability (S/M/L) + gate_risk (conformance/clippy/perf). Include a concrete recommended approach.

STEP 4 — WRITE the domain brief to: ${RES}/10_synthesis/${l.file}
STEP 5 — RETURN the structured summary.

Full tool access; output-format restriction only. Read the notes + diff the Lancelot baseline; do not guess.`

const synthed = await parallel(LAYERS.map(l => () =>
  agent(synthPrompt(l), { label: `synth:${l.label}`, phase: 'Synthesize', schema: SYNTH })
))
const okSynth = synthed.filter(Boolean)
log(`Synthesized ${okSynth.length}/${LAYERS.length} domains. Ranking into a prioritized buildable list.`)

const rankPrompt = `You are the FINDINGS RANKER for the Lancelot byte-path mine. Produce the single prioritized, ADDITIVE, BUILDABLE findings list that a build plan will be written from.

STEP 1 — read the lens: ${L}
STEP 2 — read ALL domain briefs + the Lancelot baseline:
   cat ${RES}/10_synthesis/*.md ${RES}/05_lancelot_state/*.md
STEP 3 — collate every \`must_build\` / \`improve\` win across all 9 domains. DEDUPE. DROP anything Lancelot already HAS or that's on the Tier-D refuse list (cardinal-rule violation). For each surviving win, score: IMPACT (byte-path/operator value, weighted toward the AI/cost-effective/streaming/resilience positioning), BUILDABILITY (S/M/L), GATE_RISK (conformance floor 974 / clippy / perf-regression / pydantic-only-runtime), and CONFIDENCE (is it grounded in a real peer mechanism + a real Lancelot gap?).
STEP 4 — produce a RANKED table (highest impact×buildability÷risk first), grouped into tiers: T1 (build-now: high impact, S/M, low risk), T2 (strong, needs validation), T3 (speculative / large / deferred). Each row: win · domain · mechanism (1 line) · source cite · Lancelot gap · size · gate-risk · why-it-matters. End with an explicit "REFUSED / already-HAVE" appendix so the build plan doesn't relitigate them.

STEP 5 — WRITE the ranked findings to: ${RES}/FINDINGS.md
STEP 6 — RETURN: the T1 list (the build-now candidates) as structured items, + counts per tier.

Full tool access; read everything; do not guess. Be brutally honest about what's actually worth building vs padding.`

const ranking = await agent(rankPrompt, { label: 'findings-ranker', phase: 'Rank' })
log('Ranked findings written to FINDINGS.md.')

return {
  mined: okMined.length, mined_total: TARGETS.length,
  synthesized: okSynth.length, synth_total: LAYERS.length,
  domain_summaries: okSynth.map(s => ({ domain: s.domain, summary: s.summary, must_build_count: (s.must_build || []).length, have: s.have, refuse: (s.refuse || []).map(r => r.item) })),
  findings_path: `${RES}/FINDINGS.md`,
  ranking_return: ranking,
}
