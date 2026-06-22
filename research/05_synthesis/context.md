# Layer brief — **Context Engineering** (Lancelot agent layer)

**Synthesizer:** context-layer agent · **Date:** 2026-06-22
**Altitude:** "just build agents, brutal." Smallest primitive that earns its keep — anti-overkill AND anti-underkill.
**Sources synthesized:** `02_core/{transcripts_dlai, agentic_deep, leaked_prompts, transcripts_stanford_palantir}.md`, `03_teardowns/{manus, claude_code, codex, letta, deep_research, openai_agents, pydantic_ai}.md`, `04_byte_path/{lancelot_model_io, lancelot_serve_tools}.md`. Cardinal rule honored: runtime dep is pydantic-only; everything else lazy/test-only.

---

## 0. The one-sentence thesis for this layer

Context engineering is the agent product. The corpus is unanimous: the loop is 5–10 lines; the 10,000 engineering lines are in **what tokens are present** (`agentic_deep §1`; `transcripts_dlai §5.1`). Lancelot already owns the *byte* side of context (deterministic JSON, session affinity, cost accounting, streaming) — what is missing is the **inspectable assembly object** that sits between the agent's working state and `TransportPool.request()`. Build exactly that: a `ContextBundle` that is *structured, never string-concatenated, append-only, cache-aware, budget-bounded, and honestly inspectable* — plus the compaction strategy that keeps it under budget. Nothing larger.

The corpus also gives us the rare luxury of **convergent constants** (multiple independent production systems landing on the same number). Those are gold — ship them as defaults, don't re-derive them.

---

## 1. What Lancelot already provides (REUSE — do not rebuild)

Confirmed by direct read of `lancelot/canonical.py` + `lancelot/context.py` and the two byte-path inventory notes:

| Need | Lancelot primitive | Status | Cite |
|---|---|---|---|
| **Deterministic JSON for cache-stable prefix bytes** | `lancelot.canonical.dumps_canonical` — `json.dumps(sort_keys=True, separators=(",",":"), ensure_ascii=False, allow_nan=False).encode()` | **REUSE verbatim.** This is *the* implementation primitive for KV-cache discipline. Manus Rule 2/5, DLAI §2, agentic_deep §2.1 rule 3 all reduce to "serialize with sorted keys" — `dumps_canonical` IS that. | `canonical.py:28`; `manus §1.2`; `transcripts_dlai §2`; `agentic_deep §2.1` |
| **Session→worker KV-cache affinity** | `Response.set_session_affinity(key)` | **REUSE.** agentic_deep §2.1 rule 4 ("session ID routes to same vLLM worker → warm KV-cache") is exactly this. | `agentic_deep §2.1`; lens |
| **Cost/token accumulation across a request's sub-LLM calls** | `lancelot.context.{cost_usd, tokens_in, tokens_out}` ContextVars (+ `X-Lancelot-Cost-USD` trailer); `lancelot.upstream_metrics.extract_tokens`/`classify_status` | **REUSE.** Feeds the budget gate directly. The per-step delta is a snapshot-before/after (`cost_usd.get()`), pure user code — no new ContextVar. | `lancelot_model_io §3.1`, `§2.2` |
| **Cache-token split (1h vs 5m) extraction** | `lancelot.upstream_metrics` + `lancelot.telemetry.cost_event_from_openai_response` (already parses `cache_read_input_tokens` across 3 provider shapes) | **REUSE + small EXTEND** (add `ephemeral_1h`/`ephemeral_5m` fields for Anthropic). | `claude_code §11`; `lancelot_serve_tools §5` |
| **Outbound transport for the compaction/summary call** | `TransportPool.request()` (POST never retried, budget cap, cancel, metrics) | **REUSE.** The summarizer is just a cheaper-model `pool.request()`. | `codex §13 byte-path` |
| **Tool schema block (the second cache block)** | `app.tool_schemas()` → exact Anthropic/OpenAI/MCP wire shape; `dumps_canonical` for byte-stability | **REUSE.** The tool-schema block is one of the three cache blocks; keep it stable by always serializing through `dumps_canonical`. | `lancelot_serve_tools §2`; `manus §1.5` |
| **Streaming the assembled turn out** | `@app.stream` R3 pump + `Event`/`sse_adapter`/`_clean_field` + `lancelot.http.stream.ttft` | **REUSE.** Context layer never touches the pump. | `lancelot_serve_tools §1` |
| **Conversation identity across turns** | `lancelot.context` has `request_id`/`agent_id` but **no `conversation_id`** | **EXTEND** — add one ContextVar (see §6). | `lancelot_model_io §3.1`; `pydantic_ai` map |

