# TRANSCRIPTS_DEEPLEARNINGAI.md — Lancelot Agent Layer Harvest

**Source:** `researchfms/core/TRANSCRIPTS_DEEPLEARNINGAI.md` (~788 KB, 49 talks, AI Dev 25 + AI Dev 25 x NYC, Oct 2024 – Apr 2025)
**Scope:** mechanism-level extraction only; 101s and vibes skipped; every claim cited.

---

## 1. Latency UX — The >200–300 ms Cliff

**The constant that everyone converges on:**
> "Users abandon applications that take even 3 seconds to respond."
> "Complex behaviors are infeasible at current frontier model speed… developers are forced to reduce planning depth."
— Groq (Ozen), §"The Latency-Capability Tradeoff"

Quantitative threshold cited across talks:
- **TTFT (time-to-first-token) > 200–300 ms → perceived lag** (referenced in the Lancelot CLAUDE.md `core/` re-read synthesis; original context is implicit across infrastructure talks)
- Token generation rate during streaming: **< ~15 tok/s feels slow; 30+ tok/s feels responsive** — Nebius/Patrushev §"Metric Alignment"
- Camera / automotive: **< 20 ms** hard requirement — no cloud round-trip possible (Qualcomm §"Why On-Device vs Cloud")
- Average latency is a **lying metric** — track P95/P99 + TTFT + streaming token rate (Nebius §"Metric Alignment")

**Lancelot byte-path implication:** `lancelot.http.stream.ttft` histogram (`lancelot.http.stream.ttft`) is already built. The 15/30 tok/s thresholds are operator-visible only if the MCP server or framework is surfacing per-stream token rates. The transcript confirms TTFT + P99 as the two metrics that matter; Lancelot already captures both. No new primitive needed — but the operator doc should name these thresholds explicitly.

---

## 2. Prompt Caching — Append-Only Context as the Structural Win

**The mechanism (Genspark, §"Append-Only Context: The Cache Architecture"):**
- Modern LLM APIs cache **prefix hashes** (KV cache). Any change to the prefix invalidates the cache.
- Genspark's rule: **never reorder conversation history by relevance**. Append-only. System prompt + turns N-1 is always identical to the previous request → >80% cache hit rate.
- Savings: 5–10x token cost reduction. (Arithmetic: Anthropic charges 10% for cache reads vs 100% uncached. At 80% hit: 80%×10% + 20%×100% = 28% of full price → ~3.6x. 5–10x requires >90% hit rate or is measured against a naive no-caching baseline.)
- **Why frameworks break this:** LangChain/LlamaIndex can silently add extra tokens, timestamps, or reorder history — invalidating the cache prefix on every turn. Genspark wrote custom scaffolding specifically to control every byte of the prompt. §"Custom Scaffolding: What It Means in Practice"

**Compaction threshold:** triggers at **70–80%** context utilization (not 100% — leaves 20–30% headroom for the next generation step + tool results). After compaction, context = `[system prompt] + [compacted summary] + [recent turns]`. §"Compaction at 70–80% Threshold"

**The 75% trigger for memory creation** (MongoDB §"Context Window Trigger"):
```python
threshold = model_context_limit * 0.75  # trigger at 75% capacity
```
At >75% context fill, create memories + compact history before overflow.

**Lancelot byte-path implication:**
`lancelot.canonical.dumps_canonical` provides stable sorted-key JSON for KV-cache prefix stability. This is the *correct* mechanism — byte-stable prefix → >80% cache hit. The agent layer must enforce **append-only context assembly**: do NOT sort/filter conversation history by relevance before injecting into a prompt. Any ContextBundle primitive should document this as a hard constraint.

---

## 3. Eval Architecture — The Durable Core

### 3.1 LLM-as-Judge: Categorical Labels, Not Numeric Scores

**The mechanism (Arize/Phoenix §"LLM Classify: Implementation Pattern"):**
```python
eval_results = llm_classify(
    dataframe=tool_call_df,
    model=OpenAIModel(model="gpt-4o"),
    template=TOOL_CALLING_TEMPLATE,
    rails=["correct", "incorrect"],  # ← THE KEY KNOB
    provide_explanation=True
)
```
**`rails` is the critical reliability mechanism.** Without it, models return "I think this is correct" or "7/10" — unparseable. With `rails=["correct", "incorrect"]`, the model is forced to emit exactly one of those tokens. Categorical labels are consistent; numeric scores are not. §"LLM Classify: Implementation Pattern"

