export const meta = {
  name: 'lancelot-agents-harvest',
  description: 'Fresh deep-mine of agent frameworks + production agents + infra + core transcripts + BAD_GUIDE, Lancelot-byte-path-lensed, then 12 layer-synthesizers',
  phases: [
    { title: 'Mine: frameworks', detail: 'one agent per agent-framework teardown' },
    { title: 'Mine: production agents', detail: 'coding/vertical/browser agents — real mechanisms' },
    { title: 'Mine: research agents', detail: 'deep-research / perplexity / nia / grok / retrieval' },
    { title: 'Mine: infra', detail: 'gateways / memory / evals / durable / serving' },
    { title: 'Mine: core+guide', detail: 'YC/a16z/DLAI/Stanford transcripts + arch docs + BAD_GUIDE' },
    { title: 'Mine: byte-path', detail: 'Lancelot existing code inventory' },
    { title: 'Synthesize', detail: '12 layer briefs: loop/context/model/tools/memory/evals/stream/durable/safety/orchestration/positioning/byte-path', model: 'opus' },
  ],
}

const LENS = '/Users/seventyleven/Desktop/Lancelot/research/00_LENS.md'
const TD = '/Users/seventyleven/Desktop/researchfms/teardowns'
const CORE = '/Users/seventyleven/Desktop/researchfms/core'
const GUIDE = '/Users/seventyleven/Desktop/guidesfm/BAD_GUIDE.md'
const LC = '/Users/seventyleven/Desktop/Lancelot/lancelot'
const RES = '/Users/seventyleven/Desktop/Lancelot/research'

const MINER = {
  type: 'object',
  additionalProperties: false,
  required: ['source', 'one_liner', 'copy', 'avoid', 'byte_path_implication', 'note_path'],
  properties: {
    source: { type: 'string' },
    one_liner: { type: 'string', description: 'what this source is, one line' },
    copy: {
      type: 'array', description: 'best primitives to copy, mechanism-level',
      items: {
        type: 'object', additionalProperties: false,
        required: ['primitive', 'mechanism', 'why'],
        properties: {
          primitive: { type: 'string' },
          mechanism: { type: 'string', description: 'exact knob/constant/file/prompt — not vibes' },
          why: { type: 'string' },
          cite: { type: 'string' },
        },
      },
    },
    avoid: {
      type: 'array', description: 'anti-patterns to refuse',
      items: {
        type: 'object', additionalProperties: false,
        required: ['antipattern', 'why'],
        properties: { antipattern: { type: 'string' }, why: { type: 'string' } },
      },
    },
    byte_path_implication: { type: 'string', description: 'does Lancelot byte path provide/change/obviate this?' },
    api_shape_notes: { type: 'string' },
    note_path: { type: 'string' },
  },
}

const SYNTH = {
  type: 'object',
  additionalProperties: false,
  required: ['layer', 'decision', 'must_build', 'reuse_byte_path', 'refuse', 'brief_path'],
  properties: {
    layer: { type: 'string' },
    decision: { type: 'string', description: 'what Lancelot should do for this layer, one paragraph' },
    must_build: { type: 'array', items: { type: 'string' } },
    reuse_byte_path: { type: 'array', items: { type: 'string' } },
    refuse: { type: 'array', items: { type: 'string' } },
    open_questions: { type: 'array', items: { type: 'string' } },
    brief_path: { type: 'string' },
  },
}

// ---- miner targets: {label, phase, dir, file, sources:[abs], focus} ----
const T = (label, phase, dir, file, sources, focus) => ({ label, phase, dir, file, sources, focus })
const td = (...names) => names.map(n => `${TD}/${n}`)
const core = (...names) => names.map(n => `${CORE}/${n}`)

const FRAMEWORKS = 'Mine: frameworks'
const PROD = 'Mine: production agents'
const RSCH = 'Mine: research agents'
const INFRA = 'Mine: infra'
const COREP = 'Mine: core+guide'
const BP = 'Mine: byte-path'

