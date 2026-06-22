# Autonomous coding agents — mechanism-level distillation

> Sources: OPENHANDS.md · REPLIT_AGENT.md · JULES.md · FACTORY_AI.md · SOURCEGRAPH_AMP.md
> Date: 2026-06-22
> Focus: stuck detection, git-worktree isolation, autonomous loop discipline, reliability rails.
> Lens: what does Lancelot's byte path already provide? What is genuinely missing?

---

## 1. Stuck detection — exact thresholds

OpenHands is the only open-source agent in this batch with readable stuck-detector source. The constants are verbatim from `re_oss_work/sdk/openhands-sdk/openhands/sdk/conversation/stuck_detector.py` and `conversation/types.py:48–72`.

```python
class StuckDetectionThresholds:
    action_observation: int = 4   # same (action, observation) pair repeated N times
    action_error:       int = 3   # same action → AgentErrorEvent repeated N times
    monologue:          int = 3   # N consecutive agent MessageEvents with no action
    alternating_pattern: int = 6  # A↔B↔A↔B… alternation N times
```

When any heuristic fires, `state.execution_status = STUCK` and the run loop breaks immediately. The state machine has an explicit `STUCK` terminal state (one of 8: `IDLE / RUNNING / PAUSED / WAITING_FOR_CONFIRMATION / FINISHED / ERROR / STUCK / DELETING`). `STUCK` is NOT in the "retry" category — it requires explicit user intervention to unstick.

**Why the numbers matter:** 4 / 3 / 3 / 6 are not arbitrary; each was calibrated to avoid false-positives on legitimate repeated work (a build that loops 3 times before passing is action_observation=3, just under the threshold). For Lancelot, copying these exact thresholds as the default is safe and avoids bikeshedding.

**Jules (KNOWN verbatim, `jules.md:117–120`)** encodes stuck detection as a user-ask trigger instead:
> "You should ask for help using `request_user_input` in exactly three situations: (1) request is ambiguous, (2) tried multiple approaches and still stuck, (3) decision would significantly alter scope."

Jules has no numeric counter — the LLM is supposed to self-detect being stuck and call `request_user_input`. This is model-dependent and less reliable than OpenHands' counter-based approach. **Copy the counter approach, not the self-detect approach.**

---

## 2. The run loop FSM — exact shape

OpenHands `LocalConversation.run()` (`local_conversation.py:746–890`) is the production-verified loop. Annotated structure:

```python
# Gate 1: status check (non-blocking — FIFO mutex acquired per iteration)
if state.execution_status in (PAUSED, STUCK): break
if state.execution_status == FINISHED:
    # Stop hook gate — agent finished but a hook can reinject work
    should_stop, feedback = hook_processor.run_stop(reason="agent_finished")
    if not should_stop:
        on_event(MessageEvent(source="environment", ..., content="[Stop hook feedback] " + feedback))
        state.execution_status = RUNNING
        continue
    break

# Gate 2: stuck detector
if stuck_detector.is_stuck():
    state.execution_status = STUCK
    continue   # loop around to Gate 1, which breaks

# Gate 3: confirmation flow (WAITING_FOR_CONFIRMATION → RUNNING when user confirmed)
if state.execution_status == WAITING_FOR_CONFIRMATION:
    state.execution_status = RUNNING  # user approved

# Gate 4: the step
agent.step(state, on_event, on_token)
iteration += 1

# Gate 5: max iterations
if iteration >= max_iteration_per_run:
    if state.execution_status == FINISHED: break  # preserve FINISHED
    state.execution_status = ERROR
    on_event(ConversationErrorEvent(code="MaxIterationsReached", ...))
    break
```

Key design choices worth copying:

**FIFO mutex (not a simple Lock):** `state.py:539` uses a `FIFOLock`. Under heavy `send_message` traffic (concurrent WebSocket clients), a plain `asyncio.Lock` can starve callers. FIFO ordering prevents starvation — a natural fit for Lancelot's async model given the `asyncio.Semaphore` in `lancelot.tenant.TenantConcurrencyLimiter`.

**FINISHED is intentionally not terminal at the bottom of the loop.** This allows a concurrent `send_message` to arrive between `state.execution_status = FINISHED` and the next gate check, get the lock first via FIFO, and re-activate the conversation. Factory and Jules have the same "re-activate on new message" pattern — it is the correct default.