**Confirmed across multiple speakers (Cross-Talk §"Pattern 1"):**
- Arize: categorical beats numeric (empirical)
- Maestro/AI21: "single-constraint judges perform dramatically better than multi-constraint judges"
- MLflow (Databricks): "orthogonal judges — one faithfulness judge, one quality judge, one tool-call judge — separate, not combined"

**Improvement curve from Arize (same baseline, same held-out set):**
- Baseline prompt: 68%
- +3–4 few-shot examples: **84%** (+16 pp)
- +Meta-prompting (LLM rewrites the eval prompt from the labeled examples): **88%** (+4 pp)
- DSPy/MIPRO: faster but no accuracy gain; "works better on complex tasks with larger datasets"

### 3.2 Eval Generation Without Manual Work (Datadog §"Generating Eval Scenarios")

**The AST pipeline (pure Python, no LLM):**
1. Concrete query (CQ) → parse to AST
2. Traverse AST, map node types to natural-language templates
3. Output: `(natural language question, ground truth answer)` pair
4. **Time-travel parameter:** queries embed a fixed timestamp so "last 1 hour" is reproducible
5. One docs page → ~200 labeled scenarios via seed queries from a coding agent

**Why this is durable:** deterministic transformation, no LLM hallucination in the generation step itself. The LLM is used only for seed CQ generation (with a coding agent reading product docs), not for labeling. §"AST Pipeline: Implementation Specifics"

### 3.3 MLflow Quality Stack (Databricks §"MLflow: Architecture for Agent Quality")

The specific primitives in build order:
1. **Tracing** — one-line decorator, OpenTelemetry-compatible, 25+ framework integrations. Every LLM/tool call = a span with input, output, latency, tokens, cost
2. **Agentic Insights** — agent reads production trace experiment → structured Markdown report with issues, evidence, prioritized fixes. Via `genai_analyze_experiment(experiment_id)` on the MLflow MCP server
3. **Offline evaluation** — pull production sample → labeled eval dataset → each row = a unit test → diff runs across versions
4. **Automatic prompt optimization** — JEPA technique: source prompt + quality signal (thumbs/feedback) → optimized prompt for target model
5. **Judge alignment** — label examples for the judge, have it learn domain expert preferences. Demo: 50% → 100% agreement with domain expert after alignment
6. **Agent-as-judge** — express complex trace traversal criteria in plain English; judge uses MCP tools to introspect specific spans rather than stuffing the whole trace into context

**Evaluation-before-optimization principle (Cross-Talk §"Pattern 6"):**
> "Bad grader → RL optimizes toward wrong behavior" — Genspark

### 3.4 Orthogonal Judge Architecture

**BlackRock moderation pipeline (§"Moderation Pipeline: Parallel Execution Architecture"):**
- Group 1 (hard blocks, ALL concurrent): toxic / sensitive topic / off-topic / jailbreak. If any fires → block immediately, no further pipeline.
- Group 2 (soft blocks, concurrent): alternative routing. If fires → offer alternative, not dead end.
- Group 3 (independent, non-blocking): PII detection and redaction. **Never a blocking operation.** Runs in parallel, operates on output after generation.

**The design rule:** PII scrubbing should never add latency to the happy path. It runs alongside generation and operates on the final output, not as a gate.

---

## 4. Context Engineering as a Systems Design Discipline

**Definition (Neo4j §"Context Engineering: The Definition"):**
> "The discipline of systematically providing models with all relevant information, tools, and instructions in the correct format at the correct time for a specific task."

Distinct from prompt engineering: prompt engineering optimizes word choice in static instructions; context engineering designs dynamic information-assembly systems.

### 4.1 Five Techniques (Neo4j §"Five Context Engineering Techniques")