const TARGETS = [
  // ---------- agent frameworks (the user's core "similar projects" ask) ----------
  T('pydantic_ai', FRAMEWORKS, '03_teardowns', 'pydantic_ai.md', td('PYDANTIC_AI.md'),
    'THE closest comparable — Lancelot is pydantic-core-native. Agent/Tool/RunContext API shape, typed deps+output, model-agnostic init, output validators/retries, structured-output path, streaming, instrumentation. What to copy 1:1, what is bloat.'),
  T('langgraph', FRAMEWORKS, '03_teardowns', 'langgraph.md', td('LANGGRAPH.md', 'LANGSERVE.md'),
    'The graph tax to counter-position against: Pregel/BSP, channels+reducers, DEFAULT_RECURSION_LIMIT, checkpointer+store, durability modes, interrupt/Command HITL, prebuilt create_react_agent. What is genuinely good (durable checkpoint, remaining_steps) vs the graph-as-default trap.'),
  T('langchain', FRAMEWORKS, '03_teardowns', 'langchain.md', td('LANGCHAIN.md'),
    'LCEL/Runnable indirection (anti-pattern), init_chat_model provider abstraction, @tool error policy, the v0→v1 rewrite lesson, memory class proliferation. Copy the provider abstraction shape; refuse the universal-composition tax.'),
  T('togetherai', FRAMEWORKS, '03_teardowns', 'togetherai.md', td('TOGETHERAI.md'),
    'Model gateway + managed evals + inference. OpenAI-compatible surface, fine-tune/eval API, batched inference, cost levers. How a provider/gateway shapes the model layer; what Lancelot TransportPool already does vs Together.'),
  T('crewai', FRAMEWORKS, '03_teardowns', 'crewai.md', td('CREWAI.md', 'CREWAI_PLUS.md'),
    'Role-play agent prompt build, dual native-tools/ReAct-text loop, ToolUsage 4-stage input-repair ladder + fuzzy name fix (0.85), repeated-call short-circuit, unified memory recall weights. Copy the repair ladder; refuse two-execution-models + fake streaming + raise-on-config.'),
  T('autogen', FRAMEWORKS, '03_teardowns', 'autogen.md', td('AUTOGEN.md', 'MS_AGENT_FRAMEWORK.md'),
    'Conversable agents, group chat, the new MS Agent Framework (AutoGen+SemanticKernel merge). Multi-agent conversation patterns, termination, the round-robin/selector. What converges with the durable orchestration thesis.'),
  T('agno', FRAMEWORKS, '03_teardowns', 'agno.md', td('AGNO.md'),
    'Performance-claimed agent framework (fast instantiation, low memory). Agent/Team/Workflow levels, storage/memory/knowledge, the perf claims — are they real and do they matter (Lancelot byte path lens)?'),
  T('openai_agents', FRAMEWORKS, '03_teardowns', 'openai_agents.md', td('OPENAI_AGENTS_SDK.md', 'OPENAI_SWARM.md', 'OPENAI_OPERATOR.md'),
    'Agents SDK: handoffs, guardrails (input/output), sessions, the Runner loop, tracing. Swarm: the minimal handoff primitive. Operator: computer-use loop. Copy handoffs-as-tools + guardrails shape.'),
  T('claude_agent_sdk', FRAMEWORKS, '03_teardowns', 'claude_agent_sdk.md', td('CLAUDE_AGENT_SDK.md'),
    'Subprocess-delegated loop (anti-pattern for a general framework), but the hooks (5-stage), permission modes, MCP-in-process, session/transcript model are gold. Copy hooks+permissions; refuse opaque-binary loop + single-vendor.'),
  T('smolagents', FRAMEWORKS, '03_teardowns', 'smolagents.md', td('SMOLAGENTS.md'),
    'Code-as-action agents (the model writes Python that calls tools). CodeAgent vs ToolCallingAgent, the sandbox, minimal core. When code-action beats JSON-tool-calls; the security boundary.'),
  T('llamaindex', FRAMEWORKS, '03_teardowns', 'llamaindex.md', td('LLAMAINDEX.md'),
    'Workflows (event-driven steps), AgentWorkflow, FunctionTool, query engines, the return_direct + ToolOutput(is_error) pattern. Copy the tool error capture + workflow event model; assess workflow-vs-loop.'),
  T('dspy', FRAMEWORKS, '03_teardowns', 'dspy.md', td('DSPY.md'),
    'Declarative Signatures + Modules + compiled/optimized prompts (MIPRO/BootstrapFewShot). The "programming not prompting" thesis. Is the optimizer worth it for a lean framework, or off-thesis (depreciating tactical)?'),
  T('mastra', FRAMEWORKS, '03_teardowns', 'mastra.md', td('MASTRA.md'),
    'TS agent framework: agents, workflows (suspend/resume), memory (working+semantic), evals, RAG. The suspend/resume durable shape + evals-in-the-box. Copy the durable workflow ergonomics; map to Lancelot.'),
  T('vercel_ai_sdk', FRAMEWORKS, '03_teardowns', 'vercel_ai_sdk.md', td('VERCEL_AI_SDK.md', 'V0_VERCEL.md'),
    'generateText/streamText/generateObject, tool-calling loop (maxSteps/stopWhen), the streaming protocol + UI message parts, provider registry. Best-in-class streaming/tool ergonomics. Copy the stopWhen + structured stream-part model.'),
  T('google_adk', FRAMEWORKS, '03_teardowns', 'google_adk.md', [...td('GOOGLE_ADK.md'), `${TD}/ADK_RE_NOTES/01_PYTHON_CORE.md`, `${TD}/ADK_RE_NOTES/02_MULTI_AGENT.md`, `${TD}/ADK_RE_NOTES/03_TOOLS.md`, `${TD}/ADK_RE_NOTES/04_RUNTIME_STREAMING_SESSIONS.md`, `${TD}/ADK_RE_NOTES/05_MODELS_EVAL_DEPLOY.md`],
    'Google Agent Dev Kit: Agent/Runner/Session/Event, runtime+streaming (bidi), tools, multi-agent transfer, eval, deploy. The Runner/Event/Session model + bidi streaming. Copy the event-sourced session model; map to Lancelot stream.'),
  T('semantic_kernel', FRAMEWORKS, '03_teardowns', 'semantic_kernel.md', td('SEMANTIC_KERNEL.md'),
    'Plugins/functions, planners, the kernel abstraction, filters (the hook analogue), connectors. What the planner cult gets wrong; the filter pipeline. Copy filters; assess planners as tactical.'),
  T('letta', FRAMEWORKS, '03_teardowns', 'letta.md', td('LETTA.md'),
    'Virtual context (LLM edits own memory via tools), core/recall/archival tiers, tool-rule constraint solver (Init/Terminal/Continue/Child/Approval), shared memory blocks, sleeptime consolidation, GBNF grammars. Copy the tool-rule solver + memory-as-tools + page-table metadata; refuse the 2134-line monolith + mandatory Postgres.'),
  T('multiagent_fw', FRAMEWORKS, '03_teardowns', 'multiagent_frameworks.md', td('AGENCY_SWARM.md', 'CAMEL_AI.md', 'METAGPT.md', 'MAGENTIC_ONE.md', 'TASKWEAVER.md'),
    'Multi-agent orchestration frameworks. Communication topologies, role assignment, termination, shared state vs message passing. Extract the GENUINELY useful orchestration primitives (orchestrator-workers, shared typed state) vs role-play theater.'),
  T('strands', FRAMEWORKS, '03_teardowns', 'strands.md', td('STRANDS.md'),
    'AWS Strands: model-driven agent loop, the stream normalizer (strip blank text, coerce end_turn-with-tool, classify mid-stream finish_reason:error), hooks, MCP. Copy the stream normalizer + model-driven loop.'),
  T('lowcode_fw', FRAMEWORKS, '03_teardowns', 'lowcode_frameworks.md', td('DIFY.md', 'LANGFLOW.md', 'OKIBI.md'),
    'Visual/low-code agent builders (graph-DSL UIs). The graph-as-product trap — what they bury, why visual graphs anti-scale for real agents. Mostly an anti-pattern note: why Lancelot must NOT be this.'),
  T('hermes_core', FRAMEWORKS, '03_teardowns', 'hermes_core.md', td('HERMES.md', 'HERMES_DEEP_AGENT_LAYER.md'),
    'The agent-core reference. error_classifier 22-reason FailoverReason enum (4-way switch), memory-freeze invariant, 10-layer cached prompt, AuxiliaryClient, two-tier self-improvement, ship-0-builtin-tools. Copy the error classifier + memory freeze + aux client; refuse the 3600-line loop + 60-param constructor.'),
  T('hermes_gateway', FRAMEWORKS, '03_teardowns', 'hermes_gateway.md', td('HERMES_DEEP_GATEWAY.md', 'HERMES_DEEP_PROVIDERS.md', 'HERMES_DEEP_ACP.md'),
    'Hermes provider/gateway layer: ProviderProfile dataclass + 5 polymorphic hooks + OMIT three-state sentinel, plugin discovery, the gateway/ACP. Copy ProviderProfile+OMIT — the model-adapter shape that sits ON Lancelot TransportPool.'),
  T('mistral_agents', FRAMEWORKS, '03_teardowns', 'mistral_agents.md', td('MISTRAL_AGENTS_API.md', 'MISTRAL_FORGE.md', 'MISTRAL_FORGE_FUNCTIONS.md'),
    'Mistral Agents API + Forge: server-side agent API, connectors, function-calling, handoffs. The hosted-agent API shape; structured function calling. What a provider-native agent API does that Lancelot could expose over its byte path.'),

  // ---------- production agents (real mechanisms in the wild) ----------
  T('claude_code', PROD, '03_teardowns', 'claude_code.md', td('CLAUDE_CODE.md'),
    'Reference coding agent. __SYSTEM_PROMPT_DYNAMIC_BOUNDARY__ static/dynamic cache split (1h/5m), 12 TerminalReason values, getContextUsage per-category, ToolSearch deferred MCP, hooks, permission modes, todo/compaction. Copy the cache boundary + stop reasons + context introspection + deferred tools.'),
  T('codex', PROD, '03_teardowns', 'codex.md', td('CODEX.md', 'CODEX_DESKTOP.md'),
    'Codex CLI/desktop. No-max-iterations loop (token-budget + cancel only), auto-compact at 95%, AGENTS.md walk (MAX_SCAN_DEPTH=3), Code Mode isolate, ToolSearchHandler, parallel tool RwLock. Copy the budget-only loop + compaction trigger + AGENTS.md.'),
  T('cursor_zed', PROD, '03_teardowns', 'cursor_zed.md', td('CURSOR.md', 'ZED.md'),
    'Prompt assembly (Handlebars-conditional on active tools), MCP Tool Search 77K→8.7K tokens, fetch_rules pull-mode, apply-model. Copy conditional prompt assembly + pull-mode rules + tool-search token win.'),
  T('devin_warp', PROD, '03_teardowns', 'devin_warp.md', td('DEVIN.md', 'WARP.md'),
    'Cross-model failover (Sonnet→Gemini→GPT, "same-model retry repeats failures"), in-place compaction (window freed not expanded, structured payload not LLM discretion), think with 13 enumerated triggers, capability-bitmap prompt, planning gate. Copy failover + compaction + think triggers.'),
  T('antigravity', PROD, '03_teardowns', 'antigravity.md', td('ANTIGRAVITY.md'),
    'Gemini Antigravity: PLANNING→EXECUTION→VERIFICATION mode-gate via task_boundary tool (architectural not advisory — "most impactful reliability pattern"), mixin prompt with DynamicContent budget fallback, knowledge subagent on stop_hook, waitForPreviousTools DAG parallelism. Copy the mode-gate (opt-in) + mixin budget fallback.'),
  T('character_ai', PROD, '03_teardowns', 'character_ai.md', td('CHARACTER_AI.md'),
    'Prompt Poet truncation to next multiple of truncation_step (KV-cache stable), truncation_priority, JudgeJudy 3-judge panel (Sonnet/GPT/Gemini, pointwise 1-5, 0.7 threshold), sticky-session-per-chat_id, is_pinned + TooManyPins. Copy the judge panel + cache-aware truncation + pinning.'),
  T('browser_use', PROD, '03_teardowns', 'browser_use.md', td('BROWSER_USE.md', 'MULTION.md'),
    'done(text,success) with 6-step pre-done verification checklist, compaction anti-hallucination prompt ("only mark done on explicit confirmation"), one-shot read_state channel, Anthropic double-serialization repair, prompt inflated to ≥4096 for cache. Copy done-verification + anti-hallucination compaction + one-shot channel.'),
  T('manus', PROD, '03_teardowns', 'manus.md', td('MANUS.md'),
    'Manus context-engineering lessons (KV-cache discipline = ~10× cost reduction from architecture, append-only, stable prefix, deterministic JSON, mask-don-t-remove tools, filesystem-as-context, recitation/todo to re-anchor goal). The canonical context-engineering source. Copy ALL the cache discipline rules.'),
  T('composio', PROD, '03_teardowns', 'composio.md', td('COMPOSIO_DEEP.md', 'COMPOSIO_R4_CORE.md', 'COMPOSIO_R4_TOOLS.md', 'COMPOSIO_R4_ROUTER.md'),
    'Meta-tools (6 tools the LLM sees, 10K+ server-side), COMPOSIO_MULTI_EXECUTE (ThreadPoolExecutor max_workers=5 + single backend call), 3-point modifier engine, execution envelope {data,error,successful}, deferred tool search. Copy meta-tools + multi-execute + envelope.'),
  T('autonomous_coding', PROD, '03_teardowns', 'autonomous_coding.md', td('OPENHANDS.md', 'REPLIT_AGENT.md', 'JULES.md', 'FACTORY_AI.md', 'SOURCEGRAPH_AMP.md'),
    'Autonomous coding agents. OpenHands stuck-detection thresholds (repeated=4, error-loop=3), git-worktree isolation, the autonomous loop discipline. Copy stuck detection + isolation; extract reliability rails.'),
  T('vertical_agents', PROD, '03_teardowns', 'vertical_agents.md', td('SIERRA.md', 'DECAGON.md', 'LINDY.md', 'HARVEY_AI.md', 'HIPPOCRATIC_AI.md', 'WEDGE.md', 'STARDRIFT.md', 'GORDIAN.md'),
    'Production vertical agents (CX/legal/health/ops). Guardrails, the confidence router (full [0,1], no dead zone — the Wedge bug), validate-before-side-effect (Parahelp 99% on billing), reliability-as-distribution, deployment. Copy confidence router + pre-execution validate; extract production reliability discipline.'),

  // ---------- research agents ----------
  T('deep_research', RSCH, '03_teardowns', 'deep_research.md', td('CLAUDE_RESEARCH.md', 'OPENAI_DEEP_RESEARCH.md', 'GEMINI_DEEP_RESEARCH.md', 'DEEP_RESEARCH_FAST_MODE_RE.md'),
    'define_outcome bounded generate→grade→revise (default 3/max 20), orchestrator-workers (depth-limit=1, workers research / orchestrator synthesizes), URL gate (web_fetch only seen URLs = structural SSRF defense), the source-quality-tool-removed-mid-flight lesson, fast/full route classification. Copy bounded outcome + depth=1 + URL gate.'),
  T('perplexity', RSCH, '03_teardowns', 'perplexity.md', td('PERPLEXITY_DEEP.md', 'PERPLEXITY_COMPUTER.md'),
    'True-delta vs cumulative streaming bug (pick ONE convention globally), ToolChoice.NONE on final step to force synthesis, BrowseSafe async post-tool scanner (replaces injected content), MCP-eats-context lesson. Copy the streaming convention + force-synthesis + post-tool sanitizer.'),
  T('nia', RSCH, '03_teardowns', 'nia.md', td('NIA.md', 'NIA_R4_AGENTS_ORCH.md', 'NIA_R4_PROMPTS_MODELS.md', 'NIA_R4_INDEXING_QUERY.md'),
    'JSON-action ReAct {action,args,reason} self-parsed (provider-agnostic, observable, interruptible), think({reflection}) sentinel, 4-tier memory with lineage (source_ids→tool_calls→model_version), two-stage synthesis (synthesis LLM never sees system prompt). Copy JSON-action loop + think + memory lineage + two-stage synthesis.'),
  T('grok_heavy', RSCH, '03_teardowns', 'grok_heavy.md', td('GROK_HEAVY.md', 'GROK_420_AGENTIC.md'),
    'Multi-agent with NO convergence metric (terminate on turns∧time∧tokens), leader IS one of the N agents AND the synthesizer (no extra aggregation model), reasoning_content as separate stream channel. Copy resource-bound termination + leader-is-synthesizer; refuse convergence-based termination.'),
  T('retrieval_agents', RSCH, '03_teardowns', 'retrieval_agents.md', td('TAVILY.md', 'FIRECRAWL.md', 'EXA.md', 'TAVILY_R3_SEARCH_BACKEND.md'),
    'Distilled-reflection memory (compress each iteration to notes, raw only at synthesis = 66% token reduction), Firecrawl 50-item rolling window + planner(Gemini Pro)/executor(Flash 10× cheaper), Exa highlights (500 chars = 8000 chars accuracy, 16× fewer tokens). Copy distilled-reflection + role-tiered models + highlights.'),

  // ---------- infra (model / memory / eval / durable / serving) ----------
  T('gateways_routing', INFRA, '03_teardowns', 'gateways_routing.md', td('LITELLM.md', 'OPENROUTER.md', 'PORTKEY.md')),
  T('gateways_more', INFRA, '03_teardowns', 'gateways_more.md', td('BIFROST.md', 'KONG_AI_GATEWAY.md', 'NEXOS_AI.md', 'ORQ.md'),
    'How model gateways relate to Lancelot TransportPool. 4-method Config (get_supported/map_openai_params/transform_request/transform_response, OpenAI canonical), error-typed fallback lists (context_window/content_policy/general), require_parameters=true (CRITICAL for tool agents), cache-sticky routing, DualCache, tool-name sanitization, Retry-After. Decide: what does Lancelot already do (TransportPool) vs what the thin adapter adds.'),
  T('memory_systems', INFRA, '03_teardowns', 'memory_systems.md', td('MEM0.md', 'GRAPHITI.md', 'SUPERMEMORY.md', 'COGNEE.md'),
    'Mem0 ADD-only extraction, Graphiti/Zep temporal knowledge graph (valid_at/invalid_at/expired_at, never delete, bi-temporal), confidence decay, recall_as_of. Copy the temporal model + ADD-only + 4-tier; assess graph-vs-vector.'),
  T('memory_vector', INFRA, '03_teardowns', 'memory_vector.md', td('TURBOPUFFER.md', 'LANCEDB.md', 'R2R.md', 'MIROFISH.md'),
    'RRF k=60 default reranker, unified rank_by algebra (BM25+ANN one query) vs two-pass, asymmetric embeddings (source≠query), tunable k1/b, OCC retry with slot-backoff, two-tool MCP (execute+search), GraphStorage abstract iface. Copy RRF + hybrid + asymmetric + pluggable backend.'),
  T('evals_obs', INFRA, '03_teardowns', 'evals_obs.md', td('LAMINAR.md', 'WEIGHTS_AND_BIASES.md', 'ADALINE.md', 'CODERABBIT.md'),
    'THE WEDGE. Eval-as-run (eval == production code path), judge calibration (precision/recall vs humans — a miscalibrated judge is worse than none), pass^k + per-step rate (reliability is a distribution), trace-based process evals (score the path not just the answer), OTel gen_ai.* spans, incident→regression in one line. Copy eval-as-run + judge calibration + pass^k + process evals.'),
  T('durable_exec', INFRA, '03_teardowns', 'durable_exec.md', td('PREFECT.md', 'COGNITION_INFRA.md', 'DISPATCH.md', 'TEMPORAL_RE_NOTES.md'),
    'Durable execution for the tactical↔durable seam. Event-sourced checkpoint/resume, the step() bracket (side effects skipped on resume), sync-vs-async durability (async re-fires LLM on crash → divergent → corrupt log; default SYNC), durable promise for HITL, submit/attach/cancel, seq via DB IDENTITY not MAX+1. Copy the seam mechanism + sync default + durable promise. (If TEMPORAL file missing, skip it.)'),
  T('serving_inference', INFRA, '03_teardowns', 'serving_inference.md', td('VLLM.md', 'SGLANG.md', 'TGI.md', 'LITSERVE.md', 'RAY_SERVE.md'),
    'Lancelot IS the serving layer — what to adopt. Mid-stream errors as finish_reason:error chunks (HTTP 200 already sent — classify per-chunk), @with_cancellation two-task race (request.is_disconnected broken under ASGI), out-of-band watchdog, KV-cache + prefix sharing, sticky sessions, continuous batching, three-state health. Map each to what Lancelot already has (R3 pump, C2 cancel, /livez/readyz, batch).'),

  // ---------- core transcripts (the user's explicit "analyze each transcript" ask) ----------
  T('yc_root', COREP, '02_core', 'transcripts_yc_root.md', core('TRANSCRIPTS_YC_ROOT_ACCESS.md', 'TRANSCRIPTS_YC_ROOT_ACCESS_PLAYBOOK.md'),
    'HUGE file (~3MB) — GREP/PAGE, do not read whole. Founder playbook for agents: build order, what real YC founders say about agent reliability, cost discipline, "just build agents" vs over-engineering, the durable-vs-tactical clock, eval craft, distribution. Extract the build-order + cost + refusal signal.'),
  T('yc', COREP, '02_core', 'transcripts_yc.md', core('TRANSCRIPTS_YC.md'),
    'YC transcripts: vertical agents, "don\'t build a better LangGraph wrapper", reliability bar for production, what founders ship. Extract the positioning + reliability + build-fast signal.'),
  T('dlai', COREP, '02_core', 'transcripts_dlai.md', core('TRANSCRIPTS_DEEPLEARNINGAI.md'),
    'HUGE (~788KB) — page/grep. Andrew Ng agentic design patterns (reflection/tool-use/planning/multi-agent), evals as the bottleneck, TTFT/latency UX (>200-300ms perceived lag), cost-effective patterns, the iterative-improvement loop. Extract the design patterns + eval + latency/cost signal.'),
  T('a16z', COREP, '02_core', 'transcripts_a16z.md', core('TRANSCRIPTS_A16Z.md'),
    'a16z: market/positioning, what is durable vs tactical, the agent infra thesis, where value accrues (harness not weights), pricing/cost. Extract the positioning + durable-layer + moat signal.'),
  T('stanford_palantir', COREP, '02_core', 'transcripts_stanford_palantir.md', core('TRANSCRIPTS_STANFORD.md', 'TRANSCRIPTS_PALANTIR.md'),
    'Stanford: research taste, verifiable reward (RLVR), problem selection, the Bitter Lesson. Palantir: ontology, durable execution, await-condition, production agent ops. Extract research-taste + durable-execution + verifier-first signal.'),
  T('msft_google', COREP, '02_core', 'transcripts_msft_google.md', core('TRANSCRIPTS_MICROSOFT.md', 'TRANSCRIPTS_GOOGLE.md'),
    'Microsoft + Google agent patterns: AutoGen lessons, ADK design, Gemini agentic, big-tech reliability + safety + observability. Extract the convergent production patterns.'),

  // ---------- core synthesis + RE docs ----------
  T('arch_catalog', COREP, '02_core', 'arch_catalog.md', core('06_AGENTIC_ARCHITECTURES.md', '13_AGENTIC_AI_ARCHITECTURE_MAP.md'),
    'The agentic architecture catalog/map. The canonical patterns (ReAct, reflection, orchestrator-workers, evaluator-optimizer, routing), when each applies, the anti-patterns. Extract the pattern taxonomy + when-to-use + what-to-default.'),
  T('agentic_deep', COREP, '02_core', 'agentic_deep.md', core('12_AGENTIC_AI_DEEP.md', '09_FRONTIER_AGENTIC_SYSTEMS.md'),
    'Deep/frontier agentic systems. Mechanism-level patterns from frontier systems, context engineering, tool use, the reliability frontier. Extract the frontier mechanisms that are durable bets.'),
  T('startup_pipelines', COREP, '02_core', 'startup_pipelines.md', core('08_STARTUP_AI_PIPELINES.md'),
    'HUGE (~678KB) — page/grep. Cost-effective production AI pipelines (the user\'s "cost effective" emphasis). Caching, batching, model tiering, prompt-cache, distilled context, the real cost levers startups use. Extract the cost-effectiveness playbook.'),
  T('leaked_prompts', COREP, '02_core', 'leaked_prompts.md', core('07_LEAKED_PROMPTS.md'),
    'HUGE (~128KB) — real production system prompts. Context structure, tool descriptions, safety/trust-hierarchy patterns, the think/plan scaffolds, anti-injection. Extract the prompt-structure + trust-hierarchy + reflection-scaffold patterns.'),
  T('re_startups', COREP, '02_core', 're_startups_map.md', core('14_EXTERNAL_AGENTIC_PRODUCTS_RE.md', '15_TOP_AGENTIC_STARTUPS_RE.md', '16_MISSING_TOP_AGENTIC_STARTUPS_RE.md', '17_NEXT_TOP_AGENTIC_INFRA_STARTUPS_RE.md'),
    'The competitive/primitive map — external products + top + missing + next infra startups. What primitives win, what is commoditized, where the white space is for a lean agent framework on a byte path. Extract the primitive map + white-space.'),
  T('how_they_did_it', COREP, '02_core', 'how_they_did_it.md', core('01_HOW_THEY_DID_IT.md', '03_FOUNDER_PLAYBOOK.md', '04_UNDER_THE_HOOD.md', '11_REVOLUTIONARY_AI_STARTUPS.md'),
    'Founder playbook + how real AI startups were built + under-the-hood. Build order, what to refuse, the schlep, ship-fast cadence, the revolutionary-primitive bet. Extract the build discipline + refusal + primitive-over-wrapper signal.'),

  // ---------- BAD_GUIDE (the field manual) ----------
  T('bad_guide_ai', COREP, '02_core', 'bad_guide_ai_research.md', [GUIDE],
    'Read the AI-RESEARCH-TASTE + MODERN-CTO + EVALS/RELIABILITY sections of BAD_GUIDE (grep section headers; it is ~280KB). Extract: reliability-as-distribution, verifier-first, the harness-is-the-moat, build-for-18-months-out, eval craft, the "just build agents" discipline, taste in problem selection. Cite the named people/essays.'),
  T('bad_guide_build', COREP, '02_core', 'bad_guide_build_positioning.md', [GUIDE],
    'Read the FOUNDER-MIND + REVOLUTIONARY-BUILDING + POSITIONING/REFUSAL + B2B-GTM sections of BAD_GUIDE. Extract: the contrarian-secret, primitives-not-wrappers, scope-reduction-is-quality, opinionated-defaults-as-moat, what-to-refuse, cost-effectiveness as positioning, distribution. The "what to build and what to refuse" taste layer.'),

  // ---------- Lancelot byte-path inventory (the NEW Lancelot-specific work) ----------
  T('lancelot_model_io', BP, '04_byte_path', 'lancelot_model_io.md', [`${LC}/llm.py`, `${LC}/upstream_metrics.py`, `${LC}/context.py`, `${LC}/canonical.py`, `${LC}/batch.py`],
    'Read this Lancelot source IN FULL. Inventory EXACTLY what the byte path already provides for an agent\'s model layer: TransportPool (request/stream/fanout/spawn_child/relay, retries+Retry-After, rate_limit, mTLS, traceparent+idempotency inject, cost emit), upstream_metrics (provider/token/cost/error-taxonomy), context ContextVars (cost/tokens/trace/agent), canonical (cache-stable JSON), batch (coalescing). For each agent-framework model/context/orchestration primitive, mark REUSE / EXTEND / BUILD-ON-TOP.'),
  T('lancelot_serve_tools', BP, '04_byte_path', 'lancelot_serve_tools.md', [`${LC}/stream.py`, `${LC}/tool.py`, `${LC}/mcp.py`, `${LC}/metrics.py`, `${LC}/telemetry.py`, `${LC}/net.py`, `${LC}/secrets.py`, `${LC}/tenant.py`, `${LC}/ratelimit.py`, `${LC}/webhook.py`, `${LC}/app.py`],
    'Read this Lancelot source IN FULL. Inventory what the byte path provides for tools/streaming/serving/safety: stream (R3 pump, SSE, TTFT, backpressure, disconnect-cancel), tool (ToolSpec schema + output_schema), mcp.mount, metrics (RED + TTFT), telemetry, net (SSRF), secrets, tenant (per-tenant concurrency), ratelimit, webhook, app (the App/serve seam). For each agent-framework tools/stream/safety/serving primitive, mark REUSE / EXTEND / BUILD-ON-TOP. Note the pydantic-only-runtime cardinal rule constraints.'),
]

