# Multi-Agent Orchestration Frameworks — Distilled Note

Sources read in full: Agency Swarm v1.9.8, CAMEL-AI v0.2.91a4, MetaGPT (commit 11cdf46), Magentic-One (autogen v0.4 + magentic-ui 2025), TaskWeaver v0.0.12.

---

## 1. Communication topology taxonomy (the real dividing line)

Five distinct topologies. Topology is the highest-signal design choice because it determines cost structure, failure modes, and what orchestration logic you need:

| Framework | Topology | Routing mechanism |
|---|---|---|
| **Agency Swarm** | Explicit DAG — declared `communication_flows` list | SendMessage tool; LLM picks recipient from enum; recursive depth-first call stack |
| **MetaGPT** | Pub/sub actor bus — `Environment.publish_message` | `cause_by` (Action FQN); each role's `_watch` set is its subscription filter |
| **Magentic-One** | Star — orchestrator at center; agents never call each other | Progress ledger JSON (1 LLM call/turn): `next_speaker` field drives dispatch |
| **CAMEL** | Fixed pair (RolePlaying) or coordinator-workers DAG (Workforce) | Workforce: asyncio task channel + coordinator decompose/assign; RolePlaying: turn-taking |
| **TaskWeaver** | Star with Planner at center; strict linear chain (User→Planner→Worker→Planner→User) | Planner's `send_to` field; runtime-enforced enum `workers ∪ {User}` |

**The non-obvious finding:** agency architectures that let agents address each other directly (MetaGPT's pub/sub, CAMEL's Workforce async dispatch) are harder to reason about for correctness and cost estimation, but require fewer LLM calls for routing. Star topologies (Magentic-One, TaskWeaver) are cheaper per-turn on routing but the orchestrator is the bottleneck and single point of failure.

---

## 2. Agency Swarm: SendMessage-as-tool, the per-thread concurrency guard, and the handoff variant

### 2.1 Inter-agent communication is a tool call — the key mechanism

`src/agency_swarm/tools/send_message.py:312`. Every agent-to-agent call in Agency Swarm is mediated by a `FunctionTool` called `SendMessage`. The sender's LLM emits a tool call; the tool `on_invoke_tool` handler recursively calls `recipient_agent.get_response(...)` and returns `result.final_output` as the tool's string output. From the sender's runner's perspective, B is a tool that returns text.

**Why this matters:** tool-call retry, streaming, guardrails, and tracing all apply to inter-agent calls for free. No separate message bus or runtime needed.

**Verbatim tool schema** (`send_message.py:91-119`):
```json
{
  "type": "object",
  "properties": {
    "recipient_agent": {
      "type": "string",
      "enum": ["AgentB", "AgentC"],
      "description": "The name of the agent to send the message to."
    },
    "message": {
      "type": "string",
      "description": "Specify the task required for the recipient agent to complete. Focus on clarifying what the task entails, rather than providing exact instructions. Make sure to include all the relevant information from the conversation needed to complete the task."
    },
    "additional_instructions": {
      "type": "string",
      "description": "Optional. Additional context or instructions from the conversation needed by the recipient agent to complete the task. If not needed, provide an empty string."
    }
  },
  "required": ["recipient_agent", "message", "additional_instructions"],
  "additionalProperties": false
}
```

Note: `additional_instructions` is `required` even though it is "optional" per the docstring. The comment at line 116 is verbatim: "OpenAI API requires all properties in 'required' array, even optional ones." This is the OpenAI `additionalProperties=false` constraint — make all properties required, use empty string as the sentinel.

**Tool description verbatim** (lines 39-48 — the framing that shapes LLM behavior):
```
Use this tool to facilitate direct, synchronous communication between specialized agents within your agency.
...
You are responsible for relaying the recipient agent's responses back to the user, as the user does not have direct access to these replies. Keep engaging with the tool for continuous interaction until the task is fully resolved. Do not send more than 1 message to the same recipient agent at the same time.

Available recipient agents:
- AgentB: <description from AgentB.description>
```

The "Available recipient agents:" roster is built dynamically from each receiver's `description` attribute. If `description=None`, it becomes literal "No description provided". Strongly set `description` on every agent.

### 2.2 Per-thread concurrency guard — deadlock prevention

`send_message.py:374-388`:
```python
async with self._pending_lock:
    pending_set = self._pending_per_thread.setdefault(thread_key, set())
    if recipient_key in pending_set:
        return ("Error: Cannot send another message to '{recipient_agent_name}' "
                "while the previous message is still being processed. ...")
    pending_set.add(recipient_key)
```

`_pending_per_thread: dict[thread_id, set[recipient_name]]` + `asyncio.Lock`. Refuses with a friendly error if the LLM tries to send two simultaneous messages to the same recipient. The LLM can read this error and wait. This is the fix for a common failure mode: models sometimes emit two `send_message` tool calls in one turn before getting the first reply.

### 2.3 Handoff variant

`send_message.py:564-617`. When you want an agent to take over entirely rather than reply-then-return:

```python
class Handoff:
    add_reminder: bool = True  # default True
    def create_handoff(self, recipient_agent):
        handoff_object = handoff(
            agent=recipient_agent,
            tool_name_override=f"transfer_to_{name.replace(' ', '_')}",
        )
```

Default reminder injected on handoff (verbatim, line 603): `"Transfer completed. You are {recipient_agent_name}. Please continue the task."`. Override per-agent via `Agent(..., handoff_reminder="<custom>")`. Inserted messages are tagged `"message_origin": "handoff_reminder"` so downstream filters can identify framework-inserted vs model output.

Tool name format: `transfer_to_{recipient_name.replace(' ', '_')}` — spaces become underscores.

### 2.4 Static DAG enforcement

`communication_flows` is declared at `Agency()` construction time; it cannot change mid-conversation. The LLM literally cannot address an agent it isn't allowed to — the enum constraint in the JSON schema enforces this. This is the capability most agent frameworks lack: explicit topology that survives adversarial model output.

### 2.5 Conversation-starter cache

`agent/conversation_starters_cache.py:74-117`. Fingerprint = SHA over 14-key payload: `instructions, shared_instructions, prompt, model, usage_model, model_settings, input_guardrails, output_guardrails, tools, tool_use_behavior, reset_tool_choice, mcp_servers, mcp_config, handoffs, output_type`. Cache stored on disk at `.agency_swarm/cache/conversation_starters/<agent_name>_<starter_hash>.json`. Match conditions: first message, `callerAgent=None`, after normalize. On hit: serves cached response in milliseconds with no LLM call. This is the latency optimization for common openings.

### 2.6 AgentFlow `>` DSL — the `__bool__` hack

`agent/agent_flow.py:73-83`. `a > b > c` is Python comparison chaining; Python evaluates `(a > b) and (b > c)`. The `and` short-circuit calls `__bool__` on the first operand. Agency Swarm overrides `__bool__` to append `(a,b)` to a class-level `_chain_flows` list as a side effect, then returns `True`. The author's comment (line 78 verbatim): "This is a hack to work around Python's comparison chaining." It works; the `Agency` constructor calls `AgentFlow.get_and_clear_chain_flows()` to collect all accumulated edges.

### 2.7 MCP lifecycle — singleton driver

`tools/mcp_manager.py:30-39`. Process-singleton `PersistentMCPServerManager` with one background asyncio loop per MCP server. Per-server command queue + driver task. Timeouts:
- `connect`: 20s
- `list_tools`: 10s  
- `call_tool`: 120s
- `cleanup`: 10s

The singleton is necessary because MCP servers use stdio/SSE transports that don't tolerate multiple parallel connections. MCP tools look like normal `FunctionTool`s to the OpenAI Agents SDK.

### 2.8 Constants

| Constant | Value | Source |
|---|---|---|
| Default model | `"gpt-5.4-mini"` | `agent/core.py:143` |
| `validation_attempts` | 1 | `agent/core.py:88` |
| `Runner.max_turns` (upstream) | 10 | openai-agents SDK |
| SendMessage `additionalProperties` | false | `send_message.py:118` |
| Handoff `add_reminder` | True | `send_message.py:567` |
| Required Python | ≥3.12 | `pyproject.toml` |

---

## 3. Magentic-One: The dual-ledger orchestrator pattern

This is the highest-signal primitive in this entire batch — verified to contribute ~6 pp on GAIA when replaced with round-robin.

### 3.1 The two loops

`_magentic_one_orchestrator.py:262-449`:

**Outer loop (run once at start, re-run on stall):**
1. `_facts ← LLM(ORCHESTRATOR_TASK_LEDGER_FACTS_PROMPT(task))` — closed-book pre-survey
2. `_plan ← LLM(ORCHESTRATOR_TASK_LEDGER_PLAN_PROMPT(team))` — bullet plan
3. Broadcast ledger_full_prompt(task, team, facts, plan) to all agents; send `GroupChatReset` to reset each agent's local history

**Inner loop (once per turn):**
1. `progress_ledger ← LLM(ORCHESTRATOR_PROGRESS_LEDGER_PROMPT)` → JSON
2. Retry up to `_max_json_retries = 10` times on parse failure
3. If `is_request_satisfied.answer` → call `_prepare_final_answer()`, done
4. Stall accounting: `not is_progress_being_made OR is_in_loop → n_stalls += 1`; else `n_stalls = max(0, n-1)`
5. If `n_stalls >= max_stalls=3` → `_update_task_ledger()` → re-enter outer loop
6. Broadcast `instruction_or_question.answer`; dispatch `next_speaker.answer`

**This means 1 LLM call per turn for both routing and progress assessment combined.** Two outer-loop calls at start, two more on each stall-triggered re-plan. Very token-efficient.

### 3.2 The five prompts (verbatim)

**Facts survey** (`_prompts.py:6-32`). The "Ken Jennings-level trivia, Mensa-level puzzles" framing is load-bearing — it activates stronger recall:
```
Below I will present you a request. Before we begin addressing the request, please answer the following pre-survey to the best of your ability. Keep in mind that you are Ken Jennings-level with trivia, and Mensa-level with puzzles...
...
1. GIVEN OR VERIFIED FACTS
2. FACTS TO LOOK UP
3. FACTS TO DERIVE
4. EDUCATED GUESSES
```