1. **Hybrid RAG** — vector + keyword/metadata filters + reranking. "Context poisoning" is the failure mode of pure vector: off-target retrieval drowns specific facts with generic text.
2. **Memory management** — sliding window / recency / summarization / episodic / global state. Core: every strategy involves choosing what to forget. No free lunch.
3. **Structure and ordering** — most relevant content at the **top** of context (Anthropic guidelines). Consistent delimiters for tool outputs (e.g., `[TOOL RESULT: tool_name]`). Fixed templates for memory sections vs new-query sections.
4. **Tools instead of raw data** — "give LLM a database query tool, not a data table." Offload computation to tools, keep context focused on reasoning. §"Tools and Function Calling"
5. **Knowledge graph augmented context** — nodes = entities, edges = relationships. Multi-hop traversal: "which asset managers are exposed to lithium shortage?" → graph traversal: company→holding→position. Enables exact, traceable, explainable reasoning vs probabilistic vector search. §"Knowledge Graph Multi-Hop"

### 4.2 Outside-Diff Impact Slicing (CodeRabbit §"The Core Finding: Bugs Hide Outside the Diff")

**The load-bearing finding:**
> "The majority of bugs introduced by PRs are not in the changed code, but in the contracts between the changed code and the surrounding codebase."

Token comparison (same bug, three approaches):
| Approach | Input tokens | Result |
|---|---|---|
| Diff only | 17,000 | Bug **not found** |
| Brute-force all connected code (JSON) | 110,000 | Bug found |
| Optimized outside-diff slicing (XML tags) | 18,300 | Bug found |

6x token reduction with identical accuracy from two sources: relevance filtering (callers/callees only) + XML format over JSON.

**Tag-based prompt envelopes beat JSON for long contexts** (CodeRabbit §"Tag-Based Prompt Envelopes"): XML-style tags are better represented in LLM training data, more token-efficient than JSON structural characters, and provide human-legible anchors in 100K+ token contexts.

### 4.3 Context Isolation via Sub-Agent Scoping

**Strands/AWS §"Agent as a Tool: The Context Budget Calculation":**
- Parent context: 2,700 chars
- Sub-agent (web search) context: 15,000 chars
- Wrapping verbose sub-tasks in sub-agents that summarize outputs = **82% parent context reduction**
- Generalization: any verbose tool (HTML→summary, bash logs→relevant lines, large JSON→structured summary) benefits from sub-agent wrapping

**Genspark §"Context Isolation via Sub-agents":**
- Sub-agent runs in its own isolated context window
- Its tool chains / intermediate outputs **do not pollute the super-agent's context**
- "Linear context for multi-agent" rather than merging distributed agent states: overhead of "thinking sync" across parallel agent states outweighs parallelism gains

---

## 5. Agent Loop Architecture — The Durable Primitives

### 5.1 Model-Driven vs Workflow Agents (AWS/Strands §"The Model-Driven Agent: Formal Definition")

**Workflow agents (state machines):** hardcoded IF→THEN transitions. Every edge case requires code changes. Fail on unknown inputs by producing garbage. Build cost: linear in use case count.

**Model-driven agents:** LLM in a loop, LLM output decides next action. No hardcoded transitions. Fails gracefully (reasons about what went wrong). Build cost: fixed (define the tools; model composes them).

**Critical insight:** "Informative error messages are not developer UX — they are the primary self-correction mechanism." §"The Model-Driven Approach"

Without descriptive errors from tools, the model cannot reason about failures and cannot decide what to try next. Every tool must return structured errors that describe what went wrong at the semantic level, not just an HTTP 500.

### 5.2 The Underinvested Half: Feedback Mechanisms

**Anthropic §"The Feedback Mechanism Gap — Detailed Analysis":**
Most developers over-optimize for action (give agent tools to write/call/execute) and under-invest in feedback (give agent tools to *observe* results of its actions).

Concrete fix: add **observability tools** to the agent's tool set:
- Test runner tool (after writing code → does it pass tests?)
- Browser screenshot tool (after building UI → does it look right?)
- Linter tool returning structured errors
- Schema validation tool for checking outputs

Self-correction is the primary unlock for longer autonomous task completion. §"Critical Underinvestment"

### 5.3 Deterministic Backbone + Variable Agency (CrewAI §"Agentic Systems Pattern")