// ---------- layer synthesizers ----------
const L = (label, file, focus) => ({ label, file, focus })
const LAYERS = [
  L('loop', 'loop.md', 'The <200-line async ReAct loop: continue-iff-not-cancelled, named StopReason enum, budget+grace-call, stuck detection, tool-rule solver (opt-in), mode-gate FSM (opt-in), two-stage synthesis. What Lancelot builds, the exact default constants, the loop contract.'),
  L('context', 'context.md', 'Context engineering: ContextBundle (inspectable, never string-concat), KV/prompt-cache discipline (static/dynamic boundary, append-only, deterministic JSON — note lancelot.canonical already does this), observation-masking compaction + LLM compaction fallback + anti-hallucination prompt, distilled-reflection ResearchMemory, per-category context introspection, budget as hard constraint. agent.inspect() honesty.'),
  L('model', 'model.md', 'Model layer ON TOP of lancelot.llm.TransportPool: ProviderProfile+OMIT adapter (message-shape translation, OpenAI canonical → N dialects), per-node ModelPolicy (require_parameters CRITICAL, error-typed failover, cross-model not same-model retry, reasoning-effort dial, cache-sticky), AuxiliaryClient, init_model("provider:model"). Be explicit: TransportPool already does transport/retry/cost/rate/mTLS — the adapter only adds translation+routing+failover.'),
  L('tools', 'tools.md', 'Tools: @tool (pydantic schema — lancelot.tool already derives it), dispatch ALWAYS {data,error,ok} never raises, 4-stage input-repair ladder + fuzzy name (0.85) + repeated-call short-circuit, annotation bits (readOnly/destructive/idempotent) → permission routing, bounded-parallel reads (5) with order preservation + sequential writes, validate()-before-side-effect, think+done builtins (ship ~0 domain tools), MCP demoted+deferred (lancelot.mcp.mount), meta-tools.'),
  L('memory', 'memory.md', 'Memory: one Memory object (short channel + long vector/KV + compaction), 4-tier typed (scratchpad/episodic/fact/procedural) + per-type TTL, memory-as-tools (remember/recall/recall_as_of, ADD-only), temporal (valid_at/invalid_at, never-delete, confidence decay), RRF k=60 + asymmetric embed + pluggable backend (sqlite-vec dev), virtual-context page-table metadata, bounded-by-default, consolidation hooks. Pydantic-only-runtime: sqlite-vec lazy.'),
  L('evals', 'evals.md', 'THE WEDGE. eval.unit (deterministic feature×scenario, CI, sub-minute) → eval.judge (panel, calibrated precision/recall) → eval.experiment, define_outcome(rubric, max_iterations REQUIRED) bounded, eval==run (shares prod path, native traces), pass^k + per-step rate (reliability is a distribution), process/trace evals (assert the path), verify_grounding, p50/p80 horizons, OTel gen_ai.* (lancelot already emits cost/TTFT/upstream). Never in the hot path.'),
  L('streaming', 'streaming.md', 'Streaming ON the Rust R3 pump: one structured event stream (typed parts: message.delta/reasoning.delta/tool.start/tool.result/status/error), true-delta convention globally, reasoning as separate channel, stream normalizer (strip blank text, coerce end_turn-with-tool, classify mid-stream finish_reason:error), resumable (seq_id + cursor replay), [DONE]. lancelot.stream already gives SSE+TTFT+backpressure+disconnect-cancel — build the event taxonomy + normalizer on top.'),
  L('durable', 'durable.md', 'The tactical↔durable seam (the thesis): SAME loop, durable=True flips runtime. Event-sourced internally, in-memory (tactical) vs checkpointer (durable). step() bracket (side effects skipped on resume), sync default (async re-fires LLM → corrupt log), submit/attach/cancel, ctx.hitl.request (durable promise, checkpoint-before-pause), await condition, max_llm_calls/max_budget_usd. Byte-identical when off. Lancelot graceful-drain/webhook lens.'),
  L('safety', 'safety.md', '5-stage lifecycle hooks (pre/post model, pre/post tool, on_stop/error/idle, async, updatedInput/updatedToolOutput), 4-valued permission (allow/deny/ask/defer) + session modes + per-tool override, confidence router full [0,1] no dead zone, prompt trust hierarchy (system>framework_rules immutable>agent>user; security only in immutable), tool trust_level + ACL-trim before assembly, URL gate (SSRF — lancelot.net already), max_budget_usd default-on, sandbox-first code exec.'),
  L('orchestration', 'orchestration.md', 'OPT-IN, separate import, single-agent default. Sub-agent = context isolation (returns typed compact summary, no parent-history default, depth cap=1), fan_out(max_concurrency 4-6) — note lancelot.llm.TransportPool.fanout + spawn_child are byte-path analogues to reuse, orchestrator-workers (shared typed state not message-passing), cross-model reviewer hook, graph escape hatch (Send/Command, map-reduce only). Refuse: convergence-termination, nested orchestrators, subagents writing final report, role-play theater.'),
  L('positioning', 'positioning.md', 'The "just build agents brutal" thesis FOR LANCELOT: the bet (loop is 5-10 lines, harness is the product), the wedge (lean agent layer on a Rust byte path — cost-effective by construction: prompt-cache stable, batch coalescing, cost-tracked, streaming), what to BUILD vs COMPOSE vs REFUSE (the Tier-D do-not-do list for agents), non-goals (not a graph-DSL, not a router cult, not MCP-first, not replacing AI logic), naming, the cost-effectiveness story, distribution. Ground in BAD_GUIDE + transcripts + RE-startup map.'),
  L('byte_path', 'byte_path_integration.md', 'THE LANCELOT-SPECIFIC SYNTHESIS. Read research/04_byte_path/*.md (the code inventory) + every layer. Produce the definitive REUSE / EXTEND / BUILD-ON-TOP / SKIP table: for each agent-framework primitive, what Lancelot already provides (TransportPool, stream, canonical, batch, tool, mcp, context, metrics, net, secrets, tenant) vs what the agent layer adds. The module map for lancelot.agents. Honor the pydantic-only-runtime cardinal rule (lazy provider/sqlite-vec imports). This is what makes Lancelot agents NOT a generic clone.'),
]

