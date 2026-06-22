# Lancelot Agent Layer — the **loop** brief

> Layer synthesizer: the `<200-line` async ReAct loop.
> Altitude: "just build agents, brutal" — smallest primitive that earns its keep, anti-overkill AND anti-underkill.
> Cardinal rule honored: runtime dep is `pydantic>=2.7` only; provider SDKs + sqlite-vec lazy-imported; the loop adds **zero** new runtime deps.
> Every claim cites the per-source mechanism note under `research/02_core` / `research/03_teardowns` / `research/04_byte_path`.

---

## 0. The one-sentence decision

**Build `lancelot.agent.run()` — a single, ~150-line model-driven recursive async loop** whose only job is: assemble request → call the model through the byte path → if tool calls, dispatch them (ordered, parallel-capable) and recurse → else synthesize and stop. The loop owns a **named `StopReason` enum**, a **multi-axis budget with a graceful grace-call**, **counter-based stuck detection**, and exactly one extension seam (a hook registry). Two FSM extensions ship **opt-in and off by default**: a **tool-rule solver** and a **mode-gate**. Everything transport (the actual HTTP call, retry, cost, cancel, stream, trace) is **reused verbatim** from `TransportPool` / `context` / `stream`. The loop builds **none** of that.

The corpus is unanimous: *the loop is 5–10 lines and is not where anyone competes* (`agentic_deep.md §1`: Claude Code's Rust loop is 88 lines incl. error handling; `langgraph.md §3.2`: a graph for a linear ReAct loop is "pure overhead"; `bad_guide_build_positioning.md §3`: "delete the harness so a smarter model's capability overhang is not capped by your plumbing"). So the loop file stays tiny and the *constants + the StopReason taxonomy + the budget contract* are the product.

---

## 1. The loop contract (the shape, grounded)

Every production loop in the corpus is the **same recursion**, differing only in budget surface and stop taxonomy. The convergent shape:

```
async def run(state, *, budget, hooks, tool_rules=None, mode_gate=None) -> Result:
    while True:
        # GATE A — budget (checked at the TOP of every cycle, before any model call)
        stop = budget.check(state)            # turns → cost_usd → tokens, in that priority
        if stop: return _finish(state, stop)  # StopReason.budget_*  (graceful, see §3)

        # GATE B — stuck detection (counter-based, NOT model self-report)
        if state.is_stuck():                  # §4 thresholds
            return _finish(state, StopReason.stuck)

        # GATE C — resume short-circuit: if the last message already has unанswered
        #          tool calls (crash/HITL resume), SKIP the model call, go straight to dispatch
        if _has_pending_tool_calls(state):
            assistant = state.last
        else:
            # MODEL PHASE — the ONE byte-path call
            assistant = await _call_model(state, hooks, tool_rules, mode_gate)
            state.append(assistant)           # APPEND-ONLY (KV-cache discipline, §6)

        # DISPATCH
        if assistant.finish_reason == "length":         # context truncated mid-call
            return _finish(state, StopReason.length)    # HARD — do not burn retries
        calls = assistant.tool_calls
        if not calls:                                    # model emitted content, no tool
            if _needs_synthesis(state):                  # two-stage synthesis, §5
                continue
            return _finish(state, StopReason.end_turn)

        # tool calls WIN over text this turn (discard preamble text) — pydantic_ai.md §3
        results = await _dispatch_tools(calls, hooks, tool_rules)   # ordered, parallel-capable §7
        state.extend(results)                            # one user message, one block per result
        if any(r.terminal for r in results):             # stop_after_tool / TerminalToolRule
            return _finish(state, StopReason.tool_rule)
        if any(r.needs_input for r in results):          # HITL pause
            return _finish(state, StopReason.hitl_pause) # serialize & return resumable state
        # loop (recurse)
```

This is `strands.md §1c` (Bedrock recursive `event_loop_cycle`), `codex.md §1` (`run_turn` — unbounded loop, `!needs_follow_up` OR `CancellationToken` terminates), `openai_agents.md §4` (`NextStep` discriminated union — *do not implement as a flat if-else chain*), `letta.md §3` (`_decide_continuation` V3 FSM), `agno.md §1` (loop lives in `Model.response()`, break-after-running-tools), and `vercel_ai_sdk.md §1` (`do…while` with `stopWhen`). Five+ independent implementations, one shape.

**Two design decisions the corpus forces:**

1. **The budget check is at the TOP of the cycle, before the model call** (`strands.md §1c`; `langgraph.md §2.7` `remaining_steps < 2 and has_tool_calls` guard). Checking after wastes the call you can't follow up on. The `< 2` (not `< 1`) threshold is load-bearing: a tool call needs ≥2 more steps (execute + synthesize), so stop *one step early* if a tool call would dead-end.

2. **Tool-calls-win-over-text** (`pydantic_ai.md §3`): when a response has BOTH text and tool calls (Anthropic preambles do this), execute the tools and discard the text this turn. Missing this confuses the loop into an unnecessary model retry.

---

## 2. The named `StopReason` enum (the marquee primitive — BUILD)

Every serious framework ships a **typed, machine-readable termination taxonomy**. The corpus is loud and consistent that this is non-negotiable for callers: `claude_code.md §2` (12-value `TerminalReason` + 5-value `ResultMessage.subtype`), `letta.md §3` (11 named stop reasons), `agno.md §6` (`RunPaused`/`RunCompleted`/`RunError` wire values), `vercel_ai_sdk.md §2.3` (7-value normalized `finishReason`), `agentic_deep.md §7` (`error_max_turns` / `error_max_budget_usd` / ...).

**Ship a single flat enum — the union of what callers actually branch on, deduped to the minimum:**

```python
class StopReason(str, Enum):
    end_turn          = "end_turn"           # model produced content, no tool call — normal
    budget_turns      = "budget_turns"       # hit max_turns
    budget_cost       = "budget_cost"        # hit max_cost_usd
    budget_tokens     = "budget_tokens"      # hit max_tokens
    length            = "length"             # finish_reason=length; context truncated mid-call
    stuck             = "stuck"              # stuck-detector fired (§4)
    tool_rule         = "tool_rule"          # a terminal tool ended the loop
    hitl_pause        = "hitl_pause"         # a tool requested user input/approval — resumable
    cancelled         = "cancelled"          # client disconnect / SIGTERM / CancellationToken
    refusal           = "refusal"            # model refused (provider refusal stop)
    error             = "error"              # unrecoverable framework/API error
```

Rationale for the exact set (anti-overkill): this is the **intersection** of what callers branch on across `claude_code.md §2`, `letta.md §3` (`schemas/letta_stop_reason.py`), `vercel_ai_sdk.md §2.3`. We drop the per-vendor leaves (`rapid_refill_breaker`, `image_error`, `insufficient_credits`, `guardrail_intervened`) — they collapse into `error` / `cancelled` / `end_turn`, or belong to a hook (a guardrail tripwire is a hook concern, §8). We keep `length` separate from `budget_tokens` because they need *different* handling: `length` means "the model's single response was cut" (a `max_tokens` config problem — `pydantic_ai.md §2` `check_incomplete_tool_call`: do NOT burn retries, tell the operator to raise `max_tokens`), while `budget_tokens` means "the agent's cumulative budget is spent."

**The `Result` carries the StopReason + the always-present cost** (`agentic_deep.md §7`: `total_cost_usd` present on ALL subtypes including errors). Lancelot gets this for free — `lancelot.context.cost_usd` already accumulates across every sub-call (`lancelot_model_io.md §2.4`).

**Normalize `finish_reason` to a 7-value internal enum** before the loop reasons over it (`vercel_ai_sdk.md §2.3`: `stop|length|content-filter|tool-calls|error|other|unknown`, raw preserved separately). This is the model-adapter layer's job (a sibling brief), but the loop *consumes* the normalized value, so the contract must be: the loop never sees a raw provider string.

---

## 3. Budget + the grace-call (BUILD — multi-axis, priority-ordered, graceful)

The corpus splits hard into two camps and the synthesis must pick correctly:

- **No hard turn cap** (`codex.md §1` "NO `max_iterations`"; `agno.md §1`/`§17.1` `while True`, only `tool_call_limit`). Honest flag from `agno.md §17.1`: *"A stuck model + no tool cap = infinite loop. Always add a hard `max_turns` ceiling."*
- **Hard turn cap** (`openai_agents.md §4` `DEFAULT_MAX_TURNS=10`; `pydantic_ai.md §13` request-limit `50`; `vercel_ai_sdk.md §1.2` agent default `stepCountIs(20)`; `dspy.md` `ReAct.max_iters=20`; `autonomous_coding.md §2` `max_iteration_per_run=500`).

**Decision: ship a multi-axis budget, priority-ordered, with a hard turn ceiling as the backstop AND a graceful grace-call.** This is the Strands design (`strands.md §1b`) — the single cleanest budget surface in the corpus — combined with the LangGraph graceful-exit primitive (`langgraph.md §2.1`).

```python
@dataclass
class Budget:
    max_turns: int = 20            # the backstop. NOT None (Agno's lesson). DSPy/Vercel-Agent converge here.
    max_cost_usd: float | None = None   # reads lancelot.context.cost_usd — already accumulated
    max_tokens: int | None = None       # reads tokens_in+tokens_out ContextVars
    # priority on simultaneous trip: turns → cost → tokens   (strands.md §1b)
```

**Constant choice — `max_turns = 20`:** the modal default across the independent agent-*class* implementations (`vercel_ai_sdk.md §1.2`, `dspy.md react.py:17`). OpenAI's `10` (`openai_agents.md §4`) is for short triage workflows; `500` (`autonomous_coding.md §2`) is a *per-run* coding-agent cap. `20` is the right "just build agents" default for a general loop — high enough not to clip real multi-tool work, low enough to fail fast on a spin. Env override `LANCELOT_AGENT_MAX_TURNS` per the Lancelot knob convention (`strands.md §1a` makes the same recommendation for its constants).

**The grace-call (the non-obvious primitive — COPY).** When the budget is about to trip, do NOT raise `RecursionError` and do NOT silently stop. Make ONE final model call with a budget-aware nudge so the model wraps up cleanly (`langgraph.md §2.1`/§2.7: `remaining_steps` is visible to the node; graceful exit string `"Sorry, need more steps to process this request."`; `claude_code.md §7`: Anthropic `output_config.task_budget` *"the model is made aware of its remaining token budget so it can pace tool use and wrap up before the limit"*). The mechanism:

- If `budget.check` returns a trip but `remaining == 1` (or the model still has tool calls outstanding and `remaining < 2`), inject a `[system] You are running low on budget; produce your best final answer now.` user-role message and force one no-tool synthesis call, then stop with the real `StopReason.budget_*`.
- For Anthropic models specifically, *also* set `task_budget` on the request so the model self-paces for free (`claude_code.md §7`/§11, `claude_agent_sdk.md` — *zero* extra implementation, one JSON field + one beta header on the `TransportPool.request` the loop already makes). This is defense-in-depth: model self-paces first (free, trained), harness grace-call second, hard turn-cap last (`claude_code.md §7`: the three form defense-in-depth).

**Per-step cost isolation is BUILD-ON-TOP, not new state** (`lancelot_model_io.md §3.1`): snapshot `cost_usd.get()` before each step, subtract after. No new ContextVar. The cumulative axes (`max_cost_usd`, `max_tokens`) read the existing ContextVars directly — the byte path already accumulates them (`lancelot_model_io.md §2.2`/§2.4).

---

## 4. Stuck detection (BUILD — counter-based, exact thresholds)

The single best mechanism-level find for the loop is OpenHands' stuck detector (`autonomous_coding.md §1`), the only open-source readable thresholds in the corpus. **Copy the constants verbatim** — they are calibrated to not false-positive on legitimate repeated work:

```python
class StuckThresholds:
    action_observation: int = 4   # same (action, observation) pair repeated N times
    action_error:       int = 3   # same action → error repeated N times
    monologue:          int = 3   # N consecutive assistant messages with NO tool call
    alternating:        int = 6   # A↔B↔A↔B… tool-call alternation N times
```

**Decision: build counter-based, NOT model-self-report.** The corpus is explicit: `autonomous_coding.md §1` — *"Jules has no numeric counter — the LLM is supposed to self-detect... This is model-dependent and less reliable... Copy the counter approach, not the self-detect approach."* Devin's 13-trigger `think` tool (`devin_warp.md §3`) is the self-report variant and is flagged as a *prompting-only, depreciating* primitive (`devin_warp.md §3` hype flag: "works until models natively do this reflection"). The counter is durable; the prompt list churns on the model clock.

`stuck` is a **terminal** state, not a retry state (`autonomous_coding.md §1`: STUCK requires user intervention; `letta.md`-style: surface it as a typed reason). The loop stops with `StopReason.stuck` and returns the trajectory so the caller can decide.

Anti-ping-pong for multi-agent handoffs (`strands.md §8`: `repetitive_handoff_detection_window`) is the *same idea at the orchestration layer* — out of scope for the single-agent loop, noted for the orchestration brief.

---

## 5. Two-stage synthesis (BUILD — force a structured/final answer)

The recurring "model ended its turn without doing the thing we need" problem, with a convergent two-stage fix:

**Stage 1 — let the model end naturally.** If it produced content and no tool call and we don't need structured output, that's `end_turn`. Done.

**Stage 2 — force it if a contract is unmet.** Two sub-cases, both in the corpus:

(a) **Structured-output not produced** (`strands.md §7`, `pydantic_ai.md §7`, `agno.md §7`, `manus.md §4`): if the caller asked for a typed result and the model ended its turn without it, append a one-liner and force one more call.
- The exact nudge (`strands.md §7`, verbatim): `"You must format the previous response as structured output."`
- Force via `tool_choice={"any":{}}` not a named-tool force (`strands.md §7`: more portable — works on models without named-tool forcing).
- If forcing was *already attempted* and it STILL didn't comply → `StopReason.error` (don't loop forever).
- The `final_result` output-tool trick (`pydantic_ai.md §7`/§13): structured output IS a tool named `final_result` desc `'The final response which ends this conversation'`; the model "returns" by calling it. Lancelot reuses `@app.tool` + auto `output_schema` (`lancelot_serve_tools.md §2`) to define this tool — no new schema machinery.