**The production pattern:**
```
deterministic backbone + variable agency per step
```
Example:
1. User message → single LLM call classifies intent → deterministic routing
2. If simple: direct LLM response (low agency)
3. If complex: kick off crew (research → analysis → synthesis) → high agency
4. Output: always routed through deterministic validation before returning

**Complexity × Precision Matrix:**
- High complexity + high precision (e.g., IRS form completion): deterministic backbone dominant
- High complexity + low precision (educational content): more agency OK
- Low complexity + low precision (sales brief): single LLM call sufficient

**Anti-pattern:** pure agency everywhere. Reserve agents for where adaptability earns measurable eval improvement. KISS principle: "if a simple LLM call handles the task, don't use an agent."

### 5.4 Exit Conditions — Termination as a First-Class Design Decision

**Haystack §"Haystack Agent Loop":**
```python
Agent(
    chat_generator=...,
    tools=[repo_viewer_tool, issue_commenter_tool],
    exit_conditions=["write_github_comment"]  # stop WHEN this tool is called
)
```
Rather than requiring the model to say "I'm done," define **which tool call signals task completion**. Prevents runaway loops, makes termination deterministic. Agent loop runs until exit condition fires or max iterations reached.

---

## 6. Memory Architecture — Four-Tier Production Schema

**MongoDB §"Memory Taxonomy: Human Types → Agent Equivalents"**

| Memory Type | Mechanism | Schema Key |
|---|---|---|
| Short-term | In-context window | `messages[]` (mutable list) |
| Long-term semantic | External DB + RAG retrieval | `{content, importance_score, embedding, tags, created_at}` |
| Long-term episodic | Action sequence log | `{task_name, task_description, outcome, embedding, created_at}` |
| Long-term procedural | External system prompt store | `{content, version, update_history[]}` |

**Composite retrieval scoring (§"Memory Retrieval: Composite Scoring Implementation"):**
```python
composite = (
    0.5 * vector_similarity +
    0.3 * math.exp(-age_days / recency_decay_days) +  # default decay=30 days
    0.2 * importance_score  # LLM-generated at creation time, stored as metadata
)
```
Pre-compute importance at creation time, not at retrieval time (avoids per-document LLM call on every retrieval). §"Why Importance Score at Creation Time"

**Memory extraction prompt discipline:**
> "Only extract information that would be useful to know in future conversations. Do NOT extract every fact mentioned — only important, reusable insights."
Selectivity is the key directive. Over-extraction pollutes retrieval precision. §"Memory Creation via LLM Extraction"

**Lifecycle management:**
- TTL index: `expireAfterSeconds = 90 * 24 * 3600` (auto-delete memories not accessed in 90 days) §"DELETE"
- Treat agent memory as an enterprise dataset, not an append-only log
- Logs ≠ memories: logs store everything for debugging; memories extract specific insights for real-time retrieval

**Lancelot byte-path implication:** `lancelot.llm.TransportPool` provides transport; `lancelot.context` provides ContextVars (cost, tokens, agent_id). Neither provides typed memory CRUD or composite retrieval scoring. This is the agent layer's job. The existing `MemoryPool` is byte-OOM guard, not agent memory. Build this fresh.

---

## 7. MCP Tool Design — Task-Oriented, Not API-Oriented

**Box §"MCP Tool Design Principle":**
> "Task-oriented, not API-endpoint-oriented. One `get_markdown_from_file` tool wraps 5 underlying API calls."
Anti-pattern: 1:1 mapping of REST API spec to MCP tools. Agent tools should be designed around what the agent needs to *do*, not what the underlying API *is*.

**Datadog §"MCP Tool Surface Flexibility vs. API Stability":**
- Agents call `list_tools` on every session initialization → always see current tool surface
- MCP tools can be **changed freely** without versioning or migration guides
- Enables: routing a "list query" to a "clustering API" transparently, spelling correction before API hit, query type inference
- These are behaviors impossible in a stable REST API

