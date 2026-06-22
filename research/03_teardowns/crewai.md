# CrewAI — Mechanism-level distillation for Lancelot

Sources: `CREWAI.md` (Rounds 1/2/3, 2283 lines) + `CREWAI_PLUS.md` (Rounds 1/2, 881 lines).

---

## 1. Dual execution models — and why to copy only one

CrewAI auto-detects whether the model supports native function calling and dispatches to one of two loops (`crew_agent_executor.py:292`, KNOWN):

```python
def _invoke_loop(self) -> AgentFinish:
    use_native_tools = (
        hasattr(self.llm, "supports_function_calling")
        and callable(getattr(self.llm, "supports_function_calling", None))
        and self.llm.supports_function_calling()
        and self.original_tools
    )
    if use_native_tools:
        return self._invoke_loop_native_tools()   # OpenAI/Anthropic tool_calls JSON
    return self._invoke_loop_react()               # regex parse "Action:" text
```

**The ReAct text path is a depreciating tactical hack** — it exists for models that don't support structured tool calls (older/smaller models). On modern OpenAI and Anthropic models the native path is always taken. Do not copy the ReAct regex loop for new builds. Copy the native-tools loop only.

The native loop (KNOWN, `crew_agent_executor.py:449`):
```
while True:
    if iterations >= max_iter (25): handle_max_iterations_exceeded(); return
    answer = llm.call(messages, tools=openai_tools)
    if isinstance(answer, list) and _is_tool_call_list(answer):
        tool_finish = _handle_native_tool_calls(answer, available_functions)
        if tool_finish is not None: return tool_finish
        continue
    # model returned text → wrap as AgentFinish and return
```

**Lancelot implication:** `lancelot.mcp.mount` + `@app.tool` already produce OpenAI-schema tool specs. The native loop (≤30 lines) is the correct loop to build on. The ReAct parser is noise.

---

## 2. ToolUsage 4-stage input-repair ladder + fuzzy name fix

This is the highest-value piece in the codebase. Source: `tool_usage.py` (KNOWN, §R3.3).

### Fuzzy tool-name correction — `_select_tool`

```python
# tool_usage.py:732 (KNOWN)
best = max(tools, key=lambda t:
    SequenceMatcher(None, sanitize(t.name), sanitize(asked_name)).ratio())
if best.ratio() > 0.85 or exact_match:
    return best
raise ToolSelectionError(listing_all_valid_tools)
```

Threshold: **>0.85**. This silently fixes `web_serch` → `web_search`. Below threshold, the error message lists valid tools (injected back into messages so the model self-corrects next turn).

### 4-stage `_validate_tool_input`

```python
# tool_usage.py:857 (KNOWN)
1. json.loads(tool_input)                  # fast path, strict JSON
2. ast.literal_eval(tool_input)            # Python literals: {'key': 'val'}, True, None
3. json5.loads(tool_input)                 # trailing commas, comments, unquoted keys
4. repair_json(tool_input, skip_json_loads=True) → json.loads   # json_repair lib
   # else: emit ToolValidateInputErrorEvent; raise
```

Every malformed tool argument blob goes through all four before failing.

### `_tool_calling` recursive re-parse

```python
# tool_usage.py:834 (KNOWN)
def _tool_calling(self, tool_string):
    try:
        try:    return self._original_tool_calling(tool_string, raise_error=True)
        except:
            if self.function_calling_llm:
                return self._function_calling(tool_string)   # LLM-assisted: ask an LLM to re-emit valid JSON
            return self._original_tool_calling(tool_string)
    except:
        self._run_attempts += 1
        if self._run_attempts > self._max_parsing_attempts:   # 3 default, 2 for big models
            return ToolUsageError("...Moving on then.")
        return self._tool_calling(tool_string)   # recurse
```

LLM-assisted re-parse prompt (KNOWN, `tool_usage.py:794`):
```
The schema should have the following structure, only two keys:
- tool_name: str
- arguments: dict (always a dictionary, with all arguments being passed)
Example: {"tool_name": "tool name", "arguments": {"arg_name1": "value", "arg_name2": 2}}
```