// ============ run ============
log(`Mining ${TARGETS.length} source clusters (Sonnet), then ${LAYERS.length} layer-synthesizers (Opus).`)

const minePrompt = (t) => `You are mining ONE source cluster for the Lancelot agent-framework harvest — an independent, mechanism-level deep read.

STEP 1 — read the lens (mission, quality bar, Lancelot's EXISTING byte path): ${LENS}
STEP 2 — read these source(s) IN FULL (page large files, grep to load-bearing sections — some are 100KB-3MB): ${t.sources.join('  ·  ')}
STEP 3 — FOCUS: ${t.focus}

Extract ONLY the best, mechanism-level: the exact knob / constant / file / prompt / API shape — never vibes ("good error handling" is useless; "json.loads→ast.literal_eval→json5→repair_json then SequenceMatcher>0.85" is the goal). Skip 101s. Cite every claim (source filename + section). Be honest: flag hype and depreciating tactical hacks (things that churn on a ~1-2yr model clock) vs durable primitives. For EVERY primitive, apply the Lancelot byte-path lens: does TransportPool/stream/canonical/batch/tool/mcp/context already provide it, change it, or obviate it?

STEP 4 — WRITE your distilled note (markdown, mechanism-level, cited) to: ${RES}/${t.dir}/${t.file}
STEP 5 — RETURN the structured digest.

You have FULL tool access (Read, Grep, Bash, WebFetch). "Research output" restricts only the FORMAT of your reply — it does NOT restrict your inputs. READ THE FULL SOURCE; do not guess.`

