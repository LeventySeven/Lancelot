# Vertical agents — production CX / legal / health / ops (mechanism-level distillation)

Sources: SIERRA.md, DECAGON.md, LINDY.md, HARVEY_AI.md, HIPPOCRATIC_AI.md, STARDRIFT.md, GORDIAN.md, WEDGE.md (all under `researchfms/teardowns/`)

---

## 1. Lookup vs Action taxonomy — the single most important tool design decision

Every production vertical agent (Sierra, Decagon, Harvey, Wedge/CollectWise-Agent) distinguishes two and only two tool classes:

- **Lookup** — read-only, retry-safe, no financial or record side-effects.
- **Action** — has side-effects. **DO NOT RETRY.** "Issuing a refund twice" is the canonical anti-pattern cited verbatim by Sierra's Studio linter.

Sierra enforces this at **schema registration** (Lookup / Action is a required field per tool, not a convention). The linter rule fires on mismatches: "A tool's name and description suggest one type but it's classified as the other." (SIERRA.md §2.3)

At **runtime** the Sierra executor refuses to re-run an action already present in `session.committed_actions` (SIERRA.md pseudocode line 234-236). The Wedge/CollectWise pattern does the same via Retell's `tool_call_strict_mode: true` — the LLM cannot emit a malformed action call and the webhook backend never does an unconditional upsert on a non-idempotent operation (WEDGE.md RN3.2.A).

**What to build for Lancelot:** `ToolSpec` should grow a mandatory `kind: "lookup" | "action"` field. The agent executor must hash executed action calls and refuse duplicates within a session. This is a one-field schema change + a `Set[call_fingerprint]` in executor state — not a feature, a correctness invariant.

---

## 2. Confidence router — `"high"|"medium"|"low"` with `decision_path`, deterministic rule-based

Stardrift's Starlink predictor (`StarlinkCheckResponse`, STARDRIFT.md line 1506-1514):

```python
class StarlinkCheckResponse(BaseModel):
    verdict: str            # "yes" | "no" | "likely" | "unlikely" | "unknown"
    confidence: str         # "high" | "medium" | "low"  — STRING, not float, UX-friendly
    source: str             # "tail_number" | "fleet_rule" | "airline_rule" — decision source
    decision_path: list[str]  # AUDIT TRAIL — ordered steps ["we checked X, then Y"]
    explanation: str
```

Three-tier waterfall, evaluated top-down, first match wins:
1. Exact tail-number lookup (highest specificity)
2. (airline_iata, plane_type) fleet rule
3. airline_iata blanket rule

"Determinism over ML: Rule-based system means consistent answers, faster latency, no LLM hallucination, no model drift." (STARDRIFT.md line 1944)

**The Wedge "confidence bug" the task prompt references:** WEDGE.md (RN2.12 / line 2646-2658) reveals that Wedge's Navis copilot uses `confidence: 0.85` (default quick level) and `confidence: 0.92` (immediate level) — **two hard-coded floats** with no routing logic between them. There is no dynamic confidence classifier; the confidence number is baked to the verbosity level chosen by the caller, not computed from evidence. This is the "dead zone" bug: a scalar float attached to an output mode is not a confidence signal — it cannot route decisions, it cannot escalate reliably, and it does not reflect actual model uncertainty.

**The correct design:** string enum `"high"|"medium"|"low"` + `decision_path: list[str]` audit trail + a deterministic rule or lightweight classifier that assigns confidence from evidence (Stardrift). The router then maps:
- `high` → proceed autonomously
- `medium` → proceed with audit trail written
- `low` → escalate to human or ask clarifying question

Harvey calls this `assistant_ask_user_question_tool` — a ReAct clarification branch that fires on `low` confidence, never on `high`. (HARVEY_AI.md §tool registry)

---

## 3. Validate-before-side-effect — Gordian basket check pattern

Before any payment/fulfillment action, Gordian runs a revalidation cycle (GORDIAN.md line 817-819 + basketValidator.ts):

```
POST /basket/check → { status: "queued" }
poll GET /basket   (30 × 2s = 60s ceiling)
  until valid_from timestamp advances
if any item.validity.status == "price_changed" | "unavailable":
    remove item
    do NOT proceed to POST /fulfill
only call POST /fulfill after revalidation confirms clean basket
```

