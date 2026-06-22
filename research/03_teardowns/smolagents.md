# smolagents — Lancelot agent harvest

> Source: `SMOLAGENTS.md` (RE teardown, two passes: 2026-05-20 + 2026-05-30, commit `406520d`, smolagents `1.27.0.dev0`). Apache 2.0.
> Focus: code-as-action paradigm, the sandbox, the loop, and the multi-agent injection mechanic.

---

## What smolagents IS, in one sentence

Code-action: the LLM emits a Python code block that is executed inside a custom AST-walking interpreter (`LocalPythonExecutor`); tool calls are just Python function calls inside that code. One emission can contain loops, variable accumulation, and function composition — no JSON-tool-call round-trips required.

---

## 1. The core primitive: code-as-action vs JSON-tool-calls

**Why code-action beats JSON-tool-calls for compositional tasks:**
- One emission = one Python block that can call multiple tools, branch, loop, accumulate results into variables.
- `ToolCallingAgent` needs N LLM round-trips for N tool calls; `CodeAgent` can do N calls in one block.
- Benchmark claim (INFERRED): ~30% fewer steps on same-complexity GAIA tasks; 44% Open GAIA with 70B open models (§9).
- Rule 4 in the system prompt explicitly guards against over-chaining: *"Do not chain too many sequential tool calls in the same code block when the output format is unpredictable… print() to use them in the next block."* (§2.1, `code_agent.yaml`)

**When code-action loses:**
- Models that don't reliably emit well-formed Python → use `ToolCallingAgent`.
- Multi-tenant/untrusted execution → remote executor (E2B/Docker/Modal) adds latency and a serialization round-trip; managed agents break with remote execution (§R2.7).

**Lancelot byte-path lens:** `@app.batch` already handles request coalescing at the transport layer. Code-action reduces *agent-side* LLM call overhead; both are complementary. The managed-agent-as-callable pattern (§R2.12) is directly analogous to `TransportPool.spawn_child` + `TransportPool.fanout` — Lancelot already has the byte-path substrate.

---

## 2. The `FinalAnswerException` terminate trick (load-bearing mechanism)

`local_python_executor.py:1630-1654` (§R2.2):

```python
# Inside evaluate_python_code, BEFORE execution:
def final_answer(*args, **kwargs):
    raise FinalAnswerException(previous_final_answer(*args, **kwargs))
static_tools["final_answer"] = final_answer  # monkeypatch

# After execution:
except FinalAnswerException as e:
    return e.value, True   # is_final_answer=True
```

The agent's "I'm done" signal is a regular Python call that raises a catchable exception. No separate protocol. This crosses the remote-executor sandbox boundary as a real Python exception (`FINAL_ANSWER_EXCEPTION = "FinalAnswerException"` string match, `remote_executors.py:68`), deserialized via `"safe:"+json` / `"pickle:"+b64` with `allow_pickle=False` default (§R2.11).

**Lancelot implication:** the `StopReason` / loop-termination mechanic for a code-action agent should use this pattern — a sentinel exception thrown by a `final_answer()` callable injected into the executor namespace, caught by the step loop. No extra boolean flag, no special return value.

---

## 3. The AST executor — three-way DoS bound (the safety guarantee)

`local_python_executor.py` (§R2.1). The executor does NOT use `exec()`. It parses to AST and walks with `evaluate_ast` (~50 node types). Three independent bounds:

| Guard | Value | Source |
|---|---|---|
| AST operation count | `MAX_OPERATIONS = 10_000_000` | `local_python_executor.py:58` |
| While-loop iterations | `MAX_WHILE_ITERATIONS = 1_000_000` | `local_python_executor.py:59` |
| Wall-clock timeout | `MAX_EXECUTION_TIME_SECONDS = 30` (`signal.alarm` on POSIX) | `local_python_executor.py:60` |
| Output length cap | `DEFAULT_MAX_LEN_OUTPUT = 50_000` chars | `local_python_executor.py:57` |

The operation counter increments on every `evaluate_ast` call — a loop over 10M items raises `InterpreterError`, no hang.

**Dunder block** (`local_python_executor.py:68`, §3.3):
```python
def nodunder_getattr(obj, name, default=None):
    if name.startswith("__") and name.endswith("__"):
        raise InterpreterError(f"Forbidden access to dunder attribute: {name}")
    return getattr(obj, name, default)
```
Applied at every `Attribute` AST node (`evaluate_attribute`, line 390-391) — not just when the LLM calls `getattr()` by name. Blocks the entire `().__class__.__bases__[0].__subclasses__()` escape family.

