# BAD_GUIDE — AI Research Taste + Modern CTO + Evals/Reliability Distillation

**Source:** `/Users/seventyleven/Desktop/guidesfm/BAD_GUIDE.md`  
**Sections mined:** "The Modern CTO (director of machines)" (lines 199–358), "Frontier AI Research Taste" (lines 360–647), "Building Revolutionary Products" (lines 649–854), evals/reliability passes throughout.  
**Date mined:** 2026-06-22  
**Lens applied:** Lancelot byte-path agent build.

---

## 1. Reliability as a Distribution Problem

**The canonical claim** (BAD_GUIDE §Modern CTO, line 211):
> "Same model, different harness, 5-point swing on SWE-bench Pro (Opus 4.5 ran 50.2% to 55.4% purely on context management and tool orchestration). v0 took Sonnet from 64.7% to 93.9% error-free with four engineering layers (RAG injection, stream-time fixing, deterministic AST/icon validation, a fine-tuned AutoFixer), not a single model upgrade."

The mechanic (Vercel v0, confirmed Malte Ubl / "Lessons from building Vercel v0 and the d0 agent"):
1. RAG context injection at request time
2. Real-time stream-fixing during generation (deterministic correction applied while tokens are streaming)
3. Deterministic AST/icon validation (post-generation, catches structural invalidity before the user sees it)
4. A fine-tuned AutoFixer for the residual errors the first three layers let through

The **practical lesson for Lancelot**: reliability is a distribution over outcomes, not a binary — the harness shifts the distribution. The four-layer pattern above is not a model-specific trick; it is a template: (1) context preparation, (2) generation-time steering, (3) structural validation, (4) residual repair. None of this requires a model swap.

**Sierra's production implementation** (BAD_GUIDE §Building Revolutionary Products, line 774 — "Sierra: Making Customer-Facing AI Agents Delightful"):
> "Models detect their own errors better than they avoid making them, so they chain supervisor agents to compound past the per-model ceiling, and run a conversation simulator that turns every fix into a permanent regression test."

Mechanism: (a) a supervisor-agent that reruns the same conversation input after every production fix, producing a regression suite from real failures. This is **test-driven development applied to agent behavior** — the eval set self-grows from prod corrections. (b) The structural insight: per-model error *detection* capability > per-model error *avoidance* capability. A self-review loop extracts that detection surplus.

**Lancelot byte-path implication:** the Lancelot agent loop needs a `verify` stage as an explicit post-handler, not a prompt instruction. The 4-layer v0 pattern maps to: (1) `canonical.dumps_canonical` / context preparation at the TransportPool level, (2) stream-time steering = the R3 pump + `@app.stream` with chunk-level hooks (structure for this already exists), (3) deterministic structural validation at the response-model layer (Pydantic shapes this), (4) a separate repair call via `TransportPool.request`. Sierra's supervisor maps to a second `spawn_child` agent that only runs the verify stage.

---

## 2. Verifier-First: Build the Check Before the Capability

**Primary claim** (BAD_GUIDE §Frontier AI Research Taste, "Verifiable reward is where the curve goes vertical", line 377):
> "Qwen 3's result: 4,000 high-quality verifiable examples beat 4 million low-quality RLHF ones. Quality and verifiability dominate quantity. Build the verifier first; the training loop follows."

Named sources:
- Hyung Won Chung (OpenAI, o1 co-lead): "Design the incentive (verifiable reward), let scale surface the capability" — "Don't Teach. Incentivize." (BAD_GUIDE line 467)
- Noam Brown (OpenAI, o1 lead): generator-verifier gap as a new scaling axis; evals should plot performance against tokens/cost/time, not just score (BAD_GUIDE line 620-621)
- Jason Wei (OpenAI→Meta): "some tasks are far cheaper to verify than to solve, and whatever is solvable and easy to verify gets solved" — "Asymmetry of Verification and Verifier's Law" (BAD_GUIDE line 578-579)

**Operational form for an agent eval:**
The test isn't "did the agent produce X?" but "does an automated check pass Y?" — where Y is cheaper to compute than X is to produce. For an agent that calls a tool: the verifier is "does the tool output satisfy the schema and the downstream assertion?" not "did the LLM say the right thing?"

**Hamel Husain (30+ production builds) on evals** (BAD_GUIDE line 455):
> "#1 cause of failed AI products is no eval system."