Safety margin: 6 minutes before `valid_until` (GORDIAN.md line 1799). Card data never touches Gordian's infra: aliased via VGS (Very Good Security) vault at the API perimeter (GORDIAN.md line 54-56). The `last_action` field logs the exact step for idempotent retry detection (GORDIAN.md line 2282).

This maps directly onto any "payment agent" use case: do not proceed from basket→fulfill without an async revalidation cycle. The 6-minute safety margin + 60s poll ceiling are the production constants.

For non-payment domains (Harvey's AI Claims Manager, Wedge's billing reconciliation), the equivalent is:
1. Lookup claim status / price → state A
2. Run validation check (async, poll to completion)
3. Only emit the side-effecting action (submit claim, post payment) after validation confirms state A still holds
4. On staleness → abort + re-queue, never silently proceed

---

## 4. Parallel safety model — non-blocking bad-actor + specialist fan-out

Two patterns observed:

**A. Bad-actor classifier (Sierra, Decagon):** A separate classifier runs *concurrently* with generation on every turn. It does NOT block the response; it inspects the incoming message against known jailbreak/abuse patterns while the primary model reasons. If positive, response is intercepted. SIERRA.md pseudocode line 175; DECAGON.md per-turn loop.

**B. Hippocratic constellation (Hippocratic AI paper, Algorithm 1 — HIPPOCRATIC_AI.md §1.2):** 16 specialist LLMs all fan out in parallel every turn. Each is 50-100B int8-quantized; the primary is 70-100B bf16. Orchestration is deterministic Python (not LLM-routed). Per-turn latency is bounded by the *slowest specialist*, not their sum (~200-500ms for primary + 7 specialists in parallel). Each specialist emits a structured task string injected as `<SpecialistName TASK>preamble</SpecialistName TASK>` into the primary's next turn.

```
# Algorithm 1 (verbatim from paper §2.3):
Phase 1: fan-out — all N specialists receive (H_t, S_t) in parallel
Phase 2: collect outputs — update state S_{t+1}, accumulate task set T_{t+1}
Phase 3: rebuild primary prompt — P_A1(H_t, S_{t+1}, T_{t+1}) → primary generates r_t
```