**`DANGEROUS_MODULES`** (10, `local_python_executor.py:130`): `builtins, io, multiprocessing, os, pathlib, pty, shutil, socket, subprocess, sys` — blocked even if in `authorized_imports`.

**`DANGEROUS_FUNCTIONS`** (9, `local_python_executor.py:143`): `builtins.compile`, `builtins.eval`, `builtins.exec`, `builtins.globals`, `builtins.locals`, `builtins.__import__`, `os.popen`, `os.system`, `posix.system`.

**Safe builtins whitelist** (§3.2): `print, range, enumerate, zip, len, min, max, sum, abs, round, pow, int, float, str, bool, list, tuple, dict, set, frozenset, bytes, bytearray, sorted, all, any, map, filter, ord, chr, next, iter, divmod, callable, getattr (= nodunder_getattr), hasattr, setattr, issubclass, type, complex` + exception classes. NOT included: `exec, eval, compile, __import__, globals, locals, dir, vars, id, memoryview, open, input, breakpoint, help`.

**Import safety** (§R2.1, `local_python_executor.py:1309-1342`): `check_import_authorized` with `build_import_tree` supports wildcard prefixes (`numpy.*`). Allowed modules are wrapped by `get_safe_module` (`:1280-1306`) which **recursively rebuilds the module stripping dunder attributes** — so even an authorized module can't expose `__builtins__`.

**Default `BASE_BUILTIN_MODULES`** (11, `utils.py:49-61`): `collections, datetime, itertools, math, queue, random, re, stat, statistics, time, unicodedata`. User extends via `additional_authorized_imports`.

**Lancelot implication:** For a local code-execution sandbox in Lancelot's agent layer, this three-way bound (op count + while-iteration cap + wall-clock) is the correct approach. The `nodunder_getattr` replacement and `get_safe_module` wrapping are the two non-obvious escape blockers. Production/multi-tenant → remote executor (E2B/Docker/Modal) is the right call, not a tighter local sandbox.

---

## 4. Error taxonomy in the loop — the retry mechanic (load-bearing)

`agents.py:594-599` (§R2.4):

```python
except AgentGenerationError as e:
    raise e          # framework bug → EXIT immediately
except AgentError as e:
    action_step.error = e   # model produced bad code → LOG and ITERATE
```

`AgentError` (bad model output) goes into the step's `error` field, gets rendered into the next turn's messages, and the loop continues. `AgentGenerationError` (framework/API bug) exits.

The error-as-observation mechanic: `ActionStep.to_messages()` appends a verbatim retry coach line when an error is present (`memory.py:142`):
> *"Now let's retry: take care not to repeat previous errors! If you have retried several times, try a completely different approach."*

Error message includes exact source segment: `ast.get_source_segment(code, node)` (`local_python_executor.py:1660`) — the model sees which line failed, not a generic exception.

**Lancelot implication:** Two-tier error taxonomy (`FrameworkError` → exit; `ModelError` → retry) is the right pattern. The retry coach line is worth copying verbatim. The exact source segment in the error message (from `ast.get_source_segment`) dramatically helps self-correction vs. a raw traceback.

---

## 5. Memory step schema (the replay/context format)

`memory.py:51-89` (§R2.3). `ActionStep` fields:
- `step_number`, `timing: Timing`
- `model_input_messages: list[ChatMessage]`
- `model_output_message`, `model_output: str`
- `tool_calls: list[ToolCall]`
- `error: AgentError | None`
- `code_action: str | None` (CodeAgent only — the parsed code blob)
- `observations: str | None`
- `observations_images`
- `action_output: Any`
- `token_usage: TokenUsage | None`
- `is_final_answer: bool`

Every step has `to_dict()` (JSON-serializable → replay format) and `to_messages()` (context assembly). `write_memory_to_messages()` (`agents.py:1646`) concatenates each step's `to_messages()` — this is the sole path from stored memory to LLM input.

`ToolCall` schema (`:24-39`): `name`, `arguments`, `id`. `PlanningStep`, `TaskStep`, `SystemPromptStep`, `FinalAnswerStep` are the other step types.

**Lancelot implication:** `ActionStep` is a good schema for Lancelot's agent step type. The `code_action` field for CodeAgent vs. `tool_calls` list for ToolCallingAgent is the clean fork point. The `to_messages()` method on each step (not a central renderer) is a good encapsulation pattern — each step knows how to render itself.

---