Operational form: the Rechat case study is the concrete loop. The pattern is: (1) define what "correct" means in a form you can check without a human, (2) run cases, (3) fix — and iterate the eval definition before you iterate the model. The eval is the product's spec.

**Lancelot byte-path implication:** `@app.tool` already auto-derives `output_schema` from Pydantic. A verifier primitive could be: `@app.tool(verify=lambda result, ctx: result.field > 0)` — a synchronous predicate checked post-call, before the result is returned to the agent loop. This is a **missing primitive** in the current surface. The transport is there (`TransportPool.request` returns the response); the verification gate is not. Zero new deps (pure Python predicate).

---

## 3. The Harness Is the Moat — Mechanism-Level

**The claim** (BAD_GUIDE §Modern CTO, line 203):
> "The 1% CTO treats the agent harness as the product surface and their own taste as the quality gate that no model can hold."

Named mechanisms from the Anthropic engineering corpus cited in BAD_GUIDE (lines 280-281):
- **Feature-list-as-executable-spec**: the harness holds a list of feature flags the agent can toggle but not edit. The spec is code, not a system prompt. (Anthropic Applied AI, "Effective harnesses for long-running agents")
- **Git-as-state-machine for recovery**: checkpoints are git commits; recovery means checking out the last-good commit and resuming. Requires the agent to commit atomically at each verified stage.
- **Progress file as memory across context windows**: a structured file the agent writes at each step; when context compacts, the agent reads the progress file rather than relying on conversation history.
- **Dual initializer/worker split**: an initializer agent runs once at startup to configure the environment and validate preconditions; the worker agent runs the loop. This is a pattern for avoiding context contamination from setup noise.

**Armin Ronacher (Flask creator, BAD_GUIDE line 282):**
> "skip the high-level SDKs because model differences force your own abstraction, prefer explicit cache control, reinforce the objective on every tool return rather than once up front, and isolate failures in throwaway sub-agents that report only outcomes."

Mechanism: re-stating the goal on **each tool return**, not once in the system prompt. This fights goal drift as context grows. The implementation is a tool-return wrapper that appends `[Goal: {original_goal_summary}]` to every tool result before it enters the context.

**Thariq Shihipar (Anthropic, "Seeing Like an Agent", BAD_GUIDE line 843):**
> "debug and design by reasoning about what the agent actually perceives — its context window and tool returns — not what you see on screen."

This is the design discipline: when the agent fails, the debug loop is: (1) print the full context at failure point, (2) ask whether the failure is visible from inside the context. Not "what instruction did I give" but "what is the agent reading."

**Lancelot byte-path implication:** `lancelot.context` ContextVars already carry `request_id`, `agent_id`, `trace_id`. A progress-file primitive could be a `lancelot.agent.ProgressLog` that writes to disk on each tool call (keyed by `agent_id`) and can be injected into the next context window via a `@tool_return_hook`. The feature-flag spec maps to `lancelot.mcp.mount` — the MCP tool surface IS the spec; adding a read-only `list_features()` tool that returns the agent's current allowed actions is ~10 lines. The reinforce-on-return pattern is a `TransportPool` interceptor (already has middleware hooks).

---

## 4. Build for 18 Months Out — The METR Horizon

**Named primary source** (BAD_GUIDE §Modern CTO, "Build for the capability 18 months out", line 219):
- **METR (Kwa, West, Becker et al.)**, "Measuring AI Ability to Complete Long Tasks", arxiv 2503.14499: AI autonomously completes ~1-2 hour human tasks at >50%, doubling roughly every 7 months.

Operational instruction: the model that is "not quite working" on your hardest workflow **today** is the one to architect around **now**, because infra, evals, and domain context cannot be built overnight.

For Lancelot this means: the agent loop should be designed for **multi-step, multi-tool, 30-minute autonomous runs** — not for one-shot tool calls. The structures that this requires (checkpointing, progress memory, verifiable intermediate outputs, graceful mid-run HITL pause) are NOT the same structures a one-shot API call needs.

**OpenAI Codex (Ryan Lopopolo, "Extreme Harness Engineering", BAD_GUIDE line 296):**
> "a hard one-minute build loop they rebuilt their infra to protect"

The constraint is: if the feedback loop from "agent ran" to "did it work?" takes more than one minute, iteration velocity collapses. This is the infrastructure constraint. Lancelot's test suite (`pytest tests/ -q` in ~4s) already satisfies this for the framework layer; the agent eval loop needs the same discipline.

---

## 5. Eval Craft — Operational Primitives