(b) **Bare-expression / forgot-to-finish repair** for weaker models (`smolagents.md §7` `fix_final_answer_code`): cheap post-processor that wraps a final bare value into the finish call. Low cost, meaningful win on cheap models. Optional.

**Honest-bounded:** post-task extraction as a *separate* LLM call (`manus.md §4`, `agno.md §7` `parser_model`) — extract the typed result from the final transcript in one extra call, with zero-value fallback on failure so consumers never handle null (`manus.md §4`). This is the *cleanest* structured-output design (keeps the loop uncontaminated by formatting) but it costs an extra call. Ship the in-loop force (a) as the default; expose post-task extraction as an opt-in `extract_schema=` for callers who want loop purity. Do NOT ship `output_model` / `followups` (`agno.md §7`) — that's the 6-calls-per-turn cost trap (`agno.md §7` worst case) the cardinal rule rejects.

---

## 6. KV-cache discipline baked into the loop's state contract (REUSE byte path + enforce)

This is the **#1 free cost lever** and the loop is where it's enforced or lost (`agentic_deep.md §2.1`: input:output ratio is 100:1, cached vs uncached is a 10× cost difference; `manus.md §1`/§5: the six rules; `bad_guide_build_positioning.md §4`: "10x cost impact that most teams never make").

The loop's contract enforces three rules; the byte path already provides the mechanism:

1. **Append-only event stream** (`manus.md §1`/§5, `agentic_deep.md §2.1` rule 2): the loop NEVER mutates an emitted message; it only appends. This is a hard invariant of `state.append`/`state.extend` above. A 100-turn task gets a near-full cache hit every turn because only one observation was appended (`manus.md §5`).
2. **Stable serialization** → `lancelot.canonical.dumps_canonical` for every tool-schema and JSON-into-prompt (`lancelot_model_io.md §4`, `manus.md §2`: *"dumps_canonical is the implementation primitive for Rule 2 (mask-don't-remove)"*). REUSE — already built.
3. **Static/dynamic prompt split** (`claude_code.md §1`, `agentic_deep.md §2.5`, `autonomous_coding.md §5`): a sentinel marks where prompt stability ends. Ship `LANCELOT_PROMPT_DYNAMIC_BOUNDARY = "__LANCELOT_PROMPT_DYNAMIC_BOUNDARY__"` (`claude_code.md §11` recommends exactly this) — the model-adapter layer applies the cache_control split; the *loop* just keeps the static prefix (system + tools) frozen across turns and appends dynamic context after the boundary.

**Mask-don't-remove tools** (`manus.md §2`, `agentic_deep.md §2.1` rule 5): the loop must keep the tool-schema block byte-stable within a run. If a tool must be made unavailable, mask it (filter at decode / tool-choice level), never delete it from the schema list mid-run — deleting busts every cache entry after the mutation point. This is a loop invariant, enforced by the tool-rule solver's *allowed-set* computation (§9) operating on a stable superset.

