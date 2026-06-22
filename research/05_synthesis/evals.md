# Lancelot — Evals Layer Brief (THE WEDGE)

**Layer:** `lancelot.eval` (+ `@app.tool(verify=...)`, `lancelot.context.force_eval`)
**Altitude:** "just build agents, brutal" — the smallest eval primitives that earn their keep. Anti-overkill (no LangSmith/Braintrust SaaS clone) AND anti-underkill (the #1 cause of failed AI products is *no* eval system — Hamel Husain, `bad_guide_ai_research.md §5`).
**Synthesizer date:** 2026-06-22
**Sources:** `03_teardowns/{evals_obs,deep_research,google_adk,dspy,mastra,openai_agents}.md`, `02_core/{bad_guide_ai_research,transcripts_dlai,transcripts_yc,transcripts_yc_root,transcripts_stanford_palantir,transcripts_msft_google,transcripts_a16z,how_they_did_it}.md`, `04_byte_path/{lancelot_model_io,lancelot_serve_tools}.md`.

---

## 0. The decision in one paragraph

The corpus is unanimous and load-bearing: **evals are the crown jewels — more durable than prompts, more durable than the loop** ("the company with the best evals always knows when it improved and always catches regressions" — `transcripts_yc.md:55`; "#1 cause of failed AI products is no eval system" — `bad_guide_ai_research.md §5`). And Lancelot has a *structural* edge no Python eval lib has: **eval == run on the same byte path**. The cost/TTFT/token/error-taxonomy ContextVars and OTel `lancelot.upstream.*` histograms already fire on every TransportPool call; an eval that runs the agent over Lancelot's own transport gets native traces, native cost accounting, and native cancellation *for free* — it does not need to bolt on a tracer. So Lancelot **BUILDS** a tiny three-tier eval module — `eval.unit` (deterministic, CI, sub-minute), `eval.judge` (calibrated panel), `define_outcome` (bounded generate→grade→revise) — plus two surgical hooks (`@app.tool(verify=...)`, `force_eval` ContextVar). It **REUSES** the entire byte path as the trace/cost/transport substrate (no rebuild). It **REFUSES** the SaaS observability platform, the storage/ClickHouse layer, generic metrics (ROUGE/BERTScore), self-referential eval loops, and anything in the hot path. The runtime dep stays **pydantic-only**; provider SDKs + sqlite-vec stay lazy.

The unifying mechanism, stated once: **an eval is a `(input, output|trace) -> Verdict` function.** Tier 1 makes that function pure-Python and deterministic; Tier 2 makes it a calibrated LLM judge; Tier 3 wraps it in a bounded revise loop. Everything below is how to build exactly that and nothing more.

---

## 1. THE WEDGE: eval == run (why Lancelot wins here, not LangSmith)

Every other framework bolts evals onto a *separate* trace store. Laminar runs a filter→summarize→agent pipeline over a 50–200k-token raw trace pulled from ClickHouse (`evals_obs.md §1.1`); W&B Weave reconstructs the span tree and re-derives `usage` from streamed deltas because the eval ran somewhere else (`evals_obs.md §4.2`); Adaline needs a `runEvaluation: bool` flag on `POST /logs/span` to force eval on a production span (`evals_obs.md §1.3`). **All of that machinery exists to reconnect the eval to the run.** Lancelot doesn't need it, because in Lancelot the eval *is* the run:

| Reconnection problem the corpus solves the hard way | Lancelot already has it (byte path) |
|---|---|
| W&B streaming accumulator folds SSE deltas → reconstruct `usage` (`evals_obs.md §4.2`, anti-pattern #5) | `_scan_last_data_frame` + `track_usage=True` on `pool.stream()` already retains the last `usage` frame and accumulates `cost_usd` (`lancelot_model_io.md §1.3`) |
| Adaline `runEvaluation` flag to force eval on a span (`evals_obs.md §5.3`) | a `lancelot.context.force_eval` ContextVar reads the *same* per-request seam (`evals_obs.md §7`) — one new ContextVar, no storage |
| Laminar self-traces every signal step via `emit_internal_span` for per-step rate (`evals_obs.md §3.1`) | every TransportPool call already emits `lancelot.upstream.{requests,duration,ttft,tokens,cost}` (`lancelot_model_io.md §2.5`) — per-step rate falls out |
| Weave needs `tracing_sample_rate` + "child traces never independently sampled" to keep complete eval trees (`evals_obs.md §4.2`) | `traceparent` auto-inject + asyncio copy-on-Task ContextVars give complete sub-call trees with no sampler (`lancelot_model_io.md §1.8, §3`) |

**The positioning sentence:** *"Lancelot evals share the production byte path — same transport, same cost meter, same traces. An eval run and a prod run are the same code; the only difference is who supplied the input."* This is the a16z "the verifier is the hardest part of RL" insight (`transcripts_a16z.md:230`) made cheap: the transport-level verifier (`classify_status` error taxonomy) is already there; the eval layer just adds the assertion.

---

## 2. MUST BUILD (smallest primitives, exact shapes + defaults)

### 2.1 `eval.unit` — deterministic feature×scenario, CI, sub-minute (BUILD FIRST)

The most durable metric in the entire corpus is **model-agnostic, deterministic, and free**. Build this before any LLM judge.

**Mechanisms to copy:**
- **Tool-trajectory match** — `EXACT | IN_ORDER | ANY_ORDER` over the `(tool_name, args)` sequence. ADK default = EXACT: `len(actual)==len(expected)` AND every zipped pair matches (`google_adk.md §12`). This is "assert the path" / process eval done deterministically — no judge, no tokens. **Default mode: `IN_ORDER`** (EXACT is too brittle — fails when the model finds a valid alternate path; ADK's own anti-pattern warns against EXACT-only, `google_adk.md:427`).
- **Tool-call validity** — does the call parse? does it satisfy the input schema? Eugene Yan: "reach for the cheap, specific metric before the expensive subjective one" — tool-call validity "catches 60%+ of production failures in practice" with zero LLM cost (`bad_guide_ai_research.md §5`). Lancelot already has the validator: `ToolSpec._validator` (`lancelot_serve_tools.md §2`).
- **Crashed case scores zero, never kills the run** — DSPy `failure_score = 0.0` (`dspy.md:342`). A raising prediction → score 0.0, not an exception. A `max_errors` budget aborts only if too many raise. This is the single most-overlooked harness-correctness decision.
- **Concurrency** — run cases concurrently (`asyncio.gather`), like Weave's `predict_and_score` (`evals_obs.md §1.2`). Sub-minute is the bar (OpenAI Codex "one-minute build loop they rebuilt infra to protect", `bad_guide_ai_research.md §4`; DLAI "2-minute eval cycle from code change to dashboard", `transcripts_dlai.md:270`).

**API sketch:**
```python
from lancelot.eval import unit, Case, trajectory

cases = [
    Case(input={"q": "refund order 123"},
         expect_tools=["lookup_order", "issue_refund"],   # IN_ORDER by default
         check=lambda out: out["status"] == "refunded"),    # pure predicate, the verifier
]

report = await unit(
    cases,
    run=agent_endpoint,            # ANY async (input)->{output, tool_calls}; the SAME prod handler
    trajectory_mode="in_order",    # "exact" | "in_order" | "any_order"
    concurrency=8,
)
# report.passed / report.failed / report.failures[] / report.pass_rate
assert report.pass_rate >= 0.999   # the CI gate
```

**Defaults (every one cited):**
| Constant | Value | Source |
|---|---|---|
| `trajectory_mode` | `"in_order"` | `google_adk.md:427` (EXACT too brittle) |
| `failure_score` | `0.0` (crash ≠ abort) | `dspy.md:342` (`evaluate.py:81`) |
| `concurrency` | `8` | Weave `asyncio.gather` pattern, `evals_obs.md §1.2` |
| `max_errors` | `len(cases)` (abort only if all raise) | `dspy.md` ParallelExecutor |
| release gate | `pass_rate >= 0.999` | Jake Heller 999/1000, `transcripts_yc_root.md:72` |

### 2.2 `eval.judge` — calibrated LLM-as-judge panel (Tier 2)

Build this *only* for what Tier 1 can't check objectively. The corpus pins the calibration mechanics precisely.

**Mechanisms to copy:**
- **Categorical rails, NOT numeric scores.** The judge must emit exactly one of a fixed label set — `rails=["correct","incorrect"]` (`transcripts_dlai.md:60`). "Categorical labels are consistent; numeric scores are not." Binary pass/fail beats Likert (Hamel + Shreya, `bad_guide_ai_research.md §5`). **Default rails: `["pass","fail"]`**; judge returns `{"verdict": <rail>, "reason": str}`.
- **One judge = one constraint (orthogonal panel).** "Single-constraint judges perform dramatically better than multi-constraint" (Maestro/AI21); "orthogonal judges — one faithfulness, one quality, one tool-call — separate, not combined" (MLflow/Databricks) — both `transcripts_dlai.md:64-65`. A `panel=[...]` is N independent judges, each scoring one axis, results NOT merged into a blended number.
- **The four model-agnostic failure axes** to ship as named built-in judges (Azure AI Foundry, `transcripts_msft_google.md §1.5`): `Intent` (interpreted task wrong), `Relevance` (went down a different path), `Groundedness` (asserts facts with no tool output to back them), `ToolAccuracy` (wrong tool / too many similar tools), `ToolOutputUtilization` (called the right tool, then ignored its output and hallucinated anyway). These are "the four ways a tool-calling agent fails — model-agnostic failure modes, not eval tactics."
- **`verify_grounding` as a deterministic-ish judge** — `Groundedness` is the highest-value cheap one: a judge prompt that checks whether every assertion in the response appears in `tool_outputs` (`transcripts_msft_google.md:100`). Pair with Jake Heller's grounding instruction: "answer only from what I give you; if it says the sky is purple, the sky is purple" — separates retrieval failure from reasoning failure (`transcripts_yc_root.md:76`).
- **Majority vote for stability** — ADK `final_response_match_v2`: 5 judge calls, majority vote, valid=1.0/invalid=0.0, score = `num_valid/num_evaluated`, `NUM_RUNS=2` (`google_adk.md §12`). **Default: `samples=1` (single call); `samples=5` for the panel-stability mode** — 5× cost is opt-in, not default (anti-overkill).
- **Judge alignment is a first-class step, not an afterthought.** The judge must be tuned to a human on ~50 labeled cases until agreement >90% before you trust it at scale: 50%→100% agreement after alignment (`transcripts_dlai.md:91`); improvement curve baseline 68% → +few-shot 84% → +metaprompt 88% (`transcripts_dlai.md:67-70`). Ship `judge.align(labeled_cases)` that reports agreement and warns below threshold.

**API sketch:**
```python
from lancelot.eval import judge, Judge, groundedness, tool_accuracy

faithfulness = Judge(
    name="faithfulness",
    rails=["pass", "fail"],          # categorical, binary
    prompt="Answer only from the tool outputs. Verdict pass iff every claim is supported.",
    model="gemini-2.5-flash",        # judge can be any provider via TransportPool
    samples=1,                       # 5 for majority-vote stability mode
)

report = await judge(
    cases,
    panel=[faithfulness, groundedness(), tool_accuracy()],  # orthogonal, NOT merged
    run=agent_endpoint,
)
# report.per_judge["faithfulness"].pass_rate, .per_judge["groundedness"]...

# calibration, run ONCE before trusting the judge at scale:
agreement = await faithfulness.align(human_labeled_cases)   # warns if < 0.9
```

**Defaults (every one cited):**
| Constant | Value | Source |
|---|---|---|
| `rails` | `["pass","fail"]` (categorical, binary) | `transcripts_dlai.md:60`, `bad_guide_ai_research.md §5` |
| judges per axis | 1 (orthogonal, never blended) | `transcripts_dlai.md:64-65` |
| `samples` (single call) | `1` (5 = opt-in majority-vote) | `google_adk.md §12` (`eval_metrics.py:93`) |
| `align` agreement floor | `0.90` (warn below) | `transcripts_dlai.md:91` |
| default judge model | env / caller-supplied, never hardcoded | `bad_guide_ai_research.md §9` (model-agnostic) |
| response-match threshold | `0.8` (when used) | `google_adk.md §12` (`eval_config.py:160`) |

### 2.3 `define_outcome` — bounded generate→grade→revise (Tier 3, the in-run quality loop)

This is the corpus's most-cited *in-run* eval pattern and it maps 1:1 to outcomes-based pricing (`how_they_did_it.md:263` — "the system must be able to define and measure an outcome to charge for it").

**Mechanisms to copy (Anthropic Deep Research, `deep_research.md §2`):**
- **Five verdict states** (NOT binary here — this is a control-flow grader, not a metric): `pass / needs_revision / needs_more_research / needs_restart / unresolvable`. On `unresolvable` → **log and STOP, do not loop** (`deep_research.md:46`).
- **`max_iterations` is REQUIRED, not optional.** Default 3 revisions, hard cap 20. The cap is the reliability bound — no unbounded self-revision (this is also the Misra-impossibility guard, see §4).
- **Rubric ceiling** 262,144 chars (256 KiB) — the rubric is passed as text to a judge model.
- **Cheap inline `advisor`** — a single-model "is this good enough to proceed?" check *short of* a full grade cycle, for between-step gating (`deep_research.md:44`).

**API sketch:**
```python
from lancelot.eval import define_outcome

grade = define_outcome(
    rubric="A correct refund: order located, amount matches, confirmation returned.",
    max_iterations=3,        # REQUIRED — no default that hides unboundedness
)

result = await grade.run(
    generate=agent_step,     # async (state)->output
    revise=agent_revise,     # async (output, verdict.reason)->output
)
# result.verdict in {pass, needs_revision, needs_more_research, needs_restart, unresolvable}
# result.iterations, result.final_output
```

**Defaults (every one cited):**
| Constant | Value | Source |
|---|---|---|
| `max_iterations` | `3` default, `20` hard cap, **required arg** | `deep_research.md:40` |
| verdict states | 5 (`pass/needs_revision/needs_more_research/needs_restart/unresolvable`) | `deep_research.md:46` |
| `unresolvable` behavior | log + STOP (never loop) | `deep_research.md:46` |
| rubric max | `262_144` chars | `deep_research.md:42` |

### 2.4 `@app.tool(verify=...)` — the verifier hook (build the check before the capability)

The single most-cited research-taste principle: **"build the verifier first; the capability follows"** (Hyung Won Chung, Jason Wei's Verifier's Law, Noam Brown's generator-verifier gap — all `bad_guide_ai_research.md §2`). Lancelot already auto-derives `output_schema` from Pydantic; the *verification gate* is the missing primitive.

```python
@app.tool(verify=lambda result, ctx: result.refund_amount > 0)
async def issue_refund(order_id: str) -> RefundResult: ...
```
A synchronous predicate checked **post-call, before the result returns to the loop**. Zero new deps (pure Python). On verify-fail → structured `ToolVerifyError` the loop can branch on (maps to `classify_status` → "permanent"/fix-the-input, `lancelot_model_io.md §2.3`). This is `bad_guide_ai_research.md §2`'s exact recommendation.

### 2.5 `lancelot.context.force_eval` — the eval==run toggle

One new ContextVar (default `False`). When a handler/middleware sets it, the eval layer forces a judge/grade on *this* call regardless of sample rate — the Adaline `runEvaluation` pattern (`evals_obs.md §5.3, §7`) on Lancelot's existing per-request seam. An incident handler can replay a failed request with `force_eval=True` to confirm the judge would have caught it. ~5 lines; no storage.

---

## 3. REUSE from the byte path (do NOT rebuild — these ARE the eval substrate)

| Eval need | Byte-path primitive | Why it's already enough |
|---|---|---|
| **Native traces** for process/trajectory evals | `traceparent` auto-inject + asyncio copy-on-Task ContextVars (`lancelot_model_io.md §1.8, §3`) | complete sub-call tree, no sampler, no ClickHouse — eval==run |
| **Cost per eval** / per-step cost | `cost_usd` ContextVar + `lancelot.upstream.cost.usd` histogram + `X-Lancelot-Cost-USD` (`lancelot_model_io.md §2.4-2.5`) | Noam Brown: "evals should plot against tokens/cost, not just score" (`bad_guide_ai_research.md §2`) — already emitted |
| **Per-step rate** (reliability is a distribution) | `lancelot.upstream.{requests,duration,ttft}` per call (`lancelot_model_io.md §2.5`) | Laminar self-traces each step to get this; Lancelot emits it natively |
| **Streamed-call token correctness** | `_scan_last_data_frame` + `track_usage=True` (`lancelot_model_io.md §1.3`) | solves Weave's streaming-accumulator anti-pattern #5 for free |
| **Transport-level verifier** | `classify_status` → `ok/quota/timeout/permanent/transient/upstream_5xx` (`lancelot_model_io.md §2.3`) | the agent loop's error-branch selector; 422→"permanent"=fix-the-input | 
| **Tool input/output validation** | `ToolSpec._validator` + auto `output_schema` (`lancelot_serve_tools.md §2`) | tool-call-validity unit metric needs zero new code |
| **Judge LLM calls** | `TransportPool.request()` — retry, budget cap, cancel, cost (`lancelot_model_io.md §1.1`) | judge is just another LLM call on the same pool; provider-agnostic |
| **pass^k / N trials** | `TransportPool.fanout(first_n, concurrency)` (`lancelot_model_io.md §1.7`) | run k rollouts concurrently for pass^k; W&B `trials=` analogue (`evals_obs.md §3.2`) |
| **Eval-gen / case batching** | `@app.batch(size_fn, max_size)` (`lancelot_serve_tools.md §11`) | coalesce eval-case LLM calls under a token budget |
| **Cache-stable case hashing** | `dumps_canonical(obj)` (`lancelot_model_io.md §4`) | stable eval-case fingerprints; `structural_skeleton_hash` (`evals_obs.md §7`) is the complement for cache reuse across runtime-param-only diffs |
| **Telemetry sink for judge spans** | `telemetry.set_sink` / `emit` (`lancelot_serve_tools.md §5`) | plug Langfuse/Phoenix if the operator wants it — Lancelot doesn't host it |
| **Eval runner as daemon** | `@app.lifetime_task` (`lancelot_serve_tools.md §11`) | continuous/online eval loop runs as a supervised background task |
| **Secrets for judge keys** | `lancelot.secrets.get(...)` (`lancelot_serve_tools.md §7`) | judge provider creds via the same lazy backend |
| **SSE framing for eval-progress stream** | `Event` + `sse_adapter` + `_clean_field` (`lancelot_serve_tools.md §1`) | stream eval progress to a dashboard with TTFT for free |

**Net:** the eval module is ~3 small files of *assertion + control-flow* logic. The transport, cost, trace, validation, batching, and streaming under it are all already built and load-tested. That ratio is the wedge.

---

## 4. REFUSE (off-thesis / depreciating / overkill — stop and check this list)

1. **❌ A SaaS observability/eval platform** (ClickHouse store, dashboards, hosted traces, distributed dedup queues). Laminar's `spans.rs` is 4,459 lines; W&B's `async_batch_processor` has a 31 MiB request limit and a 10k queue (`evals_obs.md §4.2, §8`). That is a *product*, not a primitive. Lancelot emits OTel `lancelot.upstream.*` + a `set_sink` hook — the operator points Langfuse/Phoenix/Datadog at it. **Lancelot is the byte path, not the eval SaaS.**

2. **❌ Generic metrics — ROUGE / BERTScore / blended numeric scores.** "Invisible to real failure modes" (Hamel, `bad_guide_ai_research.md §10.3`); "one client's core bug was date-handling failing 66% of the time, invisible to generic scores" (`bad_guide_ai_research.md §5`). Categorical/binary task-specific checks only. (ADK ships ROUGE-1 `response_match_score` — we expose it as an *optional* check, never the default, and never the headline.)

3. **❌ Numeric/Likert judge scores as the primary output.** Categorical rails beat numeric, empirically and across speakers (`transcripts_dlai.md:60-65`). A blended "7.3/10 quality score" is the anti-signal.

4. **❌ Self-referential eval loops (model judges its own output with no external grounding).** Misra's impossibility theorem: "without new grounding… outputs converge to a stationary distribution… a confident-but-wrong equilibrium" (`transcripts_a16z.md:133, 230`). Every eval MUST connect to a real verifier — schema, predicate, test output, HTTP/DB state, or human label. This is *why* `eval.unit` (deterministic) is built first and `define_outcome` has a hard `max_iterations` cap.

5. **❌ Evals that measure proxy/surface features** (response length, confidence words, format compliance). Reward hacking applies to evals: "any eval that measures proxy metrics will be gamed by a capable model… measure the actual outcome" (`transcripts_stanford_palantir.md:108, 442`). Verbose-response RLHF reward-hacking is the named failure (`transcripts_stanford_palantir.md:96`).

6. **❌ Anything in the hot path.** RED/TTFT metrics are operational; evals are a *side channel*. `force_eval` rides the existing per-request `finally` seam; judge calls are async and never block the response. The const tier (183k rps, zero-GIL) gets NO eval overhead by design (`lancelot_serve_tools.md §4` — native server emits no OTel either). Eval-as-run means *shares the path*, NOT *runs inline on every prod request*.

7. **❌ DSPy-style optimizer / prompt auto-tuning (MIPROv2, COPRO, RFT loops).** "Evaluation before optimization" is the rule (`transcripts_dlai.md:94`), and the *eval* is the durable layer; the optimizer is user-side agent logic on a 12-month model clock. Lancelot ships the grader (the reward signal); the RL/optimization loop is the user's (cardinal rule: AI logic stays user-side). The DSPy optimizer *signatures* are worth copying verbatim *if* a user builds this — but Lancelot does not ship it (`dspy.md:499`).

8. **❌ A "data viewer" / annotation UI.** Hamel calls the custom viewer "the highest-leverage thing you build" (`bad_guide_ai_research.md §5`) — but it's a UI product, not a framework primitive. We make traces queryable via OTel + `set_sink`; the viewer is the operator's (or a thin example).

9. **❌ Hardcoding judge model names or model-specific knobs in the module.** "Any primitive that mentions specific model names is a liability on a ~12-month clock" (`bad_guide_ai_research.md §9`). Judge model is always caller/env-supplied. (`gemini-2.5-flash` appears in cited *defaults* as ADK's choice — we cite it, we don't bake it.)

---

## 5. The build order (information-gained-per-unit-time — `bad_guide_ai_research.md §9`)

The riskiest assumption to kill first (Steinhardt): *is reliability attributable to the harness?* So the eval that isolates harness-vs-model is the first artifact — and it's `eval.unit` (deterministic, same prompts, swap harness config, measure delta). Concretely:

1. **`eval.unit`** (trajectory + tool-validity + predicate, `failure_score=0.0`, concurrent) — deterministic, CI, sub-minute. The 999/1000 gate. *This is the `make m1-conformance`-style gate for agent quality* (`transcripts_yc.md:57`).
2. **`@app.tool(verify=...)`** — the verifier hook (build-the-check-first). ~20 lines, zero deps.
3. **`force_eval` ContextVar** — the eval==run toggle. ~5 lines.
4. **`eval.judge`** (categorical rails, orthogonal panel, 4 built-in axes, `align()`) — only for what's not objectively checkable.
5. **`define_outcome`** (5 verdicts, required `max_iterations`) — the in-run revise loop + outcomes-pricing signal.

Tiers 1–3 ship together (a week of work, pydantic-only). Tiers 4–5 follow once the deterministic floor is proven.

---

## 6. Cardinal-rule compliance

- **Runtime dep = pydantic-only.** `eval.unit`/`@app.tool(verify=)`/`force_eval` are stdlib + pydantic. `eval.judge`/`define_outcome` make LLM calls via the *existing* `TransportPool` (no new dep). Provider SDKs + `sqlite-vec` (if a future case-store wants vector retrieval) stay lazy-imported. No `httpx`/`opentelemetry`/`tiktoken` pulled by `import lancelot.eval`.
- **Owns the byte path, not the agent.** The eval *runner* and *verifier hook* are byte-path-adjacent (they ride transport, cost, traces). The *judge prompts*, *rubrics*, *test cases*, and *optimization loop* are user-supplied — agent logic stays user-side.
- **Never in the hot path.** Side-channel only; const tier untouched.
- **Validate under real load.** Per the M4 lesson — the eval harness itself should run against a *real* agent endpoint (subprocess + HTTP), not just an in-process `TestClient`, so it exercises the path a bug would live on. `transcripts_yc.md:111` names this exact moat: "the eval harness tied to real load tests."

---

## 7. Open questions (flag for the orchestrator)

1. **Case store?** Do we ship a minimal `lancelot.eval.store` (sqlite, lazy `sqlite-vec` for semantic case retrieval) so the 10→50→100→1000 corpus (`transcripts_yc_root.md:71`) and "prod sample → labeled eval row → unit test" loop (`transcripts_dlai.md:89`) have a home — or is persistence strictly user-side? Leaning: a thin sqlite-only store (no vec by default) is in-thesis (it's plumbing, not AI logic); semantic retrieval over cases stays lazy.
2. **Eval-gen from AST** (Datadog: one docs page → ~200 deterministic `(question, ground-truth)` pairs, no LLM in the labeling step, `transcripts_dlai.md:73-82`) — genuinely durable and deterministic, but is "generate eval cases" agent logic (user-side) or framework convenience? Leaning: provide the *deterministic* AST→case helper as an example, not a core primitive.
3. **`p50/p80 horizon` reporting** — the METR task-horizon framing (`bad_guide_ai_research.md §4`, `transcripts_yc.md:47`) says reliability is a distribution over task *length*. Do we surface per-case latency/cost percentiles in `report` (cheap — the histograms already exist) so `unit`/`judge` reports show p50/p80, not just pass-rate? Leaning: yes, it's free from existing metrics and matches Noam Brown's "plot against tokens/cost/time."
4. **`pass^k` as a first-class `unit(..., k=N)` arg** vs. leaving it to the caller wrapping `fanout`. Leaning: a `k=` arg (default 1) that runs N trials per case and reports pass^k is cheap and matches W&B `trials=` (`evals_obs.md §3.2`) — reliability-as-a-distribution made one line.
5. **Trajectory match for *streamed* tool calls** — the trajectory comparator needs the assembled tool-call list; confirm `track_usage`/stream accumulation surfaces tool_use blocks (not just `usage`) for streamed agent runs, or document that trajectory evals run on the non-streamed path.