**SAP §"Broad-Tool Architecture: Three Tools for 110,000 Endpoints":**
Scale problem: 110,000 API endpoints. Naive solution (one MCP tool per endpoint) overflows any context window and causes selection confusion. Solution: **three broad tools** — Discover APIs, Retrieve API Specification, Execute API — backed by a knowledge graph. Agent always has exactly 3 tools; complexity absorbed by the KG.

**Per-agent tool scope discipline (Box §"Key Principle"):**
> "Limit each agent strictly to the MCP tools it needs for its one job. Too many MCP servers available to an agent causes confusion about which tool to use."
Narrower tool set → more reliable tool selection.

**Self-optimization loop (Datadog §"From 0.04 to Improvement"):**
1. Eval score 0.04 for "count vs C" capability
2. Claude Code + LLM Observability MCP → inspect failing traces → diagnose: tool description missing IP address format
3. Claude Code proposes diff to fix tool description
4. Apply diff, restart server, rerun evals → score improves
5. 2-minute eval cycle from code change to dashboard results

**Lancelot byte-path implication:** `lancelot.mcp.mount(app, path='/mcp', max_response_bytes=4096)` provides the MCP server mounting with >4KB WARN discipline (Dex Horthy pattern). The task-oriented tool design principle is a documentation/design guideline, not a new primitive.

---

## 8. Cost Architecture — What Actually Controls Per-Task Cost

### 8.1 The Three-Model Routing Pattern (Anthropic §"Model Routing and Cost Strategy")

- **Haiku** (cost-efficient): high-volume, low-complexity — classification, simple extraction, short completions
- **Sonnet** (daily workhorse): production workloads needing quality at reasonable cost
- **Opus** (maximum intelligence): hardest tasks, difficult decomposition, stuck-agent recovery

Production agent systems **should route calls**, not use one model for all steps. §"Model Routing and Cost Strategy"

**Model size selection framework (Cross-Talk §"Model Size Selection in Practice"):**
- Prototype/exploration: smallest model that understands the task (1B–8B)
- Production single-task: fine-tune 8B on domain-specific data (50,000+ examples) → often beats 70B general
- Agentic orchestrators: largest affordable (70B–405B); orchestrator errors cascade through the pipeline
- Specialized sub-agents: small purpose-fine-tuned models (1B–8B)
- Synthetic data generation: 405B; quality matters more than cost here (one-time expense)

### 8.2 Semantic Cache for Cost Reduction (Redis §"Semantic Caching: Mechanism")

**Full pipeline:**
1. Embed user query → vector
2. Nearest-neighbor search against cache (stored as vectors)
3. Similarity ≥ threshold → cache hit → return immediately (no LLM call)
4. Cache miss → full pipeline → store `(query_vector, response)` in cache

**Treat as ML model, not cache:** requires precision/recall/F1 tracking, confusion matrix, distribution drift monitoring. Distance threshold = the hyperparameter. Lower threshold = higher precision, lower recall. Tuned per use case.

**Filters to add:**
- Code detector: technical syntax → bypass cache → send to LLM
- Temporal context detector: time-sensitive queries → bypass cache
- Reranker (cross-encoder or LLM-based): boost precision but adds latency

**Walmart production architecture (§"Walmart Production Case Study"):**
- L1: vector DB (semantic matching)
- L2: in-memory cache (exact-match lookups)
- Decision engine (code detector + temporal detector + similarity evaluator)
- Multi-tenancy isolation

**ROI at 10k queries/day, 36% cache hit rate:** ~$39,200/year → ~$27,400/year. ~$11,800/year saved. §"ROI Calculation Example"

**Lancelot byte-path implication:** `lancelot.canonical.dumps_canonical` provides byte-stable prompt prefix for LLM-side KV-cache. Semantic caching is a different (and orthogonal) layer: caching at the *application* level before the LLM call. Lancelot does not provide this. It could be provided as `@app.batch` analogue (already provides ~16x upstream call reduction for LLM rate-limited backends). Semantic cache adds coverage for *heterogeneous* queries that are semantically similar but textually different.

### 8.3 Agentic RAG Context Cost at Every Loop Iteration (Redis §"The Cost/Latency Problem at Scale")