## 6. The planning-interval mechanic

`agents.py:550-551` (§R2.4):

```python
if planning_interval is not None and (step_number == 1 or (step_number - 1) % planning_interval == 0):
    ... yield PlanningStep ...
```

Default is `None` (never plan). When set, the agent re-plans on step 1 and every `N`th step after. Planning step is a separate LLM call that updates a text plan stored in memory. This is distinct from the action step — the plan can refer to tools and observations, and the action step then executes against the plan.

**Lancelot implication:** Planning-interval as an optional `int | None` knob on the agent constructor (not baked in) is the right ergonomic. `None` = ReAct-only; any integer = periodic planning. The first step always gets a plan when `planning_interval` is set — good default.

---

## 7. `fix_final_answer_code` — model repair for weak models

`local_python_executor.py:332` (§3.11):

If the model's last code statement is a bare expression (`ast.Expr`) and `final_answer` is in `static_tools`, automatically wraps it: `final_answer(<expr>)`. Improves success rate on weaker models that forget to call `final_answer` explicitly.

**Lancelot implication:** Worth including as a parsing post-processor. Low cost, meaningful win on weak/cheaper models.

---

## 8. `PromptTemplates` TypedDict — the prompt shape (§2.4)

`agents.py:166`:

```python
class PromptTemplates(TypedDict):
    system_prompt: str
    planning: PlanningPromptTemplate      # initial_plan, update_plan_pre_messages, update_plan_post_messages
    managed_agent: ManagedAgentPromptTemplate  # task (parent → child), report (child → parent)
    final_answer: FinalAnswerPromptTemplate    # pre_messages, post_messages
```

The `managed_agent` sub-template has a `task` string (how the parent formats a subtask to the child) and a `report` string (how the child formats its result back to the parent). This is the structured seam for multi-agent context flow.

**Lancelot implication:** The TypedDict structure for prompt templates is a good baseline for Lancelot's agent layer — typed, swappable, separate sub-templates for each role in the interaction. The `managed_agent.task/report` pair is the right abstraction for parent-child context isolation.

---

## 9. Managed agents = callables in the executor namespace (§R2.12)

`agents.py:491`:

```python
self.python_executor.send_tools({**self.tools, **self.managed_agents})
```

A managed agent's `__call__` runs its full multi-step loop and returns the formatted report string. From the LLM's perspective, it's just a Python function call. The parent never sees the child's steps — just the result string.

Contract: `name` + `description` required (`agents.py:373`). Remote executors don't support managed agents (the callable can't be injected into a remote sandbox, `agents.py:1608-1609`).

**Lancelot byte-path lens:** `TransportPool.spawn_child` and `TransportPool.fanout` are the byte-level analogues — they don't inject callables, but they do provide the context isolation (traceparent, agent_id inheritance) and partial-failure shape that multi-agent orchestration needs. For Lancelot's code-action agent, injecting sub-agents as callables is the right pattern; the byte-path tracking is already provided by `TransportPool`.

---

## 10. Remote executor wire protocol — `allow_pickle=False` default (§R2.11)

`remote_executors.py:74,118-130,328-329`:

- Variables serialized as `"safe:" + json` (JSON-safe objects) or `"pickle:" + base64(pickle)` (complex types).
- `allow_pickle=False` by default — pickle fallback requires explicit opt-in.
- `FinalAnswerException` crosses the sandbox boundary as a real Python exception; detected by string match on `error.name == "FinalAnswerException"`.
- Package install in sandbox: `!pip install {imports}` via the sandbox's shell (`:139`).

This is the same posture as Letta (cross-teardown convergence): never deserialize untrusted pickle by default.