**`max_iteration_per_run` default = 500** (`state.py:106`). This is the PER-RUN cap, not a lifetime cap. On resume, the counter resets. Per-turn budget (tokens) is separate from per-run iteration cap. For Lancelot agents: use 500 as the default iteration cap with a configurable override.

---

## 3. Isolation: git-worktree per task

**Replit (KNOWN, `core-concepts/agent/task-system`):** background tasks run in **"isolated copies of your project"** (task board: Drafts → Active → Ready → Done). Each background task gets its own project copy. Agent handles conflict resolution automatically when applying changes back to main.

**Factory (KNOWN, `cli/droid-exec/overview`):** `-w/--worktree [name]` flag creates a `<branch>-wt` git worktree. `--fork <id>` forks an existing session into a new run (branches off). The combination of fork-session + worktree = parallel-branch development where each branch lives in its own filesystem worktree, preventing file conflicts. Verbatim: "The combination of fork-session + worktree lets Droids do **parallel-branch development** where each branch lives in its own filesystem worktree, preventing file conflicts."

**OpenHands (KNOWN, cloud layer):** `working_dir/{conversation_id.hex}` per conversation when sandbox-grouping is enabled (`live_status_app_conversation_service.py:306`). The app-server appends a per-conversation hex suffix so multiple conversations sharing one sandbox don't collide.

**Sourcegraph Amp (KNOWN, `amp-examples/automation/sonarqube-amp/parallel-worktree/README.md`, 291L):** explicit parallel-worktree pattern example in the open-source examples. Agent spawns multiple `Task` sub-agents concurrently; each operates in its own `git worktree`. The 291-line README is the production recipe for this pattern.

**Pattern for Lancelot:** `git worktree add /tmp/lancelot-task-<uuid> <base-branch>` at task spawn time. Each parallel agent run gets its own working directory. On task completion, merge/apply, then `git worktree remove`. Worktrees share the `.git/` object store — they are cheap (no clone). This is the correct isolation unit for agent parallelism.

---

## 4. ActionBatch: decouple lifecycle from the step

OpenHands' `_ActionBatch` dataclass (`agent.py:111–236`) is the cleanest tool-call lifecycle pattern in any of these sources. It is a frozen dataclass (`@dataclass(frozen=True, slots=True)`) with a three-stage protocol:

```python
# Stage 1: prepare — validate, filter hooks, truncate at finish, dispatch
batch = _ActionBatch.prepare(action_events, state, executor, tool_runner, tools)
# Stage 2: emit — emit events in original order
batch.emit(on_event)
# Stage 3: finalize — handle FinishTool / iterative refinement
batch.finalize(on_event, check_iterative_refinement, mark_finished)
```

**`_truncate_at_finish` (agent.py:127–153):** if the LLM emits a `FinishTool` call in the middle of a batch (mixed with other tool calls), the batch is truncated at `[:finish_idx+1]` and any actions after the finish are warn-logged and discarded. This prevents the LLM from both "finishing" and continuing to act in the same turn — a common failure mode.

**`get_unmatched_actions` (state.py:472–512):** on resume, scans events in reverse (recent events more likely to be unmatched) and returns `ActionEvent`s with no matching `ObservationEvent`, `UserRejectObservation`, or `AgentErrorEvent`. This drives both the confirmation flow and crash recovery (executed-but-not-yet-observed actions are re-emitted). The `AgentErrorEvent` match is by `tool_call_id`, not `action_id` — critical for crash recovery where an error was emitted after a server restart with different in-memory state.

**The Lancelot implication:** Lancelot's existing `@app.batch` coalesces HTTP requests; this is about coalescing LLM tool-call batches inside the agent loop. They are different levels. The `_ActionBatch` pattern — freeze at creation time, execute, emit in order, finalize last — is worth copying for any agent loop implementation.

---

## 5. Prompt-cache stability in multi-turn agents

OpenHands `LocalConversation._pin_prompt_cache_key()` (`local_conversation.py:194`) sets a stable cache key on every reachable `LLM` based on the `conversation_id` before each run. This enables Anthropic/OpenAI prompt caching to hit on **resume of the same conversation**.

The mechanism: `agent.get_all_llms()` (`base.py:691–759`) recursively walks `MultimodalRouter.text_llm`, `MultimodalRouter.multimodal_llm`, `RandomRouter.llms`, etc. to find all reachable `LLM` instances. Each gets `llm._prompt_cache_key = conversation_id`. The multi-LLM walk is necessary because routers compose LLMs.