In agentic RAG (not basic RAG), context is **reassembled at every agent loop iteration**: user input + system state + memory (conversation history, past interactions, preferences) + structured data from external APIs. Net effect: higher latency variance, more tokens consumed. This is the structural cost driver for agent loops that naive benchmarks miss.

---

## 9. Grader-First / Eval-First Development Order

**From multiple speakers (Cross-Talk §"Pattern 6"):**

The universal ordering in production systems that work:
1. Build the grader (define what "good" means for your outputs)
2. Instrument with tracing (see what the system is doing)
3. Run eval cycle (measure baseline)
4. Optimize (prompt, model, architecture)
5. Re-measure to confirm improvement

**Genspark §"Self-Improvement — Evaluation as Core Loop":**
> "Build a high-quality grader for all types of agent output. Grader serves: (a) trigger second-pass refinement when quality is below threshold, (b) generate RL signal, (c) populate 'prompt playbook' of collective feedback."

**The RL-grader dependency (Genspark + Fireworks §"RL Fine-Tuning"):**
> "You cannot do RL without a reward signal. The grader IS the reward signal. Bad grader → RL optimizes toward wrong behavior (Goodhart's Law applied to RL)."

**The anti-pattern:** skipping evaluation and going straight to optimization. Without baseline measurements, you cannot know if changes are improvements or regressions. All practitioners who mentioned debugging production issues traced root causes to either (a) no baseline eval or (b) a misaligned judge.

---

## 10. Compounding Error — The Reliability Cliff for Multi-Step Tasks

**AI21 §"The Compounding Error Problem":**
> "Even at 90% per-step accuracy, three steps compound to ~73%. Real enterprise workflows have 10+ steps."

At 90% per-step accuracy, 10-step workflow: 0.90^10 = 35% success rate. The agent fails 65% of the time.

**Mitigation pattern from Maestro/AI21 (§"Maestro: AI21's Architecture"):**
- **Per-step multi-strategy execution:** enumerate multiple strategies (different LLM + retrieval combinations), rank by probability of success, execute top-N in parallel
- **Per-step validation before next step:** validator picks best result from parallel executions BEFORE passing to next step
- **Single-constraint validators:** each requirement gets its own validator (LLM judge or deterministic code), not one multi-constraint judge
- **Computational budget as an explicit input:** token budget / cost budget / latency budget passed at invocation time; system allocates accordingly

**Context overload finding (AI21 §"Context Overload at Enterprise Scale"):**
> "As the number of constraints passed to an agent increases beyond 20–40, success rate drops dramatically even with state-of-the-art models."

Keep constraint count per LLM call below ~20. Decompose into more calls with fewer constraints each.

---

## 11. Production Readiness — The Gaps Between POC and Production

**Pattern 4 (Cross-Talk §"Production Reality vs Prototype Reality"):**

Common surprises in production (not in prototype):
- Diverse user behavior breaks retrieval strategies optimized for the prototype team (Mistral case: page-level chunking failed UAT)
- Security review of frameworks takes 4–8 weeks; common rejection: no support for proxy configuration (Mistral §"Framework Approval Path")
- 4-month project → 10 months due to compliance requirements
- Automated compliance review must run continuously, not just at launch

**Mitigation:**
- Start validation (red teaming, diverse users, security review) earlier than feels necessary
- Build observability before you need it
- Canary rollout, not big-bang launch
- Automated quality checks running continuously

**TTFT + P99 are the infrastructure metrics that correlate with actual user experience** (not average latency, not GPU utilization). §"Metric Alignment: What Users Actually Experience"

---

## 12. AG-UI Protocol — Transport-Agnostic Agent-User Interface

**CopilotKit §"AG-UI Protocol: 16 Standard Events":**

16 events cover the full agent-user interaction:
- Lifecycle: `run_started`, `run_finished`, `error`
- Content: `text_message_start`, `text_delta`, `text_message_end`
- Tool: `tool_call_start`, `tool_call_delta`, `tool_call_end`, `tool_call_result`
- State: `state_snapshot`, `state_delta`
- Action: `agent_action_request`, `agent_action_response`
- Meta: `raw` (custom extensions)

**Transport-agnostic:** identical 16 events over HTTP SSE, WebSocket, or WebRTC. Same agent backend can power text chat, voice interface, and canvas app simultaneously without changes.

