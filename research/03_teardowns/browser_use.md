# Browser-Use + MultiOn — Agent Framework Teardown

**Sources:** `researchfms/teardowns/BROWSER_USE.md` (1,899 lines, two rounds), `researchfms/teardowns/MULTION.md` (814 lines + coverage expansion)
**Lens:** Extract mechanism-level agent primitives for Lancelot's byte-path framework. Lancelot owns transport/streaming/serving; the agent half (loop, context engineering, memory, evals, durable seam, safety, orchestration) is the build target.

---

## 1 — The 6-step pre-done verification checklist (Browser-Use `system_prompt.md:138-152`)

The single most copy-worthy non-obvious primitive. Before calling `done(success=True)`, the LLM is *instructed* to run:

1. Re-read the user request verbatim.
2. Count items found vs. items requested.
3. Verify actions *actually completed* (form submitted? file saved?).
4. Verify data grounding — every URL/price/name appears in a tool output.
5. Check for blocking errors (payment declined, login failed → `success=false`).
6. ANY uncertainty → `success=false`.

**Why durable:** This is not a prompt trick — it is a *behavioral contract* baked into the system prompt that forces the LLM to do explicit completion verification rather than optimistically calling done. Compaction anti-hallucination (§2) plus pre-done verification (here) are the two places Browser-Use's production experience surfaces as structural prompt engineering.

**Lancelot byte-path:** Lancelot has no done-action signal. Lancelot's `@app.tool` + `ToolSpec` auto-derives `output_schema`, but has no concept of a run-termination action or a verification gate. The agent layer needs both: a `done(text, success)` action AND this 6-step gate injected into the system prompt.

---

## 2 — Compaction anti-hallucination system prompt (`message_manager/service.py:213-301`)

The full compaction prompt (verbatim from `BROWSER_USE.md §6.3`):

```
You are summarizing an agent run for prompt compaction.
Capture task requirements, key facts, decisions, partial progress, errors, and next steps.
Preserve important entities, values, URLs, and file paths.
CRITICAL: Only mark a step as completed if you see explicit success confirmation in the history.
If a step was started but not explicitly confirmed complete, mark it as "IN-PROGRESS".
Never infer completion from context — only report what was confirmed.
Return plain text only. Do not include tool calls or JSON.
Keep under 6000 characters if possible.
```

**The mechanism:** `compact_every_n_steps=25`, `trigger_char_count=40000` (~10k tokens), `keep_last_items=6` (last 6 steps survive uncompacted). Summary capped at `summary_max_chars=6000`. Call uses `page_extraction_llm` or main LLM.

**Why "Never infer completion from context" is load-bearing:** LLMs summarizing multi-step agent histories will optimistically mark steps as done when context implies success. The explicit instruction fights this. This is a *hard-won production lesson*, not a theoretical concern.

**Lancelot byte-path:** Lancelot has no compaction primitive. Lancelot's `canonical.dumps_canonical` ensures prompt prefix stability, but that operates on the outbound request. The agent layer needs a compaction call that sits in the ContextBundle assembly step — a `compact_history(history, max_chars=6000)` that uses this exact system prompt as a template.

---

## 3 — One-shot read_state channel (`message_manager/service.py`, `agent/prompts.py`)

When an action returns `include_extracted_content_only_once=True`, the content goes into `read_state` (not long-term memory). It appears in ONE subsequent message, then is discarded. Used for: extract action output, read_file, dropdown options.

**The structural benefit:** separates "large one-off read" from "persistent memory". Without this, large extractions pollute every subsequent message token budget. The `read_state` channel gives the LLM one chance to process big content, then clears it — O(1) budget per action, not O(N) per remaining step.

**Constants:** `MAX_CONTENT_SIZE=60000` chars (`message_manager/service.py:354`). When extracted_content < 10000 chars → goes to `long_term_memory`; else → saved to file system, memory becomes `"Query: ... Content in {file} and once in <read_state>."`.

**Lancelot byte-path:** Lancelot's `@app.tool` return values go to the LLM as tool output. There is no distinction between one-shot large content and persistent memory. The agent layer needs a `read_state` annotation on tool returns that controls lifetime in the ContextBundle.

---

## 4 — Anthropic double-serialization repair (`llm/anthropic/chat.py:225-244`)

When Claude emits tool_use output, the SDK repairs double-serialization with this exact code:

```python
_input = content_block.input
if isinstance(_input, str):
    _input = json.loads(_input)
elif isinstance(_input, dict):
    for key, value in _input.items():
        if isinstance(value, str) and value.startswith(('[', '{')):
            try:
                _input[key] = json.loads(value)
            except json.JSONDecodeError:
                cleaned = value.replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')
                try:
                    _input[key] = json.loads(cleaned)
                except json.JSONDecodeError:
                    pass
```

**Why this exists:** Claude occasionally emits `{"actions": "[{...}]"}` instead of `{"actions": [{...}]}` — a string that looks like JSON. This intercepts validation failure BEFORE incrementing the failure budget, attempts `json.loads` on any string starting with `[` or `{`, then retries validation. The fallback strips control characters and retries once more.

**This is not optional glue code.** In production deployments against Claude 3.x, this repair fires regularly. Without it, valid agent outputs increment failure counters and degrade task completion rates.

**Lancelot byte-path:** Lancelot's `upstream_metrics` does provider detection. The model adapter layer (absent today) needs provider-specific output normalization. The Anthropic adapter should include this repair before schema validation. Lancelot's `TransportPool` handles transport; the repair lives in the message-shape translation layer that Lancelot doesn't yet have.

---

## 5 — Provider-aware `llm_timeout` table (Browser-Use `agent/service.py:266-282`)

Auto-detected per model name (KNOWN, verbatim):
- `"gemini"` → 75s
- `"gemini-3-pro"` → 90s
- `"groq"` → 30s
- `"o3"` / `"claude"` / `"sonnet"` / `"deepseek"` → 90s
- default → 75s

And for step_timeout: 180s (views.py:86). Extract `llm_timeout`: 120s (tools/service.py:1163).

**Why durable:** These are not guesses — they're production-calibrated from real p95 latency observations across providers. Groq's 30s reflects their aggressive QPS limits; Sonnet's 90s reflects Anthropic's extended latency on long context. A generic 60s timeout will either cut off slow providers or waste budget waiting for failed fast ones.

**Lancelot byte-path:** `TransportPool` has a per-request timeout. `upstream_metrics` does provider detection. The missing piece: `ProviderProfile.default_timeout` so route configurations auto-set sensible timeouts rather than requiring each call site to hard-code them.

---

## 6 — Anthropic 4096-token cache-threshold padding (`agent/prompts.py:17-24`)

Browser-Use inflates its Anthropic-flash system prompt to `>=4096 tokens` specifically to hit Anthropic's prompt cache threshold. A dedicated variant `system_prompt_anthropic_flash.md` (240 lines) exists only for this. `cache_control='ephemeral'` is set on every Anthropic call (`llm/anthropic/chat.py:190`).

**The mechanism:** Anthropic's prompt caching requires a minimum of 1024 tokens (as of 2025) in the cached prefix. The break-even is roughly $0.30 per 100 steps on a 5KB system prompt when cache hits — non-trivial in production. The variant pumps the prompt past the threshold even when flash mode strips the reasoning fields.

**Lancelot byte-path:** `canonical.dumps_canonical` already provides prefix stability (the byte-path prerequisite for cache hits). The agent layer needs to be aware of the Anthropic cache threshold and ensure the system prompt + tool schema block exceeds it. Lancelot's `TransportPool` auto-injects nothing prompt-related — the agent assembles the messages and owns this.

---

## 7 — Step budget warning + force-done (`agent/service.py:876-894`, `system_prompt.md:133-137`)

Two mechanisms that work together:

**Budget warning at 75%:** When `step_number >= 0.75 * max_steps`, inject into the user message: *"You have used 75% of your step budget. Critically evaluate completion strategy."*

**Force-done at last step:** When `step_info.is_last_step()`, inject: *"This is your LAST step. Call done immediately with whatever results exist."* — prevents the agent from silently running off the end with no output.

**Lancelot byte-path:** Neither primitive exists in Lancelot. The agent loop needs `step_info(n, max)` injected into the context bundle per step, and the ContextBundle assembly needs to detect the 75% and last-step signals.

---

## 8 — Escalating loop-detection nudges, NOT hard-blocking (`agent/views.py:157-249`)

