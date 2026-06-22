# Lancelot Agent Layer — **Tools** Synthesis Brief

**Layer:** tools (registration → schema → dispatch → repair → permission → result discipline)
**Altitude:** "just build agents, brutal." The smallest tool primitive that earns its keep.
**Date:** 2026-06-22
**Author:** tools layer-synthesizer
**Inputs:** all `02_core/`, `03_teardowns/`, `04_byte_path/` notes + ground-read of `lancelot/tool.py`, `lancelot/mcp.py`, `lancelot/app.py`.

---

## 0. The one-paragraph decision

Lancelot already owns the **tool *substrate*** — `@app.tool` → `ToolSpec` (with unenforced `read_only`/`scopes` fields), Pydantic-derived `input_schema` + `output_schema`, `app.tool_schemas()` (Anthropic/OpenAI/MCP wire shape), `app.call_tool()`, and `lancelot.mcp.mount` with the 4 KB warn. What it lacks is the **dispatch discipline** every production agent converged on: a `call_tool` that **never raises** but returns `{data, error, ok}`; a **4-stage input-repair ladder** + **fuzzy-name correction (>0.85)** + **repeated-call short-circuit**; **annotation bits** (`read_only`/`destructive`/`idempotent`) wired to a **permission gate** (`allow/deny/ask` + a single pre-tool hook); **bounded-parallel reads (5) with order-preserved results + sequential writes (action dedup)**; and two builtins (`think`, `done`) so we ship ~zero domain tools. Build exactly that thin invocation layer on top of the existing `ToolSpec`. **REUSE** the byte path for everything outbound (`TransportPool.fanout` is the parallel-tool primitive; `assert_safe_url` gates fetch tools; `dumps_canonical` stabilizes the schema block for KV-cache). **REFUSE** MCP-as-default (demote to `lancelot.mcp.mount`, opt-in), the BM25/dependency-graph tool router, the ReAct text-parse path, code-action sandbox in v1, and a per-provider schema-munge matrix.

---

## 1. What the corpus converged on (the durable tool primitives)

These recur across ≥3 independent production systems and survive the model clock. Each is mechanism-level + cited.

### 1.1 Dispatch ALWAYS returns `{data, error, ok}` — never raises (the #1 invariant)

The single most-repeated tool-layer mechanism. A tool error is **data the model reads and recovers from**, not an exception that kills the loop.

- **Composio** (`composio.md` §3, `tools.py:84-87`): every execution — remote, custom, meta — returns the same `ToolExecutionResponse = {data: dict, error: str|None, successful: bool}`. Pydantic validation failure → `{data:{}, error:"Input validation failed: …", successful:False}`, never a raise.
- **DeepLearning.AI transcripts** (`transcripts_dlai.md` §5.1, AWS/Strands + Anthropic): *"Informative error messages are not developer UX — they are the primary self-correction mechanism."* and *"the underinvested half is feedback."* The error string is the corrective prompt.
- **smolagents** (`smolagents.md` §4, `agents.py:594-599`): two-tier taxonomy — `AgentError` (bad model output) → log into the step's `error` field, render into next turn, **continue**; `AgentGenerationError` (framework/API bug) → **exit**. The error includes the exact failing source segment (`ast.get_source_segment`), not a generic traceback.
- **Pydantic AI** (`pydantic_ai.md` §2, `tool_manager.py:183-191`): the retry payload IS the structured Pydantic error — `ValidationError.errors(include_url=False, include_context=False)` sent back verbatim. *"Show the model its own mistakes."*

**Ground truth:** Lancelot's current `app.call_tool` (`app.py:430-440`) does `spec._validator(**kwargs).model_dump()` then `spec.func(**validated)` — it **raises** `KeyError` on a bad tool name and `ValidationError` on bad args. That is the exact wrong behavior for an agent loop. This is the headline BUILD.

### 1.2 The 4-stage input-repair ladder + fuzzy-name (>0.85) + repeated-call short-circuit

