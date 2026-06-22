# How They Did It — Mechanism-Level Distillation

*Sources: `core/01_HOW_THEY_DID_IT.md`, `core/03_FOUNDER_PLAYBOOK.md`, `core/04_UNDER_THE_HOOD.md`, `core/11_REVOLUTIONARY_AI_STARTUPS.md`. Every claim is cited by filename + section.*

---

## 0. The single most load-bearing finding

The corpus converges on one claim you cannot skip:

> "The agent loop is not where anyone competes. The product is the harness around it — context/cache discipline, tool design, error recovery, memory, evals, durable state."
> — *04_UNDER_THE_HOOD.md §3 "The Moat Is The Pipeline, Not The Model"*

Lancelot's lens confirms this: TransportPool, stream, batch, canonical, mcp are the *harness* primitives. The loop itself — 5-10 lines of ReAct — is not a moat. The moat is everything the loop can call.

---

## 1. Build order discipline: primitive first, product never

**04 §5 (Architecture-First):**

| Founder | Built first | Did NOT build first |
|---|---|---|
| Perplexity | Retrieval + synthesis pipeline | Chat interface |
| Cursor | Speculative editing system | IDE chrome |
| Figma | WebGL renderer + multiplayer protocol (4 years!) | Design UI |
| Linear | Sync engine | Kanban board |
| Stripe | The API | Dashboard |

**The concrete test (04 §1):** "If you can swap out the underlying model/API and the product is basically the same, you built a wrapper." If removing the model requires rethinking the entire architecture, you built a primitive.

**Lancelot implication:** Before shipping any agent feature, can you state the primitive being rebuilt in one sentence? "The LLM call, from fire-and-forget HTTP to metered/observable/resilient transport" = TransportPool — already built. "The agent context, from ad-hoc dict to typed, compacted, cache-stable bundle" = NOT yet built. Build that, not the loop.

---

## 2. Context/cache is the #1 cost lever — stated as an architectural rule, not an optimization hint

**11 §2.3 (Manus — CodeAct + Context Engineering):**
- KV-cache hit rate is Manus's #1 metric. Stated ratio: **100:1 input:output tokens**.
- Cache hit = **10x cost reduction** per round-trip.
- State machine uses **logit masking** (masks tool tokens after use, does NOT remove them from context — removing would break the KV-cache prefix).
- Event stream is **append-only**; no in-place mutation of the history.
- Every context engineering decision flows from "preserve the longest stable prefix."

**04 §2 (Cursor — Context Engine):**
- Cursor uses **blob-storage KV caching** for embeddings: 8-10ms warm query latency, 20x cost reduction.
- Merkle tree file hashing synced every ~5 minutes — single root hash comparison, only drill down on mismatch. The Nth company to index the same codebase gets near-instant results (hash hits).
- Embedding vectors stored in Turbopuffer (S3-backed vector DB) — NO source code on servers.