Privacy gating (PHI): a two-state machine. State 1 = no PHI in primary context. Privacy Model (binary classifier) verifies identity against a protected DB. State 2 = PHI loads. This is **structural isolation** (PHI physically absent from the primary's context until verified), not a prompt guard. 100% identity verification vs 96% GPT-4 baseline (HIPPOCRATIC_AI.md §3.1). Harvey uses the same structural pattern via ethical walls: per-chunk `ethical_wall_id` written at ingest, hard pre-filter at query time — not re-ranking, not a prompt rule (HARVEY_AI.md §5.2).

**For Lancelot:** `TransportPool.fanout(coros, first_n, deadline, cancel_laggards)` already implements Phase 1+3 as a byte-path primitive. The Hippocratic pattern adds a Phase 2 state-accumulation step that Lancelot doesn't have — that belongs in the agent loop, not the transport layer.

---

## 5. Progressive disclosure under token budget (Sierra)

Sierra compiles an agent to a block graph. At each turn, the runtime evaluates block conditions (`if current_topic == "billing"`, `if authenticated == true`) and injects only the blocks whose conditions are true — tools, rules, workflows, knowledge — into the context. The agent starts minimal (brand voice, basic tools) and expands as conversation state advances (SIERRA.md §2.2, pseudocode).

"A workflow becomes just another piece of context made available when conditions are met, rather than the organizing paradigm for the entire system." — SIERRA.md §2.1.

Token budget is the enforcing constraint: the runtime can't inject everything, so conditions act as relevance filters. This is **structural context engineering** — the agent never sees irrelevant tools or policies.

Tool count discipline: ~30-40 active tool ceiling (Sierra). 2-4 skills per agent step (Lindy). Harvey scopes tools per workflow node (not globally active). LINDY.md counter-incentive quote: "AI Agents are powerful but more expensive and potentially less reliable than standard actions. Use them only when the next steps are truly uncertain."

---

## 6. Document-span-selection (DSS) before synthesis — Harvey's anti-hallucination grounding

Harvey inserts a DSS step before any synthesis LLM call (HARVEY_AI.md §workflow graph, node type `extract/DSS`):
1. Retrieve document chunks
2. Run extractive grounding: select exact spans from retrieved text (not paraphrases)
3. Only then pass selected spans to synthesis LLM

This prevents citation chains from breaking on paraphrase. For legal/regulatory domains where verbatim citation is required, paraphrase → synthesis breaks traceability. The DSS node forces the synthesis LLM to work from fixed textual anchors.

Multi-model routing (Harvey): deterministic classifier (not an LLM call) picks from GPT-5.5 (general assistant), Claude Opus (Lexis-strict citations), o3 (reasoning-heavy), Gemini pydantic (structured Tax-QA), Claude (translation). (HARVEY_AI.md §multi-model matrix)

---

## 7. Iterative step-scoping — Decagon's scoped instruction feeding

Decagon does NOT give the agent the full task context at step 0. Each step of a multi-step workflow reveals only the scoped instruction for that step (DECAGON.md §architecture). The agent sees:
- Step N system prompt (narrowly scoped)
- Step N tools (subset relevant to this step)
- Step N inputs

This prevents the agent from hallucinating ahead of its current position in the workflow and prevents tool pollution (step 3 tools are not visible at step 1).

GEPA for prompt optimization: 20-100 samples sweet spot; frontier reflection model required; 1500-char length constraint acts as regularization (−0.8% perf, 4× compression); ~150 LLM calls per run. (DECAGON.md §GEPA constants)

---

## 8. Per-call state isolation — Wedge/Navis audit pattern

Every call session is isolated to its own `callId`-keyed state slice: `{notes, documentNotes, aiChatMessages, transcript, quickSuggestion}`. Per-call tables: `ai_chat_messages(call_id, ...)`, `transcript_segments`, `call_notes`. Transfer creates a new `calls` row (preserves immutable per-agent record) rather than reassigning owner (WEDGE.md RN2.12 / line 2630).

This is the audit primitive: per-call isolation = per-call immutable log. For HIPAA "AI Governance" this is load-bearing. The pattern applies to any domain where audit trail completeness matters (insurance, billing, legal).

---

## 9. Tiered verbosity contract — actionable at integration point

Wedge/Navis `sendAiChatMessage(responseLevel: 'instant'|'quick'|'immediate')` returns `{response, suggestions: string[], confidence: number, responseId}`:
- `instant` → one sentence, no suggestions
- `quick` → 2-3 sentences + `suggestions[]`, confidence ~0.85
- `immediate` → structured multi-section markdown (policy/verification/exceptions/next-steps) + longer `suggestions[]`, confidence ~0.92

(WEDGE.md RN2.12 line 2655-2658)

This is the ergonomically correct response-contract shape for a copilot: the caller selects the verbosity tier, not the model. The model operates under the constraint of that tier. This prevents over-explanation in `instant` mode and under-explanation in `immediate` mode.

---

## 10. What Lancelot already provides vs what it lacks

| Need | Lancelot status |
|------|-----------------|
| Outbound LLM transport + retry/rate-limit | DONE — `TransportPool` |
| Parallel fan-out | DONE — `TransportPool.fanout` |
| Spawn child with trace propagation | DONE — `spawn_child` |
| Per-request cost/token metering | DONE — `upstream_metrics` + `context` ContextVars |
| Tool registration + auto JSON schema | DONE — `@app.tool` / `ToolSpec` |
| MCP server mount | DONE — `lancelot.mcp.mount` |
| Streaming with backpressure | DONE — R3 pump |
| TTFT metric | DONE — `lancelot.http.stream.ttft` |
| Lookup/Action taxonomy on ToolSpec | MISSING — needs `kind` field + executor idempotency guard |
| Confidence router (string enum + decision_path) | MISSING — no routing layer |
| Pre-execution validate pattern | MISSING — no async revalidation primitive |
| Progressive disclosure / block-condition graph | MISSING — context engineering primitive |
| Per-session committed-action deduplication | MISSING — agent executor |
| PHI / ethical wall structural gating | MISSING — context isolation not in scope for v1 |
| Durable checkpoint / HITL seam | MISSING — noted in lens as build target |