The loop does NOT build a compaction engine — that is the **context brief's** job. But the loop's append-only + boundary contract is the precondition that makes compaction (observation-masking, the 95% trigger) implementable later (`codex.md §2`, `agentic_deep.md §2.2`/§2.3). The loop exposes the hook (`PreCompact`-equivalent, §8) and stops there.

---

## 7. Tool dispatch within a turn (BUILD — ordered, parallel-capable)

When the model emits N tool calls in one response, the loop dispatches them. The convergent correct shape:

- **Default: parallel execution, deterministic history ordering** (`pydantic_ai.md §5`, `codex.md §5`). Results are appended in **call-index order regardless of completion order** (`pydantic_ai.md §5`: "Parallel execution, deterministic transcript. Missing it makes replays non-deterministic"; `codex.md §5`: `FuturesOrdered` NOT `FuturesUnordered`).
- **Implement with `asyncio` `gather`-with-index** (not a thread pool — `agno.md §17.3`: "ThreadPoolExecutor for background LLM calls adds GIL-acquire overhead; the correct pattern is async"). For Lancelot this is plain `asyncio.gather` over `app.call_tool(...)` coroutines (`lancelot_serve_tools.md §2`: `call_tool` is already the validated invocation substrate), results re-sorted by index.
- **Sequential override**: if any called tool is flagged `sequential=True`, force the whole batch sequential (`pydantic_ai.md §5`). And parallel is **disabled whenever any tool-rule is attached** (`letta.md §4`: "parallel tool calls disabled because tool rules attached") — ordering constraints and parallelism are incompatible.
- **Truncate-at-finish** (`autonomous_coding.md §4` `_truncate_at_finish`): if the model emits a terminal/finish tool mid-batch, truncate the batch at that call and warn-discard the rest. Prevents "finish AND keep acting" in one turn.
- **One-tool-per-turn is a valid alternative mode** (`manus.md §1`: "Choose only one tool call per iteration" — maximal KV-cache determinism). Do NOT make it the default (loses the multi-tool throughput win) but the dispatch path naturally supports N=1.

**Manage the byte-path concurrency cap:** N parallel tool calls that each hit the LLM share the `TransportPool` connection cap (`lancelot_model_io.md §1.6`/§7.e: `max_connections=100` silently caps fanout). For *outbound LLM* fan-out within the agent (e.g. a "review with another model" tool), reuse `TransportPool.fanout(first_n, deadline, cancel_laggards, concurrency)` (`lancelot_model_io.md §1.7`) — the byte path already has the ordered-completion + partial-failure shape.