**Plan prompt** (`_prompts.py:35-40`):
```
Fantastic. To address this request we have assembled the following team:
{team}
Based on the team composition, and known and unknown facts, please devise a short bullet-point plan for addressing the original request. Remember, there is no requirement to involve all team members...
```

**Progress ledger** (`_prompts.py:55-100`) — the core per-turn JSON call:
```
- Is the request fully satisfied?
- Are we in a loop where we are repeating the same requests...?
- Are we making forward progress?
- Who should speak next? (select from: {names})
- What instruction or question would you give this team member?
```

Output validated against `LedgerEntry` Pydantic model (`_prompts.py:103-118`):
```python
class LedgerEntry(BaseModel):
    is_request_satisfied: LedgerEntryBooleanAnswer
    is_in_loop: LedgerEntryBooleanAnswer
    is_progress_being_made: LedgerEntryBooleanAnswer
    next_speaker: LedgerEntryStringAnswer
    instruction_or_question: LedgerEntryStringAnswer
```

When the model client supports structured output, the model is called directly with `json_output=LedgerEntry`.

**Facts update** (on stall) — explicitly asks for AT LEAST ONE new educated guess. The "add or update one educated guess" instruction is what prevents the replanning LLM from just rehashing the old plan.

**Plan update** (on stall):
```
Please briefly explain what went wrong on this last run (the root cause of the failure), and then come up with a new plan that takes steps and/or includes hints to overcome prior challenges and especially avoids repeating the same mistakes.
```

### 3.3 Stall counter — hysteresis

`_magentic_one_orchestrator.py:393-406`:
```python
if not progress_ledger["is_progress_being_made"]["answer"]:
    self._n_stalls += 1
elif progress_ledger["is_in_loop"]["answer"]:
    self._n_stalls += 1
else:
    self._n_stalls = max(0, self._n_stalls - 1)   # heals over time
```

Progress *decrements* the stall counter; loop or no-progress increments it. Three sequential bad turns trigger re-plan; a good turn partially heals. This prevents premature replanning on a single bad step.

### 3.4 State persistence

`_magentic_one_orchestrator.py:225-245`. `MagenticOneOrchestratorState` serializes the full orchestrator state (`message_thread, current_turn, task, facts, plan, n_rounds, n_stalls`) to a Pydantic model. A paused run can be resumed.

### 3.5 Orchestrator system prompt = ""

`_prompts.py:3`. The orchestrator has no persistent persona — all behavior is in the per-call user-message prompts. This means the orchestrator is fully stateless across calls; its "knowledge" of the task comes entirely from the in-context ledger.

### 3.6 Single-agent shortcut

`_magentic_one_orchestrator.py:342-346`:
```python
if len(self._participant_names) == 1:
    progress_ledger["next_speaker"] = {
        "reason": "The team consists of only one agent.",
        "answer": self._participant_names[0],
    }
```

For single-agent teams, `next_speaker` is bypassed — avoids a wasted parsing round.

### 3.7 Context assembly — tool events stripped

`_magentic_one_orchestrator.py:514-529`. Tool-call request/execution events are stripped from the orchestrator's context; only the text responses matter. Agent responses appear as `UserMessage` regardless of which agent produced them. Orchestrator's own messages appear as `AssistantMessage`.

### 3.8 Re-planning resets agent state

`_magentic_one_orchestrator.py:264-270`. On `_reenter_outer_loop`, the orchestrator sends `GroupChatReset` to every participant, clearing their local history. Re-planning is a hard reset. No agent state carries over into the new plan.

### 3.9 Magentic-UI additions (2025)