`ActionLoopDetector`: rolling window of 20 action hashes (SHA-256 of `"{action_name}::{json.dumps(params, sort_keys=True)}"`, truncated to 16 hex). Three *soft* escalation thresholds (never blocks):
- 5+ repetitions: mild advisory.
- 8+ repetitions: "are you still making progress?"
- 12+ repetitions: strong advisory.
- 5+ consecutive stagnant pages (fingerprint = (url, element_count, sha256(dom_text)[:16])): separate stagnation warning.

**[CORRECTION re Round 1]:** The teardown explicitly corrects a common misunderstanding: loop detection is PURELY advisory. The hard abort is from the failure budget (`max_failures=5`), not the loop detector. Conflating them is a design error.

**Why the distinction matters for Lancelot:** The agent loop needs both: (a) soft loop nudges that add context to the prompt and (b) a separate hard failure budget. Implementing loop detection as a guard (blocking the next action) would cut off legitimate near-identical steps (e.g., typing characters into a field).

---

## 9 — MultiOn: `CONTINUE`/`ASK_USER`/`DONE` status + multi-turn escalation (`MULTION.md §2.4, §8.3`)

The three-state run status (KNOWN from Fern SDK):
- `DONE` — agent completed successfully.
- `CONTINUE` — agent exhausted `max_steps`; user can call `browse` again with same `session_id` to extend.
- `ASK_USER` — agent encountered genuine ambiguity (e.g., "indoor or outdoor seating?"); agent returns the question in `message`, caller provides answer in next call's `cmd`.

**Why `ASK_USER` is a first-class action, not an error:** Forcing a guess doubles failure probability on ambiguous branches. A `CONTINUE`/`ASK_USER`/`DONE` triad is cleaner than a single boolean `done`. The `CONTINUE` status enables per-step billing extension rather than full restarts — a commercial design that also happens to be technically correct.

**Lancelot byte-path:** The agent layer's `done(text, success)` action should be extended to a three-state outcome. `CONTINUE` maps to "budget exhausted, resumable" — which requires the durable seam (event-sourced checkpoint). `ASK_USER` maps to HITL (human-in-the-loop) — which also requires the durable seam. Both gates on the durable checkpoint primitive.

---

## 10 — Agent Q: step-level DPO with MCTS process supervision (MULTION.md §3, §A.1-A.10)

This is a training-time primitive, not a runtime one — but it's the most durable finding in the MultiOn teardown.

**The mechanism (verbatim from paper §5):**
1. For each task, build an MCTS tree: at each node, sample K=4 candidate actions from policy.
2. AI critic iteratively ranks K actions (remove top, re-rank, repeat — full ranking from K rounds).
3. UCB1 selection: `Q(h_t, a) + c_exp * sqrt(log N(h_t) / (1 + N(h_{t+1})))`.
4. Execute selected action, roll out to terminal, backpropagate R ∈ {0,1}.
5. Construct DPO preference pairs: `(h_t, a_w, a_l)` where `|Q(a_w) - Q(a_l)| > θ_threshold`.
6. DPO update: step-level (per-action), not trajectory-level.

**Why step-level beats trajectory-level:** OpenTable averages 13.9 steps. Trajectory DPO makes ALL steps dispreferred if step 7 failed — even if steps 1-6 were correct. Step-level credit assignment (`agent/views.py` analog: action + result per step) is finer-grained.

**Key numbers (KNOWN — paper §6.2):**
- Base LLaMA-3-70B zero-shot: 18.6%
- + RFT (600 successful trajectories only): 67.2% — the massive SFT jump
- + DPO (outcome only): 71.8%
- + MCTS-only: 75.2%
- + Agent Q (MCTS + AI process supervision + DPO): 81.7%
- + MCTS at inference (ludicrous mode): 95.4%

**The honest heuristic:** RFT alone (600 successes, SFT) gives 67.2% — a 48-point jump from base. The entire MCTS+DPO stack adds another 28 points. For a first pass, just collecting 600+ successful trajectories and doing SFT is the cheapest high-leverage move before doing RL.

**Lancelot byte-path:** Lancelot has no training pipeline. But the *data collection infrastructure* for SFT/DPO is affected by Lancelot's byte path:
- `TransportPool` auto-injects `traceparent` — every agent run produces a distributed trace, enabling trajectory reconstruction from OTel.
- `upstream_metrics` emits `ok/quota/timeout/permanent/transient` per call — these are already the labels needed to construct preference pairs (success vs. failure trajectories).
- Lancelot's `@app.batch` is not a training primitive, but the coalescing insight (prefix-grouping) is the batch-collection strategy for SFT: group trajectories by URL prefix to maximize training diversity.