**Net:** the byte path already covers caching mechanics (serialization + affinity) and cost accounting. The agent layer must NOT re-implement any of these — it composes them.

---

## 2. What Lancelot must BUILD (the context primitives, smallest-first)

Ordered by leverage. Each is grounded in convergent production evidence; each is small.

### B1 — `ContextBundle`: the inspectable, structured, append-only assembly object

**The core build.** Every failure mode in the corpus traces to string-concatenated prompts that no one can inspect or cache. Manus's entire discipline is "three blocks, append-only" (`manus §1.5`). Genspark "wrote custom scaffolding specifically to control every byte of the prompt" because LangChain/LlamaIndex "silently add tokens, timestamps, or reorder history — invalidating the cache prefix on every turn" (`transcripts_dlai §2`). The bundle is the anti-string-concat primitive.

**Mechanism (the durable three-block structure — `manus §1.5`, `agentic_deep §2.5`, `claude_code §1`):**

```
ContextBundle
├── system     (frozen prefix — cache 1h)      ─┐ static
├── tools      (tool-schema block — cache 5m)   │ → SYSTEM_PROMPT_DYNAMIC_BOUNDARY
│   ────────── DYNAMIC BOUNDARY ──────────────  ─┘
├── dynamic    (CLAUDE.md-equiv, date, plan/todo, memory index — cache 5m)
└── events[]   (append-only: Message|Action|Observation|Plan|Knowledge|Other)
```

- **Append-only invariant**: events are only appended, never mutated or reordered. This is what makes the prefix-up-to-turn-T byte-identical at T+1 → guaranteed cache hit on everything before the last observation (`manus §1.1`, `§1.5`; `transcripts_dlai §2` "never reorder by relevance"). The bundle **enforces** this — `append_event()` exists; there is no `edit_event()`.
- **Typed event taxonomy** (`manus §1.5`, verbatim 7 types; `leaked_prompts §mechanism-1`): `Message | Action | Observation | Plan | Knowledge | Other`. Each event has a deterministic type tag and renders through a fixed template with **consistent delimiters** (`[TOOL RESULT: name]` / XML-style tags — `transcripts_dlai §4.1.3`). XML tags beat JSON for long context: CodeRabbit's 6× token reduction came partly from XML-over-JSON envelopes (`transcripts_dlai §4.2`).
- **`SYSTEM_PROMPT_DYNAMIC_BOUNDARY` marker** (`claude_code §1`, `agentic_deep §2.1` rule 6): a sentinel that splits cached-forever (system+tools, 1h TTL) from per-turn (dynamic+recent events, 5m TTL). Up to 4 cache breakpoints per request. For Anthropic, the bundle emits the two-element `system: [{text, cache_control:{ttl:"1h"}}, {text, cache_control:{ttl:"5m"}}]` shape; for other providers, the boundary is advisory (still drives append-only discipline).
- **`.render()` → `(messages, system, tools)`** ready for `TransportPool.request()`; **`.to_cache_key()`** → `hashlib.sha256(dumps_canonical({...})).hexdigest()` (the lens-stated pattern; `lancelot_model_io §4`).

**Why structured, not string:** inspectability (B3), cache-boundary placement, per-category budgeting, and append-only enforcement are all impossible on a flat string. This is the single highest-leverage primitive — it is also small (a dataclass + a renderer + a few invariant-guarding methods).

**Anti-string-concat is a hard rule:** the task statement says "never string-concat." The bundle's only public mutators are `append_event`, `set_dynamic(section, value)`, `freeze_system`. There is no `bundle += "..."`.

### B2 — Compaction: observation-masking first, LLM-summary as fallback, with the convergent constants

The corpus gives a **tiered** strategy (zero-cost tiers before any API call) and a remarkably convergent trigger threshold.

**Tier 0 — Observation masking (the best finding; build this first).** Keep full action + reasoning history; replace *old observations* (tool-result bodies) with a placeholder, retaining the last N turns of full observations. Result: **52% cost savings with a 2.6% solve-rate *improvement*** — old tool outputs are rarely re-read, old reasoning still informs decisions (`agentic_deep §2.2`). Mechanism: after N turns, replace `Observation.content` with `"[cleared to save context]"` but keep the `Action` (tool_use) record so the model knows *what* happened. Anthropic ships this as `clear_tool_uses_20250919`; benchmark 335K→173K peak with 7 clearing events, **zero API calls** (`agentic_deep §2.3`, §6). This is pure dict manipulation — no model call, no new dep. **Default: keep last 10 turns of observations** (`agentic_deep §2.2`), `mask_after_turns=10`.