### Constants

| Constant | Default | Big-model override |
|---|---|---|
| `_max_parsing_attempts` | 3 | 2 |
| `_remember_format_after_usages` | 3 | 4 |
| SequenceMatcher threshold | >0.85 | same |
| `OPENAI_BIGGER_MODELS` | gpt-4, gpt-4o, o1, o1-mini, o1-preview, o3, o3-mini | — |

### Repeated-call short-circuit

```python
# tool_usage.py:701 (KNOWN)
if (tool_name, arguments) == tools_handler.last_used_tool:
    return i18n_slice("task_repeated_usage")
    # "I tried reusing the same input, I must stop using this action input."
```

**Lancelot implication:** the 4-stage repair + 0.85 threshold + repeated-call short-circuit are durable primitives that belong in Lancelot's `lancelot.tool` layer. `lancelot.mcp` already has the 4KB warn discipline; these are the *invocation-side* complements.

---

## 3. Streaming is a re-entrant thread trick — not native streaming

`Crew.kickoff` with `stream=True` (KNOWN, `crew.py:977`):

```python
if self.stream:
    enable_agent_streaming(self.agents)
    ctx = StreamingContext()
    def run_crew():
        self.stream = False
        crew_result = self.kickoff(inputs, input_files)   # re-enter sync
        ...
    streaming_output = CrewStreamingOutput(
        sync_iterator=create_chunk_generator(ctx.state, run_crew, ctx.output_holder))
    return streaming_output
```

Streaming = run the normal blocking pipeline in a thread, emit events to a queue, expose a sync iterator. No native streaming LLM output is surfaced chunk-by-chunk to the caller.

**Lancelot implication:** Lancelot's `@app.stream` already does the correct thing — async-gen → Rust R3 pump → bounded-mpsc → hyper with backpressure and GIL dropped per chunk. Do not copy the thread-trick pattern. Lancelot's streaming is structurally superior.

---

## 4. Force-final-answer / max-iter graceful exit

Verbatim prompts from `translations/en.json` (KNOWN, §R2.1):

```
force_final_answer:       "Now it's time you MUST give your absolute best final answer.
                           You'll ignore all previous instructions, stop using any tools,
                           and just return your absolute BEST Final answer."

force_final_answer_error: "You can't keep going, here is the best final answer you generated:\n\n {formatted_answer}"
```

The loop also has a "format error" re-injection (KNOWN):
```
"I did it wrong. Invalid Format: I missed the 'Action:' after 'Thought:'. I will do right next,
 and don't use a tool I have already used."
```

**Lancelot implication:** copy the max-iter graceful exit verbatim. The two-prompt sequence (soft nudge → hard fallback) is battle-tested on thousands of production runs.

---

## 5. Memory recall weights — durable scoring formula

Unified memory recall (KNOWN, `recall_flow.py`, §R2.3 / §R2.10):

```
score = semantic_weight * cosine
      + recency_weight  * exp(-elapsed_days * ln(2) / recency_half_life_days)
      + importance_weight * importance
```

Defaults:
- `semantic_weight = 0.5`
- `recency_weight = 0.3`
- `recency_half_life_days = 30`
- `importance_weight = 0.2`
- `consolidation_threshold = 0.85` (cosine above → dedup/merge)
- `consolidation_limit = 5` (nearest records compared during consolidation)
- `confidence_threshold_high = 0.8` (confident → return as-is)
- `confidence_threshold_low = 0.5` (uncertain → deep exploration)
- `complex_query_threshold = 0.7`
- `exploration_budget = 1` (rounds of deep recall)
- `query_analysis_threshold = 200` (chars; shorter queries skip LLM analysis)

**Lancelot implication:** `lancelot.context.cost_usd/tokens_in/out` + the `canonical.dumps_canonical` KV-cache primitive are the byte-path side of memory. Lancelot does not yet have a typed agent memory store (4-tier RAM/episodic/semantic/procedural). These weights are the correct defaults when that store is built.

