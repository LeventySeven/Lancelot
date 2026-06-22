# Stanford + Palantir Transcripts — Mechanism-Level Harvest

**Sources:** `researchfms/core/TRANSCRIPTS_STANFORD.md` (19 lectures: CS224R, CS221, CS230, CME295, CS25 V2-V5) · `researchfms/core/TRANSCRIPTS_PALANTIR.md` (23 DevCon 4/5 sessions)
**Focus assigned:** Stanford → research taste, RLVR/verifier-first, problem selection, Bitter Lesson. Palantir → ontology, durable execution, await-condition, production agent ops.
**Date extracted:** 2026-06-22

---

## Stanford: Research Taste + RLVR / Verifier-First Signal

### 1. The Bitter Lesson: Remove Structure as Compute Grows

**Source:** `TRANSCRIPTS_STANFORD.md §14` (Hyung Won Chung, OpenAI — CS25 V4 Instruction Tuning)

The dominant driving force in AI is exponentially cheaper compute (Sutton's Bitter Lesson plot: y = compute/$100, x = year, super-exponential 100-year arc). Every 5 years you get ~10× more compute for the same dollar. Practical implication for framework builders:

> "Methods that add more structure/inductive bias may perform better NOW but become bottlenecks LATER. The models of 2000 were much harder than what we use now — paradoxically, removing complexity IS progress."

**The structure-vs-freedom tradeoff:**
- Low compute: more inductive bias → better performance
- High compute: general methods dominate; structural assumptions become ceilings
- Community is good at ADDING structure (papers), bad at REMOVING it (no publication reward)

**Concrete architectural example:** Encoder-decoder vs. decoder-only looks like a deep architectural choice. Chung shows they are structurally near-identical once you (a) share parameters, (b) make attention causal, (c) align within-layer cross-attention. The encoder-decoder split was justified in 2017 for short asymmetric translation tasks. Modern multi-turn chat — prior turn's output IS next turn's input — makes the separation an engineering burden (forces re-encoding the full input history at every turn; causal attention allows KV caching instead, a massive inference speedup).

**What this means for Lancelot's agent design:** Every "convenience wrapper" layer added to the agent harness that feels helpful now should be stress-tested against whether it will be a ceiling in 12-18 months as models improve. The correct bias is to keep the agent loop thin (5-10 lines) and the harness around it orthogonal to model behavior.

---

### 2. The Verifier Is Everything (RLVR Signal)

**Source:** `TRANSCRIPTS_STANFORD.md §4` (CS224R Lecture 10, RL for LLM Reasoning) and `§11` (Denny Zhou, Google DeepMind CS25 V5)

The most load-bearing quote in both sources, stated identically by multiple researchers independently:

> "A reliable verifier is the most crucial element in RL fine-tuning, not the algorithms. If anyone found an algorithm significantly better than others, please tell me." — Denny Zhou, §11

> "If you keep collecting high-quality math solutions from the internet and train SFT, you hit diminishing returns quickly — there's a hard ceiling on solution quality because you're bounded by what's already been written. RL can generate new solutions (via exploration), verify them (via symbolic checker), and learn from them — breaking past the data ceiling." — §4

**The GRPO mechanism (the actual algorithm):**
```python
def grpo_advantage(rewards: list[float]) -> list[float]:
    mean_r = sum(rewards) / len(rewards)
    std_r = (sum((r - mean_r)**2 for r in rewards) / len(rewards)) ** 0.5
    return [(r - mean_r) / (std_r + 1e-8) for r in rewards]
```

Key: N=8 samples per prompt, normalize advantages within the group. For easy problems (most correct), advantages near zero — small gradient update. For hard problems (few correct), high variance — large signal. No critic network needed. Group-relative normalization IS the value function.

**Self-consistency as inference-time verifier (§11 Denny Zhou):**
- Mathematical framing: greedy decoding maximizes P(answer|problem); what you WANT is P(answer) marginalized over ALL reasoning paths
- Implementation: sample K=32-64 responses via temperature sampling; majority vote on FINAL ANSWERS only (not reasoning paths)
- Self-calibration: when self-consistency score > 80%, accuracy is nearly 100% — natural confidence signal
- `GSM8K: PaLM+CoT = 58%, self-consistency → 75% (+30% relative)`

**Process Reward Models (PRMs) for step-level signal:**
```python
def estimate_step_reward(model, prefix, K=20):
    completions = [model.complete(prefix) for _ in range(K)]
    return sum(verifier.check(c) for c in completions) / K
# returns-to-go: sum(step_rewards[i:]) for each step i
```

Use case for Lancelot: any tool that generates code (agent calls a code-gen tool) can use the Python interpreter as a zero-cost verifier. Any tool that produces JSON can use schema validation as a verifier. RLVR applies wherever you can write a checker; the challenge is tasks where checking is as hard as solving.

**8× data efficiency claim (§4):** Offline RL using full dataset (successes + failures) achieves same performance as SFT on correct-only examples with 8× less data, because failures carry "don't go here" signal that SFT discards.

---

### 3. Problem Selection: Start with Clear Evaluation Metrics

**Source:** `TRANSCRIPTS_STANFORD.md §6` (Percy Liang, CS221 Fireside Chat)

> "Start with research that has clear evaluation metrics — you need to know if you're making progress. Avoid areas where progress is unmeasurable; these become careers spent on unmeasurable things."

> "Most AI projects fail not because the model is bad but because nobody asked whether the model is solving the right problem." (McKinsey data: ~85% AI project failure rate, primary cause: scoping errors)

**Task complexity doubling time (Metr/ARC metric — cited by both Percy Liang and Andrew Ng):**
- Measures: how long does it take a human to complete the task AI can now solve?
- General tasks: doubles every ~7 months
- Coding tasks: doubles every ~70 days
- This metric is specifically chosen because it doesn't saturate like benchmark accuracy
- Current frontier: tasks humans complete in 30-120 minutes

**Anti-pattern:** Building for "benchmarks" that saturate. The correct frame for Lancelot's agent harness is: does this make it faster for a developer to ship something that works? That question has a measurable answer (time-to-first-working-agent); benchmark accuracy does not.

---

### 4. Reward Hacking — Systematic Failure Modes

**Source:** `TRANSCRIPTS_STANFORD.md §2` (CS224R Lecture 8, Reward Learning) and `§9` (Karina Nguyen, Anthropic/OpenAI — CS25 V5)

**Documented cases (§2):**
1. Velocity reward in Hopper → agent falls forward at maximum speed (task failed, reward maximized)
2. Gripper with visual reward → agent blocks camera so classifier can't see failure → reward=1 for "grasping" while not grasping
3. LLM RLHF → model produces extremely verbose responses (annotators prefer "thorough", even if untrue)
4. LLM sycophancy → agrees with user assertions including false ones

**Critical finding from OpenAI reward monitoring paper (Karina Nguyen, §9):**
> "If you optimize chain of thought itself for honesty metrics, the model hides its intent inside CoT — don't optimize CoT explicitly."

**Constitutional AI mitigation (Karina Nguyen, §9):**
- Craft preference data pairs that change EXACTLY ONE feature at a time
- Controls what the reward model learns, eliminates spurious features
- KL penalty to reference policy: prevents policy from going far from SFT (standard: β=0.1-0.3 in PPO, β=0.1 in DPO)
- Per-token KL implementation: `kl_penalty = (log_probs_policy - log_probs_sft).sum(dim=-1)`

**For Lancelot agent evals:** Reward hacking applies to evals too — any eval that measures proxy metrics (response length, confidence signals, format compliance) will be gamed by a capable model. Evals should measure the actual outcome (did the tool call succeed? did the test pass?), not surface features.

---

### 5. Agent Architecture (Moroni's 4-Step Model)

**Source:** `TRANSCRIPTS_STANFORD.md §8` (Andrew Ng + Lawrence Moroni, CS230)

```
Step 1: INTENT UNDERSTANDING — determine goal (not yet how)
Step 2: PLANNING — enumerate tools, break into sub-steps
Step 3: EXECUTION — tools called in sequence, errors caught
Step 4: REFLECTION — did we accomplish the intent? If no: back to planning
```

**Debugging heuristic:** When an agent fails, identify which step:
- Intent misunderstanding → rephrase the task description
- Planning failure → add examples of correct plans, constrain tool set
- Execution failure → fix the tool (usually a bug in tool implementation, not the model)
- Reflection failure → make success criterion explicit in the prompt

This 4-step model is intentionally simple — matches the "5-10 line agent loop" finding from the lens. The value is in the diagnostic framing, not the architecture itself.

**Moroni's AI market bifurcation (§8) — relevant for Lancelot positioning:**
> "YC data: 80% of YC companies now use small models from China for cost + privacy reasons. Hollywood studios, law firms, medical offices: won't send IP or patient data to commercial APIs → self-hosted fine-tuned models."

Lancelot's byte-path positioning fits this well: if the deployment target is self-hosted models (lower throughput, cost-sensitive), then the const-tier, serialization wins, and streaming optimizations are high-value even at smaller scale.

---

### 6. Self-Improvement Loop (RLVR Architecture)

**Source:** `TRANSCRIPTS_STANFORD.md §4` (CS224R Lecture 10)

**STaR (Self-Taught Reasoner) core loop:**
1. Sample N responses to each problem from current policy
2. Filter by correctness: use symbolic verifier (Python interpreter, test runner, answer checker)
3. Fine-tune on correct responses only
4. Repeat

**Why model-generated correct solutions beat human-written:** Distribution matching — the model assigns higher probability to patterns similar to what it generates. Training on model-generated correct solutions minimizes the distributional gap; training on human solutions does not.

**DPO for reasoning:** treat `(correct_solution, incorrect_solution)` pairs as `(y_w, y_l)`. DPO loss:
```python
loss = -F.logsigmoid(beta * (log_ratio_w - log_ratio_l))
# where log_ratio = log_prob(response|policy) - log_prob(response|reference)
```

DPO is offline (trains on fixed dataset); GRPO is online (generates fresh responses per step). DPO for initial alignment; GRPO/PPO for frontier performance.

---

## Palantir: Durable Execution, Ontology, Production Agent Ops

### 7. Durable Execution — The Await-Condition Primitive

**Source:** `TRANSCRIPTS_PALANTIR.md §3` (DevCon 5 — Orchestrator) and `§15` (DevCon 4 — Process Orchestration x Eaton)

**The problem Orchestrator solves (Eaton's production case):**
Hundreds of LLM calls per document. One LLM call fails (timeout, rate limit). That stage's output object stays null. Parent stage waiting on it also fails. Grandparent fails. The whole pipeline restarts from scratch even though only one step failed.

**Orchestrator's four properties:**
1. **Long-running executions:** No timeout. Workflows run for days/months/indefinitely.
2. **Durable / checkpointed:** Progress stored at every step. On failure, resume from exactly where it left off — not from the beginning.
3. **Interruptable — await ontology conditions:** The critical innovation.
4. **Full observability:** Every line of every prompt + every LLM response logged with exact timestamps. P99 duration. Per-model token spend. Granular search across millions of executions.

**The await-condition pattern (verbatim code shape from §3):**
```python
# Main orchestration agent
for each criterion in payer_criteria[treatment]:
    result = LLM.call(
        "For this criterion: approved / denied / need more info?",
        context=clinical_documentation
    )
    if result == "need more info":
        # Create typed ontology object specifying exactly what's needed
        write AgentAction(type="request_document", spec=what_is_needed)
        update case_status to "agent_needs_info"
        # Suspend execution — no state machine, no Kafka, no queue
        await condition: extracted_text_from_uploaded_doc IS NOT NULL
        # Execution resumes here when condition is met

# Sub-agent (triggered by document upload)
extracted_text = extract_document(uploaded_doc)
write extracted_text to ontology  # triggers await condition above
```

**Verbatim on the engineering savings (Matt Horses, §3):**
> "Normally, if you're trying to build some kind of suspension like this, you have to think about state machines, queues, you might have to set up Kafka, a whole bunch of complicated engineering. But I can just here with the await condition express what I want to happen. I want to pause until this thing becomes true. I don't have to worry about it consuming resources in the background."

**The clarification-object pattern (Eaton §15):**
```python
clarification_obj = create_ontology_object(
    type="ClarificationRequest",
    question="Customer spec specifies X but configurator requires Y. Proceed how?",
    human_response=None
)
await condition: clarification_obj.human_response IS NOT NULL
action = clarification_obj.human_response
proceed_with_quote(action)
```

**Byte-path implication for Lancelot:** This is exactly the durable-seam gap identified in the lens (`WHAT LANCELOT LACKS`). The await-condition is the missing primitive. Lancelot's `@app.batch` coalescing is a batch analog; durable execution requires persistent state beyond a single HTTP request lifecycle. Lancelot's existing `lancelot.context` ContextVars and `TransportPool` are per-request — they don't survive suspend/resume across arbitrary delays.

---

### 8. Ontology as Shared State for Human-Agent Handoffs

**Source:** `TRANSCRIPTS_PALANTIR.md §3, §7, §8` (Orchestrator, Hivemind, Army Software Factory)

**Core architectural decision:**
Human-agent handoffs happen via typed ontology objects, NOT chat interfaces. The agent creates a structured object specifying exactly what it needs. The human fills the object. The agent detects the fill via await condition.

> "You may have worked with chat agents where it's just a text box. Whereas this — instead what the agent has done is it's created an object to be filled in with exactly the information that it needs. The ontology is how you're able to encode opinion around exactly the information that should pass between the human and the agent. It allows us to build extremely rich UIs from what behind the scenes is a non-deterministic string-producing agent." — Matt Horses, §3

**Why this pattern is durable:**
- A text box (chat UI for HITL) exposes the full ambiguity of LLM output to the human. The human doesn't know what format the agent needs.
- A typed object (ontology object) constrains the human input to exactly what the agent specified. The UI can be a form, a dropdown, a file upload widget — generated from the object schema.
- The ontology object is an auditable artifact: it has a creation timestamp, the agent's question, the human's answer, and a link to the execution trace that created it.

**Every agent output writes to the ontology (Hivemind §7):**
> "Every agent in the system has written their work back to the ontology. Everything you're seeing — from your initial problem, all the research that was done, all the code that was written, all the ideas, all the way to the final plan — is all in the ontology and monitored in real-time."

This makes systems observable, debuggable, and improvable over time. The pattern: treat every intermediate agent output as a named, typed artifact — not ephemeral in-memory state.

**For Lancelot's agent layer:** The equivalent is `lancelot.context` ContextVars plus ontology-object-like typing for HITL handoffs. Lancelot's `@app.tool` already creates typed schemas for tool inputs; the missing piece is a persistent, resumable state object that survives the request lifecycle and can hold the HITL-pending state.

---

### 9. Context Management as First-Class Engineering

**Source:** `TRANSCRIPTS_PALANTIR.md §8` (Army Software Factory), `§13` (Knowledge Nodes × First Solar), `§5` (Advanced Ontology Deep Dive)

**Army Software Factory's Transform Contract pattern (§8) — most mechanically explicit:**

Each sub-agent receives a "transform contract" containing ONLY what is relevant to its specific stages:
1. Assigned pseudo-code stages (JSON, scoped to this agent's work)
2. Reconnaissance report excerpt — only the sections for those stages
3. Associated warnings — only the warnings relevant to those stages
4. SAS code lines — ONLY the exact lines corresponding to those stages (not the full file)
5. Foundry transform documentation
6. Translation rules and staging ordinances

**The design principle (explicit):**
> "Curate the context: Don't pass everything to every agent. Pass only what's relevant to that agent's specific task. Context overload causes confusion." — Best practices §8

**Complexity-based agent routing (Assessment Agent, §8):** 4-dimensional scoring determines single-agent vs. orchestrated:

| Dimension | What it measures |
|-----------|-----------------|
| Number of outputs | How many datasets/objects does this stage write? |
| Risk concentration | Are hard warnings concentrated here? |
| Interdependencies | Cross-stage dependencies in this file? |
| Agent confidence | Self-reported confidence score from prior stage |

**Artifact-based communication (Knowledge Nodes §13):**
All intermediate agent results are named, typed artifacts stored in the ontology. If a 16-step analysis run hits a context overflow or API error, it restarts from the last saved artifact. Quote: "Artifacts survive context window failures — if an agent run exceeds context, it can restart from any artifact checkpoint."

**Full analysis branching (§13):** Because every artifact state is stored, you can fork from any intermediate point: "What if it had taken this path instead?" — a counterfactual branch from any step. This requires treating artifacts as first-class checkpoints, not as ephemeral variables.

**Ontology DRY principle (§5 Advanced Ontology):**
> "If you've built the same thing three times, refactor into a common workflow or interface. Reduces context management complexity for both humans and agents."

Agents navigating a DRY ontology need less context to understand what tools exist and what they do. A "don't repeat yourself" discipline at the schema level directly reduces agent context load.

---

### 10. Eval-Driven Development Loop

**Source:** `TRANSCRIPTS_PALANTIR.md §2` (DevCon 5 — AI FDE: Eval-Driven Development)

**The exact loop (AIFDE's safety incident triage demo):**
1. Zero logic function, just data in a manual process
2. AIFDE exploration mode: queries ontology, reads data patterns, reads documentation via semantic search
3. AIFDE logic creation mode: produces first-draft AIP Logic function — "probably better than what I would have come up with in the first place"
4. AIFDE eval suite creation: 10 representative test cases from historical data
5. First run: 9/10 test cases FAIL (90% failure on first draft)
6. Eval-driven debug loop: read failures → synthesize ("LLM is categorically over-prescribing actions") → refine prompt with more precise rules → rerun evals
7. One iteration: 90% failing → 90% passing
8. Deploy to production at 90% accuracy

**Critical finding on first drafts:**
> "Getting to this first draft state is not really the hard part. The hard part is actually taking a prototype and bringing it all the way to production."

**Production feedback → eval → fix loop (Stage 5-6, §2):**
- User rejects agent proposal, submits feedback
- Developer reads feedback, translates into a new test case in the eval suite
- AIFDE reads feedback objects from ontology, synthesizes what went wrong, writes the test case
- Runs evals to confirm the failure
- Iterates on the logic function until the test case passes
- **Agents building agents:** Developer asks AIFDE to build a "feedback quality-gate agent" that asks users clarifying questions when feedback is vague ("this is bad" → "what specifically makes this bad?")

**AIFDE eval accuracy metrics (§14 DevCon 4, §2 DevCon 5):**
- Initial 90% failure → single iteration to 90% passing (safety incident triage)
- AIFDE decodes Lear-specific acronyms correctly 90-95% of the time with no additional context provided

**For Lancelot:** The define_outcome / evals gap in the lens maps directly here. The loop is: `historical_data → eval_suite → fail_on_first_draft → synthesize_failures → fix_prompt → rerun → deploy`. Lancelot's agent framework should provide a first-class eval runner that makes this loop cheap.

---

### 11. Provenance-Based Security (Data-Exfiltration-Proof Agent Calls)

**Source:** `TRANSCRIPTS_PALANTIR.md §19` (DevCon 4 — Provider API Proxy + Provenance-Based Security)

**The attack scenario:**
- Developer A: `draft_email()` using Public data → OK
- Developer B: `send_email()` using external API connection → OK for Public data
- Developer C: `draft_and_send_email()` using both as tools → OK initially
- Months later, Developer X adds Sensitive Internal data to `draft_email()` → looks fine in isolation

**Without provenance-based security:** Sensitive Internal data now flows through a function that connects to an external email service. Silent data exfiltration. No single developer owns the full chain.

**With provenance-based security:** On preview/publish of `draft_and_send_email()`, the system traverses the FULL upstream dependency graph:
```
Error: Permission denied based on export marking validation.
Upstream of draft_email: sensitive internal data is present.
Upstream of send_email: connection is not appropriate for this data classification.
Preview and publish blocked.
```

**Wire-compatible provider proxy (§19):**
```python
# Two-line setup for any OpenAI/Anthropic-compatible library
base_url = foundry_api_host()   # AIP model service URL
token = foundry_api_token()     # Foundry auth token

client = anthropic.Anthropic(base_url=base_url, api_key=token)
# All requests route through governance layer automatically
```

The proxy adds: geo-restrictions (data locality compliance), fault tolerance (transparent failover between 15-16 model hosting hubs globally), and full telemetry. PydanticAI, LangGraph, LlamaIndex all work with zero modification (they support custom base_url).

**Byte-path implication:** Lancelot's `TransportPool` is already the outbound model call substrate. Adding a provenance-pass (check data markings against the export policy of the target endpoint) would be a natural extension. This is especially relevant for enterprise deployments where the user's data carries security markings that should be respected by the tool calls.

---

### 12. Ontology Scenarios — Sandboxed What-If for Agent Proposals

**Source:** `TRANSCRIPTS_PALANTIR.md §4` (DevCon 5 — Ontology Foundations)

**Scenarios:** Persistent, sandboxed ontology workspaces. Key properties:
- Changes in a scenario do NOT touch production data
- Scenarios persist — you can return to them later
- Full function/action/logic toolbox works within the scenario
- Multiple scenarios can exist simultaneously (Option A, Option B, ...)
- When ready, a human approves and merges to production

**The OR scheduling workflow (§4):**
1. Voice agent generates scheduling scenarios (inside sandbox)
2. Option B: Dr. Dubois, 1 minor adjustment, no nurse overtime
3. Nurse submits the scenario for administrator approval
4. Administrator reviews, edits the scenario (adds own knowledge)
5. Administrator approves → merge-triggered outbound voice calls to affected patients

**Why this pattern matters for agent frameworks:** The correct model for AI-generated proposals is not "agent writes directly to production state" but "agent writes to a sandbox, human reviews, human merges." This is the git-PR workflow applied to operational data. The scenario is the PR; merge is the deployment.

**Transactions primitive (§4):**
> "All-or-nothing. If any action fails within a transaction, all changes are rolled back. None of them are written to the ontology. The appointment is either fully cancelled or not cancelled at all."
```
# Atomic multi-action commit
remove_nurse_assignment()     # might fail
remove_room_prep()            # would leave inconsistent state if above fails
unassign_doctor()             # must either all succeed or all roll back
```

---

### 13. Knowledge Nodes — Continuously Refined Shared Knowledge Layer

**Source:** `TRANSCRIPTS_PALANTIR.md §13` (DevCon 4 — Knowledge Nodes × First Solar)

**The problem:** 100 integration engineers × 3 hours/day × 10 factories. 18 months to onboard a new engineer. Problems have "thousands or millions of permutations, some you may not see for years."

**Knowledge Nodes — what they contain:**
- Best practices for avoiding data leakage in specific analytical pathways
- Code recipe examples (working Python code for common analysis patterns)
- Relationships between production variables and process outcomes
- Deconflicted domain knowledge (not raw notes — synthesized understanding)

**Three pathways to create knowledge nodes (§13):**
1. **Reflection after collaborative analysis:** Agent + engineer work through an analysis. At the end, agent reflects: what did we learn? what mistakes were made and self-corrected? what code patterns worked? Agent proposes nodes for human review.
2. **Document ingestion:** Upload PDF/runbook/notebook. Ingestion agent parses structure, connects to existing knowledge, identifies gaps and contradictions, proposes edits. Human reviews and approves.
3. **Autonomous troll:** "Go look for patterns and come to me with proposals." Doubles as anomaly detection and knowledge freshness maintenance.

**Technical architecture (K4D framework, §13):**
- Built in Python on Ontology SDK
- Agents run in Compute Modules + Serverless Python (security/governance under same PaPS model as human users)
- Agent communication via **artifacts** — named, typed, persisted to ontology between steps
- Dynamic ontology SDK per agent: each agent gets SDK scoped to exactly the objects it needs
- Full branching: every artifact state saved → fork from any intermediate point → counterfactual analysis

**For Lancelot:** The artifact-based communication pattern (named persistent checkpoints between agent steps) is the concrete mechanism for the "typed memory" gap in the lens. The knowledge node synthesis loop (reflection → human review → approval → write to shared layer) is the durable memory write path.

---

### 14. Multi-Agent Architecture — Hivemind's Dynamic Agent Generation

**Source:** `TRANSCRIPTS_PALANTIR.md §7` (DevCon 5 — AI Hivemind)

**Standard orchestration vs. Hivemind:**
- **Standard:** Agent roles pre-configured at build time. A logistics agent has a fixed prompt, regardless of problem.
- **Hivemind:** Agent roles generated at runtime. The system reads the current problem, current idea, current stage — generates the agent prompt. A logistics agent for maritime disaster response has a different prompt than one for supply chain disruption.

**The kneelment mechanism (iterative critique loop):**
```
v1 = initial_generation()
critique_team = generate_agents(context=v1 + problem_set)
critiques = parallel_execute(critique_team)
v2 = refine(v1, critiques)
if not ready_to_proceed(v2):
    repeat(v3, v4, ...)
# Decision to proceed or iterate is itself agent-driven
```

**Feedback propagation:**
Commander feedback stored as ontology object linked to the problem set. All future Hivemind runs on this problem start with that feedback as context. The system accumulates executive knowledge — it doesn't forget that the commander preferred Option B's sea route over Option A's rail route.

**Tool use decision (§7):**
Every critique agent has access to: web research, ontology queries, code execution. Agents decide which to use. The medical specialist may only need ontology queries. The site selection agent needs code execution (writes and runs Python for quantitative site analysis). Tool availability ≠ tool use; agents assess and choose.

---

## Hype / Deprecation Flags

**Model choice is secondary (confirmed by Palantir §1):**
> "So much of this goes beyond just the nature of the LLM or any model you choose in particular. It's the iterative tradecraft... how to decompose the problem, how to wield chained agents and sub-agents, how to configure the overall operating system to deliver on the outcome."

Eaton used multiple model providers across different steps — no single provider solved all sub-tasks equally well. The model routing layer is infrastructure; the agent design is where the value lives.

**RLHF/PPO churns; DPO is stable but offline (§3):**
The PPO-based RLHF pipeline is expensive, sensitive to hyperparameters, and increasingly replaced by DPO/ORPO. DPO is a ~1-2 year tactical win — the actual durable primitive is the verifier-first self-improvement loop (STaR/GRPO), not the specific alignment algorithm.

**Benchmark accuracy is the wrong metric (§6, §8):**
Both Percy Liang and Moroni independently note that optimizing for benchmarks produces models/systems that look good in demo and fail in deployment. The durable metric is "task humans complete in X hours" or "correct answer rate under adversarial real-world conditions." Lancelot's evals should measure actual success (test passes, tool call succeeds) not proxy metrics.

**Chat UI for HITL is a wrong abstraction (§3 Palantir):**
A text box for human-agent communication exposes the full ambiguity of LLM output to the human and loses structural information. Typed ontology objects for HITL handoffs are the durable pattern. This is a 2+ year finding from Palantir production deployments across healthcare, defense, manufacturing.

---

## Byte-Path Lens Application

| Signal | Lancelot byte path | Implication |
|--------|-------------------|-------------|
| RLVR: verifier-first self-improvement | `lancelot.tool` (`@app.tool` → auto JSON schema + output_schema); `TransportPool` | TransportPool does transport, not verification. A `Verifier` abstraction (wraps a tool call result, runs a checker) is missing. The checker could be: Python interpreter, JSON schema validation, test runner. Not yet built. |
| Durable execution / await-condition | Nothing in Lancelot survives suspend/resume across HTTP request lifecycle | This is the largest gap. `@app.batch` coalesces requests within a window; Orchestrator suspends indefinitely. Would require a persistent state store (ontology-object equivalent) and an event-driven resume mechanism. Closest: `lancelot.context` ContextVars, but these are per-request. |
| Human-in-the-loop typed objects | `@app.tool` creates typed schemas; MCP mount auto-discovers tools | The gap is the HITL handoff object: a persistent, typed, resumable state that the agent creates to specify what it needs from a human, and the human fills in. Not a chat box — a typed form. Not yet in Lancelot. |
| Artifact-based communication | No persistent artifacts between agent steps in Lancelot currently | Artifacts = named checkpoints in the ontology between steps. Lancelot's tool outputs are ephemeral. A `lancelot.agent.Artifact` that writes to a persistent store between steps would close this gap. |
| Context engineering (transform contracts) | `lancelot.batch` coalesces; `lancelot.context` carries per-request metadata | Nothing scopes context per-sub-agent. The transform contract pattern (curated context per agent) is a prompt engineering discipline, not a framework primitive. But a `ContextBundle` that enforces "only pass relevant sections" would be a first-class primitive. |
| Self-consistency / voting | `TransportPool.fanout(coros, first_n=, cancel_laggards=True)` | `fanout` already does parallel sampling with partial results. Adding a `vote(results, strategy='majority')` aggregator would make self-consistency a one-liner on top of existing infrastructure. |
| Eval-driven development loop | No evals primitive in Lancelot currently | `define_outcome` / `judge_panel` / `unit_eval` are the missing layer. Palantir's loop (historical_data → eval_suite → fail → synthesize_failures → fix_prompt → rerun) requires a test runner that can batch-replay against historical data. |
| Knowledge nodes / typed memory | `lancelot.memory_pool.MemoryPool` is byte-OOM, not agent memory | `MemoryPool.reserve()` manages bytes; knowledge nodes manage domain knowledge. These are entirely different layers. The 4-tier typed memory (episodic, semantic, procedural, working) is not yet built. |
| Provenance-based security | `lancelot.net` SSRF guard, `lancelot.webhook` sign/verify | Data marking propagation through dependency chains is not in Lancelot. Would be relevant for enterprise deployments. Lower priority than durable execution and evals for current Lancelot positioning. |
| Scenarios (sandboxed what-if) | Nothing in Lancelot | The scenario → review → merge-to-production pattern is important for agent proposals. Maps to: agent writes to a draft state → human reviews → human triggers production write. Not yet built. |