`orchestrator_config.py:7-47`. Key additions over M1:
- `cooperative_planning=True` (default) — human must approve plan before execution
- `autonomous_execution=False` (default) — human in loop during execution  
- `max_replans=3` — cap on outer-loop re-entries (M1 was uncapped)
- `max_json_retries=3` (down from M1's 10)
- `retrieve_relevant_plans ∈ {"never", "hint", "reuse"}` — plan-learning from past sessions
- `SentinelPlanStep` — monitor X every N seconds until condition Y ("Tell me When" primitive)

Progress ledger simplified to 3 questions in Magentic-UI (vs 5 in M1): `is_current_step_complete`, `need_to_replan`, `instruction_or_question`. Steps are now first-class (step_index tracked); "is step complete?" replaces M1's "is task complete?".

### 3.10 Constants

| Constant | Value | Source |
|---|---|---|
| `max_turns` | 20 | `_magentic_one_group_chat.py:117` |
| `max_stalls` | 3 | `:119` |
| `_max_json_retries` | 10 (M1) / 3 (UI) | `orchestrator.py:94` / `orchestrator_config.py:37` |
| Viewport | 1440×900px | `_multimodal_web_surfer.py:199-200` |
| MLM image | 1224×765px (0.85×) | `:203-205` |
| SoM screenshot tokens | 1105 | `:207` |
| Post-action sleep | 3s | `:771,765` |
| Off-viewport targets cap | 30 | `:545` |
| FileSurfer viewport | 5120 chars | `_file_surfer.py:84` |
| Default model | `gpt-4o` | `_m1.py:23` |

---

## 4. MetaGPT: pub/sub actors, ActionNode trees, QaEngineer cause_by state machine

### 4.1 Pub/sub via `cause_by` — the durable primitive

`metagpt/roles/role.py`, `metagpt/environment/base_env.py`. The inter-agent protocol is the FQN of the producing Action class, not agent names. `Role._watch([WritePRD, WriteDesign])` means "deliver to my inbox any message whose `cause_by` is one of these Action classes."

```python
class Message(BaseModel):
    cause_by: str = ""         # FQN of the Action that produced this message
    sent_from: str = ""        # FQN of the Role
    send_to: set[str] = {MESSAGE_ROUTE_TO_ALL}   # "<all>", "<self>", or role names
```

`Environment.publish_message` iterates roles, checks `is_send_to(message, addrs)`, and delivers to inbox. `asyncio.gather` over all non-idle roles per tick. This is true parallel execution within a tick.

**`MESSAGE_ROUTE_TO_SELF`** (a constant `"<self>"`) is rewritten by `publish_message` to the sender's FQN. Lets a role enqueue work for itself — the QaEngineer uses this heavily for its test→run→debug loop.

### 4.2 QaEngineer: cause_by IS the state machine

`metagpt/roles/qa_engineer.py:184-194`. The QA engineer's entire decision of "what to do next" is a case on the incoming message's `cause_by`:
```python
if msg.cause_by in code_filters:      # code just written → write test
    await self._write_test(msg)
elif msg.cause_by in test_filters:    # test written/debugged → run it
    await self._run_code(msg)
elif msg.cause_by in run_filters:     # code ran → debug if failed
    await self._debug_error(msg)
```

No explicit FSM object; the type system encodes the FSM. The loop: WriteCode → WriteTest → RunCode → DebugError → WriteTest, capped at `test_round_allowed=5`.

### 4.3 ActionNode trees — structured output via declaration

`metagpt/actions/action_node.py:47-75`. Each node declares `{key, expected_type, instruction, example}`. `fill(context, llm)` renders the prompt and parses `[CONTENT]...[/CONTENT]` back to a typed Pydantic object.

The rendering template (verbatim):
```
## context
{context}
-----
## format example
{example}

## nodes: "<node>: <type>  # <instruction>"
{instruction}

## constraint
{constraint}

## action
Follow instructions of nodes, generate output and make sure it follows the format example.
```

Constraint footer (verbatim, `action_node.py:50-52`): `"Language: Please use the same language as Human INPUT.\nFormat: output wrapped inside [CONTENT][/CONTENT] like format example, nothing else."`

Uses `tenacity` retry: `@retry(wait=wait_random_exponential(min=1, max=60), stop=stop_after_attempt(6))`.

**Write code retry constants** (`write_code.py:94`): 6 attempts, exponential 1-60s backoff.

### 4.4 The `_think()` prompt — numeric stage selection

`roles/role.py:54-69` (`STATE_TEMPLATE`, verbatim):
```
...
Now choose one of the following stages you need to go to in the next step:
{states}

Just answer a number between 0-{n_states}, choose the most suitable stage according to the understanding of the conversation.
...
If you think you have completed your goal and don't need to go to any of the stages, return -1.
Do not answer anything else, and do not add any other information in your answer.
```

Output is a single integer. This is one of the cheapest orchestration mechanisms in the batch — the LLM returns a digit, not JSON, and it drives the entire state transition.

### 4.5 Three react modes

`roles/role.py:82-89`, `RoleReactMode` enum:
- `REACT` (default, `max_react_loop=1`) — think → act, once
- `BY_ORDER` — execute `actions` list in fixed order, `max_react_loop=len(actions)`
- `PLAN_AND_ACT` — builds a plan first, then executes each task

Same role class, one enum field. No subclassing needed to switch modes.

### 4.6 Cost-budget as termination signal

`metagpt/team.py:41`. `Team.invest(investment)` sets `cost_manager.max_budget`. `Team.run()` raises `NoMoneyException` when accumulated LLM cost exceeds budget. CLI defaults: `investment=$3.0`, `n_round=5`. A hard token-cost ceiling is architecturally important — without it, a looping agent will run indefinitely.

### 4.7 TeamLeader T-shirt sizing (MGX routing)

`roles/di/team_leader.py:20-24`. The routing logic is explicit t-shirt sizing (verbatim, lines 19-25):
- **XS** (snake game, calculator): skip PRD/Design SOP, go straight to Engineer
- **S** (photo gallery, feedback form): same
- **M** (menu ordering, news aggregator): standard SOP
- **L** (booking, inventory management): standard SOP
- **XL** (social media, e-commerce): full SOP + explicit deployment step

Default tech stack baked in (rule 14, verbatim): `"Vite, React, MUI, Tailwind CSS"`. Web app is the default output. For XS/S, this bypasses a full 5-role SOP and cuts cost by ~60-70%.

### 4.8 RoleZero THOUGHT_GUIDANCE (5-step CoT scaffold)

`roles/di/role_zero.py:95-115` (verbatim):
```
First, describe the actions you have taken recently.
Second, describe the messages you have received recently...
Third, describe the plan status and the current task. Review the history, if Current Task has been undertaken and completed by you or anyone, you MUST use the Plan.finish_current_task command to finish it first...
Fourth, describe any necessary human interaction. Use RoleZero.reply_to_human to report your progress if you complete a task or the overall requirement... Use RoleZero.ask_human if you failed the current task...
Fifth, describe if you should terminate, you should use end command to terminate if any of the following is met: [3 conditions]
```

This CoT scaffold forces the model to (a) review recent actions, (b) check if current task is done before acting, (c) consider human escalation, and (d) identify termination conditions on every turn. It's what prevents the common "keep doing the same thing" loop.

**`QUICK_THINK_SYSTEM_PROMPT`** (line 148): classifies incoming query as `QUICK` / `SEARCH` / `TASK` / `AMBIGUOUS` BEFORE the main agent loop. Simple questions short-circuit without spawning a multi-step plan.

### 4.9 Code execution constants

`actions/run_code.py:113`: subprocess timeout **10s**. Output truncation: stdout **500 chars**, stderr **10,000 chars** (lines 140-141). Auto-installs `requirements.txt` + `pytest` before every execution.

### 4.10 Data Interpreter kernel constants

`actions/di/execute_nb_code.py`. Jupyter kernel timeout **600s/cell**. `INSTALL_KEEPLEN=500` — only first 500 chars of `pip install` output kept. `INI_CODE` (line 32) suppresses Python warnings and root logger on kernel start — prevents DeprecationWarning spam from filling LLM context.

---

## 5. CAMEL: The inception prompt (the original role-collapse prevention mechanism)

### 5.1 The inception prompts — verbatim

`camel/prompts/ai_society.py:65-107`. The **five load-bearing design choices:**

1. **"Never forget you are {X}"** at the start of EVERY turn — strong role anchor. Without this, models role-flip after ~5-10 turns.
2. **"Never flip roles!"** — explicit prohibition. Models default to being helpful, which means switching to whichever role would be more helpful.
3. **Fixed format envelopes** (`Solution: <YOUR_SOLUTION>` / `Instruction: <YOUR_INSTRUCTION>`) — makes the conversation machine-readable.
4. **`Always end <YOUR_SOLUTION> with: Next request.`** — explicit turn-handover token. Removes ambiguity about whether the assistant is done.
5. **`<CAMEL_TASK_DONE>` as termination sentinel** — the user agent decides termination by emitting a string; termination check is a substring search.

**ASSISTANT_PROMPT** (partial verbatim):
```
===== RULES OF ASSISTANT =====
Never forget you are a {assistant_role} and I am a {user_role}. Never flip roles! Never instruct me!
...
Unless I say the task is completed, you should always start with:
Solution: <YOUR_SOLUTION>
<YOUR_SOLUTION> should be very specific, include detailed explanations...
Always end <YOUR_SOLUTION> with: Next request.
```

**USER_PROMPT** (partial verbatim):
```
===== RULES OF USER =====
Never forget you are a {user_role} and I am a {assistant_role}. Never flip roles! You will always instruct me.
...
When the task is completed, you must only reply with a single word <CAMEL_TASK_DONE>.
Never say <CAMEL_TASK_DONE> unless my responses have solved your task.
```

This design was validated by the original CAMEL paper (2303.17760) to sustain hundreds of role-played turns without role collapse. The patterns are durable because they address a structural problem with LLMs (their "helpful" default), not a specific model version.

**Honest assessment:** this is architecturally useful for GENERATING synthetic training data (the original use case) or for sustained two-agent dialogues. For practical agent task execution, the back-and-forth dialogue model is expensive (2× LLM calls per step) and the user-agent-decides-termination semantics are fragile (the user agent may emit the sentinel too early or too late).

### 5.2 Workforce pause/resume/HITL

`workforce.py:1958-2200`. Full HITL operations:
- `pause()` / `resume()` — `pause_event` propagated to every agent via `_attach_pause_event_to_agent`
- `save_snapshot(description)` / `restore_from_snapshot(idx)` — full state checkpoint
- `modify_task_content(task_id, new_content)` — edit a queued task mid-run
- `reorder_tasks(task_ids)`, `add_subtask(parent_id)`, `remove_task(task_id)`
- `stop_gracefully()` vs `stop_immediately()` — waits for in-flight vs cancels immediately

Every agent created in the Workforce gets a reference to the workforce's `pause_event`. Pausing the workforce atomically pauses every agent between steps.

### 5.3 AgentPool — pre-cloned pool for parallel execution

`single_agent_worker.py:54`. Each `SingleAgentWorker` owns an `AgentPool` of pre-cloned `ChatAgent` instances. Metrics tracked: `_total_borrows`, `_total_clones_created`, `_pool_hits`. Parallel subtasks borrow from the pool; on pool miss, clone a new agent. This is the mechanism that makes CAMEL Workforce scale horizontally within a worker node.

### 5.4 External tools — the approval gate primitive

`chat_agent.py:512`. `external_tools: Optional[List[FunctionTool]]` — tools whose calls are *returned* to the caller (`info["external_tool_call_requests"]`) instead of auto-executed. The agent loop terminates and hands control back to the caller when an external tool is requested. This is the hook for "let me approve before running" workflows and for tools that need to run in a different process/sandbox.

### 5.5 `prune_tool_calls_from_memory` — token saver

`chat_agent.py:511`. `prune_tool_calls_from_memory=False` (default). When `True`, ASSISTANT messages with `tool_calls` are scrubbed from memory after each step. Useful for multi-step agentic workflows where tool-call records bloat the context. Trade-off: the agent loses traceability of its own tool-call history.

---

## 6. TaskWeaver: Code-as-tool-call, AST verification, stateful Jupyter kernel

### 6.1 The core primitive: Python is the tool-call format

`taskweaver/planner/planner.py`, `taskweaver/code_interpreter/code_interpreter/code_generator.py`. Every action is Python code emitted by the LLM as a JSON field (`reply_type: "python"`, `reply_content: "<code>"`). Tools ("plugins") are injected into the kernel's namespace as callable Python functions. The LLM writes `result = my_plugin(arg1, arg2)` — no JSON-schema function call syntax needed.

### 6.2 Planner structured output (verbatim schema)

`planner_prompt.yaml:141-182`. The Planner must output this JSON every turn:
```json
{
  "plan_reasoning": "why this plan",
  "init_plan": "decomposed steps with dependency annotations",
  "plan": "merged plan (sequential/no-dep steps merged)",
  "current_plan_step": "step being executed",
  "stop": "InProcess | Completed | Clarification | AdditionalInformation | SecurityRisks | TaskFailure",
  "send_to": "User or worker name",
  "message": "..."
}
```

`send_to` is runtime-constrained to `workers ∪ {"User"}` and cannot be "Planner" (`planner.py:79-81`). On invalid output: append bad response + `revise_message` to history, loop back to self. `max_self_ask_num=3` before failing.

**Two-phase planning** is the novel ergonomic primitive: `init_plan` (full decomposition with dependency annotations) is then refined into `plan` (merged steps). The agent always shows both, which allows humans to understand how the agent simplified the decomposition.

### 6.3 AST verification — the security primitive

`code_interpreter/code_verification.py:1-288`. `FunctionCallValidator(ast.NodeVisitor)` walks every `Import`, `ImportFrom`, `Call`, `Attribute` node.

**Always-blocked regardless of config** (`DANGEROUS_BUILTINS`, lines 7-23):
```python
DANGEROUS_BUILTINS = [
    "getattr", "setattr", "delattr",
    "vars", "globals", "locals",
    "__getattribute__", "__setattr__", "__delattr__",
    "__dict__", "__class__", "__bases__", "__subclasses__",
    "__mro__", "__builtins__",
]
```

Default `allowed_modules` (`code_interpreter.py:35-43`): `["pandas", "matplotlib", "numpy", "sklearn", "scipy", "seaborn", "datetime", "typing"]`. Default `blocked_functions` (lines 45-65): 18 names including `eval, exec, execfile, compile, open, input, getattr, setattr` etc.

`code_verification_on=False` by default, but **auto-flipped to True when `kernel_mode="local"`** (lines 113-122) with a warning. Container mode without verification is somewhat safer due to isolation.

On verification failure: bounce back to CodeInterpreter with formatted error + `revise_message`. Retry up to `max_retry_count=3`. After 3 failures: propagate failure to Planner.

### 6.4 Container kernel — the `extra_hosts` security primitive

`ces/environment.py:272-278`. The container kernel blocks the "magic domain" container→host escape:
```python
extra_hosts={
    "host.docker.internal": "0.0.0.0",
    "host.containers.internal": "0.0.0.0",
    "host.lima.internal": "0.0.0.0",
}
```

This prevents LLM-generated code inside the container from reaching back to the host's `localhost`-bound services (Postgres, Redis, local APIs). Docker Desktop on macOS/Windows automatically resolves `host.docker.internal` to the host IP — without this block, a malicious prompt could exfiltrate data or call internal APIs.

Auto-update of kernel image: compares local SHA against registry SHA on every session start; pulls if outdated (`environment.py:161-171`). Default image: `taskweavercontainers/taskweaver-executor:latest`.

Container readiness timeout: **30s** (`environment.py:281-290`). UID/GID propagation on non-Windows — avoids "everything is root-owned" volume bug.

### 6.5 Plugin namespace injection

`plugin/` + kernel `ctx_magic.py`. Plugins are Python files decorated with `@register_plugin`. The kernel's `%load_plugin` magic `exec()`s the plugin's source in the kernel's namespace, binding the `__call__`-decorated class as a top-level callable. The LLM calls plugins as plain Python function calls. This is the cleanest tool-call model in the batch: tools ARE Python functions; the LLM writes Python.

**`plugin_only` variant** (`code_interpreter/code_interpreter_plugin_only/`): restricts the agent to ONLY call registered plugins. No `import` statements, no free-form code. This is the appropriate sandbox for customer-facing agents with a curated tool set.

### 6.6 PluginSelector — embedding-based dynamic plugin selection

`code_interpreter/plugin_selection.py:128-156`. Default `top_k=3` (`CodeGeneratorConfig:47`). Top-k by cosine similarity to the user query; no threshold (unlike the experience retrieval). `SelectedPluginPool` keeps track of which selected plugins were ACTUALLY USED in generated code; unused plugins are evicted from the pool, used plugins persist across turns. Embeddings cached in `meta_<plugin>.yaml` keyed by md5 of the plugin spec.

`enable_auto_plugin_selection=False` by default — all plugins go into the prompt. Activate when plugin count exceeds ~10.

### 6.7 RoundCompressor — sliding window summary

`memory/compression.py:14`. Defaults: `rounds_to_compress=2, rounds_to_retain=3`. Compression triggers when there are ≥ 5 un-compressed rounds. Produces a string summary that replaces all but the last 3 rounds in the prompt. `previous_summary` is accumulated; `processed_rounds` tracks already-summarized rounds. Incremental: each run adds to the prior summary rather than starting fresh.

### 6.8 Experience replay (opt-in)

`memory/experience.py:43`. `retrieve_threshold=0.2` cosine similarity minimum. After a successful task, `ExperienceGenerator` summarizes "what worked" as a text lesson. At query time: embed the query, retrieve top-k above threshold, inject as `experience_instruction` preamble in the Planner prompt: "Before starting planning, please refer to the following experiences and lessons learned from the previous tasks and include them in your plan." Default: `use_experience=False`.

### 6.9 Constants

| Constant | Value | Source |
|---|---|---|
| `max_internal_chat_round_num` | 10 | `session.py:22` |
| `Planner.max_self_ask_num` | 3 | `planner.py:84` |
| `CodeInterpreter.max_retry_count` | 3 | `code_interpreter.py:28` |
| `TRUNCATE_CHAR_LENGTH` | 1500 | `code_executor.py:14` |
| `allowed_modules` | pandas, matplotlib, numpy, sklearn, scipy, seaborn, datetime, typing | `:35-43` |
| `DANGEROUS_BUILTINS` | 16 names | `code_verification.py:7-23` |
| Container ports | 12345-12349 (shell/iopub/stdin/hb/control) | `kernel/launcher.py:22` |
| Container readiness | 30s timeout | `environment.py:281-290` |
| Experience `retrieve_threshold` | 0.2 | `memory/experience.py:54` |

---

## 7. Cross-cutting patterns — what survives model generations

### 7.1 Structured output with explicit required-all schema

Agency Swarm, TaskWeaver, and Magentic-One all enforce JSON schemas with `additionalProperties=false` and ALL fields in `required`. This is the only reliable way to get consistent structured output across models. Agency Swarm's comment is verbatim: "OpenAI API requires all properties in 'required' array, even optional ones" — use empty string as the sentinel for optional string fields.

### 7.2 Self-correction loops with a hard cap

All five frameworks have an LLM self-correction loop for malformed output:
- Agency Swarm: `validation_attempts=1` (one retry on output guardrail failure)
- MetaGPT: `write_code` has 6 retries with exponential backoff (1-60s)
- Magentic-One: `_max_json_retries=10` for progress ledger parsing
- TaskWeaver: `max_self_ask_num=3` (Planner) + `max_retry_count=3` (CodeInterpreter)
- CAMEL: `ChatAgent.step_timeout` per step + `max_tool_calling_iters`

The cap is critical. Without it, a model stuck in a format error loop burns tokens indefinitely. The universal pattern: append bad output + error message to conversation history → re-invoke same LLM → repeat up to N times → fail hard.

### 7.3 Context truncation before LLM calls

Every production system truncates raw execution output before feeding it back. Constants: TaskWeaver 1500 chars, MetaGPT stdout 500 / stderr 10,000. Without truncation, a single `pip install` or stack trace can consume the entire context window.

### 7.4 Budget / turn caps as mandatory architecture

`max_turns`, `max_stalls`, `max_internal_chat_round_num`, `test_round_allowed`, `investment` — every framework has multiple hard caps. Missing any one cap converts a failed agent run into an infinite token burn. The minimum set for any agent system: turn cap (hard LLM call limit), stall/loop detector (semantic progress check), and cost cap (USD ceiling).

### 7.5 Star topology beats peer-to-peer for auditability

Magentic-One (star, orchestrator at center) and TaskWeaver (star, Planner at center) are the most auditable architectures. Peer-to-peer (MetaGPT pub/sub) and tool-recursive (Agency Swarm) are harder to trace and debug. Star topologies have a natural single point for logging, permission checks, and HITL interrupts.

### 7.6 Tools-as-code (TaskWeaver) vs tools-as-JSON (everyone else)

TaskWeaver's insight: Python is a better tool-call format than JSON schema for data manipulation tasks because (a) the LLM knows Python natively, (b) composition is trivial (variables flow between calls), and (c) the stateful kernel gives persistent state across turns. The trade-off: AST verification is required for safety; JSON-schema tools can validate inputs structurally without trusting the LLM.

### 7.7 Deprecated / tactical hacks — DO NOT copy

- **Role-playing dialogue as a production task execution primitive** (CAMEL's RolePlaying) — works for synthetic data generation; impractical for production tasks (2× cost, fragile termination). Durable only as a training-data technique.
- **CAMEL inception prompts for general agents** — useful technique but 2023-era; modern models (GPT-4o, Claude 3+) have strong enough instruction following that explicit role-collapse prevention is rarely needed outside edge cases.
- **MetaGPT's fixed SOP** (old `use_fixed_sop=True` path) — the current production path (MGX/RoleZero) uses tool calling. The fixed SOP with PRD→Design→Code is a demo artifact, not the production system.
- **Agency Swarm's `AgentFlow.__bool__` hack** — works, but rely on explicit tuple lists or a proper builder API for any new framework.

---

## 8. Lancelot byte-path lens

| Primitive | Lancelot coverage | Gap |
|---|---|---|
| Star topology dispatcher | `TransportPool.fanout` + `spawn_child` provide the byte-path substrate | Need the orchestration layer: ledger pattern, stall counter, typed progress JSON, re-plan on stall |
| Per-agent outbound calls | `TransportPool.request` + auto-inject request_id/traceparent/agent_id | Already done |
| Cost cap / turn cap | `lancelot.context.cost_usd` ContextVar accumulates; no hard cap + early-exit logic yet | Need: `max_cost_usd` ceiling check before each turn |
| Structured output enforcement | `lancelot.tool` / `@app.tool` produces JSON schema; `ToolSpec.output_schema` | Need: Pydantic-validated `LedgerEntry`-style progress JSON per turn |
| Streaming agent output | `@app.stream` (R3 pump, TTFT, backpressure) | Already done |
| Context window management | Not present | Need: round compressor / sliding window summary |
| Stateful code execution | Not Lancelot's domain (AI-logic boundary) | TaskWeaver's kernel is user-side |
| Per-agent concurrency guard | `lancelot.tenant.TenantConcurrencyLimiter` (per-tenant semaphore) | Could reuse; exact semantics differ |
| Tool-call self-correction loop | Not present | Need: N-retry loop with error feedback to LLM |
| Agent-to-agent comms as tool call | `TransportPool.spawn_child` is the byte-path analogue | Need: thin `SendMessage`-style FunctionTool wrapper that calls `spawn_child` |
| Budget cap as exception | `lancelot.ratelimit` (rate limiting); no USD cost exception | Need: `NoMoneyException`-style hard stop + `X-Lancelot-Cost-USD` header already emitted |
| State persistence / resume | `lancelot.secrets` (config), no agent-state checkpoint | Need: `MagenticOneOrchestratorState`-style Pydantic serialization |
| HITL pause/resume | Not present | Need: pause_event propagation pattern (CAMEL Workforce model) |

**The specific Magentic-One dual-ledger pattern is the highest-value transfer.** Lancelot's `TransportPool` already handles the byte path for the inner-loop LLM calls; what's missing is the structured orchestration wrapper: one LLM call per turn that answers `{is_satisfied, is_looping, is_progressing, next_agent, instruction}` as typed Pydantic JSON, driving dispatch via `TransportPool.fanout`/`spawn_child`. The stall counter + re-plan outer loop is a pure Python mechanism with no transport dependency.

The **TaskWeaver AST verifier + `extra_hosts` block** are directly relevant for any Lancelot endpoint that exposes code execution. Both are pure Python (the verifier) and Docker configuration (the host block) — no transport changes needed.

The **Agency Swarm per-thread concurrency guard** is directly reusable for any endpoint that dispatches to multiple sub-agents: `asyncio.Lock` + `dict[thread_id, set[agent_name]]`, refuse with user-readable error message.

---

## 9. API shape notes worth stealing

**Agency Swarm SendMessage required-even-if-optional:**
All fields in `required` + use `""` as the optional-string sentinel. The LLM will never omit a required field; it will omit an optional one, breaking structured output parsers.

**Magentic-One `reason` field on every JSON answer:**
Each answer in `LedgerEntry` is `{reason: str, answer: bool|str}`. The `reason` is chain-of-thought; the `answer` is the machine-parseable value. This pattern makes it easy to debug why the orchestrator made a decision without adding a separate verbose reasoning step.

**MetaGPT `cause_by` as routing key:**
Using the producing Action class's FQN as the message topic is cleaner than explicit string topics. Agents subscribe to Action types, not to other agent names. This decouples the topology from the agent registry.

**TaskWeaver `send_to ∈ workers ∪ {"User"}` runtime constraint:**
Runtime-enforce the `send_to` enum against the actual registered workers. The LLM cannot address a non-existent agent. Validate at Planner response parse time, not just in the schema — schemas don't prevent hallucinated agent names.

**CAMEL `<SENTINEL_DONE>` termination:**
For two-agent loops, a string sentinel is the cheapest termination mechanism. The user agent decides termination; the termination check is a substring search. For N-agent orchestration, the Magentic-One `is_request_satisfied` JSON field is cleaner.