---

## 6. Memory extraction and consolidation prompts (durable)

KNOWN verbatim from `translations/en.json` (§R2.1). The extraction system prompt is worth copying as a baseline:

```
You extract discrete, reusable memory statements from raw content.
Each memory must:
- Be one clear sentence or short statement
- Be understandable without the original context
- Capture a decision, fact, outcome, preference, lesson, or observation worth remembering
- NOT be a vague summary or a restatement of the task description
...
Preserve exact names and numbers — never generalize (e.g. keep "lavender gin fizz" not just "cocktail")
...
Presupposed facts: When the user reveals a fact indirectly in a question, extract that fact.
...
If there is nothing worth remembering (e.g. empty result, no decisions or facts), return an empty list.
```

The consolidation prompt decision tree (KNOWN):
```
For each existing memory, choose one action:
- 'keep': still accurate and not redundant
- 'update': update with new information
- 'delete': outdated, superseded, or contradicted
Also decide insert_new=true/false.
Be conservative: prefer 'keep' when unsure.
```

**Lancelot implication:** these prompts encode accumulated failure modes from real production use. Copy them as the baseline for Lancelot's memory encoding layer, not as a starting-from-scratch experiment.

---

## 7. Guardrail retry contract

`task.py:269` + `llm_guardrail.py:98` (KNOWN, §R3.4):

- A guardrail is `callable(TaskOutput) -> (bool, Any)` OR a string (auto-wrapped into an LLM validator agent).
- String guardrail judge prompt (KNOWN, verbatim):
  ```
  Ensure the following task result complies with the given guardrail.
  Task result: {task_output.raw}
  Guardrail: {description}
  - Confirm if the Task result complies with the guardrail.
  - If not, provide clear feedback explaining what is wrong.
  - Focus only on identifying issues — do not propose corrections.
  Returns: LLMGuardrailResult(valid: bool, feedback: str | None)
  ```
- Retry loop: up to `guardrail_max_retries=3` (default). Validation error re-injection slice (KNOWN):
  ```
  ### Previous attempt failed validation: {guardrail_result_error}
  ### Previous result:
  {task_output}
  Try again, making sure to address the validation error.
  ```
- `GuardrailResult` enforces result XOR error invariant via Pydantic field validator.
- **Callable guardrails are dropped on checkpoint serialization** (UserWarning). String guardrails survive. Important for durable agents.

**Lancelot implication:** `@app.tool` has output_schema auto-derivation. The guardrail pattern (`callable or string, retry up to N, inject feedback`) is the correct contract for Lancelot's agent-level output validation surface.

---

## 8. Context-window overflow handler — the summarization prompt

KNOWN verbatim, `translations/en.json:summarize_instruction` (§R2.1):

```
Analyze the following conversation and create a structured summary that preserves all
information needed to continue the task seamlessly.

Create a summary with these sections:
1. Task Overview: What is the agent trying to accomplish?
2. Current State: What has been completed so far? What step is the agent on?
3. Important Discoveries: Key facts, data, tool results, or findings that must not be lost.
4. Next Steps: What should the agent do next based on the conversation?
5. Context to Preserve: Any specific values, names, URLs, code snippets, or details referenced.

Wrap your entire summary in <summary> tags.
```

After summarization, the restored system message prefix:
```
<summary>
{merged_summary}
</summary>

Continue the task from where the conversation left off.
The above is a structured summary of prior context.
```

**Lancelot implication:** this belongs in Lancelot's context engineering layer (ContextBundle compaction). The 5-section structure is a proven template for mid-task compression that preserves actionability.

---

## 9. Planning: one-shot plan injection onto task descriptions

`planning_handler.py:55` + `crew.py:1405` (KNOWN, §R3.6):