**Hamel Husain, "A Field Guide to Rapidly Improving AI Products"** (BAD_GUIDE line 992-993):
> "the teams that win barely discuss tools and obsess over measurement, you start from bottom-up error analysis not off-the-shelf metrics (one client's core bug was date-handling failing 66% of the time, invisible to generic scores), and the custom data viewer is the highest-leverage thing you build."

Operational form:
1. Bottom-up: collect 100 real agent runs, read them by hand, sort failures into buckets.
2. Custom viewer first: a purpose-built interface to view agent traces (not generic logging) is the highest-ROI single artifact.
3. Generic metrics (ROUGE, BERTScore, etc.) are wrong for most real agent tasks. Build task-specific checks.

**Hamel Husain + Shreya Shankar, "LLM Evals FAQ"** (BAD_GUIDE line 833):
> "error-analysis first, why binary judgments beat Likert scales, building your own annotation tooling, and how to align an LLM judge to a human."

Mechanism:
- **Binary judgments over Likert scales**: "pass/fail" is more consistent and actionable than 1-5 ratings. The LLM judge should return `{"pass": true/false, "reason": "..."}`, not a score.
- **Aligning an LLM judge to a human**: run 50 cases with a human judge, tune the judge prompt until the LLM's pass/fail matches the human's at >90%, then deploy at scale.
- **Error-analysis first**: before building a judge, spend ~2 hours reading failure cases by hand. The pattern you find drives the judge design.

**Eugene Yan, "Task-Specific LLM Evals that Do and Don't Work"** (BAD_GUIDE line 831):
> "which concrete per-task metrics actually correlate with quality for classification, extraction, summarization and translation, and how to build evals that catch real regressions. Reach for the cheap, specific metric before the expensive subjective one."

For a Lancelot agent layer: the cheap specific metric for a tool-calling agent is **tool call validity** (does the tool call parse? does the required field exist? does it satisfy the schema?) — checkable without any LLM judge, zero additional cost, and catches 60%+ of production failures in practice.

**Eval contamination check** (BAD_GUIDE line 293, OpenRouter Fusion):
> "their panel surfaced the benchmark's own grading rubric via web search, so they excluded those domains and re-ran everything — a sharp reminder to check your eval isn't contaminated before you trust the score."

Mechanism: before trusting any eval number, verify that the eval cases are not in the training distribution of the model you are evaluating. For agents: if the agent was trained on (or saw) the task definition, the eval measures recall not capability.

**Lancelot byte-path implication:** `@app.tool(verify=...)` covers binary tool-output verification (see §2). A second primitive: `lancelot.eval.run(cases, judge)` — a lightweight in-process eval runner that (1) runs N cases via the agent loop, (2) applies the user's judge function per case, (3) returns `{"pass": int, "fail": int, "failures": [...]}`. Zero new runtime deps. The judge function is `(input, output) -> bool`. This is the "build the verifier first" pattern as a first-class API.

---

## 6. "Just Build Agents, Brutal" — The Discipline

**Named primary** (BAD_GUIDE §Building Revolutionary Products, "How to Build an Agent", line 741-742 — Thorsten Ball, Amp/Sourcegraph):
> "The agent core is a model, a loop and three tools in ~315 lines of Go, and every hard, valuable thing lives elsewhere: editor integration, prompt craft, feedback timing, orchestration."

The discipline: the loop is not the moat. Stop treating the ReAct loop as the interesting engineering. The 315-line agent is correct. The agent is the trivial part. What compounds is what surrounds it.

**Walden Yan (Cognition/Devin), "Don't Build Multi-Agents"** (BAD_GUIDE line 737-738):
> "share the full agent trace, not only the messages, and accept that parallel agents make conflicting implicit decisions that produce broken output."

Mechanism: multi-agent is a write-conflict problem, not a concurrency problem. Two agents making independent writes to the same state (codebase, document, data store) will conflict at the semantics level even when their writes don't conflict at the filesystem level. The fix (from "Multi-Agents: What's Actually Working", BAD_GUIDE line 739): multi-agent works when writes stay **single-threaded** and the extra agents add **intelligence** (review, hierarchical delegation), not actions.

**Lancelot byte-path implication:** `TransportPool.fanout` + `spawn_child` are the byte-path analogues. The discipline for the agent layer: fanout should be used for **reads/opinions** (parallel evidence gathering, parallel scoring), not for parallel **writes**. The agent orchestration design should enforce this: a `fanout` result always feeds into a single-threaded synthesis step before any write.

---