const mined = await parallel(TARGETS.map(t => () =>
  agent(minePrompt(t), { label: t.label, phase: t.phase, schema: MINER, model: 'sonnet' })
))
const ok = mined.filter(Boolean)
log(`Mined ${ok.length}/${TARGETS.length} notes. Synthesizing ${LAYERS.length} layers.`)

const synthPrompt = (l) => `You are the **${l.label}** layer-synthesizer for the Lancelot agent framework ("just build agents, brutal" — lean, cost-effective, no overkill, no 500-node graphs).

STEP 1 — read the lens: ${LENS}
STEP 2 — read the FULL distilled harvest the miners just wrote (one shot):
   cat ${RES}/03_teardowns/*.md ${RES}/02_core/*.md ${RES}/04_byte_path/*.md
   (these are per-source mechanism notes; for depth on a load-bearing claim, open the specific note or the raw source it cites.)
STEP 3 — synthesize the **${l.label}** dimension across ALL of them: ${l.focus}

Decide, at the "just build agents brutal" altitude (smallest primitive that earns its keep — anti-overkill AND anti-underkill): what Lancelot must BUILD, what it REUSES from the existing byte path (TransportPool / stream / canonical / batch / tool / mcp / context / metrics / net / secrets / tenant), and what it REFUSES (off-thesis / depreciating / overkill). Ground every call in citations from the notes. Include a concrete recommended API sketch (the shape users touch) and the exact default constants. Honor the cardinal rule: runtime dep is pydantic-only; provider SDKs + sqlite-vec lazy-imported.

STEP 4 — WRITE the layer brief to: ${RES}/05_synthesis/${l.file}
STEP 5 — RETURN the structured summary.

Full tool access; output-format restriction only. Read the notes; do not guess.`

const synths = await parallel(LAYERS.map(l => () =>
  agent(synthPrompt(l), { label: `synth:${l.label}`, phase: 'Synthesize', schema: SYNTH })
))
const synthsOk = synths.filter(Boolean)
log(`Synthesized ${synthsOk.length}/${LAYERS.length} layer briefs into ${RES}/05_synthesis/.`)

return {
  mined: ok.length, mined_total: TARGETS.length,
  synthesized: synthsOk.length,
  layer_summaries: synthsOk.map(s => ({ layer: s.layer, decision: s.decision, must_build: s.must_build, reuse_byte_path: s.reuse_byte_path, refuse: s.refuse, open_questions: s.open_questions })),
  notes_dirs: [`${RES}/03_teardowns`, `${RES}/02_core`, `${RES}/04_byte_path`],
  synthesis_dir: `${RES}/05_synthesis`,
}