- Default planner model: **`gpt-4o-mini`** (overridable).
- Output: structured list `[{task_number: int, task: str, plan: str}]`.
- Injection: `task.description += plan_map[n]` — plans are **concatenated onto** task descriptions before the normal run. No separate execution stage.
- Per-task summary template fed to planner (KNOWN, §R3.6): includes `task_description`, `task_expected_output`, `agent.role`, `agent.goal`, `task_tools`, `agent_tools`, and optionally `agent_knowledge`.

Planning system prompt (KNOWN, verbatim):
```
Focus on CONCRETE, EXECUTABLE steps. Each step must clearly state WHAT ACTION to take and
HOW to verify it succeeded. The number of steps should match the task complexity.
Hard limit: {max_steps} steps.

Rules:
- Each step must have a clear DONE criterion
- NO standalone "thinking" or "planning" steps — act, don't just observe
- The last step must produce the required output

After your plan, state READY or NOT READY.
```

**Lancelot implication:** the plan-injection pattern (one-shot plan prepended to task prompt) is simple and cheap (~1 extra LLM call before execution). The `gpt-4o-mini` default is correct (planning is structured output over a fixed schema; small model is sufficient). Worth copying for Lancelot's planning mode.

---

## 10. OpenAI structured output compatibility requirement

KNOWN, `memory/analyze.py` (§R2.4):

```python
class ExtractedMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")   # REQUIRED for OpenAI structured output
```

OpenAI structured output (`response_format=BaseModel`) rejects schemas with `additionalProperties` not set to false. Every Pydantic model used for LLM-structured output must explicitly set `model_config = ConfigDict(extra="forbid")`. Anthropic and Gemini tolerate extras; OpenAI does not. This is a hard production constraint that bites silently (the schema validates locally but the API call fails).

**Lancelot implication:** `@app.tool` auto-derives `output_schema`. If Lancelot exposes structured output for agent memory/guardrails, every schema-bearing Pydantic model must carry `extra="forbid"`.

---

## 11. Memory slice — the "don't just scan, enumerate" pattern

KNOWN verbatim, `translations/en.json:memory` (§R2.1):

```
# Memories from past conversations:
{memory}

IMPORTANT: The memories above are an automatic selection and may be INCOMPLETE.
If the task involves counting, listing, or summing items (e.g. 'how many', 'total', 'list all'),
you MUST use the Search memory tool with several different queries before answering — do NOT rely
solely on the memories shown above. Enumerate each distinct item you find before giving a final count.
```

This is the kind of instruction you only write after watching agents give wrong counts repeatedly. It's a durable heuristic: LLM recall-from-injected-context is unreliable for exhaustive enumeration; forcing the tool-search path is the fix.

---

## 12. HITL pre-review: distill feedback into reusable lessons

KNOWN verbatim, `translations/en.json` (§R2.1):

Distill system:
```
You extract generalizable lessons from human feedback on system outputs. A lesson should be a
reusable rule or preference that applies to future similar outputs — not a one-time correction
specific to this exact content.
If the feedback is just approval or contains no generalizable guidance, return an empty list.
```

Pre-review system (applied before human sees output, using past lessons):
```
You are reviewing content before a human sees it. Apply the lessons from past human feedback
to improve the output. Preserve the original meaning and structure, but incorporate the
corrections and preferences indicated by the lessons.
```

**Lancelot implication:** this "distill-then-apply" HITL loop is a durable design: store generalizable lessons separately from task outputs, apply them as a pre-filter. Not a feature for a first agent release but the correct architecture for iterative improvement.

---

## 13. What to REFUSE from CrewAI

### Avoid: two execution models

Maintaining both a ReAct text-parsing loop and a native-tools loop doubles the surface to test and debug. CrewAI's ReAct path is legacy; it exists only to support models without native function calling. For Lancelot's agent layer, build exactly one loop: native function calls. The native-tools model is where all modern providers have converged.

### Avoid: fake streaming via thread re-entry

CrewAI's streaming = re-run the blocking pipeline in a thread. This is complexity for no structural benefit. Lancelot's `@app.stream` async-gen → Rust pump is already correct; don't replicate the thread trick.