**03 §6 (Poetic's meta-system vs fine-tuning):**
- Fine-tuning is a *capital trap*: spend millions, next frontier release makes your fine-tuned version obsolete. Harnesses are model-agnostic.
- Poetic scored 54% on ARC-AGI V2 at $32/problem vs Gemini 3 Deep Think at 45%/$70/problem using **reasoning harnesses on cheaper models** (03 §6 "The Meta-System Approach").
- Upshot for Lancelot: `lancelot.canonical.dumps_canonical` (stable sorted-key JSON for cache prefix stability) is already the exact primitive the corpus calls the "#1 free cost lever." Wire it to every LLM call by default; document the `10x+ token win` explicitly.

---

## 3. The Cascade pattern: expensive plan, cheap execute

**04 §9 (The Cascade Architecture Pattern):**
- Cursor: GPT-4/Sonnet generates rough sketch → fine-tuned Llama-3-70B apply model implements it at **1,000 tok/s** (13x faster).
- v0: Frontier model generates → AutoFixer model (RL fine-tuned, speculative decoding) corrects at **8,130 chars/sec** (10-40x faster than GPT-4o-mini).
- Perplexity: Expensive cross-encoder at L2 reranking → cheap XGBoost final scoring at L3.

**The mechanism:** This is NOT "use a small model." It is a trained *apply* model that takes a sketch and deterministically implements it against the actual target (file, codebase, output format). The apply step requires its own training because naive deterministic matching fails ~40% of the time (04 §2 Cursor).

**Lancelot implication:** TransportPool.fanout + spawn_child are the *infrastructure* for cascade. But the *agent-level* cascade (plan-model → apply-model route) is not yet a Lancelot primitive. A `cascade(plan_call, apply_call)` or dual-route context shape belongs in the agent layer, not the byte path.

---

## 4. KV-cache prefix — the exact mechanism

**04 §2 (Cursor — Cache Warming):**
- While you type, Cursor **proactively warms the inference cache** with current file contents. When you hit Enter, very few tokens need fresh computation. This is why suggestions appear "instant."

**11 §2.3 (Manus — append-only event stream + logit masking):**
- Append-only context = prefix never changes = cache ALWAYS hits the full history prefix. Any mutation of prior tokens invalidates the entire cache.
- Logit masking (not token removal) to suppress already-used tool names. The token IS still in context; only the probability is zeroed. Removing it would shorten the prefix and break the cache.

**Direct Lancelot wire-in:** `lancelot.canonical.dumps_canonical` already sorts keys for stable serialization. The next layer is a *context assembly* primitive that guarantees append-only ordering of the prompt prefix across turns. This is what the agent layer needs — not implemented yet.

---

## 5. Build order: what to refuse

**03 §5 (The Wrapper Trap):**
> "Companies that raise money for 'AI-powered [existing workflow]' without rebuilding the primitive... When the LLM improves or the incumbent adds the same feature, the wrapper has no moat. The test: if you can swap out the underlying model and the product is basically the same, you built a wrapper."

**03 §3 (v0 lesson):**
- Composite architecture → 64.71% → 93.87% error-free. **29 percentage point lift through engineering, not model improvement.** (04 §8 v0 teardown)
- "Don't train your own foundation models if you want to iterate fast; use a composite model architecture to stay flexible as the field evolves." (Malte Ubl, 04 §8)

**ElevenLabs stealth pattern (01 §6):**
- 12 months in stealth perfecting technology while others shipped ChatGPT wrappers.
- "Stealth works when you're building genuine technical breakthroughs."
- 3 complete rewrites before settling on architecture (Turbopuffer, 01 §2).

**What to refuse for Lancelot agent layer:**
1. Do NOT wrap a ReAct loop and call it a framework. The loop is 5-10 lines. If that is all Lancelot ships, it is a wrapper.
2. Do NOT fine-tune on any specific model. Build harnesses (context engineering + cascade architecture) that are model-agnostic and improve with every frontier release.
3. Do NOT build tool output schemas from scratch per-tool. `@app.tool` auto-derives `output_schema` — that is already a primitive. Extend it rather than re-inventing.

---

## 6. Multi-model routing — the production pattern

**11 §2.1 (Sierra — constellation of 15+ models):**
- NOT "pick the best model." Routes DIFFERENT tasks to DIFFERENT models. Different subtasks within a SINGLE conversation run on different models in parallel.
- Agent OS 2.0: memory + context + action as first-class capabilities.
- Outcomes-based pricing (pay for results, not API calls).

**11 §2.4 (Harvey — multi-model orchestrator):**
- Routes Claude vs Gemini **by task type** (not by query).
- Custom "voyage-law-2-harvey" embeddings trained on 20B+ legal tokens → 25% fewer irrelevant results.
- Mid-2025 pivot: all integrations become tool calls to a pure agent framework.

**04 §2 (Perplexity model routing):**
- 14 models across 6 providers.
- Sonar (70B) for real-time synthesis, DeepSeek R1 for chain-of-thought, GPT-5.2/Claude Sonnet 4.6/Kimi K2.5 for Pro tier.
- Retrieval stack operates UPSTREAM of model selection — same documents regardless of synthesis model.

**Lancelot implication:** TransportPool already handles multi-provider transport. The *routing decision* (which model for which task type) is not yet a Lancelot primitive. A `ProviderProfile` or `TaskRouter` that maps task-type → model is the agent-layer gap. This is NOT the byte path — it is message-shape translation (the model adapter layer listed in the lens as LACKING).

---

## 7. The pipeline moat formula

**04 §3 Pattern 2:** Every moat is a *compound system*:
- **Perplexity:** 200B-URL index + custom crawler + 6-gate citation pipeline + custom embeddings + engagement feedback loop.
- **Cursor:** Speculative decoding on existing files + Priompt prompt compiler (JSX, priority scores, binary search on token budget) + RL-retrained tab model (400M daily signals, 90-min retrain) + fork-level editor control.

**The concrete mechanism — Priompt (04 §2 Cursor):**
- JSX components for prompt building.
- Each context fragment gets a priority score (cursor line = highest, decreasing by distance).
- When context exceeds token budget: binary search to find optimal cutoff — NOT simple truncation.
- Decouples raw data from prompt rendering. Enables A/B testing against historical prompt data.

**Lancelot implication:** Priompt's priority-based token-budget fitting is exactly what "context engineering as a primitive" means in the lens. This is the `ContextBundle` / `inspect()` gap. The mechanism: each observation has a priority score; when assembling the prompt, binary-search the token budget to find the optimal cutoff, then serialize with `dumps_canonical` for cache stability.

---

## 8. Perplexity's 6-gate citation pipeline — the no-hallucination architecture

**04 §2 (Perplexity — The 6-Gate Citation Pipeline):**

1. **Gate 1 — Intent classification:** Routes to index type (trending vs evergreen); complex → fan-out decomposition into parallel sub-queries.
2. **Gate 2 — Hybrid retrieval:** 60+ sources via BM25 + dense + hybrid fusion simultaneously. Deep Research = hundreds of sources.
3. **Gate 3 — Prefiltering:** Heuristics remove non-responsive or stale content.
4. **Gate 4 — Progressive ML reranking (L1-L3):** L1 fast lexical+embedding; L2 cross-encoder; L3 XGBoost with ~0.7 quality threshold (top ~30% survive). **Critical failsafe: if insufficient results meet threshold, ENTIRE result set discarded, retrieval restarts.**
5. **Gate 5 — Engagement-based selection:** User click/upvote/downvote continuously updates source rankings. Bad sources dropped within ~1 week.
6. **Gate 6 — Structured prompt assembly:** Citation markers, URLs, dates, ranked excerpts embedded BEFORE the LLM generates. The LLM does NOT decide what to cite — it follows pre-assembled evidence.

**The key insight (04 §2):** "Pre-training is like a no-notes-allowed exam; RAG is like an open-book exam." The quality comes from the retrieval pipeline, not the LLM. The LLM is the rendering layer.

**Lancelot implication:** TransportPool handles the *call* to the model. But the pre-call pipeline (retrieval, reranking, structured prompt assembly) is the agent-layer gap. Gate 6's "citation markers pre-embedded before generation" is a specific prompt-assembly pattern — a job for the context engineering primitive, not the byte path.

---

## 9. Verifiable reward signals — what to build in

**03 §6 (Chollet's Intelligence Framework):**
> "Any domain where solutions can be formally verified (code, math) can be fully automated. Domains without verifiable rewards (essays, law) will see slow progress because they depend on expensive human annotation. Build in domains with verifiable rewards."

**03 §6 (Cursor RL loop):**
- Reward structure: +0.75 accepted, -0.25 rejected, 0 silence.
- Full retrain cycle: **1.5-2 hours**. Multiple checkpoint deployments daily.
- ~100 different suggestions generated, reward function trained on which ones humans actually accept.

**03 §6 (Synthetic data taxonomy, Cursor/Lex Fridman):**
Three categories, ranked by yield:
1. Distillation — cannot exceed teacher. Useful for fast task-specific models.
2. Asymmetric difficulty — introducing bugs is easier than finding them; use that direction.
3. **Generation + verification** — produce many, keep only verified-correct. **"Most likely to result in massive gains."** (Proven for code/math because unit tests/proofs are verifiable.)

**Lancelot implication:** The evals primitive (unit / judge panel / define_outcome — listed in the lens as LACKING) is directly tied to "verifiable reward." The framework needs a first-class `define_outcome(fn)` that returns a boolean or scalar — the exact signal Chollet is pointing at. Without it, you cannot run a feedback loop.

---

## 10. Revenue-per-employee — the small-team formula

**01 §12 Pattern 2:**

| Company | Team | Rev/Employee |
|---|---|---|
| Midjourney | ~100 | $5M |
| Cursor | ~200 | $5M+ |
| Perplexity | ~60 | $3.3M |
| Turbopuffer | ~35 | ~$1M+ |

**The mechanism (01 §12 Pattern 1):** Outsource mature infrastructure (Vespa for search, Turbopuffer for vectors, AWS for compute), own differentiation. Perplexity's 38-60 person team serves 45M users because they do NOT rebuild search infrastructure.

**Turbopuffer's customer acquisition (01 §2):** Simon flew to SF unannounced, helped Cursor debug Postgres issues, demonstrated expertise. Justine worked 24 hours straight to add a critical feature Notion requested, closed the deal. "Every message replied within minutes... feels like these guys work at Notion."

**Lancelot implication (honest evaluation):** The byte path (Rust/hyper, 183k rps const tier, R3 pump) IS the outsource-mature-infrastructure play for the agent layer — users get production-grade HTTP transport without building it. The Lancelot agent framework should be the same: outsource the byte path to Lancelot, own agent logic. This IS the positioning; do not drift.

---

## 11. Agent architecture patterns from Tier 2 (production agents)

**11 §2.2 (Cognition/Devin — Planner/Coder/Critic):**
- Three specialized sub-models: Planner (strategy), Coder (trillions of code tokens), Critic (adversarial review).
- SWE-1.6 = 100x more RL compute than SWE-1.5 on the same base model.
- Discovery: **correctness-only rewards produce "AI slop"**. Need multi-dimensional reward (correctness + style + conciseness).

**11 §2.3 (Manus — CodeAct paradigm):**
- Generates executable Python as the *universal action format* (not JSON tool calls per-tool, not text). One tool schema covers all actions.
- Strictly **ONE tool call per iteration** (prevents cascading failures, makes state simpler).
- GAIA benchmark: 86.5% vs OpenAI Deep Research 74.3%.

**11 §2.9 (Emergence AI — Agents Creating Agents):**
- Meta-agent orchestrator auto-generates, orchestrates, and operates specialized agents without human coding.

**11 §3.3 (Dust — Unix filesystem abstraction):**
- Maps Slack/Notion/GitHub into Unix-like structures with 5 commands: `list`, `find`, `cat`, `search`, `locate_in_tree`.
- Coordinator agent classifies complexity, decomposes tasks, orchestrates up to 6 concurrent sub-agents.

**Lancelot implication:** The CodeAct "one tool schema covers all actions" pattern is relevant to `@app.tool` — but CodeAct uses executable Python, which is a different primitive (code execution sandbox). Lancelot's tool primitive is JSON-schema-typed calls; the CodeAct pattern would require a sandbox (E2B/Daytona tier) that is explicitly out of scope. Dust's Unix filesystem abstraction for enterprise data IS a viable agent primitive if Lancelot ever builds a memory/retrieval layer — but that is the user's side per cardinal rule.

---

## 12. Durable execution — the one infrastructure piece agents cannot skip

**11 §6 (Temporal):**
- Durable execution platform. Workflows survive crashes, 380% YoY growth, used by Netflix and OpenAI.
- "THE foundation for reliable long-running agents."

**11 §3.6 (Hermes Agent — SQLite persistence + RL environments):**
- FTS5 full-text search over all past sessions. Learning cycle as first-class architectural concern. Self-improving — autonomously creates skills across sessions.

**Lancelot implication:** Durable execution (event-sourced checkpoint/resume/HITL) is listed in the lens as LACKING and is a confirmed need from the corpus. This is a *seam*, not a byte-path primitive — Lancelot should expose the right ASGI hooks for Temporal (or equivalent) to attach to, not own the durable-execution layer itself.

---

## 13. The "revolutionary bet" signal from Tier 1 (what NOT to chase)

**11 §1 (Tier 1 — Rebuilding Fundamental Primitives):**
Companies rebuilding the transistor (Extropic), the learning objective (AMI Labs/JEPA), the robot policy (Rhoda), the protein (Nabla Bio), the robot body abstraction (Skild), the scientific experiment (Lila Sciences).

These are 3-7 year bets requiring $300M-$1.4B raises and specific hardware/biology access. **Not relevant to Lancelot's 12-18 month build horizon.** The corpus is clear: revolutionary-primitive bets at Tier 1 are NOT the path for a 1-5 person framework team. The relevant tier for Lancelot is Tier 2 (agentic architectures) and Tier 3 (agent infrastructure).

---

## 14. What the corpus flags as depreciating tactical hacks

These churn on a ~1-2 year model clock — durable framing only, do not hardcode:

1. **Specific model routing thresholds** (e.g., "simple queries → Sonar, complex → GPT-5"). Model capability distribution shifts every 3-6 months.
2. **Fine-tuned models for specific tasks** without a retrain pipeline. A fine-tune is a snapshot; without the RL loop (Cursor's 90-min retrain), it decays.
3. **Token count as proxy for context quality.** Manus's KV-cache insight is durable (prefix stability); specific token budgets are not.
4. **Any latency number as a SLA.** Numbers from the corpus (358ms Perplexity p50, 260ms Cursor tab, 8ms Turbopuffer warm query) are baselines for the *pattern* (latency IS the product) — not targets to hardcode.

---

## 15. API shape signal: what the production agents expose

**11 §2.4 (Harvey):** Parameterized jurisdiction tools — agent selects country, configuration determines data sources. Tool schema is a first-class parameter, not hardcoded.

**04 §2 (Perplexity — Three API Products):**
- Search API: raw ranked results, no LLM synthesis. 50 QPS flat rate.
- Sonar API: search-augmented chat completion.
- Agent API: universal model gateway, full OpenAI SDK compatibility at `/v1/responses`.
- **Strategic insight:** Start with the infrastructure API (Search), expose the product API (Sonar), build the universal gateway (Agent API). Developers who adopt the gateway for convenience lock into the infrastructure.

**11 §2.1 (Sierra):** Outcomes-based pricing — pay for results, not API calls. Not a billing model — an architectural signal: the system must be able to define and measure an outcome to charge for it. This maps directly to `define_outcome(fn)` in the evals primitive.

---

## Summary: the 7 gaps the corpus confirms Lancelot needs

These are NOT the byte path (already built). These are the *agent harness* gaps, ordered by corpus frequency:

1. **Context engineering primitive** — priority-scored token-budget fitting (Priompt pattern), append-only assembly, `dumps_canonical` wire-in. Most cited, most load-bearing. (`ContextBundle`, `inspect()`)
2. **Model adapter / message-shape translation** — ProviderProfile, OpenAI-canonical → N-dialects, OMIT sentinel. TransportPool does transport; nobody yet does shape translation. (`ProviderProfile`)
3. **Verifiable outcome / evals** — `define_outcome(fn)` returning bool/scalar. Required for any RL loop, required for outcomes-based pricing signal. (`define_outcome`)
4. **Typed memory (4-tier, retrieval)** — RRF retrieval, temporal scoping. Corpus confirms this (Hermes FTS5, Cursor Turbopuffer KV cache, Harvey custom embeddings). (`memory_pool` is byte-OOM, not agent memory)
5. **Durable seam** — ASGI hooks for Temporal-style checkpoint/resume/HITL. Not ownership — just the seam. (`lancelot.durable`)
6. **Agent-level safety** — permissions allow/deny/ask/defer, confidence router, 5-stage hooks. Corpus confirms (Decagon AOPs, MindFort full-loop exploit→patch, Sierra guardrails).
7. **Cascade routing** — plan-model → apply-model two-stage pattern. `TransportPool.fanout` + `spawn_child` are the byte-path analogs; the *decision routing* at the agent level is not yet a primitive.

---

*Note path: `/Users/seventyleven/Desktop/Lancelot/research/02_core/how_they_did_it.md`*