The highest-value reusable mechanism in the entire corpus — and **model-independent** (it's a function of LLM output fragility, not capability).

- **CrewAI** (`crewai.md` §2, `tool_usage.py`) — copy verbatim:
  - Fuzzy name (`_select_tool`, `:732`): `SequenceMatcher(None, sanitize(t.name), sanitize(asked)).ratio()`, threshold **> 0.85**. Silently fixes `web_serch → web_search`. Below threshold → error lists valid tools (re-injected so the model self-corrects).
  - 4-stage `_validate_tool_input` (`:857`): `json.loads` → `ast.literal_eval` → `json5.loads` → `repair_json`. Every malformed arg blob runs all four before failing.
  - Repeated-call short-circuit (`:701`): if `(tool_name, args) == last_used_tool` → return `"I tried reusing the same input, I must stop using this action input."` Degenerate-loop guard, model-independent.
  - Constants: `_max_parsing_attempts=3` (2 for big models); `_remember_format_after_usages=3`.
- **smolagents** `fix_final_answer_code` (`smolagents.md` §7): bare-expression last statement → auto-wrap `final_answer(<expr>)`. The "weak-model repair" pattern generalizes.

**Honest dependency note:** `json5` and `json_repair` are third-party. Cardinal rule = pydantic-only runtime. So stages 3-4 must be **lazy-imported behind a try/except that degrades gracefully** (if absent, the ladder is `json.loads → ast.literal_eval` only — stdlib, always present). `SequenceMatcher` is `difflib` (stdlib). `ast.literal_eval` is stdlib. The core ladder ships dep-free; the two extra rungs activate iff the operator has the libs.

### 1.3 Tool annotation bits → permission routing (`lookup`/`action`, readOnly/destructive/idempotent)

Every vertical production agent makes the **Lookup vs Action** split a *required* schema field, not a convention — because it's a correctness invariant for retry safety.

- **Vertical agents** (`vertical_agents.md` §1, SIERRA/DECAGON/HARVEY/WEDGE): two and only two tool classes — **Lookup** (read-only, retry-safe) vs **Action** (side-effects, **DO NOT RETRY**, "issuing a refund twice" is the canonical anti-pattern). Sierra enforces `kind` at **schema registration** (a linter fires when name/description suggest one but it's classified the other). Runtime: the executor refuses to re-run an action already in `session.committed_actions`.
- **MCP annotation semantics** (`memory_vector.md` §5, turbopuffer; `claude_agent_sdk.md` Mech 4): `readOnlyHint: true` lets the client **optimistically parallelize**; non-readOnly = mutation = serialize. The two-tool split (`search_docs` readOnly / `execute` mutating) is the canonical shape.
- **Claude Agent SDK** (`claude_agent_sdk.md` Mech 3): 4-value `permissionDecision: allow | deny | ask | defer` (`defer` passes to the next hook). `PreToolUse` can `updatedInput` (rewrite args before exec) and `additionalContext`; `PostToolUse` can `updatedToolOutput` (rewrite result before the model sees it).
- **Strands** (`strands.md` §4): `BeforeToolCallEvent` can **swap `selected_tool`, rewrite input, or cancel** — "the clean way to implement allowlists, sandboxing, rate limits, dry-run mode without touching the loop."
- **Factory 5-tier** (`autonomous_coding.md` §10): `READ_ONLY / LOW / MEDIUM / HIGH / UNSAFE`. Fail-fast: exceeding the tier → stop, non-zero exit, no partial changes.

**Ground truth:** `ToolSpec` already has `read_only: bool` and `scopes: list` (`tool.py:29-30`) — *unenforced anywhere*. The BUILD is: add `destructive`/`idempotent` (default-derived from `read_only`), and a single pre-tool permission hook that reads them. The annotation bits already auto-flow to `tool_schemas()` for the wire (`readOnlyHint`).

### 1.4 Bounded-parallel reads (5) + order-preserved results + sequential writes

The universal concurrency contract: parallelize independent reads, cap the fan-out, keep transcript order deterministic, serialize side-effects.

- **Composio** (`composio.md` §2): `MAX_PARALLEL_WORKERS = 5` (client-side `ThreadPoolExecutor`) — the local fan-out cap. (Server batch cap is 50; the agent-loop number is **5**.)
- **Order-preservation regardless of completion order** — the *key invariant*, cited 4×:
  - Pydantic AI (`pydantic_ai.md` §5, `_agent_graph.py:1916`): results appended in `sorted(call_index)` order. *"Parallel execution, deterministic transcript. Missing it makes replays non-deterministic."*
  - LlamaIndex (`langgraph`-grep, `llamaindex.md`): `ctx.collect_events(expected=[ToolCallResult]*N)` returns in declared order.
  - Cursor (`leaked_prompts.md` §6): *"DEFAULT TO PARALLEL… sequential ONLY when output of A is input to B. 3-5× faster."*
- **Sequential writes / action dedup** — Manus one-tool-per-turn for the *trace* (`manus.md` §1.1); vertical agents' `committed_actions` set (`vertical_agents.md` §1). Side-effecting calls run one-at-a-time and a `(name, canonical_args)` fingerprint set refuses dupes within a session.

**Byte-path reuse:** `TransportPool.fanout(coros, first_n=, deadline=, cancel_laggards=True, concurrency=5)` is the outbound primitive for parallel *tool→HTTP* calls. The tools layer wraps it with the **read/write classification + ordered re-join** (fanout returns completion-order; the tools layer re-sorts to call-index order — that's agent-layer glue, ~10 lines).

### 1.5 `validate()`-before-side-effect (the two-phase action guard)

- **Gordian** (`vertical_agents.md` §3): `POST /basket/check` → poll `GET /basket` (30×2s = 60s ceiling) → only `POST /fulfill` after revalidation confirms the basket is still clean (price unchanged, items available). 6-minute safety margin before `valid_until`. `last_action` field logs the exact step for idempotent retry detection.
- **OpenAI Operator** (`openai_agents.md` §9): `pending_safety_checks` on every `computer_call` — `[{code, message}]` (codes: `financial_action`, `email_send`, `pii_disclosure`, …) emitted before risky actions; runtime blocks until acknowledged.

**Shape:** a tool registered as `action` can declare `validate=<callable>`; the executor runs `validate(args)` immediately before `func(args)` and aborts (returns `{ok:False, error:...}`) on a stale/failed check. Not a separate framework — a kwarg on the action tool + an executor branch.

### 1.6 `think` + `done` builtins (ship ~0 domain tools)

The framework ships **two** tools, both trivial; everything else is the user's domain.

- **`think`** — no-op reflection sentinel. NIA (`nia.md` §2, verbatim): `{"action":"think","args":{"reflection":...}}` → fixed return `"Reflection recorded. Continue with next action."` The content never hits any server; it (1) keeps the JSON-action invariant, (2) exposes CoT on the wire, (3) **is the confidence gate** before `done`. OpenHands `ThinkTool` (`autonomous_coding.md` §9): `think(thought)` → `"Your thought has been logged."` agno `ReasoningTools` (`agno.md`): `think(title, thought, confidence=0.8)`. Devin's 13-trigger think (`devin_warp.md` §3) shows *when* to think, but the **tool is a 1-line no-op**.
- **`done` / `final_answer`** — the loop-termination sentinel as a tool call, not a magic string.
  - Pydantic AI (`pydantic_ai.md` §7): output tool named **`final_result`**, desc `"The final response which ends this conversation"`. Model "returns" by calling it; args validated against the output schema. After it fires, send a synthetic `"Final result processed."` ack.
  - smolagents `FinalAnswerException` (`smolagents.md` §2): `final_answer()` raises a catchable exception → `(value, is_final_answer=True)`. No separate protocol.
  - Haystack `exit_conditions=["write_github_comment"]` (`transcripts_dlai.md` §5.4): *"define WHICH tool call signals completion"* rather than relying on the model to say "I'm done."

**Why this is the brutal default:** the BAD_GUIDE positioning (`bad_guide_build_positioning.md` §3) — *"every abstraction is a liability… delete the harness so a smarter model's overhang isn't capped."* We give the agent `think` + `done` + the user's domain tools. We do **not** ship 30 pre-built tools (CrewAI's 30+ is named as scope-creep bloat, `crewai.md` §13).

### 1.7 Schema-from-signature with docstring→description injection

- **Pydantic AI** (`pydantic_ai.md` §8, `_function_schema.py`): `griffe`-parsed docstring (Google/NumPy/Sphinx) injects `arg: description` into each property; `use_attribute_docstrings=True` picks up field docstrings; first `RunContext` param stripped from schema. *"Cheap and materially improves model tool-use."*
- **Composio `enhance_schema_descriptions`** (`composio.md` §7): appends `"Please provide a value of type <type>."` + `"This parameter is required."` to each property. **Flagged depreciating** — a prompt nudge that the next model generation likely doesn't need.
- **OpenAI `ensure_strict_json_schema`** (`openai_agents.md` §10): optional fields → `T | null` required (not `required:false`); `additionalProperties:false`; no mixed-nullable `anyOf`; inline `$ref`. *"Painful to derive; lift as-is."* CrewAI corollary (`crewai.md` §10): `model_config = ConfigDict(extra="forbid")` is **mandatory** for OpenAI structured output.

**Verdict:** `_build_tool_schema` (`tool.py`) already does the Pydantic `TypeAdapter().json_schema()` derivation — the docstring-description injection is a cheap **EXTEND** (parse `func.__doc__`; no `griffe` dep needed — a stdlib regex over the param section is enough). `extra="forbid"` and the strict-schema transform should be an **opt-in `strict=True`** on `@app.tool` (provider-conditional; default off to stay verbatim like Anthropic/Google).

---

## 2. REUSE — what the byte path already gives the tools layer (do NOT rebuild)

Grounded in `04_byte_path/lancelot_serve_tools.md` + `lancelot_model_io.md` + direct file read.

| Tool-layer need | Byte-path primitive (file) | How the tools layer uses it |
|---|---|---|
| Tool registration + JSON-Schema | `ToolSpec` + `_build_tool_schema` (`lancelot/tool.py`) | The dataclass IS the registry. `read_only`/`scopes` fields **already exist** (unenforced) — wire them. |
| Tool list for the prompt | `app.tool_schemas()` (`lancelot/app.py:392`) | Returns `{name, description, input_schema[, output_schema]}` = exact Anthropic/OpenAI/MCP wire. |
| Tool execution (validated) | `app.call_tool(name, /, **kwargs)` (`app.py:430`) | The *call site* to wrap with `{data,error,ok}` + repair + dedup. Pydantic `_validator` reused as repair stage 0. |
| Parallel tool→HTTP fan-out | `TransportPool.fanout(first_n,deadline,cancel_laggards,concurrency)` (`llm.py:800`) | The bounded-parallel-reads engine (set `concurrency=5`). Returns completion-order → tools layer re-sorts to call-index. |
| Sub-agent / agent-as-tool I/O | `TransportPool.spawn_child(parent, session_affinity)` (`llm.py:905`) | Snapshots traceparent/request_id/agent_id before Task reset — context isolation for agent-as-tool. |
| Fetch/browse tool URL safety | `assert_safe_url` / `SSRFError` (`lancelot/net.py`) | **MANDATORY** before any user-URL tool hits TransportPool. Covers CGNAT + IPv4-mapped-IPv6 that naive `is_private` misses. |
| Schema-block KV-cache stability | `dumps_canonical(obj)` (`lancelot/canonical.py`) | Serialize the tool-schema block with this so the prefix is byte-stable (Manus mask-don't-remove, `manus.md` §1.2). Also the dedup fingerprint: `sha256(dumps_canonical({name,args}))`. |
| Provider credential access | `lancelot.secrets.get("OPENAI_API_KEY")` (`lancelot/secrets.py`) | Tools read creds here (not `os.environ`); SIGHUP rotation + audit hook for free. |
| Large tool-output detection | 4 KB warn in `mcp.py:109` (`lancelot/mcp.py`) | The threshold to **enforce** (truncate + handle-and-fetch) at the tools layer, not just warn. |
| Tool result truncation budget | `@app.batch` `size_fn`/`max_size` (`app.py`) | Token-budget flush logic reusable for "trim tool output to N tokens." |
| Per-step cost/token accounting | `cost_usd`/`tokens_in/out` ContextVars (`context.py`) + `upstream_metrics.classify_status` | Tool loop reads these for budget gates; `classify_status(422)→permanent` = "fix the tool args, don't retry." |
| Inbound tool-call rate cap | `lancelot.ratelimit.RateLimit` + `lancelot.tenant` | Per-tenant tool-call density control rides existing middleware. |
| Telemetry hook for tool spans | `telemetry.set_sink` / `emit` (`telemetry.py`) | Emit `tool_start`/`tool_end` events; Strands-style hooks compose here. |

**Non-rebuild rules (Tier-D anti-patterns from the notes):**
1. Do NOT add a per-chunk cross-thread bridge to stream tool output — stay on the R3 `sse_adapter` path (`lancelot_serve_tools.md` anti-pattern #1; the 16–26× cliff).
2. Do NOT fetch user URLs without `assert_safe_url` (anti-pattern #2).
3. Do NOT emit raw `str(exc)` as tool error — scrub before it enters context (anti-pattern #3; `mcp.py` currently does this).
4. Do NOT rebuild fanout/retry/Retry-After — `TransportPool` owns it; **POST is never retried** (`llm.py:17`), so the agent loop re-issues at the *loop* level, never wrapping the HTTP call in its own retry.

---

## 3. BUILD — the thin invocation layer on top of `ToolSpec`

A single new module `lancelot/agent/tools.py` (~250 LoC) — the dispatch discipline. No new runtime deps (json5/json_repair lazy-optional).

### 3.1 `ToolSpec` extensions (one-field-ish schema growth, on the existing dataclass)

```python
# extend lancelot/tool.py ToolSpec (already has read_only, scopes, input/output_schema)
kind: Literal["lookup", "action"] = "lookup"     # vertical_agents §1 — REQUIRED semantic
destructive: bool = False                          # claude_agent_sdk Mech3 annotation bit
idempotent: bool = True                            # default True for lookup; action defaults False
validate: Callable | None = None                   # vertical_agents §3 — pre-side-effect check
max_result_bytes: int = 4096                        # claude_agent_sdk _build_meta; enforce, not warn
```
Registration-time lint (Sierra pattern): warn if `kind="lookup"` but name/desc match an action verb set (`create|delete|send|pay|update|write|post`), or `LANCELOT_TOOL_STRICT=1` → raise. `tool_schemas()` auto-emits `annotations: {readOnlyHint, destructiveHint, idempotentHint}` from these bits (MCP wire).

### 3.2 `dispatch()` — the never-raises executor (the core BUILD)

```python
def dispatch(app, name: str, raw_args, *, session: ToolSession) -> dict:
    """ALWAYS returns {"data": Any, "error": str|None, "ok": bool}. Never raises."""
    # 1. fuzzy name resolve (>0.85) — difflib, stdlib
    spec = _resolve_tool(app, name)          # miss → {data:{}, error:"unknown tool 'x'; valid: [...]", ok:False}
    # 2. repeated-call short-circuit
    fp = sha256(dumps_canonical({"n": spec.name, "a": raw_args}))   # canonical.py reuse
    if fp in session.last_call_fps:
        return {"data":{}, "error":"Identical call already made; do not repeat — try a different input or call done.", "ok":False}
    # 3. 4-stage repair ladder → dict args
    args, perr = _repair_args(raw_args)      # json.loads→literal_eval→[json5]→[repair_json]
    if perr: return {"data":{}, "error": f"Could not parse arguments: {perr}", "ok":False}
    # 4. Pydantic validate (reuse spec._validator) → structured error AS the prompt
    try: kw = spec._validator(**args).model_dump()
    except ValidationError as e:
        return {"data":{}, "error": _fmt_pydantic(e), "ok":False}   # errors(include_url=False)
    # 5. permission gate (allow/deny/ask) — annotation-driven hook
    decision, kw = session.permit(spec, kw)  # ask→{data:{"__ask__":...}, ok:False}; deny→error
    if decision == "deny": return {"data":{}, "error":"Tool call denied by policy.", "ok":False}
    # 6. validate-before-side-effect (action tools)
    if spec.kind == "action" and spec.validate:
        vok, vmsg = _run_validate(spec.validate, kw)
        if not vok: return {"data":{}, "error": f"Pre-execution check failed: {vmsg}", "ok":False}
    # 7. execute; scrub + truncate result
    try: out = spec.func(**kw)
    except Exception as e:                    # tool body raised
        return {"data":{}, "error": _scrub(str(e)), "ok":False}     # net.py anti-pattern #3
    if spec.kind == "action": session.committed.add(fp)
    session.last_call_fps.add(fp)
    return {"data": _truncate(out, spec.max_result_bytes), "error": None, "ok": True}
```

### 3.3 `dispatch_batch()` — bounded-parallel reads, ordered, sequential writes

```python
async def dispatch_batch(app, calls: list[ToolCall], *, session) -> list[dict]:
    reads  = [c for c in calls if app._tools[...].kind == "lookup"]
    writes = [c for c in calls if ...kind == "action"]
    # reads: parallel, capped at 5, RE-SORTED to call-index order (pydantic_ai §5 invariant)
    read_results = await _bounded_gather(reads, concurrency=5)       # via asyncio.Semaphore(5)
    read_results.sort(key=lambda r: r.call_index)
    # writes: strictly sequential (vertical_agents §1)
    write_results = [dispatch(app, w.name, w.args, session=session) for w in writes]
    return _merge_by_index(read_results, write_results)
```
(For tool→HTTP reads specifically, `TransportPool.fanout(..., concurrency=5)` is the engine; the wrapper re-orders.)

### 3.4 `ToolSession` — per-loop dedup + permission state

```python
@dataclass
class ToolSession:
    permit: Callable[[ToolSpec, dict], tuple[str, dict]] = _default_allow  # allow/deny/ask + updated_input
    last_call_fps: set[str] = field(default_factory=set)   # repeated-call short-circuit
    committed: set[str] = field(default_factory=set)        # action dedup (vertical_agents §1)
```
`permit` is the one hook (Strands `BeforeToolCallEvent` / Claude SDK `can_use_tool` shape). Default `allow`. Operator supplies `lambda spec, kw: ("ask", kw) if spec.destructive else ("allow", kw)` for HITL. `defer` not in v1 (single hook, not a chain — YAGNI per altitude).

### 3.5 The two builtins (registered once, ~6 lines each)

```python
@app.tool(description="Record a reflection / reasoning step. No side effects.", read_only=True)
def think(reflection: str) -> str:                       # nia.md §2 verbatim return
    return "Reflection recorded. Continue with next action."

@app.tool(description="Signal the task is complete and return the final answer.", read_only=True)
def done(answer: str) -> str:                            # pydantic_ai final_result + Haystack exit_condition
    raise _DoneSignal(answer)                             # caught by the loop → StopReason.completed
```
`done` is the named exit condition (the loop catches `_DoneSignal`, à la smolagents `FinalAnswerException`). `think` content surfaces on the wire (`tool_start.args.reflection`) for observability but is a no-op handler.

### 3.6 Result discipline (truncate + handle-and-fetch)

`_truncate(out, max_result_bytes)`: serialize via the same pydantic-core path as unary responses; if over budget, return a **handle** `{"__handle__": id, "preview": first_N, "bytes": total}` and stash the full blob in a per-session store keyed by id, exposing a `fetch_tool_result(handle)` builtin (Dex Horthy handle-and-fetch, `claude_agent_sdk.md` Mech4 + `lancelot_serve_tools.md` anti-pattern #5). Default `max_result_bytes=4096` (Lancelot's existing 4 KB constant — now *enforced*, not warned).

---

## 4. REFUSE — off-thesis, depreciating, or overkill

| Refuse | Why (cited) |
|---|---|
| **MCP as the default tool surface** — demote to opt-in `lancelot.mcp.mount` | Claude Agent SDK + every teardown treat MCP as *one* transport, not the core. Composio (`composio.md`) hides 10K tools behind 6 meta-tools — the agent rarely sees raw MCP. The `_native.run` server emits no MCP-side metrics (`lancelot_serve_tools.md` §4 scope note). MCP mount already exists; keep it deferred. Cardinal-rule clean: no MCP dep pulled by `import lancelot`. |
| **BM25 / dependency-graph tool router** (Composio's planner) | `composio.md` §4/§10: BM25 k1/b + the dependency-graph service are **server-side black boxes**; the recipe-cache + per-app memory is a from-scratch build. Off-altitude — that's a tool *marketplace*, not "build agents brutal." For Lancelot's expected tool counts (≤30-40, Sierra ceiling, `vertical_agents.md` §5), the full schema list fits the prompt; no search layer needed. Deferred-tool/ToolSearch (`claude_code.md` §4) is the *future* lazy-catalog option if a user wires 100+ tools — flagged, not built. |
| **ReAct text-parsing tool loop** | CrewAI (`crewai.md` §1/§13): the regex `Action:`/`Thought:` path is a *depreciating tactical hack* for models without native function-calling. All modern providers converged on native tool_calls. Build **one** loop: native tool-call. |
| **Code-action sandbox (AST interpreter) in v1** | smolagents (`smolagents.md` §3): the `LocalPythonExecutor` (10M-op / 1M-while / 30s bounds, `nodunder_getattr`, `get_safe_module`) is genuinely durable — but it's a *large* security surface and multi-tenant → remote executor anyway. `transcripts_dlai.md` §13 flags the sandboxing requirement as a deployment constraint. **Defer**; the `{data,error,ok}` tool path covers JSON-tool-call agents (the common case) at a fraction of the risk. (If built later, `allow_pickle=False` is the hard default, `smolagents.md` §10.) |
| **Per-provider schema-munge matrix (~23 transformers)** | Composio (`composio.md` §14/§16): *"depreciating tactical — as frameworks standardize on OpenAI function-calling shape (Anthropic+Google now support it), the per-provider munge becomes unnecessary."* Ship one canonical schema (`{name, description, input_schema, output_schema}`) + an **opt-in `strict=True`** for OpenAI's `additionalProperties:false`/nullable rules. Message-shape *translation* is the model-adapter layer's job, not tools'. |
| **`enhance_schema_descriptions` prompt-nudge** | Composio (`composio.md` §16): appending "Please provide a value of type X" is *"model-generation-dependent; likely irrelevant at next-gen capability."* Skip; rely on real docstrings (§1.7). |
| **`defer` as a 4th permission value + multi-hook chain** | Claude SDK ships it (`claude_agent_sdk.md` Mech3), but it only matters with a *chain* of composable hooks. Altitude says one `permit` hook. `allow/deny/ask` covers HITL. Add `defer` only if multi-hook composition is ever needed. |
| **5-tier autonomy (Factory) in v1** | `autonomous_coding.md` §10 is clean, but 5 tiers is OS/shell-permission machinery for coding agents. Lancelot's agent layer maps `kind=lookup→allow` / `kind=action+destructive→ask` — 2 effective tiers via the annotation bits. The 5-tier enum is a future `AgentAutonomyLevel`, not core. |
| **Confidence *router* as a tools primitive** | `vertical_agents.md` §2: string-enum confidence + `decision_path` is real, but it's an *output-shape / routing* concern for the loop layer, not the tool dispatcher. Tools return `{data,error,ok}`; the loop decides escalation. Cross-reference to loop/safety layer. |
| **30+ pre-built domain tools** | CrewAI's 30+ tools + Flow API + 5 SDK wrappers = the 85K-line bloat (`crewai.md` §13). BAD_GUIDE §3 (`bad_guide_build_positioning.md`): *"every abstraction is a liability."* Ship `think` + `done`. The user owns the agent's tools. |

---

## 5. Recommended public API (the shape users touch)

```python
from lancelot import App
app = App()

# 1. Registration — annotation bits are the only new surface vs today
@app.tool(description="Look up a customer by id.", read_only=True)      # kind="lookup" inferred
def get_customer(customer_id: str) -> Customer: ...

@app.tool(description="Issue a refund.", kind="action", destructive=True,
          idempotent=False, validate=lambda a: _refund_preflight(a))    # validate-before-side-effect
def issue_refund(order_id: str, amount_cents: int) -> RefundReceipt: ...

# 2. Builtins auto-available: think, done, fetch_tool_result  (app.register_builtins())

# 3. The wire — unchanged shape, now carries annotations
app.tool_schemas()
# → [{name, description, input_schema, output_schema?,
#     annotations:{readOnlyHint, destructiveHint, idempotentHint}}, ...]

# 4. Dispatch — NEVER raises; the loop reads the envelope
from lancelot.agent.tools import dispatch, ToolSession
session = ToolSession(permit=my_hitl_gate)     # default = allow-all
result = dispatch(app, "get_custmer", {"customer_id": "c_1"}, session=session)
# fuzzy-fixes "get_custmer"→"get_customer"; result == {"data": {...}, "error": None, "ok": True}

bad = dispatch(app, "get_customer", {"customer_id": 123}, session=session)
# {"data": {}, "error": "customer_id: Input should be a valid string", "ok": False}  ← the corrective prompt

# 5. Bounded-parallel reads + sequential writes, order-preserved
results = await dispatch_batch(app, model_emitted_calls, session=session)  # reads@5 parallel, writes serial
```

**MCP stays exactly where it is — opt-in:**
```python
import lancelot.mcp
lancelot.mcp.mount(app, path="/mcp", max_response_bytes=4096)   # unchanged; deferred import
```

---

## 6. Exact default constants (load-test/corpus-grounded)

| Constant | Default | Source / rationale |
|---|---|---|
| Fuzzy tool-name threshold | **0.85** | CrewAI `_select_tool` `> 0.85` (`crewai.md` §2) |
| Input-repair stages | `json.loads → ast.literal_eval → json5* → json_repair*` | CrewAI `_validate_tool_input` (`crewai.md` §2). `*` = lazy-optional (cardinal rule); stdlib stages always present |
| Max parse attempts | **3** (2 for "big" models) | CrewAI `_max_parsing_attempts` (`crewai.md` §2) |
| Parallel-read concurrency | **5** | Composio `MAX_PARALLEL_WORKERS=5` (`composio.md` §2) |
| Result-bytes budget (enforced) | **4096** | Lancelot existing `mcp.py` 4 KB warn → now enforced; Claude SDK `maxResultSizeChars` (`claude_agent_sdk.md` Mech4) |
| `think` return string | `"Reflection recorded. Continue with next action."` | NIA verbatim (`nia.md` §2) |
| `done` ack (post-call) | `"Final result processed."` | Pydantic AI synthetic ack (`pydantic_ai.md` §7) |
| Repeated-call message | `"Identical call already made; do not repeat…"` | CrewAI `task_repeated_usage` (`crewai.md` §2) |
| `done`/output tool name | **`done`** (alias `final_result`) | Pydantic AI `final_result` (`pydantic_ai.md` §13); Haystack exit-condition (`transcripts_dlai.md` §5.4) |
| Pydantic error format | `e.errors(include_url=False, include_context=False)` | Pydantic AI `_wrap_error_as_retry` (`pydantic_ai.md` §2) |
| `kind` default | `"lookup"` | Vertical agents Lookup/Action (`vertical_agents.md` §1) |
| `idempotent` default | `True` (lookup) / `False` (action) | derived from `kind`; retry-safety invariant |
| Strict-schema mode | `strict=False` (opt-in) | OpenAI `ensure_strict_json_schema` only when needed (`openai_agents.md` §10) |
| Registration-lint strictness | warn (`LANCELOT_TOOL_STRICT=1` → raise) | Sierra registration linter (`vertical_agents.md` §1) |
| Action validate poll ceiling (if used) | 60s (30×2s) | Gordian basket-check (`vertical_agents.md` §3) |

---

## 7. Open questions for adjacent layers (hand-offs)

1. **Loop layer owns `StopReason`** — `done` raises `_DoneSignal`; the loop catches it → `StopReason.completed`. The repeated-call short-circuit and budget-exhaustion are *loop* termination conditions that read tool-layer signals (`crewai.md` §4 force-final-answer; `langgraph.md` §2.7 `remaining_steps < 2` guard). Confirm the `_DoneSignal`↔`StopReason` contract with the loop synthesizer.
2. **Permission `ask` resolution** — `dispatch` returns `{ok:False, data:{"__ask__":...}}`; the **durable/HITL layer** decides how to pause+resume (Pydantic AI deferred-tool-as-graph-edge `pydantic_ai.md` §10; OpenAI `RunState` approve/reject `openai_agents.md` §5). Tools layer only emits the ask; it does not own the wait.
3. **Per-step cost/budget gate** — tools read `cost_usd`/`tokens_in/out` ContextVars, but the *gate* (`cost_usd.get() > budget`) and the `output_config.task_budget` self-pacing kwarg (`claude_code.md` §7) belong to the loop. Confirm who enforces.
4. **Strict-schema + provider dialect** — `strict=True` (OpenAI `additionalProperties:false`/nullable, `extra="forbid"`) overlaps the **model-adapter layer**'s job (message-shape translation). Decide whether strict-schema lives on `@app.tool` or in the adapter at request-build time.
5. **`fetch_tool_result` store lifetime** — the handle-and-fetch blob store is per-session; coordinate its key namespace with the **memory/context layer** (conversation_id) so handles survive across HTTP requests in a multi-turn session.

---

## 8. Summary — copy / reuse / refuse, one line each

- **COPY (build, ~250 LoC):** `dispatch()` returning `{data,error,ok}` (never raises) · 4-stage repair ladder + fuzzy-name >0.85 + repeated-call short-circuit · `kind`/`destructive`/`idempotent`/`validate` annotations on `ToolSpec` + one `permit` hook (allow/deny/ask) · bounded-parallel reads (5) re-sorted to call-index + sequential write dedup · `think` + `done` builtins + handle-and-fetch truncation.
- **REUSE (byte path, zero rebuild):** `ToolSpec`/`_build_tool_schema`/`tool_schemas`/`call_tool` (`tool.py`,`app.py`) · `TransportPool.fanout(concurrency=5)`/`spawn_child` (`llm.py`) · `assert_safe_url` (`net.py`) · `dumps_canonical` (`canonical.py`) · `secrets.get` (`secrets.py`) · 4 KB result discipline + `mcp.mount` (`mcp.py`) · `classify_status` error taxonomy (`upstream_metrics.py`).
- **REFUSE (off-thesis/overkill):** MCP-as-default (keep opt-in) · BM25/dependency-graph tool router · ReAct text-parse path · code-action sandbox (v1) · per-provider schema-munge matrix · `enhance_schema_descriptions` nudge · `defer`+multi-hook chains · 5-tier autonomy (v1) · confidence router (→ loop layer) · 30+ pre-built tools.