OpenHands' **static/dynamic system prompt split** (`agent.py:388–407`): the static part (rendered `system_prompt.j2`, same across all conversations) gets a `cache_control: {"type": "ephemeral"}` marker on Anthropic. The dynamic part (secrets, skills, runtime info) is sent as a **second content block WITHOUT a cache marker** — so cached prefix matches still hit even though the dynamic suffix changes. This is exactly the correct pattern for `lancelot.canonical.dumps_canonical`: the stable static prefix is cached; the request-specific suffix is appended uncached.

---

## 6. Security: supply-chain attack defence for AGENTS.md / skills

OpenHands' `security_risk_assessment.j2` (31L verbatim, `agent/prompts/security_risk_assessment.j2`) defines **Repository Context Supply Chain Rules** — a set of automatic HIGH-risk escalations whenever an action originates from `<UNTRUSTED_CONTENT>` / `REPO_CONTEXT` / `AGENTS.md` / `.cursorrules` / `.agents/skills/` and involves:

- Writing to `pip.conf`, `.npmrc`, `.yarnrc.yml`, `.pypirc`, `setup.cfg` (index-url / registry changes).
- Adding custom registry URLs or `extra-index-url`.
- Installing from private/non-standard registries not explicitly user-requested.
- Embedding hardcoded auth tokens in config files.
- Executing `curl|bash`, `wget|sh` (pipe-to-shell).
- Writing to `~/.config/`, `~/.ssh/`, `~/.npm/`, `~/.pip/`.
- Adding lifecycle hooks (`preinstall`, `postinstall`, `prepare`) that execute remote scripts.