**Tier 1 — LLM summary compaction (fallback when masking isn't enough).** The convergent constants:

| Constant | Value | Convergence | Cite |
|---|---|---|---|
| Trigger threshold | **0.7–0.8** of context window | DLAI (70–80%), MongoDB (0.75), Letta (0.9 prod but "industry best practice 70%"), agentic_deep ("70% prevents reaching the expensive emergency tier"), Codex (95% is the *hard* mid-turn limit, but pre-turn fires earlier) | `transcripts_dlai §2`; `agentic_deep §2.3`; `letta §7`; `codex §2` |
| Evict fraction | **0.30** (keep last 70% verbatim) | Letta `partial_evict_summarizer_percentage = 0.30` | `letta §7` |
| Snap boundary | **forward to next assistant/Action message** (never split an Action/Observation pair) | Letta | `letta §7` |
| Summary insertion | **`role=user` at index 1** (right after system) | Letta | `letta §7` |
| Retained user-instruction budget | **64,000 tokens** of user/developer/system messages survive | Codex `RETAINED_MESSAGE_TOKEN_BUDGET` | `codex §3` |
| Summarizer model | **a cheaper model** (Haiku-tier ~10× cheaper) | Letta (Haiku 4.5), Codex (gpt-5.4-mini), Letta sleeptime | `letta §7`; `codex §9` |
| Circuit breaker | **stop after 3 consecutive compaction failures** | Claude Code (added after 1,279 sessions hit 50+ fails → ~250K wasted API calls/day) | `agentic_deep §2.3` |
| Pre-compaction warning | inject `"[automated] conversation history will soon reach maximum length"` as a non-user message | Letta `NON_USER_MSG_PREFIX`, Codex summary_prefix | `letta §7`; `codex §3` |

**The compaction prompt** — copy Codex's 8-line CONTEXT CHECKPOINT prompt verbatim as the default (overridable): "Create a handoff summary for another LLM that will resume the task. Include: current progress and key decisions; important context/constraints/user preferences; what remains to be done (clear next steps); critical data/examples/references needed to continue. Be concise, structured." (`codex §3`).

**Anti-hallucination on the compaction itself:** the summary call is a *separate* model invocation (not the agent turn) — keeps the agent loop uncontaminated by formatting concerns (`manus §4` structured_output_schema pattern; `codex §3`).

### B3 — `agent.inspect()` / `ContextUsage`: per-category introspection (the honesty primitive)

The task names "agent.inspect() honesty" and "per-category context introspection" explicitly. The reference shape is Claude Code's `getContextUsage()` (`claude_code §3`): a per-category token breakdown that powers `/usage` and `/cost`. **Build the honest subset, skip the UI chrome.**

```python
@dataclass
class ContextUsage:
    total_tokens: int
    max_tokens: int
    pct: float
    sections: dict[str, int]        # {"system":.., "tools":.., "dynamic":.., "events":..}
    by_event_type: dict[str, int]   # {"Observation":.., "Action":.., "Message":..}
    masked_tokens: int              # how much observation-masking has reclaimed
    cost_usd: float                 # from lancelot.context.cost_usd
    autocompact_threshold: int
    cache_split: dict               # {"ephemeral_1h":.., "ephemeral_5m":.., "cache_read":..}
```

- `bundle.inspect()` returns this synchronously from the *current* assembly (no model call) — `systemPromptSections` is the load-bearing field ("makes visible which CLAUDE.md-equivalent files are consuming tokens," `claude_code §3`).
- Surface it as `@app.get('/agent/context')` (lens + `claude_code §3`) for an operator diagnostic, and as `agent.inspect()` for programmatic introspection.
- **Honesty discipline:** `inspect()` reports what is *actually in the rendered prompt* — not the agent's intended state. If masking dropped an observation, `inspect()` shows it as masked, not present. This is the anti-self-deception property that distinguishes it from a vanity metric.
- **Token counts:** post-call counts come free from `lancelot.upstream_metrics` (response usage). For pre-call estimation, default to a lightweight char/4 estimate (`codex APPROX_BYTES_PER_TOKEN = 4`) — **no tiktoken dep** (cardinal rule); offer an optional pluggable counter hook.

### B4 — `ResearchMemory`: distilled-reflection memory (the "selectivity" primitive)

The task names "distilled-reflection ResearchMemory." The corpus converges hard on a **selectivity-gated, tiered, file-backed** memory that is distinct from a transcript log. **Build the minimal tier; skip the ORMs.**

**The durable mechanisms (mechanism-level, all convergent):**
- **Selectivity gate at write time** — the single most-cited rule: *"Only extract information useful in future conversations. Do NOT extract every fact — only important, reusable insights"* (`transcripts_dlai §6`); Codex's no-op gate: *"Will a future agent plausibly act better because of what I write here?"* (`agentic_deep §9.2`; `codex §9`). Over-extraction pollutes retrieval precision.
- **Tiered by decay rate, not one bucket** (`agentic_deep §9.1`): hot **index** (always loaded, ~150 chars/pointer, truncated at line 200) → warm **topic files** (loaded on-demand) → cold **logs** (append-only, never loaded, grep-only). This is the Claude Code MEMORY.md / Manus `todo.md` / Codex `memory_summary.md` pattern.
- **`str_replace`-exact updates, not overwrite** — minimal diff, preserves history, model only generates the changed line; raises if `old_str` not found (no silent overwrite). Manus `file_str_replace`, Letta `core_memory_replace` (`manus §3`; `letta §2`).
- **`<memory_metadata>` page-table** (Letta) — inject "you have N messages and M facts outside your window; use your tools to access them" so the agent knows what it can't see (`letta §1`). Copy this data model; **skip the Postgres/pgvector ORM** (Letta's explicit anti-pattern, `letta` anti-patterns).
- **Distilled reflection / consolidation** — a *separate, cheaper, background* pass that reads recent activity and rewrites the memory blocks ("rewrite the entire block integrating new information; organized, readable, comprehensive" — Letta `memory_rethink`). Gated, not eager: Codex Phase-2 runs only on git-dirty workspace + rate-limit ≥25% remaining; consolidation behind a global lock (`codex §9`; `letta §6`).