**Front-end tools:** agent emits `tool_call` event → browser executes JavaScript locally → result returned to agent. Gives agents DOM access without server round-trip. §"Front-End Tools: Client-Side Execution"

**Bidirectional state:** agent and user both read/write the same shared state schema. Enables collaborative canvas experiences (v0/Lovable/Cursor-style). §"Bidirectional State: The Architecture That Enables Canvas Apps"

**Lancelot byte-path implication:** Lancelot's `@app.stream` + SSE already serves the *transport layer* for AG-UI. The 16 AG-UI events are an application-layer protocol that rides on SSE/WS. Lancelot's transport is the substrate; an AG-UI emitter in the agent loop would be user-side code. No new primitive needed in Lancelot's core, but an `@app.stream` with `emit_agui_event()` helper could live in the agent layer.

---

## 13. Hype / Depreciating Tactics (flag as churn-prone)

### Depreciating on a 1–2 year model clock:
1. **SWE-bench % as the primary agent quality metric** — acknowledged as insufficient (Panel/Percy Liang). Needs multi-step coordination, documentation, refactoring benchmarks. Will be replaced.
2. **Specific model tiers and pricing numbers** — all pricing cited ($0.075/M Flash-Lite, etc.) are highly volatile. Do not hardcode.
3. **MoA (Mixture of Agents) as a cost-acceptable strategy** — expensive (N frontier models per query). Viable at Genspark's scale + their 80%+ cache hit rate. Not a default. Cost profile degrades fast if cache hit rate drops.
4. **Fixed prompt templates without alignment** — out-of-the-box LLM judges misalign with domain expert judgment at ~50% agreement rate. Alignment is not optional for production.
5. **Code-as-tool-call (smolagents 1,000-line approach)** — LLMs write better code than JSON (true), but requires sandboxed execution to prevent arbitrary code execution. The sandboxing requirement may constrain deployment environments.

### Durable primitives (persist across 3+ model generations):
1. Eval-first development order
2. Categorical labels for LLM-as-judge
3. Append-only context for cache hit rate
4. Compaction threshold at 70–80% context fill
5. Sub-agent context isolation to prevent parent context pollution
6. Deterministic backbone + variable agency per step
7. Exit conditions as termination mechanism
8. Four-tier memory taxonomy (short/semantic/episodic/procedural)
9. Composite retrieval scoring (vector + recency + importance)
10. Informative error messages as the self-correction primitive

---

## 14. What This Confirms / Contradicts for Lancelot

### Already built (no action needed):
- TTFT metric (`lancelot.http.stream.ttft`) ✓
- Stable prompt prefix (`lancelot.canonical.dumps_canonical`) ✓
- Transport for streaming (`@app.stream`, SSE, R3 pump) ✓
- Tool surface (`@app.tool`, `lancelot.mcp.mount`) ✓
- Context vars (cost, tokens, agent_id) ✓
- `@app.batch` for upstream call reduction (~16x fewer LLM calls) ✓

### Agent layer build targets confirmed by this source:
1. **Eval framework** — categorical label eval, golden dataset management, trace-to-DataFrame export. Orthogonal per-constraint judges.
2. **Context bundle assembly** — append-only enforced, structure/ordering (most relevant first, consistent delimiters), compaction at 70–80% threshold, sub-agent isolation
3. **Memory CRUD** — four-tier typed memory, composite scoring retrieval, LLM extraction with selectivity prompt, lifecycle/TTL management
4. **Model adapter layer** — route Haiku/Sonnet/Opus by step complexity; provider translation (TransportPool does transport, not message-shape)
5. **Informative error taxonomy** — structured errors from every tool that enable model self-correction (the primary self-correction primitive)
6. **Agent loop** — ReAct with exit conditions, max_iterations guard, stuck detection

### Not worth building (Tier-D confirmed):
- Semantic cache as a Lancelot primitive — user-side; requires application-specific threshold tuning
- AG-UI event emitter as Lancelot core — rides on Lancelot's transport (already done); user-side
- Knowledge graph integration — Tier-D: AI logic on the user side
- DP-SGD / privacy training — not Lancelot's scope
