# YC Root Access Transcripts — Lancelot Agent Harvest

**Sources:** `researchfms/core/TRANSCRIPTS_YC_ROOT_ACCESS.md` (57 videos, 3MB, ~16k lines) and `TRANSCRIPTS_YC_ROOT_ACCESS_PLAYBOOK.md` (methodology).  
**Mined:** 2026-06-22. Focus: agent reliability, cost discipline, build order, context engineering, eval craft, durable state, tool design, distribution.

---

## 1. Build order + "just build agents brutal" signal

### 1a. The 3-step architecture methodology (Jake Heller / CoCounsel, `sLFv3RSj_d8`)

The most mechanism-level build-order advice in the corpus:

1. **Define the ideal customer experience first.** What UX pattern: chat-with-tools, big button, document upload? This is an *architectural constraint*, not a stylistic choice — the pipeline you build is determined by the interaction model. Build a chat-with-tools pipeline → you need a tool registry, a planning layer, a multi-turn state manager.
2. **Ask: how would the world's best human attack this problem?** That IS version-zero of the architecture. For legal research: clarify → formulate 20–50 queries → execute → read every result noting relevance → compile. OpenAI's Deep Research independently arrived at the same "clarify before search" step — validation that it's the correct structure, not a design choice.
3. **Per micro-step: code or prompt, linear or agentic?** If the best human would act linearly, implement as sequential `def step_one, step_two, step_three`. Agentic loops only where the best human would loop back (e.g., realized search didn't find the right material → try different queries). "Sometimes it's like really linear and sometimes it's agentic." The failure mode: defaulting to agentic because it sounds more sophisticated. A pipeline that is linear should be implemented linearly — less latency, more predictable, easier to eval.

**Byte-path implication:** Lancelot has `@app.tool` / `ToolSpec` for the tool layer and `lancelot.context` for multi-turn state. The planning/routing layer between micro-steps is still missing — this is the "agent loop" Lancelot LACKS per the lens.

---

### 1b. Self-improving loop: the 5-layer architecture (Jared Friedman / YC, `t-G67yKAHBQ`)

Jared's "self-improving company" talk is the highest-level build-order framework in the corpus:

1. **Sensor layer** — inputs from the real world: customer emails, support tickets, code changes, telemetry.
2. **Policy/decision layer** — rules about what the system can do autonomously, what it must ask a human for, what it must log.
3. **Tool layer** — deterministic APIs: query DB, look up calendar event.
4. **Quality gate** — evaluation checks, safety filters, human review for high-risk actions.
5. **Learning mechanism** — the system interacts with the real world, identifies where it fails, loops back.

The YC internal example is the most concrete: a monitoring agent observes every query made by every YC employee, identifies failures, asks "why did this fail — new deterministic tools? updated skills file? different DB view?", writes the fix, submits a PR, routes through agent code reviewer, merges, deploys overnight. "For me, that was like the holy shit moment. That's not just AI making you 20 or 30% more valuable. It is the AI going through this loop to figure out how to self-improve."

The `skills file` is a declarative artifact (editable by the monitoring agent without retraining) that represents what capabilities the agent has and when to invoke them. When a query fails because "we need to update the skills file," the monitoring agent adds a new skill definition — the agent gains new capabilities without a model deployment.

**Byte-path implication:** Policy/decision layer (what requires human approval) maps directly to the HITL/permissions layer Lancelot LACKS. Tool layer is partially covered (`@app.tool`). Quality gate is the eval layer Lancelot LACKS. Learning mechanism is outside Lancelot's scope (agent logic = user side per cardinal rule).

---

### 1c. Three-phase agentic coding workflow: Research → Plan → Implement (Dex Horthy / HumanLayer, `IS_y40zY-hc`)

The mechanism Horthy's team uses daily, and the most specific build-discipline signal in the corpus:

**Phase 1 — Research:** Agent reads the codebase, produces a structured document with **explicit file names and line numbers** — not prose, but a navigational map. "The authentication flow starts in `src/auth/middleware.go:47`, calls `ValidateToken()` at line 83..." The research prompt is open source on HumanLayer's GitHub.

**Phase 2 — Plan:** Agent writes a detailed implementation plan: every file to change, specific code snippets, explicit verification/test steps per change. The plan is the artifact humans review. "If you are shouting at the agent during implementation, the plan was bad. Fix the plan, not the agent's mid-stream behavior."

**Phase 3 — Implement:** Execute the plan with a hard constraint: **keep context utilization under 40% at all times.** When approaching 40%, commit progress to the plan file (mark completed steps as done), compact intentionally, start a new context window with the updated plan as onboarding material. The 40% ceiling is not arbitrary — it leaves enough space for the agent to reason about complex decisions without context pressure that causes hallucination.

**Intentional compaction** is the mechanism for crossing context boundaries without losing state. The format: what was the goal, what was accomplished, what files were touched and what specific changes, what was tried and failed (why), next steps, constraints and design decisions established. Author-controlled, not algorithmic — Horthy says "/compact is trash" because it produces a black-box summary that loses architecturally critical information without warning.

Sub-agents = context control, not task decomposition. A sub-agent is dispatched to find relevant files and return a structured summary. The parent agent learns where the code is without spending the search cost. Ideal sub-agent response: file path, line numbers, brief explanation, explicit pointers to where the parent should read next.

Context quality hierarchy, worst to least bad: **bad information** (actively steers wrong) > **missing information** (causes search/halt) > **noise** (correct but irrelevant, wastes tokens).

**MCP tools returning JSON blobs = anti-pattern.** "If you have MCP tools that return big blobs of JSON, that's going to flood your context window with a bunch of nonsense." MCP tools should return only the information the agent needs for the next step — structured, minimal, pre-filtered. Lancelot already has the >4KB WARN discipline in `lancelot.mcp.mount`; this validates that mechanism precisely.

**Byte-path implication:** Lancelot's `lancelot.mcp.mount` minimal-return >4KB WARN discipline is **validated by this source** as exactly the right mechanism. The context budget / compaction problem is outside Lancelot's byte path (agent logic = user side), but the tools Lancelot exposes should follow the "minimal, pre-filtered output" discipline — this is already encoded in `lancelot.mcp`.

---

## 2. Eval craft

### 2a. Per-prompt eval discipline (Jake Heller / CoCounsel, `sLFv3RSj_d8`)

The highest-signal eval methodology in the corpus:

1. Write your best-guess prompt for a specific micro-step.
2. Write **10 test cases** — (context, expected correct output) pairs. Make expected outputs objective wherever possible: single-token answers (a number 1–10, or true/false) are much faster and cheaper to evaluate than free-text. Use LLM-as-judge only where objective measurement is hard.
3. Run. **Don't stop at 6/10 passing** — keep working until 10/10. This "is what it takes. Because what you'll find is that you thought your instructions were clear, but then it gets it wrong every time on this dumb question." Each cycle: re-read instructions looking for where they're unclear, change one variable (model, temperature, a single phrase), re-run, log the result.
4. Expand to 50 → 100 → **1,000** tests. The 1,000-test suite MUST come from real user inputs, not imagined ones ("we were shocked by how dumb the legal research queries people put into CoCounsel"). Path to real-world test cases: small beta, tell customers explicitly it will be bad, ask them to report every failure. "That's how to get to a thousand tests. Usually driven by your customers."
5. Release condition: **999/1,000 passing** (99.9%). "You have some degree of reliability. It's not guaranteed and you should never tell your customers it's going to be perfect."

**Single-token output trick:** "Format the output so the classification answer comes first" — prompt the model to give a number (1–10) or binary as the literal FIRST token before any explanation. The model "thinks it's about to give a whole explanation" so reasons more carefully before that first token. Then use `stop_words` or `max_tokens=1` to cut output immediately after. "It's like stop talking, right? You've got your fast response. And you also got the AI to think a little harder." Especially relevant for classification steps inside a pipeline, and for eval purposes. Jake notes it's "more true of completion models than chat models" — the world has moved to chat, but "structuring output so the evaluable answer comes first still applies."

**Grounding instruction for retrieval-augmented systems:** "Don't answer based on any of your previously existing knowledge, just based on what I'm giving you right here. If this thing says the sky is purple, the sky is damn purple." This has two effects: (1) prevents hallucination by anchoring to retrieved text; (2) makes retrieval quality visible — if the model, constrained to the context, gives a wrong answer, the retrieval is wrong. Separates the two failure modes.

**Debugging method:** Sit down and read what the AI reads — verbatim. "We just sat down and read — sometimes it's like 10 pages or 100 pages — just read it. And we realized, oh, given this information, I would say the same thing the AI does. It doesn't have the right information." Rule of thumb: "If it's hard for you to read, it'll be hard for the AI to read."

**Per-step model routing via evals:** "There's no rule that says it has to be GPT all the way through if you have seven different steps." Run each step's eval suite against multiple models. "Gemini 2.5 Flash — beautiful model, cheap as [hell]. If it works to your level of satisfaction, use that. Don't use 2.5 Pro." The eval infrastructure is the prerequisite for intelligent model routing, which is the prerequisite for intelligent cost management.

**Reinforcement fine-tuning vs. supervised:** SFT required thousands of examples for marginal gains; sometimes made the model "dumber" by learning noise alongside signal. RFT requires **maximum 50–100 examples** + an objective quality judge function. "The hardest part is creating the objective quality judge." CoCounsel at Thomson Reuters scale has "hundreds if not thousands of prompts" — each with its own eval suite, each being RF-tuned to push from 990/1,000 to 999/1,000.

**Byte-path implication:** The eval layer is outside Lancelot's byte path. But Lancelot LACKS an eval primitive (`define_outcome`, `judge_panel`) per the lens. The single-token output trick applies anywhere Lancelot emits a structured classification in a tool response — tool output schemas should support requesting first-token classification before explanation.

---

### 2b. Eval-as-feedback-loop for memory (Sam Bhagwat / Mastra, `FTokJt1ioeg`)

LongMemEval (500 questions across 5 categories, released late 2024) as a tight eval loop for memory improvement:

1. Single-session information extraction
2. Multi-session reasoning (cross-session synthesis)
3. Temporal reasoning (time-stamped events, which is more recent?)
4. Knowledge updates (overwrite stale values with current ones — the ChatGPT identity conflation bug)
5. **Absence awareness** — the hardest: "can the agent correctly recognize when it does NOT have the information needed to answer?"

Starting at 67%, state-of-the-art at ~72%, four changes closed the gap:
- **Change 1:** LLM-generated tailored working memory template per question (domain-specific attribute tracking, not generic template)
- **Change 2:** Targeted field-level working memory updates instead of rewriting the full document. Full-document rewrites caused LLM to alter fields that should be unchanged. "Identify the specific field being updated and overwrite only that field."
- **Change 3:** Correct date handling for temporal reasoning — benchmark conversations occurred at specific historical dates but were being run at a different date; fixing this moved accuracy.
- **Change 4:** **Message grouping data structure** — messages grouped by day, with hour-level timestamps within each day group (not a flat chronological list). This gave the LLM explicit session-boundary affordances. "No changes to the underlying vector database or retrieval logic were required — only the presentation format."

Configuration sensitivity: top-K=2 is significantly worse than top-K=5, and top-K=20 is better still. "Configuration is architecturally meaningful, not a minor tuning knob."

Semantic recall: embed → store in vector DB → top-K retrieve, with a **message-range context window around each retrieved message** (analogous to `grep -C N`) — the LLM receives conversational context around the matching message, not just the matching message in isolation. This is a configurable parameter.

Prior success cases in memory can HURT performance: "the agent would slip into a local minima and kind of just like pattern match and get lazy." Past failure cases help (agent escapes stuck loops). **Safe guidance: include past failures in agent memory, not past successes** for tasks requiring genuine reasoning.

**Byte-path implication:** `lancelot.memory_pool` is byte-OOM tracking, NOT agent memory. The typed memory layer (Mastra's semantic recall + working memory + episodic memory) is part of what Lancelot LACKS per the lens.

---

## 3. Context engineering as a production primitive

### 3a. Gather-and-glean framework (Jeff Huber / Chroma, `3jN77Aw7Utk`)

**Gather (maximize recall):** Pull in all possibly relevant tokens — structured data (SQL), unstructured data (vector DBs), local file systems, tools, MCP tools, web search, conversation history. Multi-probe pattern: an LLM generates a **query plan of 10–30 distinct search probes simultaneously** across structured and unstructured sources. Only the slowest probe matters for latency; dramatically increases recall vs. a single probe.

**Glean (maximize precision):** Aggressively remove irrelevant material. Techniques in order of increasing sophistication: top-K by vector similarity → reciprocal rank fusion (RRF, combining multiple ranking signals) → learning-to-rank (LTR, OG IR technique in Elasticsearch) → dedicated reranking models → **LLM-as-gleaner** ("cheating at search"). LLM-as-gleaner outperforms algorithmic approaches for complex queries because it can reason about relevance. "You don't need state-of-the-art models for this" — small, fast, cheap models running in parallel across candidate chunks work well.

**Long context does NOT solve the problem.** Chroma's technical report: model performance on non-trivial tasks degrades precipitously at 10K tokens. Needle-in-haystack benchmarks test the bottom-left quadrant (one needle, zero reasoning required). Real tasks — "do research, synthesize findings, take action" — require both large context AND high reasoning, the upper-right quadrant where performance is worst. "You should curate your context. Long context is not a substitute for selecting the right context." Oracle (human-curated) context dramatically outperforms full context on the same tasks.

**Byte-path implication:** `lancelot.canonical.dumps_canonical` (stable JSON for KV-cache prefix stability) is validated by this source — the gather layer benefits from canonical query representations. The gather/glean architecture itself is outside Lancelot's byte path. The multi-probe parallel pattern is a use case for `TransportPool.fanout()`.

---

### 3b. Context starvation is the root cause of agent slop (Sachin / Skyvern, `1egwM88T3C0`)

"Agents produce slop primarily because they lack context, not because the underlying models are inadequate." New-hire analogy: a new employee tries to be helpful but produces poor output early because they don't know the business. Agents are identical. Three remedies:
1. Good instructions
2. Good context (company email, Slack, Notion, call recordings correlated to customer/product, direct database access)
3. Let agents critique their own work (adversarial sub-agent review)

Organizational restructuring for agent effectiveness: no DMs allowed (all questions in public channel threads so future agents benefit), record every call (internal + external + co-founder 1-on-1s), give agents access to all recorded content. "Remote companies have an inherent structural information architecture advantage" because their context is written/recorded by default.

**PRD pipeline trace:** retrieve relevant call recordings (semantic search over topic) → search Slack → search Notion → search customer communications → draft PRD with specific evidence and links → adversarial sub-agent critique → RICE prioritization (Reach, Impact, Confidence, Effort) to eliminate junk requirements.

**Byte-path implication:** The multi-source context assembly pipeline is outside Lancelot's byte path. `TransportPool.fanout()` would cover the parallel-probe step. Lancelot's `lancelot.mcp.mount` can expose any of these context sources as MCP tools for an agent layer.

---

## 4. Durable state and reliability patterns

### 4a. Checkpoint-restore as the foundational durable primitive (Trigger.dev, `_y7siiS-V5A`)

Matt and Eric (Trigger.dev) articulate the most mechanism-level durable state pattern in the corpus:

**Checkpoint-restore = freeze the complete state of a running computation (CPU, memory, file system) and resume later, potentially on a different machine, when an external condition is satisfied (human response, another agent's output, an external API call).**

Traditional approaches require rehydrating all context each time a task resumes (loading documents, re-running prior steps, rebuilding in-memory state). With checkpoint-restore, exact machine state is preserved and computation continues from exactly where it left off.

**Human-in-the-loop = compute optimization.** When the task pauses waiting for human input, Trigger frees the compute. The machine is not running and incurring cost during the wait period. When the human responds, checkpoint-restore resumes from preserved state. "Simultaneously a cost optimization and a correctness enablement."

**Idempotency:** If a task is paused mid-run and the machine fails before the checkpoint is saved, the task is re-queued and re-run from the beginning — only safe if re-running doesn't have adverse side effects (double-charging, double-sending, double-committing). Trigger's SDK provides idempotency primitives to attach to task definitions.

**Two-phase workflow pattern (Icon.com):** Phase 1 = eager context assembly (run proactively while user is doing other things — classifying uploaded assets, pre-processing documents). Phase 2 = on-demand action generation (triggered by user intent). Human review occupies the gap between phases; compute is freed during review. This is a general design pattern: "identify what context can be assembled proactively and what must wait for user specification."

**Byte-path implication:** Trigger is the execution substrate / queue layer **below** the agent orchestrator. Lancelot is the HTTP transport layer. These are orthogonal: a Lancelot-served agent endpoint would queue its long-running tasks into Trigger. Lancelot's `graceful drain` + `per-route timeout` + `SIGTERM handling` cover the HTTP serving side; durable checkpoint-restore for the agent's internal compute is outside Lancelot's scope. TransportPool already provides client-disconnect→upstream-cancel which is the Lancelot-side of the compute-free pattern.

---

### 4b. Memory file architecture as durable policy store (Parahelp, `GciivYKTK_Q`)

Parahelp's architecture is not ReAct, not workflow — a policy-guided tool-invocation agent. The agent has: general tools, a memory file system containing **natural-language policies**, and an inference engine that interprets the current ticket context against those policies to determine tool-call sequences.

**Memory files = versioned policy store.** When a new policy is committed via the assistant's configure-and-test loop (with mandatory test-gate before promotion), it is immediately available to every agent instance. No propagation delay, no inconsistent state during rollout. All 700 agents spawned for a mass follow-up share the same memory files at the moment of spawn.

**Tool-call reliability = validate function + YAML eval framework + real-time monitoring.** The `validate` function on every tool call (guardrails before execution), a YAML test framework with mock contexts (offline eval), and real-time monitoring of tool-call success rates in production. "A Stripe tool that calls the API correctly 90% of the time — and silently fails or mischarges the other 10% — is worse than no automation at all." Production monitoring watches every tool call across all customers, surfaces anomalies in tool-call success rate, policy compliance rate, and escalation rate. The combination of offline eval + online monitoring is the full quality loop.

**Model selection via internal eval, not public benchmarks.** Parahelp remained on Anthropic's model family throughout without switching — but the specific decision was driven by internal benchmark: "this model for the first time had enough intelligence on our internal benchmarks to actually be able to generate eval sets on the fly, given a batch of past tickets and existing policy files." Switching model families would require re-validating the entire eval framework against the new model's behavior.

**CLAUDE.md as reviewed artifact.** "We basically test those for weeks before anyone's allowed to change them." The prompts that define how the agent behaves across all sessions are more important than any individual code change — a wrong line in a prompt causes wrong behavior across every session until fixed. System-prompt-as-source-code insight applied operationally.

**Byte-path implication:** Lancelot's `lancelot.mcp.mount` provides the tool exposure layer. The memory file / policy store versioning is outside Lancelot's byte path. Lancelot's `lancelot.canonical.dumps_canonical` provides KV-cache prefix stability for policy file hashing.

---

## 5. Cost discipline

### 5a. Token budgeting as the primary constraint

**"Burn tokens, not headcount"** (Jared Friedman). Companies at YC Demo Day showing ~5x more revenue per employee than 18 months ago. "I think you're going to be constrained on token usage, not on headcount really, really soon." Track token usage per person as an informal diagnostic (not a performance metric) — high usage = frontier experimentation. "Directionally, figuring out who in your organization is token maxing, who is not, is a good way to think about which employees you should be spending your time with."

**40% context ceiling = cost and quality optimization** (Dex Horthy). Keeping context utilization under 40% at all times is BOTH a quality discipline (avoids hallucination under context pressure) AND a cost discipline (each compaction-and-restart spends fewer tokens on search/re-discovery and more on implementation). The productive-token-use maximization goal: "spend as many tokens as possible means use the full available capacity productively, not consume tokens on search, re-discovery, and noise."

**Per-step model routing** (Jake Heller). "If you can go with GPT-4o mini as opposed to GPT-4o, you might save a lot of bucks. Gemini 2.5 Flash — beautiful model, cheap as hell. If it works to your level of satisfaction, use that. Don't use 2.5 Pro." The eval infrastructure is the prerequisite — without per-step evals, you can't compare models meaningfully.

**Single-token output = 200x latency and cost reduction** on classification steps. Generating a 200-token explanation where only the first token is needed: stopping after one token is a ~200x reduction on the decoding step.

**Byte-path implication:** Lancelot's `lancelot.upstream_metrics` (token extraction across 3 provider shapes, `calc_cost_usd`, cost accumulates into parent via `lancelot.context.cost_usd`) covers the cost-tracking side. `@app.batch` (`~12× throughput / ~16× fewer upstream calls` vs. rate-limited LLM backend) is a direct cost-reduction primitive. `TransportPool.fanout(first_n=, deadline=, cancel_laggards=True)` covers the "only take first N responses, cancel the rest" cost discipline. `lancelot.canonical.dumps_canonical` covers KV-cache prefix stability (>80% cache hit → 5-10x token + TTFT win cited in DEEPLEARNINGAI corpus).

---

## 6. Distribution and open-source strategy

**"Agents as distribution, developers as users"** (Firecrawl panelist, `RMmEB9ajmdo`). Recall.ai discovered coding models recommend their product constantly — "entirely without deliberate marketing effort." Mechanism: well-documented in public developer content (GitHub repos, Stack Overflow, blog posts), which ends up in training data, which causes model recommendations. Open-source = larger training footprint → more LLM recommendations → higher discovery.

**Documentation as context-window engineering** (panel, `RMmEB9ajmdo`). Old docs: extensive exposition, multi-page tutorials, deeply linked cross-references. New docs: designed to fit inside a coding agent's context window in a single load, no multi-page hyperlinked structure. "Agents working through integrations with new docs get much higher accuracy and make much fewer mistakes." The mechanism: an LLM attending to a single coherent document maintains accurate context; forced multi-page navigation loses coherence at each step.

**MCP server as LLM-readable product surface** (Trigger.dev, Infisical). Trigger built an MCP server exposing all documentation, plus internal "skills" — structured guidance documents giving any coding agent specific conventions about how Trigger's TypeScript SDK should be used. "Two users now: human user and LLM user." The LLM user has different needs and requires different investment.

**Byte-path implication:** Lancelot's `lancelot.mcp.mount` provides the MCP server mount. The documentation-as-context-window engineering insight applies directly to Lancelot's own documentation — docs should be structured for single-load context consumption, with CLI-first access patterns (MCP tool calls, not deep-linked HTML).

---

## 7. Agent architecture patterns (production examples)

### 7a. Multi-party real-time sub-agent protocol (Giga, `KmdN6OQ_PwE`)

Address-swap delivery confirmation flow: AI agent is on a call with a Dasher, needs to confirm an address change with the customer. Rather than put Dasher on hold: the agent makes an **outbound call to the customer in parallel while still on the call with the Dasher** — two calls running simultaneously, both talking to the same underlying AI agent system. Confirm customer's address change, then mark the Dasher's order as delivered.

The enabling primitive: a protocol that allows making outbound calls from within a single agent session, effectively spawning a sub-agent mid-conversation with full shared context. "We added specifically because the initial product was not the best experience — we shipped a new protocol to support it."

**Context scope as the selection criterion for AI verticals:** Customer support and coding are currently tractable because "when a customer opens a support ticket, the agent learns everything relevant about the customer's issue within that conversation context window. The scope of context needed is narrow and self-contained." As context windows expand and models improve, more enterprise verticals open up. The current sweet spot = context-scope-bounded tasks.

**Byte-path implication:** `TransportPool.spawn_child(parent_request, session_affinity)` snapshotting parent traceparent + request_id + agent_id is the Lancelot-side primitive for sub-agent context inheritance. Multi-party parallel outbound is `TransportPool.fanout()`.

---

### 7b. The self-improving agent loop in production (Jared Friedman / YC, `t-G67yKAHBQ`)

YC's monitoring agent writes code, submits a PR to the YC codebase, routes the PR through an agent code reviewer, merges, deploys — without human intervention. The loop: **observe failure → diagnose why (tools? skills file? DB view?) → write fix → PR → review → merge → deploy → human observes success next morning**. "I think you can reimagine what a company is as a set of recursive self-improving AI loops."

The key condition: every step of the loop runs without (or with minimal) human intervention. The human is in the *verification* loop, not the *improvement* loop.

**Byte-path implication:** Outside Lancelot's scope. The Lancelot byte path is the HTTP transport substrate these loops execute over.

---

### 7c. Python-as-first-class-primitive in agent platforms (Giga, `KmdN6OQ_PwE`)

"Make Python a first-party member of the product itself — operators can write Python directly inside the platform to handle arbitrary business logic." The AI FDE layer: an operations-team member describes a desired outcome in plain English, Giga's AI writes the Python to implement it. "Nothing custom is built for any single customer" — every feature required by any customer must ship as a general product capability available to all. This constraint forces generality.

**Byte-path implication:** Lancelot's `@app.tool` and `lancelot.mcp.mount` are the mechanism through which custom Python logic is exposed as agent-callable tools. The platform-side of "write Python from NL and deploy it as a tool" is outside Lancelot's scope.

---

## 8. Anti-patterns to refuse

1. **Optimize per-chunk `@app.stream` overhead for LLM streaming.** Already in Tier-D. The corpus confirms: "real LLM token streaming is upstream-rate-limited (~50-200 tok/s/stream), so the framework is never the bottleneck." Zero corroboration in this corpus for per-chunk optimization.

2. **Build ReAct as the only agent loop.** Jake Heller: "sometimes it's really linear and sometimes it's agentic — you have to figure out for yourself how would the best person do this." Parahelp explicitly uses neither ReAct nor workflow graph. The correct abstraction is "linear where linear, agentic where agentic."

3. **Context window expansion as a substitute for context curation.** Chroma's technical report: performance degrades at 10K tokens on non-trivial tasks. Oracle-curated context dramatically outperforms full-context. Longer context window ≠ better accuracy. The gather-and-glean discipline is durable across generations.

4. **Past success cases in agent memory.** Chroma's research: including successful past solutions as examples can cause agents to "pattern match and get lazy" — they replay prior solutions rather than reasoning from scratch. Past failure cases help; past success cases potentially harmful for tasks requiring genuine reasoning.

5. **LLM-supervised fine-tuning as the quality lever.** Jake Heller: SFT "you'd go through thousands of different examples just for it to kind of improve in the same rate... in fact, maybe even get dumber." RFT (50–100 examples + objective quality judge) goes "a really long way." The bottleneck is building the objective quality judge function, not the training examples.

6. **Agentic loop for linear pipelines.** "Adding an agentic loop to a linear task adds latency, adds unpredictability, and makes the pipeline harder to eval." Default to linear (`def step_one, step_two, step_three`); add agentic branching only where the best human would loop back.

7. **Generic working memory templates.** Mastra's Change 1: LLM-generated tailored templates per domain substantially outperform generic templates. Template design is a first-class concern, not an afterthought.

---

## 9. Deprecating tactical patterns (churn on ~1-2yr model clock)

- **Model-specific temperature and thinking budget tuning.** Jake Heller notes Gemini's temperature range "is somehow possible up to 2 or 10 — I was surprised." Model-specific quirks churn each release. The durable discipline is "run your eval suite at temperature zero, 0.5, default" and let the eval pick the winner — the process is durable, the specific values are not.
- **Top-K specific values in RAG.** Mastra's data shows sensitivity from top-K=2 to top-K=20. The durable principle is "treat configuration as architecturally meaningful" and measure via eval. The specific optimal top-K value will shift with each model generation.
- **Specific model recommendations.** Jake Heller names Gemini 2.5 Flash specifically. These deprecate within months. The durable mechanism is "run your per-step eval suite against multiple candidate models and pick the one that passes at lowest cost."

---

## 10. API shape notes (ergonomics worth stealing)

**Tool output: answer-first ordering.** Jake Heller's single-token trick applied to tool responses: structure tool outputs so that the evaluable/actionable answer comes as the first token or field, before any explanation. Lancelot's `@app.tool` `output_schema` already auto-derives the schema — but the field ordering and the "answer-first" principle should be documented as a best practice.

**Memory working template = domain customization layer.** Mastra's working memory template approach: the template specifies which attributes the system tracks about the user and is the primary customization point. For a personal trainer AI vs. a legal research AI vs. a customer support AI, the tracked attributes differ entirely. Lancelot's agent layer should expose the working memory template as the configurable domain adaptation surface.

**Intentional compaction progress file format** (Dex Horthy, HumanLayer): `{goal, accomplished, files_touched_with_changes, things_tried_and_why_failed, next_steps, constraints_established}`. This is the minimum viable format for context handoff across agent context boundaries. Could be a Lancelot primitive: `ContextBundle.serialize_progress()`.

**Parahelp's YAML test framework with mock contexts:** offline eval = (mock context + expected tool-call sequence + expected response). Enables testing every tool integration without production side effects. Separates "does the agent call the right tools" from "do the tools work correctly."

---

## Summary: what this source adds to the Lancelot build

| Signal | Mechanism | Already in Lancelot? |
|---|---|---|
| World's-best-human architecture as version-zero | Step 2 in CoCounsel methodology | No — agent loop missing |
| Skills file as declarative capability manifest | Monitoring agent updates overnight without retrain | No — agent loop missing |
| Policy/decision layer (what needs human approval) | 5-layer self-improving loop, layer 2 | No — HITL missing |
| Eval-first: 10→50→100→1000 test cases, 99.9% pass | Per-prompt eval discipline, CoCounsel | No — eval layer missing |
| Single-token answer-first in tool responses | max_tokens=1 + stop_words | Partial — `output_schema` exists, ordering not documented |
| Gather-and-glean: 10–30 parallel probes + LLM glean | Multi-probe query plan pattern | TransportPool.fanout() covers the fanout; no glean layer |
| 40% context ceiling + intentional compaction | Research→Plan→Implement with progress file | No — context management missing |
| MCP tool minimal-return discipline | >4KB WARN | YES — `lancelot.mcp.mount` already has this |
| Canonical JSON for KV-cache prefix stability | `dumps_canonical` | YES — `lancelot.canonical.dumps_canonical` |
| Cost tracking + per-call accumulation | `cost_usd` ContextVar, `X-Lancelot-Cost-USD` | YES |
| Sub-agent context inheritance | `spawn_child` traceparent snapshot | YES |
| Targeted field-level memory writes | Working memory updates | No — agent memory missing |
| Message grouping by day + hour timestamps | Memory presentation format | No — agent memory missing |
| Idempotency for durable task execution | Trigger.dev SDK primitives | No — outside Lancelot scope (byte path) |
| Checkpoint-restore for HITL pause | Trigger.dev infrastructure | No — outside Lancelot scope |

**The core finding:** this corpus confirms the lens's split exactly. The byte path (transport, serialization, streaming, cost tracking, canonical JSON, sub-agent context, MCP minimal-return) is already in Lancelot and validated by production examples. The agent half (eval framework, policy/decision layer, typed memory, context budget management, durable state) is uniformly missing and uniformly confirmed as the hard production-differentiating work.