### Avoid: `raise` on config error at instantiation

CrewAI raises immediately (e.g., `raise Exception("Manager agent should not have tools")`) at kickoff-time configuration checks — inside `_create_manager_agent`. This is correct behavior but note that callable guardrails silently disappear on checkpoint serialization (only a `UserWarning`). For Lancelot: config errors should raise at registration time, not at runtime; serialization loss should be an error, not a warning.

### Avoid: 30K-line framework

CrewAI is ~85K lines because of: 30+ pre-built tools, the Flow API (a second separate framework), 5 native LLM SDK wrappers, 20+ event types, the CLI, the A2A/MCP protocol implementations, the knowledge system. The core loop that matters is ~1,250 lines. Don't let scope creep replicate the full package.

---

## 14. Plus/Enterprise — byte-path-relevant findings only

From `CREWAI_PLUS.md` (KNOWN):

- **Tracing event taxonomy** (KNOWN, inferred from `TraceBatchManager`): `crew_kickoff_started/completed/failed`, `task_started/completed/failed`, `agent_execution_started/completed/failed`, `llm_call_started/completed`, `tool_usage_started/completed/failed`. This is the correct OTel span hierarchy for agents. Lancelot's `lancelot.upstream_metrics` + `lancelot.context` ContextVars already cover the LLM-call and cost attribution layers; the agent/task span wrapping above them is the missing piece.
- **Ephemeral tracing** (KNOWN, `events/listeners/tracing/first_time_trace_handler.py`): for unauthenticated users, events are pushed to an ephemeral endpoint and the response contains a one-time trace URL. Good UX for eval/demo flows.
- **`trust_env=False` on httpx.Client** (KNOWN, `plus_api.py:49`): ignores `HTTP_PROXY`/`HTTPS_PROXY`. This is unusual and problematic for corporate-proxy users. Do not replicate this — Lancelot's `TransportPool` should inherit the user's proxy env by default.
- **Tracing timeout: 30s per request** (KNOWN). Reasonable for span batch delivery; fail-safe wrapper avoids breaking the application on telemetry failure (KNOWN, `SafeOTLPSpanExporter` wraps `export` in a try/except).

---

## 15. Honest assessment — durable vs. depreciating

**Durable (worth copying, persist across 3+ model generations):**
- The 4-stage JSON-repair ladder (`json.loads → ast.literal_eval → json5 → json_repair`). This is a function of LLM output fragility, not model capability, and will remain relevant.
- `SequenceMatcher>0.85` fuzzy tool-name correction. Same rationale.
- Repeated-call short-circuit (identical `(tool_name, args)` → skip). A degenerate loop guard that is model-independent.
- Memory composite scoring formula (semantic 0.5 / recency 0.3 / importance 0.2 / 30-day half-life). Weighted retrieval is durable; the exact weights are a starting point, not a truth.
- Guardrail callable/string contract + 3-retry loop. The validate-and-retry abstraction is model-independent.
- Context summarization 5-section template. Task-continuation compression structure is durable.
- `extra="forbid"` for OpenAI structured output. A provider requirement that won't change until OpenAI changes their API.

**Depreciating (tactical, model-clock ~1-2yr):**
- The ReAct text parser (regex for "Thought:" / "Action:" / "Final Answer:") — legacy for models without function calling.
- The `OPENAI_BIGGER_MODELS` list with hardcoded model names. This list churns every 6-12 months.
- `gpt-4o-mini` as the default planner model — correct now, stale in 18 months.
- Any specific model-routing logic in `LLM.__new__` (the `provider_mapping` dict).

**Hype (not what CrewAI claims):**
- "Role/goal/backstory → persona" as a system design primitive: it is literally three string-interpolated fields injected into a system prompt. The creativity lives in the user's strings, not the framework.
- "Hierarchical process": one extra agent with two delegation tools, run through the same sequential loop. Not a hierarchy; not a supervisor/worker graph. The `consensual` TODO in `process.py` has sat unfilled for 3 years.
