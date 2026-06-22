# Orchestration — synthesis brief (Lancelot agent layer)

> **Altitude:** "just build agents, brutal." The smallest orchestration primitive that earns its
> keep, drawn from the harvest. Every decision cites a note in `research/`.
> **Positioning lock:** Lancelot owns the byte path; the user owns the agent. Orchestration is
> **OPT-IN**, a **separate import** (`lancelot.agent.orchestrate`), and the **default is one agent**.
> Multi-agent is a tool you reach for when the task genuinely partitions — never the default.

---

## 0. The verdict in one paragraph

The corpus is unusually unanimous and it cuts *against* building much. The "90.2% over single-agent"
number everyone quotes is **token spend, not topology** — multi-agent uses ~15× the tokens of chat and
~4× a single agent, and "the improvement from multi-agent is mostly *more tokens / more reasoning*, not
the architecture itself" (`02_core/arch_catalog.md:22`, `:88`, `02_core/agentic_deep.md:148`). Cognition's
flat rule: *"Don't build multi-agents [as a default]… parallel agents make conflicting implicit decisions
that produce broken output"* (`arch_catalog.md:157-167`, `bad_guide_build_positioning.md:193`). The one
durable, founder-validated use of a sub-agent is **context isolation, not task decomposition**: "A sub-agent
is dispatched to find relevant files and return a structured summary. The parent learns where the code is
without spending the search cost" (`transcripts_yc_root.md:52`); wrapping a verbose sub-task in a
summarizing sub-agent = **82% parent-context reduction** (`transcripts_dlai.md:139-145`). So Lancelot
**BUILDS exactly one primitive** (`subagent()` = context-isolating spawn that returns a typed compact
summary), **REUSES the byte path** for everything I/O-bound (`fanout`, `spawn_child`, cost/trace context,
streaming), and **REFUSES** the theater (convergence-based termination, nested orchestrators by default,
sub-agents writing the final report, role-play dialogue). The agent harness is what compounds across model
generations; the orchestration *topology* gets reset by each model release (`bad_guide_build_positioning.md:209`),
so we keep the durable shell thin and refuse the depreciating bets.

---

## 1. The five orchestration shapes — KEEP-ranked by "earns its keep"

The harvest's real dividing line is **communication topology**, because it sets cost structure, failure
modes, and how much orchestration logic you must own (`multiagent_frameworks.md:7-19`). Ranked:

| Rank | Shape | Keep? | Why / cite |
|---|---|---|---|
| 1 | **Single agent** (loop + compaction) | **DEFAULT** | Reliability-first beats fragmented context; "single-threaded linear agent + compression layer" is the recommended default (`arch_catalog.md:165`). Cognition (`:157`). |
| 2 | **Sub-agent = context isolation** | **BUILD (the one primitive)** | 82% parent-context reduction (`dlai:144`); "context control, not task decomposition" (`yc_root:52`). Star topology, depth=1. |
| 3 | **Fan-out (independent subtasks)** | **REUSE byte path** | `TransportPool.fanout` already *is* this (`arch_catalog.md:96`, `:102`). Only the spawn-policy + merge is missing. I/O-bound only. |
| 4 | **Orchestrator-workers (shared typed state)** | **BUILD thin (on top of #2/#3)** | The 90.2% pattern, but the fix for "lead can't steer, subagents can't coordinate" is *external shared state + lightweight refs*, NOT message-passing (`agentic_deep.md:139-152`). |
| 5 | **Cross-model reviewer hook** | **BUILD (trivial)** | Cheapest multi-agent upgrade: different-family reviewer closes **74.7%** of the Sonnet→Opus gap, **+4.8%** on hardest (`agentic_deep.md:170-178`). |

Everything below #5 (peer-to-peer mailboxes, pub/sub actor buses, role-playing dialogue, graph DSLs as a
default, debate/voting-to-convergence) is **REFUSE-by-default** — see §5. Graph (Send/Command) is a narrow
**escape hatch** for legitimate map-reduce only (§4.4).

---

## 2. What Lancelot BUILDS (smallest primitives that earn their keep)

### 2.1 `subagent()` — context-isolating spawn (THE primitive)