**Tool-result hygiene before context (REUSE + enforce):**
- Truncate large tool outputs (`autonomous_coding.md §7`: Amp Bash 50k chars from the END; OpenHands condenser; `claude_code.md §4` `maxResultSizeChars` spill-to-storage). The loop enforces a hard cap; `lancelot.mcp.mount` already WARNs at 4096 bytes (`lancelot_serve_tools.md §3`) — the loop upgrades WARN→truncate for context-bound results.
- **Scrub exception text in tool-error returns** (`lancelot_serve_tools.md` anti-pattern #3 / §10: `mcp.py` emits `str(exc)` which leaks internals into the context window). The loop scrubs before appending a tool error.
- **Error-as-observation, not crash** (`smolagents.md §4`, `agentic_deep.md §14` item 7): a bad tool call / model error becomes the next turn's observation (the loop continues); a framework/API bug exits. Two-tier: `ModelError → retry-as-observation`, `FrameworkError → StopReason.error`. The retry coach line is copyable verbatim (`smolagents.md §4`: *"Now let's retry: take care not to repeat previous errors! If you have retried several times, try a completely different approach."*) and the structured Pydantic `ValidationError.errors()` IS the corrective prompt for tool-arg failures (`pydantic_ai.md §2`/§16: "show the model its own mistakes" — the structured error dict is sent back as the retry payload, not a vague "try again").

**Two retry budgets, separate** (`pydantic_ai.md §2`: per-tool `default_max_retries=1` and output-validation retries are *different counters* — "conflating them means a flaky tool eats your output retry budget"). Build both as small ints on the loop config. This is loop-level retry (re-issue a *new* model call), which is correct — `TransportPool` never retries the POST itself (`lancelot_model_io.md §1.1`/§7.c: "the agent loop MUST NOT wrap the LLM call in its own HTTP retry; retries at the loop level are correct").

---

## 8. The hook registry (BUILD — the ONE extension seam)

The corpus converges on a **single typed hook registry** as the clean spine that keeps the loop tiny while everything else (metrics, safety, memory, session persistence) plugs in *without touching the loop* (`strands.md §4`: the hook registry is "the extension spine"; `openai_agents.md §7`: 12 lifecycle callbacks; `pydantic_ai.md §15`: "implement `wrap_model_request` (the critical one), defer the rest"; `claude_agent_sdk.md §2`: 29-event system; `agentic_deep.md §8`: 4 handler types).

**Decision: ship a minimal hook set — the intersection that earns its keep — with mutable-field semantics.** Do NOT ship 29 events (`claude_agent_sdk.md §2`) or a 6-stage × 4-hook taxonomy (`pydantic_ai.md §15` explicitly says defer it). The minimal useful set (`strands.md §4` mutable fields + `openai_agents.md §7` timing + `claude_code.md §6` quality-gate):

```python
# fired in loop order; a hook may mutate the marked field or signal stop
before_model(ctx)                       # inspect/rewrite request; the prompt-audit hook
after_model(ctx, response)              # may set retry  (strands AfterModelCallEvent)
before_tool(ctx, call) -> Decision      # swap tool, rewrite input, or CANCEL  (strands BeforeToolCallEvent)
after_tool(ctx, call, result)           # rewrite result before model sees it  (claude_code PostToolUse updatedToolOutput)
on_finish(ctx, stop_reason)             # quality-gate: may REJECT a finish and reinject work  (claude_code Stop hook)
```

The load-bearing mutable powers (copy these exactly):
- `before_tool` can **cancel** the call (returns a synthetic tool result to the model) — `strands.md §4` `cancel_tool`: "the clean way to implement tool allowlists, sandboxing, rate limits, dry-run — without touching the loop." This is where the **safety/permission gate** plugs in (a sibling brief; the loop just provides the seam).
- `before_tool` can **rewrite input** and `after_tool` can **rewrite output** — `claude_code.md §2` `updatedInput`/`updatedToolOutput` (sanitization, truncation, redaction).
- `on_finish` can **reject the finish and reinject** — `claude_code.md §6` `Stop` hook with `decision:"block"` (quality-gate: "tests failed, keep going"); `codex.md §1` stop-hooks return `continuation_fragments`; `autonomous_coding.md §2` Gate-1 FINISHED→reactivate. This is the *async-rewake* / slow-validator pattern (`claude_code.md §6` `asyncRewake`: decouples validator latency from agent latency).

**Wire Lancelot's byte path through the hooks, not the loop body** (`strands.md §4`/§11): `after_model` fires `lancelot.upstream_metrics.record_upstream` (`lancelot_model_io.md §2.5`) and cost accumulation; `lancelot.telemetry.set_sink` (`lancelot_serve_tools.md §5`) gets a `gen_ai.span` event per loop iteration. New agent metrics follow the existing lazy-OTel pattern (`lancelot_serve_tools.md §4`: `lancelot.agent.loop_iterations`, `lancelot.agent.tool_calls` cardinality-bounded). The hook registry is the integration point so the loop stays `<200` lines.

---

## 9. Tool-rule solver (BUILD — OPT-IN, off by default)

The task names this as opt-in, and the corpus agrees it's a first-class-but-not-default primitive. Letta's `ToolRulesSolver` (`letta.md §4`) is the canonical mechanism — a per-step FSM over tool constraints.

**Decision: ship the minimal useful subset — `InitToolRule` + `TerminalToolRule` — opt-in via a `tool_rules=` kwarg, default `None`.** `letta.md §4`/§297 is explicit: *"InitToolRule + TerminalToolRule are the minimal useful subset."* The mechanism:

```python
# per step, intersect available tools with what the rules allow:
def allowed_tools(history, available, rules):
    if not history and rules.init:           # first step: only Init tools
        return [r.tool for r in rules.init]
    allowed = available
    for r in rules.applicable(history):       # ContinueAfter / Child / MaxCountPerStep
        allowed = intersect(allowed, r.allows())
    if not allowed: raise ValueError("No valid tools under tool rules")
    return allowed
```

- `InitToolRule(tool, args=...)` — must call X first; can pin args at step 0 (`letta.md §4`: arg-prefill is "a neat bonus for forced-tool patterns").
- `TerminalToolRule(tool)` — calling X ends the loop with `StopReason.tool_rule`.
- (defer `ContinueToolRule`, `ChildToolRule`, `MaxCountPerStepToolRule`, `RequiresApprovalToolRule` until a user needs them — `letta.md §4` lists all five but the minimal pair covers structured interviews / multi-step forms / approval workflows).

**Coach-message injection for forced continuation (COPY verbatim — `letta.md §3`):** when a rule forces the loop to continue, tell the model *why* via a non-user system message so it can reason about the constraint: `"[This is an automated system message hidden from the user] Continuing: child tool rule."` This is the same mechanism as the budget grace-nudge (§3) and the synthesis nudge (§5) — a typed `NON_USER_MSG_PREFIX` coach injection.

**Invariant:** any tool-rule attached → parallel tool dispatch disabled (`letta.md §4`/§101). Ordering constraints and parallelism are incompatible.

**Why opt-in, not default:** the unconstrained model-driven loop (continue-after-tool, stop-on-no-tool) is the right default for "just build agents" (`letta.md §3`: V3 *removed* heartbeats, loop continues by default). Tool rules are for *constrained* agents (forms, approval flows). Defaulting them on would be overkill for the common case.

---

## 10. Mode-gate FSM (BUILD — OPT-IN, off by default)

The plan/execute split, also opt-in per the task. The corpus has two clean variants:

- **Plan-then-execute hard gate** (`devin_warp.md §5`: Devin enters planning mode by default, "never enters standard mode on its own — the user must toggle it"; `codex.md §10`: 3-phase Plan protocol, *switched by developer messages only*).
- **Planning-interval** (`smolagents.md §6`: `planning_interval: int | None = None` — re-plan on step 1 and every Nth step; `None` = never plan).

**Decision: ship `planning_interval: int | None = None` (off by default) as the simplest mode primitive, and expose a `mode_gate=` seam for the hard plan/execute gate.** `smolagents.md §6`/§238 makes the ergonomic call: *"Planning-interval as an optional `int | None` knob (not baked in) is the right ergonomic. `None` = ReAct-only; any integer = periodic planning."* The hard gate (plan→approve→execute) rides the same `on_finish` hook + HITL-pause `StopReason` already built (§3, §8) — no new loop machinery, just a `mode` field on state and a gate the caller drives.

**Critical security invariant (COPY — `codex.md §10` anti-pattern):** mode is switched by **developer-role messages only, never user-role**. *"Allowing user messages to switch mode opens a prompt-injection surface."* The mode-gate must reject user-role mode-switch attempts.

**Why opt-in:** a bare ReAct loop needs no mode FSM (`smolagents.md §6` default `None`). Planning earns its keep for long-horizon / high-stakes tasks; forcing it on every agent is the overkill the lens warns against ("not 500-node graphs").

---

## 11. REUSE from the byte path (build NONE of this)

The loop is thin precisely because the transport substrate is done. Verbatim reuse (cited to the byte-path inventory):

| Loop needs | Byte-path primitive | Cite |
|---|---|---|
| The model call (unary) | `TransportPool.request()` — retry, Retry-After, budget cap, **POST never retried**, cancel, metrics, Idempotency-Key, traceparent | `lancelot_model_io.md §1.1`/§1.5/§7.c |
| Streaming the model | `TransportPool.stream(track_usage=True)` + client-disconnect→upstream cancel | `lancelot_model_io.md §1.2`/§1.3 |
| Streaming to the client | `@app.stream` R3 pump + `Event`/`sse_adapter`/`_clean_field` + `[DONE]` + `error_frame` + `max_stream_lifetime`/`max_stream_silence`/`defer_status` | `lancelot_serve_tools.md §1` |
| TTFT (the AI-UX metric) | `lancelot.http.stream.ttft` on first chunk | `lancelot_serve_tools.md §1`/§4 |
| Token/cost accounting + budget reads | `cost_usd`/`tokens_in`/`tokens_out` ContextVars (accumulate automatically) | `lancelot_model_io.md §2.2`/§2.4/§3 |
| Error branch selector | `classify_status` → `ok/quota/timeout/permanent/transient/upstream_5xx` (422→permanent = malformed tool call) | `lancelot_model_io.md §2.3`/§7.d |
| Tool registration + validated invocation | `@app.tool` → `ToolSpec` (auto JSON schema + `output_schema`), `app.tool_schemas()`, `app.call_tool(name, /, **kwargs)` | `lancelot_serve_tools.md §2` |
| The `final_result` output tool | `@app.tool` + auto `output_schema` | `lancelot_serve_tools.md §2`, `pydantic_ai.md §7` |
| Cache-stable serialization | `lancelot.canonical.dumps_canonical` | `lancelot_model_io.md §4` |
| Within-turn / cross-model fan-out | `TransportPool.fanout(first_n, deadline, cancel_laggards, concurrency)` — ordered completion, partial-failure shape | `lancelot_model_io.md §1.7` |
| Sub-agent context propagation | `TransportPool.spawn_child(parent, session_affinity)` — snapshots traceparent/request_id/agent_id | `lancelot_model_io.md §1.8` |
| Cancel-on-disconnect | `App(cancel_on_disconnect=True)` per-route | `lancelot_serve_tools.md §11` |
| Supervised background daemon (heartbeat / metric flush) | `App.lifetime_task` | `lancelot_serve_tools.md §11` |
| Provider credentials | `lancelot.secrets.get("OPENAI_API_KEY")` (lazy SDK, SIGHUP rotate) | `lancelot_serve_tools.md §7` |
| Tool fetch-URL safety | `lancelot.net.assert_safe_url` (SSRF, CGNAT, IPv4-mapped-IPv6) | `lancelot_serve_tools.md §6` |
| Telemetry sink (Langfuse/Phoenix) | `lancelot.telemetry.set_sink` + `Event` | `lancelot_serve_tools.md §5` |
| Inbound admission control | `RateLimit` + `TenantConcurrencyLimiter` | `lancelot_serve_tools.md §8`/§9 |
| Multi-user batching on an inference endpoint | `@app.batch(size_fn, max_size, key=...)` | `lancelot_serve_tools.md §11` |
| `traceparent` distributed trace | auto-injected by `TransportPool` | `lancelot_model_io.md §3` |
| Anthropic self-pacing | set `task_budget` on the `TransportPool.request` (one JSON field + beta header) | `claude_code.md §7`/§11 |

---

## 12. REFUSE (off-thesis / depreciating / overkill — do NOT build in the loop)

- **A graph DSL / `StateGraph.add_node/add_edge/compile` for a linear ReAct loop** — the graph tax (`langgraph.md §1`/§3.2: "3 phases of infrastructure per turn of a `while` loop"; "pure overhead" for the common case). The loop is a named `while`, not a Pregel BSP runtime. (Pydantic-AI's typed-return-annotation graph engine — `pydantic_ai.md §1` — is elegant but is the *durable-checkpoint brief's* call, not the loop's; and `pydantic_ai.md §15` forbids taking `pydantic-graph` as a dep.) Optional future surface, never the default.
- **A `while True` with NO turn ceiling** (`agno.md §1`/§17.1, `codex.md §1`) — anti-underkill; a stuck model spins to OOM. Keep `max_turns=20` backstop + stuck detector.
- **Per-chunk validation / per-chunk re-bridging on the stream** — Lancelot Tier-D (`lancelot_serve_tools.md` anti-pattern #1: the 16–26× cliff; `claude_code.md §10` anti-pattern #2; `codex.md` anti-pattern). Validate tool schemas at registration/dispatch, never per chunk.
- **Four hand-synchronized loop copies (sync/async × buffered/stream)** (`agno.md §17.2`: a maintenance + bug surface — one divergence already shipped). ONE async loop; streaming is the byte-path's R3 concern (`lancelot_serve_tools.md §1`).
- **A V8 / code-execution sandbox in the loop** (`codex.md §11` Code Mode; `smolagents.md §3` AST executor) — off the byte-path thesis; `FuturesOrdered`/`asyncio.gather` achieves within-turn parallelism without a language sandbox (`codex.md §11`: "not a copy target"). The AST-executor *bounds* (10M ops / 1M while / 30s — `smolagents.md §3`) are the reference *if* a user ever ships code-action, but the loop doesn't build it.
- **Same-model retry as the primary recovery for quota/provider errors** (`devin_warp.md §1`/§7: "often produced repeat failures" → cross-model failover). Cross-provider failover is a *model-adapter / ProviderProfile* concern, not the loop's; the loop reuses `TransportPool` transient-retry and surfaces `StopReason.error` to let the adapter decide.
- **Multi-agent shared mutable context** (`devin_warp.md §7`, `bad_guide_build_positioning.md §10`: Cognition's "don't build multi-agents" — "parallel agents make conflicting implicit decisions that produce broken output"). Single-trace context sharing via `spawn_child` is the default; fan-out (plan/execute) is fine, shared peer context is not. Orchestration brief's territory.
- **Model self-report as the stuck signal** (`autonomous_coding.md §1`, `devin_warp.md §3`) — depreciating prompting hack; use the counter.
- **A subprocess-delegated loop** (`claude_agent_sdk.md` positioning: single-vendor CLI lock-in). Lancelot owns the loop in-process.
- **Shipping a fat StopReason enum with per-vendor leaves** (`claude_code.md §2` 12 + 5 values; `letta.md §3` 11) — collapse to the 11-value intersection (§2). Overkill to surface `rapid_refill_breaker`/`image_error` as distinct caller-facing reasons.
- **Naive keyword prompt-injection guardrail** (`agno.md §12`/§17.4: easily bypassed) — that's the safety brief's call (classifier), and it rides the `before_model`/`before_tool` hook, not the loop.
- **`output_model` + `followups` + agentic-memory-per-turn** as loop defaults (`agno.md §7`: 6+ LLM calls per user turn) — the cardinal-rule cost trap.

---

## 13. Recommended API sketch (the shape users touch)

```python
import lancelot
from lancelot.agent import Agent, StopReason, Budget, hooks

app = lancelot.App()

@app.tool(read_only=True)
def search(query: str) -> list[str]:
    """Search the corpus. `query`: the search string."""
    ...

# the agent rides the existing TransportPool; no new transport config
agent = Agent(
    pool=app.transport_pool(model="claude-sonnet-...", base_url=...),  # REUSE TransportPool
    tools=app,                       # REUSE app.tool_schemas() + app.call_tool()
    system="You are a research assistant.",   # static prefix (frozen, 1h-cacheable)
    budget=Budget(max_turns=20, max_cost_usd=0.50),   # §3 — 20 is the default
    # opt-in, both default off:
    tool_rules=None,                 # e.g. [InitToolRule("plan"), TerminalToolRule("answer")]  §9
    planning_interval=None,          # e.g. 3 → re-plan every 3 steps  §10
)

agent.on(hooks.before_tool, lambda ctx, call: permission_gate(ctx, call))  # §8 safety seam
agent.on(hooks.after_model, lambda ctx, r: None)  # metrics auto-wired via TransportPool

# unary
result = await agent.run("Find the three best papers on X.")
assert result.stop_reason is StopReason.end_turn
print(result.output, result.cost_usd, result.turns)   # cost_usd from context ContextVar

# streaming to the client — REUSE @app.stream + R3 pump + TTFT
@app.stream("/agent", sse=True)
async def stream_agent(req):
    async for event in agent.stream(req.query):   # yields Event('delta'|'tool'|'done', {...})
        yield event
```

**Default constants summary (the load-test/field-calibrated numbers — opinionated, env-overridable):**

| Constant | Default | Env | Source of the number |
|---|---|---|---|
| `Budget.max_turns` | `20` | `LANCELOT_AGENT_MAX_TURNS` | `vercel_ai_sdk.md §1.2`, `dspy.md react.py:17` |
| `Budget.max_cost_usd` | `None` | — | reads `lancelot.context.cost_usd` |
| `Budget.max_tokens` | `None` | — | reads `tokens_in+tokens_out` |
| budget trip priority | `turns → cost → tokens` | — | `strands.md §1b` |
| stuck `action_observation` | `4` | — | `autonomous_coding.md §1` (OpenHands, calibrated) |
| stuck `action_error` | `3` | — | `autonomous_coding.md §1` |
| stuck `monologue` | `3` | — | `autonomous_coding.md §1` |
| stuck `alternating` | `6` | — | `autonomous_coding.md §1` |
| per-tool retries | `1` | — | `pydantic_ai.md §2`/§13 |
| output-validation retries | `1` | — | `pydantic_ai.md §2` (separate counter) |
| structured-output force nudge | `"You must format the previous response as structured output."` | — | `strands.md §7` (verbatim) |
| structured-output force | `tool_choice={"any":{}}` | — | `strands.md §7` (portable) |
| budget grace nudge threshold | `remaining < 2 and has_tool_calls` | — | `langgraph.md §2.7` |
| tool-result truncation cap | `~50_000` chars (from END) | `LANCELOT_AGENT_TOOL_RESULT_MAX` | `autonomous_coding.md §7` (Amp) |
| `planning_interval` | `None` (off) | — | `smolagents.md §6` |
| `tool_rules` | `None` (off) | — | `letta.md §3` (V3 default) |
| prompt boundary sentinel | `"__LANCELOT_PROMPT_DYNAMIC_BOUNDARY__"` | — | `claude_code.md §11` |
| coach-message prefix | `"[This is an automated system message hidden from the user] "` | — | `letta.md §3` (verbatim) |

---

## 14. Open questions (flagged, not blocking the loop build)

1. **Conversation/session identity across HTTP requests** — `conversation_id` is missing (`lancelot_model_io.md §3.1`: no cross-request store; `pydantic_ai.md §12` `resolve_conversation_id` rule + UUID7; `openai_agents.md §6` `Session` protocol with `pop_item` undo). The loop needs a `conversation_id` to thread multi-turn runs and pin the prompt-cache key (`autonomous_coding.md §5`). **Decide with the memory/session brief**: the loop should *accept* a `conversation_id` and a pluggable `Session` (4-method protocol — `openai_agents.md §6`), not own the store.
2. **HITL pause/resume durability** — the loop produces `StopReason.hitl_pause` + resumable state, but *where* it serializes (`openai_agents.md §5` `RunState.to_json()` schema-versioned; `pydantic_ai.md §10` snapshot-before-raise; `langgraph.md §2.3` `interrupt(value)`/`Command(resume=...)`) is the **durable-seam brief's** call. The loop's contract: emit a JSON-serializable resumable state + skip the model call on resume when tool results are already pending (§1 Gate C — `strands.md §1c` `_has_tool_use_in_latest_message`).
3. **Compaction trigger ownership** — the loop fires a `PreCompact`-equivalent hook and keeps the append-only contract, but the 95%-window trigger / observation-masking / `ToolCallResultSummary` (`codex.md §2`, `agentic_deep.md §2.2`/§2.3, `devin_warp.md §2`) belong to the **context brief**. Confirm the seam (a `before_model` hook that may return a shadowed `llm_input_messages` view — `langgraph.md §2.6` `pre_model_hook` — without mutating history).
4. **`Result` streaming-event vocabulary** — settle the typed event names the agent yields (`agno.md §6` `RunStarted/ToolCallStarted/...` wire values; `strands.md §3` 31-class typed events). Pick a *small* set (`delta`, `tool_call`, `tool_result`, `done`, `error`) so SSE clients key off type — but align with the streaming brief so it rides `sse_adapter` byte-identically (`lancelot_serve_tools.md §1` invariant).
5. **Debounce on streamed deltas** — `pydantic_ai.md §9` `debounce_by=0.1` (100ms) materially reduces overhead. `pydantic_ai.md §15` says expose it as a parameter, not hardcode (Lancelot already has `LANCELOT_STREAM_BUFFER`). Decide whether the agent stream surfaces a `debounce=` knob or relies on the existing buffer.