**Lancelot implication:** If Lancelot ships a code-action executor, `allow_pickle=False` should be the hard default, with a clear opt-in (documented security tradeoff). The `"safe:"+json prefix-tagged serialization protocol is clean and worth copying for any agent-state transport.

---

## 11. `ToolCallingAgent` parallel calls + state variable resolution (§R2.10)

`agents.py:1373,1354-1355`:

- `ToolCallingAgent` supports **parallel tool calls** in one turn: `process_tool_calls` collects `parallel_calls: dict[str, ToolCall]`.
- If the final answer is a string that's a key in `self.state`, it's **resolved to the stored value** — so the model can return a variable name as the "final answer" and the framework dereferences it.

**Lancelot implication:** For the JSON-tool-call agent variant, parallel tool execution in one turn is a free win for multi-tool tasks (no sequential dependency). The state-variable resolution trick is a low-cost ergonomic that prevents the model from needing to repeat data it already computed.

---

## 12. System prompt rules worth copying verbatim (§2.1)

From `code_agent.yaml`:

> Rule 3: *"Use the arguments directly as in `answer = wiki(query="...")`, not as a dict `wiki({'query': "..."})`."*
> Rule 4: *"Do not chain too many sequential tool calls in the same code block when the output format is unpredictable. Print results with `print()` to use them in the next block."*
> Rule 5: *"Call a tool only when needed, and never re-do a tool call that you previously did with the exact same parameters."*
> Rule 8: *"You can use imports in your code, but only from the following list of modules: `{authorized_imports}`."*
> Rule 9: *"The state persists between code executions: so if in one step you've created variables or imported modules in your code, these will all persist."*
> Rule 10: *"Don't give up! You're in charge of solving the task, not providing directions to solve it."*

Stop sequences for `CodeAgent`: `["Observation:", "Calling tools:"]` + the closing code tag (`agents.py:1651-1654`). The closing tag as a stop sequence makes the model halt right after the code block.

**Lancelot implication:** These rules are empirically derived. Rules 4 and 5 are the non-obvious ones (don't over-chain; don't re-call). Rule 10 is "never refuse" for code agents. The closing-tag-as-stop-sequence is the correct way to prevent the model from continuing past the code block.

---

## Anti-patterns / things NOT to copy

1. **No memory condenser.** `AgentMemory.steps` grows unboundedly — no `LLMSummarizingCondenser`, no compaction (§6). For long-horizon runs this is a design gap. Lancelot's agent layer should have a context-window manager.

2. **`planning_interval=None` default (never plan).** Planning is opt-in. That's correct for simple ReAct agents, but it means smolagents provides no default planning strategy. Lancelot should consider making planning the default for agents with > N tools or > M expected steps.

3. **`model` is just a `Callable`** — no provider profile, no OMIT sentinel, no message-shape translation. The model layer is thin; providers like Anthropic that don't accept `stop` sequences (`supports_stop_parameter`, `models.py:418`) are special-cased per-model-id. Lancelot needs a proper ProviderProfile layer.

4. **Remote executor + managed agents = unsupported.** (`agents.py:1608-1609`) This is an architectural constraint, not a choice — injecting callables into remote sandboxes requires serializing closures, which is fragile. Lancelot should track this constraint explicitly.

5. **Tool output types constrained to 11 types** (`AUTHORIZED_TYPES`, `tools.py:82-94`). This works for smolagents' use case (images/audio as `AgentImage`/`AgentAudio`) but is unnecessarily restrictive for a general framework. Lancelot's `@app.tool` / `ToolSpec` should allow richer output types.

---

## Byte-path implications summary

| smolagents primitive | Lancelot status |
|---|---|
| `CodeAgent` / `ToolCallingAgent` loop | NOT BUILT — the agent loop itself |
| `LocalPythonExecutor` (AST sandbox) | NOT BUILT — optional for code-action |
| `FinalAnswerException` terminate trick | NOT BUILT — maps to `StopReason` in the loop |
| Two-tier error taxonomy (exit vs retry) | NOT BUILT — maps to agent loop error handling |
| `ActionStep` memory schema | NOT BUILT — maps to agent context engineering |
| `PromptTemplates` TypedDict | NOT BUILT — prompt structure |
| `managed_agent` as callable in executor | `TransportPool.spawn_child/fanout` cover the byte-path side; the callable-injection pattern is agent-layer |
| `allow_pickle=False` serialization default | NOT BUILT (relevant if remote executor shipped) |
| Retry coach line in error observation | NOT BUILT — a prompt engineering primitive |
| `fix_final_answer_code` AST repair | NOT BUILT — model repair post-processor |
| Planning-interval knob | NOT BUILT |

---

## Honest hype assessment

- **Code-action vs JSON-tool-calls is a DURABLE primitive** — the expressiveness advantage (loops, variables, composition in one emission) holds across model generations. Not a tactical hack.
- **The AST executor bounds (10M ops / 1M while / 30s) are empirically calibrated constants** — worth copying directly, not reinventing.
- **The 44% GAIA Open claim is INFERRED** (not line-verified in the RE). Treat as directional, not a hard benchmark.
- **The specific model names in examples** (Claude Sonnet 4, gpt-4o-mini) will churn. The architectural patterns won't.
- **smolagents is intentionally minimal on memory (no condenser)** — this is a gap, not a feature, for Lancelot's agent layer.