This explicitly defends against malicious AGENTS.md / .cursorrules **prompt injection attacks** (attacker writes a repo that tries to exfiltrate the agent's env or steal credentials via a lifecycle hook). The check is at the action level, not the prompt level — it fires on what the LLM is about to DO, not on what was said.

For Lancelot: any agent that reads AGENTS.md / skill files from user repos should enforce this taxonomy. The exact categories above are the production-hardened list (OpenHands runs real user repos, so these have been tested against real-world injection attempts).

---

## 7. Tool result truncation constants

**Amp (KNOWN verbatim, `x1xhlol/Amp/claude-4-sonnet.yaml:898`):** `Bash` tool truncates to last **50,000 chars** of output. The truncation is from the END — recent output is preserved, early output is dropped.

**Amp `Read` tool (KNOWN verbatim, `x1xhlol/Amp/claude-4-sonnet.yaml:1715–1724`):** default window = **1,000 lines** (1-indexed), paginated via `read_range: [start, end]`. Output is line-prefixed: `1: <content>\n2: <content>...`

**Amp `Grep` tool (KNOWN verbatim, `x1xhlol/Amp/claude-4-sonnet.yaml:1323–1552`):** 250-match global cap, 15 matches per file cap, 250-char line truncation.

**OpenHands condenser (KNOWN, `preset/default.py:73`):** `LLMSummarizingCondenser(max_size=80, keep_first=4)`. The class-level defaults (`max_size=240, keep_first=2`) are much larger — the recipe aggressively compresses to 80 events to control context cost. `keep_first=4` preserves SystemPromptEvent + first few user messages. `min_condense_fraction=0.1` avoids tiny pointless condensations. Condenser uses `usage_id="condenser"` so its cost is tracked separately from the agent's main token budget.

**OpenHands EventLog (KNOWN, `agent.py:332–334` comment):** "conversations can reach 30k+ events" — `len(state.events)` is O(1); materializing `list(state.events)` on large histories is explicitly warned against. Prefix slicing `events[:N]` is the correct pattern.

---

## 8. Jules's verify-after-mutate protocol

KNOWN verbatim from `jules.md:10, 113–114`:
> "After every action that modifies the state of the codebase (creating, deleting, or editing a file), you MUST use a read-only tool (like `read_file`, `list_files`, etc.) to confirm that the action was executed successfully. Do not mark a plan step as complete until you have verified the outcome."

The `plan_step_complete(message: str)` tool (one of Jules' 33 tools) is the gate: the agent cannot call it without first doing a read-only verification. This is enforced semantically (the tool's own description says "before calling this tool, you must have already verified"). INFERRED: the runtime may also soft-check whether a read-only call appeared between the last edit and the `plan_step_complete` call.

**Parallel with Lancelot's A2 architecture requirement:** any agent loop that can write should have a verify gate. For coding agents this is a read-back of the file; for HTTP agents this is checking the response code / body of what was just sent. The Jules protocol is the strictest in the field — others (Devin, Amp) verify less rigorously.

---

## 9. Plan as a first-class entity (Jules)

Jules `set_plan(plan: str)` is a distinct tool from execution tools (KNOWN verbatim, `jules.md:9–11`). The lifecycle:
1. Explore with `list_files` / `read_file`.
2. Draft plan → `request_plan_review(plan)` — user approves.
3. `set_plan(plan)` — commits the plan.
4. For each step: do work → verify with read-only tool → `plan_step_complete(message)`.
5. `request_code_review()` → `pre_commit_instructions()` → `submit(branch, message, title, desc)`.

The plan format: markdown numbered list, sub-bullets per step. Mandatory pre-commit step (always penultimate; tool name disguised as "ensure proper testing, verification, review, and reflection").

The `record_user_approval_for_plan()` tool records plan approval — useful for audit trails in enterprise deployments where a human must approve agent plans before execution begins.

**Versus OpenHands' `ThinkTool`:** `think(thought: str)` logs a thought without state change (KNOWN verbatim, `tool/builtins/think.py`). Returns "Your thought has been logged." The thought appears in the event log and is visible to operators but does not commit a plan. Jules uses explicit plan commitment; OpenHands uses thought logging. For long-horizon tasks, Jules's explicit `set_plan` + `plan_step_complete` gating produces higher-quality output.

---

## 10. Factory's autonomy tier as a permissions primitive

Factory's five-tier permission model (KNOWN verbatim, `docs.factory.ai/cli/droid-exec/overview`):

| Tier | Allowed |
|------|---------|
| Default | Read-only ops: `cat/less/head/tail/systemctl status/echo/pwd/whoami/date/uname/ps/top/git status/log/diff/ls/find` (without `-delete` or `-exec`) |
| `--auto low` | File creation/editing inside project directories |
| `--auto medium` | `npm install`/`pip install` (no sudo), `curl/wget` to trusted, `git commit/checkout/pull`, `make/npm run build` |
| `--auto high` | `git push`, `curl\|bash`, port exposure, firewall mods, production deploys |
| `--skip-permissions-unsafe` | Everything. "Only in completely isolated environments." |

Fail-fast: exceeding the tier → stop immediately with non-zero exit code, no partial changes.

This is significantly more developed than Claude Code's single binary `--dangerously-skip-permissions`. The five-tier model is auditable, composable with CI/CD (you can safely run `--auto medium` in a PR review pipeline), and maps cleanly onto user trust levels.

**Lancelot byte-path implication:** Lancelot's `lancelot.net` (SSRF guard) + `lancelot.webhook` (Standard Webhooks sign/verify) handle the network-egress safety slice. The file-system / shell permission tier is agent-side and not yet in Lancelot. For Lancelot's agent loop, copy Factory's five-tier model verbatim as `AgentAutonomyLevel.{READ_ONLY, LOW, MEDIUM, HIGH, UNSAFE}`.

---

## 11. The Oracle pattern (Amp)

KNOWN verbatim from `x1xhlol/Amp/claude-4-sonnet.yaml:1623–1706`:

```
oracle(task: str, context: str | None, files: list[str] | None) -> analysis
```

The Oracle is hard-pinned to **OpenAI o3** with a read-only tool subset: `list_directory, Read, Grep, glob, web_search, read_web_page` (6 tools). The Oracle CANNOT edit code — it returns analysis. The main Sonnet/GPT-5 loop then implements based on Oracle output.

This is a **routing-not-context** pattern: instead of shoving the o3 prompt into the main context, you make a clean tool call, get a fresh-context analysis, and inject only the conclusion into the main thread. Benefits: (a) the reasoning model sees a clean problem statement, not a polluted 80k-token history; (b) the main loop's context doesn't grow; (c) cost is paid only when needed.

**When NOT to use the Oracle (verbatim):** Simple file reading, codebase search, web browsing, basic code modifications. The Oracle is for: code reviews, architecture feedback, finding bugs in multiple files, planning complex implementations, deep technical questions.

---

## 12. Replit's checkpoint primitive

KNOWN verbatim from `core-concepts/agent/checkpoints-and-rollbacks`:

A checkpoint captures: project files + AI conversation context + environment configuration + agent memory + database contents. This is complete state, not just code. Each checkpoint creates a corresponding Git commit. Bidirectional navigation (can roll back AND roll forward). Database rollback is opt-in (not default).

The Replit checkpoint model differs fundamentally from code-only agents: rollback restores the entire context, not just the working tree. For an HTTP-transport framework like Lancelot, the equivalent is the `EventLog` (OpenHands) or `base_state.json` + events approach: any state that changes during agent execution is either logged as an event or written to a JSON blob, so the conversation can be resumed or forked at any event boundary.

---

## 13. Deprecation / hype flags

**Model-family-specific system prompt addenda (OpenHands, `agent/prompts/model_specific/*.j2`):** specialised system prompt blocks per provider/model variant (anthropic_claude, google_gemini, openai_gpt/gpt-5, openai_gpt/gpt-5-codex). GPT-5 gets 17 lines of specific guidance including "send a brief preamble of 8–12 words before each tool call with a friendly and curious tone." This is a depreciating tactical hack — it churns on a ~1-2yr model clock as models improve at instruction following. Skip for Lancelot; use a single clean system prompt and tune model-specific behavior only when empirically necessary.

**Mermaid diagram tool (Amp, `x1xhlol/Amp/claude-4-sonnet.yaml`):** `mermaid(code: str)` renders diagrams in the VSCode webview. Only available in the VSCode extension. Generation-only — no graph reasoning. Useful for developer-facing agents but not for Lancelot's server-side transport layer.

**`SwitchLLMTool` (OpenHands, `tool/builtins/switch_llm.py`):** switches the LLM mid-conversation. Not in the default tool set; must be added explicitly. Useful for cost optimization (switch from heavy → cheap model for trivial follow-ups). This is a legitimate primitive but requires careful state tracking to avoid cache invalidation on every switch.

---

## 14. Byte-path lens: what Lancelot already provides vs. what it needs

| Agent primitive | Lancelot status |
|---|---|
| Outbound LLM transport (`TransportPool.request/stream/fanout/spawn_child`) | **DONE** — `lancelot.llm.TransportPool` |
| Provider token metering + error taxonomy (`ok/quota/timeout/permanent/transient/upstream_5xx`) | **DONE** — `lancelot.upstream_metrics` |
| Cost/trace context propagation (`cost_usd`, `agent_id`, `traceparent`) | **DONE** — `lancelot.context` ContextVars |
| Idempotency-Key injection | **DONE** — `TransportPool.request` auto-injects |
| SSRF guard for agent tool HTTP calls | **DONE** — `lancelot.net` |
| Tool schema definition (`@app.tool` → `ToolSpec` + JSON schema) | **DONE** — `lancelot.tool` |
| MCP server mount | **DONE** — `lancelot.mcp.mount` |
| Request batch coalescing for rate-limited LLM backends | **DONE** — `@app.batch` (~12× throughput) |
| Prompt-cache prefix stability | **DONE** — `lancelot.canonical.dumps_canonical` |
| **Stuck detection (counter-based: action_observation=4, action_error=3, monologue=3, alternating=6)** | **MISSING** |
| **Run loop FSM (IDLE/RUNNING/PAUSED/WAITING_FOR_CONFIRMATION/FINISHED/ERROR/STUCK/DELETING)** | **MISSING** |
| **Git-worktree isolation per parallel agent task** | **MISSING** |
| **ActionBatch prepare/emit/finalize with truncate-at-finish** | **MISSING** |
| **EventLog (file-backed, append-only, O(1) length, crash-safe)** | **MISSING** |
| **`get_unmatched_actions` for resume/confirmation** | **MISSING** |
| **Five-tier autonomy permission model** | **MISSING** (lancelot.net covers network egress only) |
| **Plan-as-entity (`set_plan` / `plan_step_complete` gates)** | **MISSING** |
| **Oracle pattern (route deep-reasoning to a separate context)** | **MISSING** (TransportPool.fanout is the byte-path analogue but not the same thing) |
| Supply-chain attack defence for AGENTS.md/skills | **MISSING** (needs prompt-level classification at the agent layer) |
| Condensation / context-window management | **MISSING** |

Lancelot already owns the full byte path (transport / serialization / streaming / resilience). The agent half — loop, memory, evals, durable state, safety — is what remains to be built.