This is the single highest-value transfer. It is a **harness** concern, not a transport concern: the only
channel parent→child is the **prompt string**, and the only channel child→parent is the child's **final
message** (`agentic_deep.md:194`, `:204`; Claude Code verbatim "stateless… its final and only message"
`claude_code.md:190-192`). Lancelot's `TransportPool.spawn_child` already carries traceparent/request_id/
agent_id at the HTTP layer (`lancelot_model_io.md:1.8`) — what's missing is the **semantic isolation +
typed-summary return**.

Hard rules baked in (all cited):
- **Depth cap = 1.** Workers may not spawn workers (Anthropic enforces this: `multiagent_params.py:23`,
  `deep_research.md:9`, `:199`). One level only; `{"type":"self"}` is the *only* legal recursion and at
  most one per roster (`deep_research.md:22`).
- **No parent history by default.** Child starts empty; caller serializes what matters into `task`. The
  Codex fork default is correct — *strip the "how" (tool-call noise), keep the "what" (decisions/facts)*
  (`agentic_deep.md:208-222`). Expose `fork: "none" | "last_n" | "full"` with **`"none"` default**.
- **Returns a typed compact summary**, not a transcript. Codex injects "Be concise in your final response —
  the orchestrator reads it *programmatically*" (`agentic_deep.md:228`, `:231`). The ideal sub-agent reply
  is structured: pointers, not prose (`yc_root:52`).
- **Per-subagent budget enforced by the harness**, not trusted to the model: soft-stop ~15 tool calls /
  ~100 sources, hard-kill at 20 / 100 (`deep_research.md:124-135`). Lancelot reads `cost_usd`/`tokens_*`
  from `lancelot.context` to enforce (`lancelot_model_io.md:2.2`, `:3`).

```python
# lancelot.agent.orchestrate  (separate import; nothing here loads unless imported)
from pydantic import BaseModel

class SubagentResult(BaseModel):
    summary: str                       # the ONLY thing the parent sees (concise, programmatic)
    refs: list[str] = []               # lightweight pointers (file:line, artifact ids, urls)
    cost_usd: float = 0.0              # snapshot delta, from lancelot.context.cost_usd
    tokens_in: int = 0
    tokens_out: int = 0
    tool_calls: int = 0
    status: str = "completed"          # completed | budget_exceeded | error | unresolvable

async def subagent(
    task: str,                          # the ENTIRE parent→child channel — serialize context here
    *,
    tools: list[str] | None = None,     # subset of app.tool_schemas(); None = inherit
    model: str | None = None,           # None = inherit parent (cost lever: cheap workers, see §3)
    fork: str = "none",                 # "none" | "last_n:<k>" | "full"  (default isolates)
    return_model: type[BaseModel] = SubagentResult,
    max_tool_calls: int = 15,           # SOFT stop (model judgment)
    hard_max_tool_calls: int = 20,      # HARD kill (harness enforced)
    max_cost_usd: float | None = None,  # harness circuit-breaker; None = inherit run budget
    parent_request=None,                # threads spawn_child for traceparent/request_id/cancel
) -> SubagentResult: ...
```

Implementation note: `subagent()` is ~a single agent-loop run with (a) a fresh isolated message list,
(b) `spawn_child(parent_request)` for the outbound calls so trace/cost propagate
(`lancelot_model_io.md:6` REUSE row), (c) a `cost_usd.get()` snapshot before/after for the per-step delta
(`lancelot_model_io.md:3.1` — BUILD-ON-TOP, pure user code), (d) the budget gates above. No new transport.

### 2.2 `fan_out()` — bounded parallel sub-agents (thin wrapper over `TransportPool.fanout`)

Fan-out earns its keep **only** for genuinely independent subtasks / I/O-bound probes
(`arch_catalog.md:100-102`, `agentic_deep.md`). The transport substrate is *already built* — `fanout(first_n,
deadline, cancel_laggards, concurrency)` returns results in completion order with partial-failure shape
(`lancelot_model_io.md:1.7`). Lancelot adds the **concurrency cap + spawn policy** on top:

```python
async def fan_out(
    tasks: list[str],
    *,
    max_concurrency: int = 5,           # see constant rationale below
    deadline_s: float | None = None,
    cancel_laggards: bool = True,
    **subagent_kwargs,
) -> list[SubagentResult]: ...
```