**Lancelot shape:** a `ResearchMemory` object over a **pluggable store** (default = a POSIX dir at a well-known path, per Anthropic deep-research's "memory is a POSIX directory, not a KV API" — `deep_research §8`). `write(insight)` is selectivity-gated; `index()` returns the hot pointers for injection into `ContextBundle.dynamic`; `reflect()` is the background consolidation (an `app.lifetime_task` or post-response `asyncio.create_task`, frequency-gated by turn counter — `letta §6`). **sqlite-vec lazy-imported** only if the operator opts into vector retrieval; default store is files (no dep).

### B5 — Budget as a hard constraint (the cost-control seam this layer owns)

Context engineering and cost are the same axis (`transcripts_dlai §8.3`: "context is reassembled at every loop iteration" — the structural cost driver). The context layer must expose budget as a *first-class input*, defense-in-depth (`claude_code §7`):

- **Token/cost budget on the bundle**: `ContextBundle(max_context_tokens=..., budget_usd=...)`. The compaction trigger (B2) keys off `max_context_tokens`; the loop's stop logic keys off `budget_usd` via `lancelot.context.cost_usd.get()`.
- **Three-layer defense** (`claude_code §7`): model self-pacing (`output_config.task_budget` — one JSON field + Anthropic beta header on `pool.request()`, free, Claude-only) → harness USD circuit-breaker (`budget_usd` → StopReason `max_budget_usd`) → turn counter (`max_turns`, default 50 from Letta/PydanticAI convergence). The context layer owns the first two inputs and feeds the loop layer.
- **The 100:1 input:output ratio** (`agentic_deep §2.1`) is *why* cache optimization matters 100× more than output optimization — budget enforcement that ignores caching is enforcing the wrong number. The bundle's cache-aware rendering (B1) is the cost lever; the budget is the ceiling.

---

## 3. What this layer REFUSES (off-thesis / depreciating / overkill / not-context)

| Refuse | Why | Cite |
|---|---|---|
| **String-concatenated prompt builders** (LangChain-style PromptTemplate accretion) | The exact anti-pattern the bundle exists to kill — silently busts the cache prefix, un-inspectable. | `transcripts_dlai §2` |
| **Semantic cache (embed query → ANN → return cached response) as a Lancelot primitive** | Application-level, requires per-use-case threshold tuning + precision/recall monitoring ("treat as an ML model"). Off the byte-path thesis; user-side. `@app.batch` already gives ~16× upstream-call reduction for the textual-dup case. | `transcripts_dlai §8.2`, `§14` Tier-D |
| **Knowledge-graph context augmentation / multi-hop retrieval** | AI logic, user-side. Lancelot owns the byte path, not the retrieval algorithm. | `transcripts_dlai §14` |
| **Mandatory vector DB / pgvector / 60-migration ORM for memory** | Letta's own explicit anti-pattern; "implement against an interface with pluggable storage." Default to files; sqlite-vec lazy + opt-in. | `letta` anti-patterns; `deep_research §8` |
| **GBNF / grammar-constrained generation for context shaping** | Depreciating on the model clock — frontier models have native tool-calling; the % needing it shrinks fast. | `letta §13` |
| **Per-chunk re-validation / re-compaction inside the stream** | Tier-D perf cliff; compaction is a between-turn operation, never per-chunk. | `codex` anti-patterns; CLAUDE.md Tier-D |
| **Hardcoding Anthropic beta header *strings* as the durable API** (`compact-2026-01-12`, `clear_tool_uses_20250919`) | The *pattern* (compaction, masking) is durable; the header churns on every model release. Implement the pattern; pass the header through as an opt-in kwarg. | `agentic_deep §15`; `claude_code §10.4` |
| **Pre-call token counting via a tokenizer dep (tiktoken)** | Violates pydantic-only. Use char/4 estimate + optional pluggable counter; post-call counts are free from upstream usage. | `lancelot_model_io §8`; `codex §4` |
| **AG-UI 16-event emitter / Code Mode V8 sandbox / ghost commits** | Either rides on transport already built (AG-UI), or is vendor infra (V8, git) — not a context primitive. | `transcripts_dlai §12`; `codex §11`, `§12` |
| **Re-implementing caching mechanics** (serialization, affinity, cost accounting) | Already built (`dumps_canonical`, `set_session_affinity`, `cost_usd`). Compose, don't rebuild. | `canonical.py`; lens |

---

## 4. Recommended API sketch (the shape users touch)

```python
from lancelot.context_engine import ContextBundle, ResearchMemory, SYSTEM_PROMPT_DYNAMIC_BOUNDARY
from lancelot.llm import TransportPool

bundle = ContextBundle(
    system="You are a research agent. ...",        # frozen → 1h cache
    tools=app.tool_schemas(),                        # tool block → 5m cache
    max_context_tokens=180_000,
    budget_usd=0.50,
)

# Append-only event stream — the ONLY way to grow context. No string concat.
bundle.append_event("Message", {"role": "user", "content": user_msg})
bundle.set_dynamic("memory_index", memory.index())   # hot pointers, dynamic block
bundle.append_event("Action", tool_use_block)
bundle.append_event("Observation", tool_result)       # large → auto-masked after 10 turns

# Honest introspection — synchronous, reflects the RENDERED prompt:
u = bundle.inspect()          # ContextUsage; also @app.get('/agent/context')
if u.pct > 0.75:              # convergent default 0.7–0.8
    await bundle.compact(pool, model="claude-haiku-...")   # mask first, summarize as fallback

# Render → straight into the existing byte path. dumps_canonical under the hood.
messages, system, tools = bundle.render()
resp = await pool.request("POST", url, json={"messages": messages, "system": system,
                                             "tools": tools, **bundle.budget_fields()})

# Distilled-reflection memory — selectivity-gated write; background consolidation.
memory = ResearchMemory(store="./.lancelot/memory")    # POSIX dir default; sqlite-vec opt-in
memory.write(insight="User prefers TypeScript over JS.")   # gated: "useful in future?"
@app.lifetime_task
async def consolidate(): await memory.reflect(pool, every_n_turns=5)  # cheap model, gated
```

### Exact default constants (ship these; do not re-derive)

```python
# ── ContextBundle ──
DYNAMIC_BOUNDARY            = "__LANCELOT_PROMPT_DYNAMIC_BOUNDARY__"  # claude_code §1 convention
CACHE_TTL_STATIC           = "1h"      # system + tools          (claude_code §1)
CACHE_TTL_DYNAMIC          = "5m"      # dynamic + recent events (claude_code §1)
MAX_CACHE_BREAKPOINTS      = 4         # API cap                 (claude_code §1)
EVENT_TYPES = ("Message","Action","Observation","Plan","Knowledge","Other")  # manus §1.5

# ── Compaction ──
COMPACT_TRIGGER_PCT        = 0.75      # 0.7–0.8 convergent      (dlai/mongo/letta/agentic_deep)
COMPACT_EVICT_FRACTION     = 0.30      # keep last 70% verbatim  (letta §7)
COMPACT_SNAP_TO            = "assistant"  # never split Action/Observation (letta §7)
COMPACT_SUMMARY_INDEX      = 1         # role=user right after system (letta §7)
RETAINED_USER_TOKEN_BUDGET = 64_000    # user/dev/system survive (codex §3)
MASK_OBSERVATIONS_AFTER    = 10        # turns of full observations kept (agentic_deep §2.2)
MASK_PLACEHOLDER           = "[cleared to save context]"          # agentic_deep §6
COMPACT_CIRCUIT_BREAKER    = 3         # consecutive failures → stop (agentic_deep §2.3)
COMPACT_MODEL_DEFAULT      = None      # operator picks a cheaper model; ~10× cheaper (letta §7)

# ── Introspection / budget ──
APPROX_BYTES_PER_TOKEN     = 4         # estimate; no tiktoken dep (codex §4)
DEFAULT_MAX_TURNS          = 50        # letta DEFAULT_MAX_STEPS / pydantic_ai both 50
MEMORY_INDEX_MAX_LINES     = 200       # hot index truncation     (agentic_deep §9.1)
MEMORY_POINTER_CHARS       = 150       # per-pointer in the index (agentic_deep §9.1)
MEMORY_CONSOLIDATE_EVERY_N = 5         # turn-gated reflection    (letta §6)
MEMORY_RATE_LIMIT_FLOOR_PCT= 25        # skip consolidation if quota tight (codex §9)
```

---

## 5. Build order (smallest earning primitive first)

1. **`ContextBundle`** (B1) — dataclass + append-only methods + `.render()` (uses `dumps_canonical`) + `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` split. Highest leverage, small. Nothing else works without it.
2. **Observation masking** (B2 Tier-0) — pure dict op, zero API cost, 52%/+2.6% finding. Lands inside `bundle.compact()`.
3. **`agent.inspect()` / `ContextUsage` + `/agent/context`** (B3) — honesty primitive; synchronous; reads the rendered bundle.
4. **LLM-summary compaction** (B2 Tier-1) — the convergent constants + Codex 8-line prompt + circuit breaker; routes summary call through `TransportPool` to a cheaper model.
5. **`conversation_id` ContextVar** (one line) — the cross-turn identity link the bundle/memory need.
6. **`ResearchMemory`** (B4) — files-default store, selectivity gate, `<memory_metadata>` page-table, background `reflect()`. sqlite-vec opt-in/lazy.

Budget (B5) is threaded through B1/B3 as inputs, not a separate module.

---

## 6. Open questions for Lancelot (the orchestrator/loop owner)

- **`conversation_id` placement**: a new `lancelot.context` ContextVar vs a field the agent passes explicitly. The byte-path note flags it missing (`lancelot_model_io §3.1`); PydanticAI/OpenAI-Agents both carry it on the run object. Recommend: ContextVar with `"-"` sentinel, set by the agent layer, read into the cache key — matches the existing `request_id` pattern.
- **Where `compact()` runs**: inline in the loop (synchronous, blocks the turn) vs `asyncio.create_task` after response (Letta sleeptime). Masking is cheap enough to run inline; LLM-summary should be inline only when *over* budget (you can't proceed otherwise), background otherwise. The loop layer decides the trigger point; the context layer provides both entry points.
- **Cache-control emission for non-Anthropic providers**: the `1h/5m` `cache_control` blocks are Anthropic-specific. OpenAI/Google auto-cache prefixes with no annotation. Recommend: the bundle emits Anthropic `cache_control` only when the provider is detected as Anthropic (`upstream_metrics.detect_provider`), and the append-only discipline carries the win on the others for free.
- **`ResearchMemory` default path + multi-worker**: a POSIX dir is single-node; multi-worker agents need a shared store (same gap as MCP session affinity, `lancelot_serve_tools §3`). Recommend: files default for the common single-node case; document the pluggable-store seam for the multi-worker case; don't build the shared backend until it's a real requirement.

---

*Every load-bearing claim above cites a specific note + section. The byte-path REUSE calls are verified against direct reads of `lancelot/canonical.py` and `lancelot/context.py`. Cardinal rule held throughout: pydantic-only runtime; provider SDKs + sqlite-vec lazy-imported.*