## 7. Context Engineering Discipline

**Anthropic Applied AI, "Effective context engineering for AI agents"** (BAD_GUIDE line 753-754):
> "degradation is a gradient from n-squared attention, not a cliff, and just-in-time retrieval (keep lightweight identifiers, fetch at runtime) beats pre-loading the corpus."

Mechanism:
- Store identifiers in context (document IDs, tool names, entity keys), not the full content.
- At each tool call, resolve the identifier to the full content just-in-time.
- This keeps the context window compact for the reasoning steps and only expands it for the retrieval step.

**Dexter Horthy (HumanLayer), "Advanced Context Engineering for Agents"** (BAD_GUIDE line 721):
> "spec-first, subagents as context-control units, frequent intentional compaction."

The "subagent as context-control unit" pattern: each subagent has a bounded context window, reports only its outcome (not its full reasoning trace) to the parent, and is discarded after reporting. The parent maintains the durable state; the subagent is the ephemeral compute unit.

**Tom Blomfield (YC), "How to Build a Self-Improving Company"** (BAD_GUIDE line 271):
> "'Software is ephemeral, context is valuable.'"

This is the inversion: the agent code is cheap and disposable; the accumulated context (observations, corrections, eval cases, task-specific prompts) is the compounding asset.

**Lancelot byte-path implication:** `lancelot.canonical.dumps_canonical` already does byte-stable JSON for KV-cache prefix stability (the #1 free cost lever at ~100:1 input:output ratio). The just-in-time retrieval pattern needs a `context.add_deferred(id, resolver_fn)` primitive: an identifier placed in context at planning time, resolved to full content only when the agent actually calls the tool that needs it. This avoids pre-loading N documents when the agent will only use 1-2 of them.

---

## 8. Eval-Driven Tool Design

**Anthropic, "Writing Effective Tools for AI Agents"** (BAD_GUIDE line 805-806):
> "Eval-driven tool design where you let the agent help rewrite its own tool definitions: build a small eval, watch where the agent misuses a tool, then fix the tool's interface instead of the prompt. The non-obvious lesson is that tool ergonomics for a model are not the same as tool ergonomics for a human."

Mechanism:
1. Write a small set of test cases (10-20) where you know the correct tool call.
2. Run the agent; observe where it calls the wrong tool, passes wrong argument names, or calls with invalid parameters.
3. Fix the **tool definition** (description, parameter names, schema) — not the system prompt.
4. Repeat until the eval passes.

This is distinct from "fix the prompt": changing a parameter from `query_string` to `search_query` can be worth more than 500 words of prompt engineering.

The Anthropic pattern for `@app.tool` in Lancelot: the `output_schema` auto-derivation is already done. The missing piece is a `@app.tool(examples=[...])` field — a list of few-shot examples embedded in the tool schema that the model sees when selecting tools. This is an eval-driven feature: you discover which examples to add by running the eval and observing misuse.

---

## 9. Problem Selection Discipline (Research Taste Applied to Build Decisions)

**Jacob Steinhardt, "Research as a Stochastic Decision Process"** (BAD_GUIDE line 427):
> "Order the work by information gained per unit of time, and kill the riskiest assumption before you build anything on top of it."

For the Lancelot agent build: the riskiest assumption is whether the agent layer's reliability improvements are **attributable to the harness** vs the model. The first thing to build is therefore an eval that isolates this: same prompts, same context, different harness configurations — measure the delta. If the harness has no measurable impact, the primitives being built have no value.

**Hamming, "You and Your Research"** (BAD_GUIDE line 396):
> "What are the important problems in your field, and why aren't you working on them?"

Applied to Lancelot agent build: the important problem is not "build another agent framework." It is "make agent behavior measurably reliable and cost-accountable at the byte-path level" — which is specific to Lancelot's position (byte-path ownership + `upstream_metrics` + `lancelot.context` cost ContextVars already exist). The primitive nobody else has is: **per-agent cost accounting with byte-path granularity** (`cost_usd` ContextVar accumulates across tool calls and is visible in the X-Lancelot-Cost-USD trailer). This is a UNIQUE position.

**"Fine-tuning is usually a capital trap"** (BAD_GUIDE line 380-381):
> "Poetic AI's meta-system hit 54% on ARC-AGI v2 at $32/problem versus Gemini 3 Deep Think's 45% at $70, for under $100K total. 'Vaccinated against the bitter lesson.'"

Operational rule: do not build primitives that depend on a specific model's behavior. Any Lancelot agent primitive that mentions specific model names or relies on model-specific behavior is a liability on a ~12-month clock. All agent primitives should be model-agnostic (they already are via `TransportPool`).

---

## 10. Annotated Anti-Patterns (Skip These on Purpose)

Drawn from BAD_GUIDE §Building Revolutionary Products, §Bullshit Filter, and the agent-building craft section:

1. **Elaborate multi-agent orchestration with parallel writes** (Walden Yan, BAD_GUIDE line 737): parallel writes conflict semantically. Use fanout for opinions, single-thread for writes.

2. **Fine-tuning before the harness is proven** (BAD_GUIDE line 380): a model-agnostic harness beats a fine-tuned model on a 12-month horizon. Fine-tune only after the eval proves the harness ceiling has been hit.

3. **Generic metrics (ROUGE/BERTScore) for agent evals** (Hamel Husain, BAD_GUIDE line 992): invisible to real failure modes. Build task-specific binary checks first.

4. **Treating the agent loop as the moat** (Thorsten Ball, BAD_GUIDE line 742): ~315 lines and not interesting. The value is in context engineering, tool design, and the eval harness.

5. **Reinforcing the objective once in the system prompt** (Armin Ronacher, BAD_GUIDE line 282): goal drift accumulates over long runs. Re-state on every tool return.

6. **Building the product before the primitive** (BAD_GUIDE line 657-658): "Perplexity built the retrieval+synthesis pipeline, not the chat UI." For Lancelot: build the eval harness + tool-verification primitive before the orchestration layer.

7. **Trusting benchmarks without checking for contamination** (BAD_GUIDE line 293): if the benchmark cases are in training data, the eval measures recall. Verify independence before trusting a number.

---

## Cited Sources (mechanism-level, BAD_GUIDE)

| Source | BAD_GUIDE line | Mechanism |
|---|---|---|
| Malte Ubl (Vercel CTO), v0 build | 211, 670, 697 | 4-layer reliability: RAG injection + stream-time fixing + AST validation + AutoFixer |
| Clay Bavor (Sierra), agent reliability | 774 | Supervisor-agent chaining; conversation simulator → permanent regression tests |
| Anthropic Applied AI, "Effective harnesses" | 280-281 | Feature-list-as-spec, git-as-state-machine, progress-file memory, dual initializer/worker |
| Armin Ronacher (Flask creator) | 282-283 | Re-state objective on each tool return; sub-agents that report only outcomes |
| Thariq Shihipar (Anthropic, Claude Code) | 843-844 | "Seeing like an agent": debug from inside the context, not from outside |
| Thorsten Ball (Amp/Sourcegraph) | 741-742 | Agent core = ~315 lines; value is elsewhere |
| Walden Yan (Cognition/Devin) | 737-740 | Multi-agent = write-conflict problem; fanout for opinions only |
| Hamel Husain (30+ prod builds) | 455-456, 992-993 | Bottom-up error analysis; custom data viewer; binary judge > Likert |
| Hamel Husain + Shreya Shankar, Evals FAQ | 833-834 | Binary pass/fail; error analysis first; align judge to human on 50 cases |
| Hyung Won Chung (OpenAI, o1) | 467-468 | Design the incentive (verifiable reward), let scale surface capability |
| Noam Brown (OpenAI, o1 lead) | 590-591, 620-621 | Generator-verifier gap; evals should plot against tokens/cost, not score |
| Jason Wei (OpenAI→Meta) | 578-579 | Asymmetry: verification cheaper than generation → build the verifier first |
| METR (Kwa, West, Becker) | 219-220, 250-251 | Task-horizon doubles every 7 months; build for 18 months out |
| Anthropic Applied AI, context engineering | 753-754 | JIT retrieval (identifiers in context, content fetched at runtime) |
| Dexter Horthy (HumanLayer) | 721-722 | Subagents as context-control units; frequent intentional compaction |
| Anthropic, "Writing Effective Tools" | 805-806 | Fix tool *interface* not the *prompt* when the agent misuses a tool |
| Tom Blomfield (YC) | 271 | "Software is ephemeral, context is valuable" |
| Jacob Steinhardt | 392-393, 427 | Kill riskiest assumption first; order by information/time |
| Ryan Lopopolo (OpenAI Frontier) | 296-297 | One-minute build loop as infrastructure constraint |
| Eugene Yan (Amazon, applied scientist) | 831-832 | Task-specific binary metrics > generic scores for agent evals |