**`max_concurrency` default = 5** — the corpus floor. Anthropic's lead spawns "typically 3 subagents at
the same time" (`deep_research.md:34`), roster by tier is `Standard 2-3 / Medium 3-5 / High 5-10`
(`deep_research.md:24-30`), `open_deep_research` ships `max_concurrent_research_units: 5`
(`deep_research.md:111`), peer-to-peer recommended scale is 3-5 (`agentic_deep.md:166`). 5 is the
defensible middle; range 4-6. **Hard roster cap = 20** distinct agents (`deep_research.md:22`).
Note the **connection-pool gotcha**: a wide fan-out silently queues behind httpx
`max_connections=100` — tune `LANCELOT_UPSTREAM_MAX_CONNECTIONS` for very wide fan-out
(`lancelot_model_io.md:1.6`, gotcha `:7e`).

### 2.3 `orchestrator_workers()` — shared TYPED state, lightweight refs (not message-passing)

The 90.2% pattern, built thin. The **critical implementation constraint** is non-negotiable: workers MUST
store output in an external store and pass **lightweight references** back; passing full output back =
context explosion (`arch_catalog.md:92`, `:84`; `agentic_deep.md:143-150`). Coordination is via **shared
typed state**, not a chat mailbox — Palantir's two-year production finding is that human↔agent and
agent↔agent handoffs go through **typed ontology objects, not chat text** ("A text box… loses structural
information"; `transcripts_stanford_palantir.md:215-220`, `:445`). So Lancelot's "shared state" is a typed
Pydantic store + a `refs` channel, period.

```python
class Artifact(BaseModel):        # workers write here; lead reads refs, not full bodies
    id: str
    kind: str                      # "finding" | "file" | "draft_section" | ...
    summary: str                   # what the lead sees inline
    body_ref: str | None = None    # pointer the lead fetches ONLY if needed (handle-and-fetch)

async def orchestrator_workers(
    plan_fn,                       # lead model call → list[str] worker tasks (1 objective each)
    *,
    store,                         # any dict-like / KV; Lancelot ships an in-proc default
    max_workers: int = 5,
    synthesize_fn=None,            # the LEAD synthesizes — NEVER a subagent (see §5)
    **subagent_kwargs,
) -> str: ...
```

The **spawn contract** (what the lead must put in each worker `task`) is the Anthropic seven-field shape:
`objective, output_format, background_context, key_questions, suggested_sources +
unreliable_sources_to_avoid, specific_tools, scope_boundaries` (`deep_research.md:11-19`). Ship this as a
docstring template, not enforced structure — it's prompt craft.

### 2.4 `reviewer()` — cross-model review hook (cheapest upgrade, build it)

A `@app.tool`-style hook that routes "review this" to a **different model family** than the orchestrator
(`agentic_deep.md:170-178`). TransportPool already routes to N providers (`agentic_deep.md:178`), so this
is ~10 lines. Fires at checkpoints (post-plan, post-implementation, pre-final) and reactively when
`StopReason == stuck`.

```python
async def reviewer(artifact: str, *, reviewer_model: str, checkpoint: str) -> str: ...
# different family → different error modes; +4.8% hardest, closes 74.7% of Sonnet→Opus gap
```

### 2.5 Two safety/correctness primitives to copy verbatim (cheap, high-value)

- **Per-recipient concurrency guard** for any dispatcher that can address the same child twice in one turn:
  `asyncio.Lock` + `dict[thread_id, set[recipient]]`, refuse with a *model-readable* error so it waits.
  This fixes the real failure mode where a model emits two `send_message` calls before the first reply
  (Agency Swarm `send_message.py:374-388`, `multiagent_frameworks.md:69-81`, `:626`). Lancelot's
  `TenantConcurrencyLimiter` is the same shape (`lancelot_serve_tools.md:182-194`) — reuse the pattern.
- **Anti-ping-pong handoff detector** (only if handoffs are ever enabled): if the last *W* handoffs used
  fewer than *U* unique agents, stop — catches A→B→A→B loops (Strands `swarm.py:245`, `multiagent_frameworks`
  not, but `strands.md:301-314`). Strands ships it **disabled by default** (`window=0`); ship it the same way
  but available, with `max_handoffs = 20`.

### 2.6 `define_outcome`-style grader (orchestration-adjacent; build the bounded loop)

If a run wants a quality gate, the bounded **generate→grade→revise** loop with a **hard revision cap** is
the durable shape: default **3 revisions, max 20**, five verdict states `pass / needs_revision /
needs_more_research / needs_restart / unresolvable`, and on `unresolvable` **log and stop** — never loop
(`deep_research.md:39-46`). This is the orchestration-level expression of the universal "self-correction
loop with a hard cap" rule (`multiagent_frameworks.md:568-577`). A cheap inline single-model `advisor` for
"good enough to proceed?" checks is the lightweight variant (`deep_research.md:44`).

---

## 3. The cost-control spine (defense-in-depth — mandatory, not optional)

Every framework in the harvest has **multiple hard caps**; missing any one converts a failed run into
infinite token burn (`multiagent_frameworks.md:583-585`). Orchestration multiplies this risk (15× tokens).
Three layers, all of which Lancelot can wire from existing context:

1. **Model self-pacing** — Claude-only, free, trained behavior: set `output_config.task_budget.total`
   (beta `task-budgets-2026-03-13`) so the model paces itself (`claude_code.md:268-289`). Add as a
   `TransportPool.request(..., task_budget_tokens=N)` kwarg (one JSON field + header).
2. **Harness circuit-breaker** — provider-agnostic: `max_cost_usd` checked before each turn against
   `lancelot.context.cost_usd.get()` (accumulates automatically across sub-calls; emitted as
   `X-Lancelot-Cost-USD`; `lancelot_model_io.md:2.4`, `:3`). On breach → hard stop (MetaGPT's
   `NoMoneyException` shape, `multiagent_frameworks.md:362-364`).
3. **Turn counter** — last-resort fallback regardless of cost/model. `max_turns` default **20**
   (Magentic-One `_magentic_one_group_chat.py:117`, `multiagent_frameworks.md:265`).

**Model-tier cost lever** (orchestration's biggest free win): orchestrator on the expensive model,
workers on cheap models — "largest affordable (70B-405B) orchestrator; specialized sub-agents 1B-8B"
(`transcripts_dlai.md:289-290`), and Claude Code's guidance: reserve `opus` for orchestrator decisions,
`haiku`/`sonnet` for workers (`claude_code.md:349`). Hence `subagent(model=...)` with **inherit default**
but cheap override.

**Termination = resource bounds only.** Combine turn-count ∧ wall-clock ∧ per-step cap as `StopReason`
(Grok Heavy: `max_turns` ∧ 200s global ∧ 120s per-call; `grok_heavy.md:18`, `:34`). Map onto Lancelot's
per-route `timeout=` and `LANCELOT_DRAIN_SECS`. **Do NOT** add a convergence/agreement loop (§5).

---

## 4. What Lancelot REUSES from the byte path (do NOT rebuild)

The harvest confirms the lens split exactly (`transcripts_yc_root.md:297`): the **I/O-bound half of
orchestration is already built and load-tested**. Reuse map (all from `lancelot_model_io.md` §6 +
`lancelot_serve_tools.md`):

| Orchestration need | Byte-path primitive | Verdict |
|---|---|---|
| Parallel worker dispatch (N at once, first-K, cancel laggards) | `TransportPool.fanout(first_n, deadline, cancel_laggards, concurrency)` | **REUSE** (`lancelot_model_io.md:1.7`, `arch_catalog.md:96`) |
| Sub-agent transport context (traceparent/request_id/agent_id snapshot, survives Task reset) | `TransportPool.spawn_child(parent, session_affinity)` | **REUSE** (`lancelot_model_io.md:1.8`, `claude_code.md:230`) |
| Cost/token accumulation across all sub-calls | `lancelot.context.cost_usd / tokens_in/out` + `upstream_metrics` + `X-Lancelot-Cost-USD` | **REUSE** (`lancelot_model_io.md:2`, `:3`) |
| Error branch selector for a worker's LLM call | `classify_status → ok/quota/timeout/permanent/transient/upstream_5xx` (422=permanent=fix-the-prompt) | **REUSE** (`lancelot_model_io.md:2.3`) |
| Streaming the agent/orchestrator output (TTFT, backpressure, SSE framing, `_clean_field`) | `@app.stream` R3 pump + `lancelot.http.stream.ttft` | **REUSE** (`lancelot_serve_tools.md:31`) |
| Per-tenant concurrency / queue-wait + the per-recipient-guard pattern | `TenantConcurrencyLimiter` (asyncio.Semaphore, `queue_wait_ms`) | **REUSE pattern** (`lancelot_serve_tools.md:182`) |
| KV-cache-stable prompt prefix (the #1 free cost lever for repeated orchestration prompts) | `lancelot.canonical.dumps_canonical` | **REUSE** (`lancelot_model_io.md:4`, `arch_catalog.md:187`) |
| Coalesce many user requests into one upstream call (inference endpoints) | `@app.batch(batch_window_ms, max_batch, size_fn)` | **REUSE** (`lancelot_model_io.md:5`) |
| Tool registry → prompt list + execute (workers' tools) | `app.tool_schemas()` + `app.call_tool(name, **args)` | **REUSE** (`lancelot_serve_tools.md:37-51`) |
| Sub-agent / worker tools that fetch user URLs | `lancelot.net.assert_safe_url` / `block_private_ips=True` | **REUSE** (`lancelot_serve_tools.md:146-156`) |
| Provider creds for cheap-worker models | `lancelot.secrets.get(...)` (+ SIGHUP rotation) | **REUSE** (`lancelot_serve_tools.md:176`) |
| Outbound rate cap shared across parallel workers (one API key) | `TransportPool` `_TokenBucket` / `rate_limit=` | **REUSE** (`lancelot_model_io.md:1.9`) |
| Background lifetime loops (heartbeat / queue poller for an orchestrator) | `App.lifetime_task(coro_fn)` | **REUSE** (`lancelot_serve_tools.md:249`) |

### 4.4 The graph escape hatch (Send/Command) — narrow, opt-in, map-reduce only

LangGraph's `Send` fan-out "is Lancelot's `fanout` with checkpoint wrapping" (`langgraph.md:280`), and a
400+-line graph builder "is functionally a `while` loop with a tool dispatcher — the complexity pays off
ONLY when you have genuine fan-out (parallel tool execution, map-reduce over a corpus)"
(`langgraph.md:159`). So Lancelot does **not** ship a graph DSL. For the legitimate map-reduce case, the
escape hatch is: hand the user `fan_out()` (the map) + a reduce function + the `interrupt(value)` /
`Command(resume=value)` **shape** for HITL (copy the shape — checkpoint-before-raise, deterministic
idempotent interrupt id; `langgraph.md:67-69`). Durable resume itself depends on a checkpoint store, which
is the separate "durable seam" gap — out of scope for orchestration, noted so the boundary is clean.

---

## 5. What Lancelot REFUSES (the no-bullshit anchor)

Each is tempting and each is contradicted by a primary source. If a future agent is about to build one,
stop here first.

- ❌ **Multi-agent as the default.** Single agent + compaction is the default; multi-agent only when the
  task *genuinely partitions* into independent streams (`arch_catalog.md:157-167`, Cognition). The headline
  win is tokens, not topology (`arch_catalog.md:22`).
- ❌ **Convergence / agreement-based termination** ("loop until 75% agree", debate-to-consensus). Grok Heavy
  proved the production system uses **resource bounds only**; a `while not converged` loop (a) runs forever
  on hard queries, (b) adds latency on easy ones, (c) costs a compare-all-N meta-call per round
  (`grok_heavy.md:124-130`, `:18`). The "75% convergence" design was a fabrication corrected on recheck.
- ❌ **Nested orchestrators by default / depth > 1.** Anthropic *prohibits* it — workers cannot have
  `multiagent` set (`deep_research.md:199`, `:9`). Depth cap = 1; `{"type":"self"}` is the only recursion.
- ❌ **Sub-agents writing the final report/output.** Explicitly prohibited: "NEVER create a subagent to
  generate the final report — YOU write it yourself" (`deep_research.md:9`, `:200`). The lead synthesizes;
  workers only research and return findings. (A separate CitationAgent post-pass that byte-identically
  validates is fine; `deep_research.md:48`.)
- ❌ **Role-play theater as a production execution primitive.** CAMEL's RolePlaying (the "Never flip roles!"
  inception prompts) is a *synthetic-data-generation* technique — 2× LLM cost/step, fragile
  user-agent-decides termination; durable only for training data, not task execution
  (`multiagent_frameworks.md:402-436`, `:597`).
- ❌ **Peer-to-peer mailboxes / pub-sub actor buses as a default topology.** Harder to reason about for
  cost & correctness; star topologies are the auditable choice with a single point for logging/permissions/
  HITL (`multiagent_frameworks.md:587-589`). The fix for "agents can't coordinate" is **external shared
  typed state + refs**, not a message bus (`agentic_deep.md:150`; Palantir typed-object handoffs
  `transcripts_stanford_palantir.md:215`).
- ❌ **A graph DSL as the front door.** 400+ lines for a `while` loop (`langgraph.md:159`); ship `fan_out` +
  a reduce fn instead, with graph reserved as the §4.4 map-reduce escape hatch.
- ❌ **Passing full worker output back to the orchestrator.** Context explosion on truncation; pass
  lightweight refs to external storage (`arch_catalog.md:92`).
- ❌ **A separate judge model for N-way self-consistency synthesis** when the leader already holds all peer
  answers — doubles cost for no architectural gain (`grok_heavy.md:130`). (Self-consistency *voting* over
  `fanout` results is fine as a one-liner aggregator: `vote(results, strategy="majority")`,
  `transcripts_stanford_palantir.md:458`.)

---

## 6. Exact default constants (single source of truth)

| Constant | Default | Range / note | Cite |
|---|---|---|---|
| `subagent` depth cap | **1** | hard; `{"type":"self"}` only recursion, ≤1/roster | `deep_research.md:9,22,199` |
| `subagent.fork` | **"none"** | "none" / "last_n:k" / "full"; strip how, keep what | `agentic_deep.md:208-222` |
| `subagent.max_tool_calls` (soft) | **15** | model-judgment soft stop | `deep_research.md:124-135` |
| `subagent.hard_max_tool_calls` | **20** | harness hard kill | `deep_research.md:135` |
| `fan_out.max_concurrency` | **5** | 4-6; "typically 3", tiers 2-5 | `deep_research.md:34,111`; `agentic_deep.md:166` |
| roster hard cap (distinct agents) | **20** | absolute | `deep_research.md:22` |
| `max_turns` (run) | **20** | last-resort termination | `multiagent_frameworks.md:265` |
| run wall-clock budget | **200s** | + per-step **120s** cap | `grok_heavy.md:18` |
| `max_revisions` (grader) | **3** | max 20; `unresolvable`→stop | `deep_research.md:40` |
| `max_handoffs` (if enabled) | **20** | anti-ping-pong disabled by default (`window=0`) | `strands.md:303-314` |
| synthesis budget reserve | **25%** | reserve for the lead's write step | `deep_research.md:93,119` |
| tool-result WARN threshold | **4096 B** | handle-and-fetch; already in `mcp.mount` | `lancelot_serve_tools.md:70`; `dlai:56` |
| worker model | **inherit** (cheap override) | opus orchestrator / sonnet-haiku workers | `claude_code.md:349`; `dlai:289` |
| `task_budget` beta (Claude self-pace) | opt-in | one JSON field + header | `claude_code.md:268-289` |

---

## 7. API surface summary (`lancelot.agent.orchestrate`, opt-in import)

```python
# single-agent default lives in lancelot.agent (the loop); orchestration is a SEPARATE import.
from lancelot.agent.orchestrate import (
    subagent,             # context-isolating spawn → typed SubagentResult  (THE primitive, §2.1)
    SubagentResult,       # summary + refs + cost/token/tool-call accounting
    fan_out,              # bounded parallel sub-agents over TransportPool.fanout  (§2.2)
    orchestrator_workers, # shared TYPED state + lightweight refs; lead synthesizes  (§2.3)
    Artifact,             # the typed shared-state record (refs, not bodies)
    reviewer,             # cross-model review hook  (§2.4)
    vote,                 # majority/quorum aggregator over fanout results (one-liner)
)
```

Honest envelope: this layer adds **zero new runtime deps** (pydantic-only rule honored — provider SDKs stay
lazy via `TransportPool`/`lancelot.secrets`), rides the byte path for all I/O, and is **inert unless
imported**. Its value is the *shell* (isolation contract, budget spine, refusal of the anti-patterns), not
new transport — because the byte path already won the transport fight (`transcripts_yc_root.md:297`).

---

*Sources (all in `research/`): `02_core/{arch_catalog, agentic_deep, transcripts_yc_root, transcripts_dlai,
transcripts_stanford_palantir, bad_guide_build_positioning}.md`; `03_teardowns/{multiagent_frameworks,
deep_research, claude_code, claude_agent_sdk, langgraph, strands, grok_heavy, openai_agents}.md`;
`04_byte_path/{lancelot_model_io, lancelot_serve_tools}.md`. Every constant and decision cited inline. No
secondary sources.*