---

## 11 — Provider-specific structured output enforcement table (Browser-Use `BROWSER_USE.md §Round2.G`)

| Provider | Mechanism | Gotcha |
|---|---|---|
| OpenAI | `ResponseFormatJSONSchema(strict=True)` | No `oneOf`; requires `additionalProperties: false`; all fields required |
| Anthropic | Single tool + `tool_choice={"type":"tool","name":tool_name}` | `cache_control='ephemeral'` on system message; repair double-serialization (§4 above) |
| Gemini | `response_mime_type='application/json'` + `response_schema=...` | Pattern/format constraints NOT enforced |
| Browser-Use cloud | `payload['output_format'] = model.model_json_schema()` | Server-side constrained decode (xgrammar-style) |

**The `SchemaOptimizer` transform:** Browser-Use ships a `llm/schema.py` that transforms a Pydantic model's JSON Schema per-provider — stripping `oneOf` for OpenAI strict, adjusting `required` fields, etc. This is the load-bearing piece for reliable structured output across providers.

**Lancelot byte-path:** `TransportPool.request()` passes JSON bodies. The model adapter layer (Lancelot's missing piece) needs per-provider schema normalization. The `SchemaOptimizer` pattern is worth copying verbatim: a stateless function `normalize_schema(schema, provider) -> schema` that transforms once at model-init time.

---

## Anti-patterns to avoid

**1. Indexing as immutable IDs.** Browser-Use's DOM indices are reassigned on EVERY `serialize_accessible_elements()` call (BROWSER_USE.md §Round2.B, Phase B.5). Any agent that caches "click index 7 later" without refreshing state will fail silently. The correct design: indices are ephemeral per-step handles; the agent must refresh state before every index-targeted action.

**2. Loop detection as a hard guard.** The teardown explicitly corrects the common misimplementation. Loop detection emits context nudges; the failure budget handles hard abort. Mixing them creates an agent that gives up on legitimate repetitive actions.

**3. Trajectory-level DPO for long-horizon tasks.** Step-level DPO with MCTS process supervision outperforms trajectory-level DPO by ~10 points on 13.9-step tasks. For agent evals, use per-step credit assignment, not per-trajectory.

**4. Oversized action spaces in fine-tuned agents.** MultiOn's 7-action space vs. Browser-Use's 30 is deliberate: smaller action space = easier fine-tuning. For an RL-trained policy, prune the action space aggressively. For a prompt-only agent, 30 actions is fine.

**5. Treating `done(success=True)` as a simple output.** Without the 6-step pre-done verification gate, LLMs optimistically report success. Verification must be in the system prompt as a mandatory pre-condition for calling done, not a recommendation.

---

## Constants for direct copy

From Browser-Use, production-calibrated (all KNOWN from source):

| Constant | Value | What it guards |
|---|---|---|
| `compact_every_n_steps` | 25 | Compaction frequency |
| `trigger_char_count` | 40,000 | ~10K tokens; compaction trigger |
| `keep_last_items` | 6 | Steps preserved uncompacted |
| `summary_max_chars` | 6,000 | Compaction output cap |
| `max_failures` | 5 | Hard abort budget |
| `max_actions_per_step` | 5 | Actions per LLM call |
| `loop_detection_window` | 20 | Action hash rolling window |
| `loop_nudge_thresholds` | 5, 8, 12 | Escalating loop warnings |
| `stagnation_threshold` | 5 consecutive | Page fingerprint stagnation |
| `llm_timeout` (Claude/Sonnet) | 90s | Per-provider LLM call timeout |
| `llm_timeout` (Groq) | 30s | Per-provider LLM call timeout |
| `step_timeout` | 180s | Hard step deadline |
| `budget_warn_at` | 75% of max_steps | Budget pressure injection |
| `anthropic_cache_min_tokens` | 4,096 | Cache-threshold inflation point |
| `read_state_max_content` | 60,000 chars | One-shot read_state cap |
| `long_term_memory_max` | 10,000 chars | In-message memory cap |
| From MultiOn: `session_idle_timeout` | 10 minutes | Session cleanup |
| From MultiOn: `browse_default_max_steps` | 20 | Per-browse call step budget |
