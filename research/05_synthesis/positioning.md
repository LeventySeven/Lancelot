# Positioning — "Just Build Agents, Brutal"

**Synthesis dimension:** positioning / the bet / the wedge / build-vs-compose-vs-refuse.
**Date:** 2026-06-22.
**Grounding:** `02_core/bad_guide_build_positioning.md`, `02_core/bad_guide_ai_research.md`, `02_core/re_startups_map.md`, the transcripts (`transcripts_yc_root.md`, `transcripts_yc.md`, `transcripts_dlai.md`), the byte-path inventory (`04_byte_path/lancelot_model_io.md`, `lancelot_serve_tools.md`), and the 45 teardowns in `03_teardowns/`. Every claim cites a source file + section/line. KNOWN = from pinned source; INFERRED/DESIGNED labeled.

---

## 0. The one-paragraph thesis

The agent loop is 5–10 lines and is not where anyone competes — this is the single most-corroborated finding in the entire corpus (see §1). The product is the harness around the loop: context/cache discipline, tool design, error recovery, evals, durable state. Lancelot's bet is to **refuse to compete on the loop** and instead ship a *lean* agent layer riding a *Rust byte path* that pure-Python frameworks structurally cannot offer. The wedge is not "faster server" (the honest benchmark forbids that claim — `bad_guide_build_positioning.md §9`); it is **cost-effective by construction**: prompt-cache-stable serialization, batch-coalesced upstream calls, in-band per-call cost accounting, and real backpressured streaming with TTFT — four byte-path primitives that already exist (`lancelot_serve_tools.md §"DOES provide"`) and that the competitive map confirms *no other framework owns* (`re_startups_map.md §1.10`). Brutal means: build the ~10-line loop and the few primitives nobody else has, **compose** the substrate that already exists, and **refuse** the graph-DSL / router-cult / MCP-first machinery that the teardowns show is pure overhead tax.

---

## 1. THE BET — "the loop is 5–10 lines; the harness is the product"

This is not an opinion; it is the convergence of every primary source in the harvest.

**Founder/practitioner voices (BAD_GUIDE):**
- Thorsten Ball (Amp/Sourcegraph): "the agent core is a model, a loop and three tools in **~315 lines of Go**, and every hard, valuable thing lives elsewhere: editor integration, prompt craft, feedback timing, orchestration" (`bad_guide_ai_research.md §6`, BAD_GUIDE line 741-742). The discipline: "the loop is not the moat. Stop treating the ReAct loop as the interesting engineering."
- Boris Cherny (Claude Code): "The loop itself is 10 lines. The product is the surrounding machinery" (harvest line 16052).
- The lens's own restatement, cross-confirmed: "The agent loop is not where anyone competes. The product is the harness around it — context/cache discipline, tool design, error recovery, memory, evals, durable state" (`00_LENS.md`; harvest line 17095).

**Teardown evidence (the loop really is trivial across all of them):**
- Claude Code / Codex / Manus: "Every production system uses a 5-10 line while loop. The reference implementation from the Rust rewrite of Claude Code is **88 lines total including error handling**. The 10,000+ engineering lines are in [the harness]" (`autonomous_coding.md`, harvest line 15563-15574).
- Agno: the canonical ReAct `while True:` lives in `Model.response()` at `models/base.py:697`, **not** in `Agent`; `Agent.run()` calls the model once and the loop is a method elsewhere (`agno.md §1`). The "interesting" code is deferred-init perf and compression, not the loop.
- Dify: "the Dify CoT runner is a reference implementation of the 5-10 line durable loop" (harvest line 9415).
- Google ADK: "ADK is not a graph, not a crew, not handoff" — the whole multi-agent tree is nested `async for` over `async def run_async(ctx) -> AsyncGenerator[Event]`, no scheduler, no message bus; the loop is a bare `while True:` at `base_llm_flow.py:851` (`google_adk.md §1`).

**The competitive corollary (`re_startups_map.md §1.1`):** "Every major framework — LangGraph, Strands, Pydantic AI, OpenHands, Mastra, CrewAI, LlamaIndex — ships roughly the same 5-10-line ReAct/function-call loop. The loop is not where anyone competes." The product is the event-sourced state machine, durable checkpoints, HITL-as-status, stuck detection, tool-call normalization.

**Why this is the bet and not just an observation:** BAD_GUIDE's wrapper test (`bad_guide_build_positioning.md §2`): "Swap the underlying model out in your head. If the product is basically the same, you built a wrapper." A loop-only framework fails this test — GPT-7 ships and the loop is unchanged and undifferentiated. The harness primitives (cache stability, cost accounting, backpressure) survive every model release. **Durable beats depreciating** (`bad_guide_build_positioning.md §9`): byte-path transport ownership, KV-cache prefix stability, backpressure are structural; specific loop tactics churn on the ~1-2yr model clock.

**Implication for Lancelot:** build the loop as ~10 lines and *stop there* (`autonomous_coding.md`: "Build it as ~10 lines; do not complicate it"). Spend the engineering on the surrounding primitives — and only the ones the byte path uniquely enables.

---

## 2. THE WEDGE — a lean agent layer on a Rust byte path → cost-effective by construction

Lancelot is structurally different from Pydantic AI / LangGraph because it already owns the byte path (`00_LENS.md §"Lancelot's existing byte path"`). The wedge is **not** raw throughput — the honest envelope forbids a blanket "faster" claim (`CLAUDE.md`; `bad_guide_build_positioning.md §5`). The wedge is **cost-effectiveness that falls out of owning the transport layer**, expressed as four primitives the competitive map confirms are white space (`re_startups_map.md §1.10`):

### 2.1 Prompt-cache-stable serialization (the #1 free cost lever)
- `lancelot.canonical.dumps_canonical` → byte-stable sorted-key JSON (`lancelot_model_io.md §4`). Genspark's rule: never reorder history; append-only → >80% cache hit → **5–10× token cost reduction** (`transcripts_dlai.md §2`). "Why frameworks break this: LangChain/LlamaIndex silently add tokens, timestamps, or reorder history — invalidating the cache prefix on every turn" (`transcripts_dlai.md §2`).
- Teardowns prove the production stakes: Browser-Use *inflates* its system prompt to ≥4096 tokens with a dedicated 240-line variant *solely* to hit Anthropic's cache threshold; break-even ~$0.30/100 steps (harvest line 1453-1455). OpenHands pins a stable cache key per `conversation_id` to hit cache on resume (harvest line 1196). Devin: "most tasks take up to 30% fewer tokens … primarily through enhanced prompt caching; switching models busts the cache" (harvest line 4548).
- **Competitive white space #3** (`re_startups_map.md §1.10`): "Canonical/KV-cache-stable serialization as a first-class primitive … referenced nowhere in [the 24-startup map]. Lancelot owns this."

### 2.2 Batch coalescing against rate-limited upstreams
- `@app.batch` (prefix-aware request grouping, `size_fn`/`max_size` budget trigger, `lancelot_serve_tools.md §11`): **~12× throughput / ~16× fewer upstream calls** vs a rate-limited LLM backend (`00_LENS.md`).
- **Competitive white space #2** (`re_startups_map.md §1.10`): "Backpressure-aware batch coalescing — no analogue in any product in this corpus. Composio batches multi-tool calls but not prefix-aware request grouping to a rate-limited LLM backend. Lancelot's ~12× throughput win is unchallenged."

### 2.3 In-band, per-call cost accounting across sub-calls
- `cost_usd` ContextVar accumulates across every TransportPool call (including `spawn_child` sub-agents) → `X-Lancelot-Cost-USD` response trailer (`lancelot_model_io.md §2-3`). `upstream_metrics` does provider detect + token extraction across 3 shapes + `calc_cost_usd` (ships NO rates — `set_rate_table` / `LANCELOT_LLM_RATES`, correct because rates churn weekly).
- **Competitive white space #4** (`re_startups_map.md §1.10`): "Byte-path-level cost accumulation across sub-calls. Langfuse/Phoenix track cost at the trace level (post-hoc). Lancelot accumulates cost in-band in the response trailer, including child spawn_child calls. No framework does this at the transport layer." BAD_GUIDE names this the UNIQUE position: "per-agent cost accounting with byte-path granularity … This is a UNIQUE position" (`bad_guide_ai_research.md §9`).

### 2.4 Real streaming: TTFT + backpressure + disconnect→cancel
- R3 pump (one bridge crossing/stream, GIL dropped per `__anext__`, event-driven backpressure), `lancelot.http.stream.ttft` histogram, client-disconnect → upstream cancel (`lancelot_serve_tools.md §1`).
- The transcripts make TTFT *the* user-facing AI metric: ">200–300 ms → perceived lag"; "average latency is a lying metric — track P95/P99 + TTFT + streaming token rate" (`transcripts_dlai.md §1`). "Users abandon applications that take even 3 seconds" (Groq, same).
- **Competitive white space #1** (`re_startups_map.md §1.10`): "Stream-first observability with TTFT on every shape. Every agent framework emits traces, but TTFT is absent from all framework-level trace data in this corpus. Lancelot already has `lancelot.http.stream.ttft` — a genuine differentiator on the byte path."

**Why these compound (the harness-is-moat mechanism):** Malte Ubl's v0 took Sonnet from 64.7% → 93.9% error-free with **four engineering layers, not a model upgrade** (RAG injection, stream-time fixing, AST validation, AutoFixer) — "the compounding moat is the harness around the model, not the model itself; it survives every frontier release" (`bad_guide_build_positioning.md §6`; `bad_guide_ai_research.md §1`). Lancelot's four byte-path primitives are exactly that kind of harness layer: model-agnostic, durable, and unavailable to a pure-Python wrapper.

---

## 3. BUILD vs COMPOSE vs REFUSE

The cardinal rule (`CLAUDE.md`): "No bullshit, no useless logic. Every change must change the byte path or the operator experience." The byte-path inventory already classified every candidate primitive as REUSE / EXTEND / BUILD (`lancelot_model_io.md §6`, `lancelot_serve_tools.md §"Cross-cutting"`). Mapped to the positioning:

### 3.1 BUILD (the lean agent half — small, durable, the actual gap)
These are missing AND durable across 3+ model generations AND not provided by the byte path. Build them *minimally*.

| Build target | Mechanism (cited) | Size discipline |
|---|---|---|
| **Agent loop** (ReAct, `StopReason`, budget, stuck-detect, exit-condition) | OpenHands stuck detector: scan ≤20 events, thresholds 4/3/3/6 (`re_startups_map.md §1.5`); Haystack `exit_conditions=["tool_name"]` (`transcripts_dlai.md §5.4`) | ~10 lines core (`autonomous_coding.md`); recovery is the density, not the loop (`hermes_core.md`, harvest 7683) |
| **Tool-input repair + error taxonomy routing** | CrewAI 4-stage ladder `json.loads → ast.literal_eval → json5 → repair_json`, `SequenceMatcher > 0.85` fuzzy name fix (`crewai.md §2`, harvest 3618-3692); route on `upstream_metrics.classify_status` → `permanent`(422)=fix-input, `quota`=back-off, `transient`=retry (`lancelot_model_io.md §2.3`) | pure Python predicate; "belongs in `lancelot.tool`" (harvest 3692) |
| **Tool-output verification gate** | `@app.tool(verify=lambda result, ctx: ...)` synchronous predicate post-call (Verifier's Law: verify cheaper than generate — Jason Wei; "build the verifier first" — `bad_guide_ai_research.md §2`); the #1 cause of failed AI products is no eval (Hamel, same) | zero new deps |
| **Minimal eval harness** | `lancelot.eval.run(cases, judge)` → `{pass, fail, failures}`; binary pass/fail > Likert; align judge to human on 50 cases (`bad_guide_ai_research.md §5`); 10→50→100→1000, release at 999/1000 (`transcripts_yc_root.md §2a`); categorical `rails=["correct","incorrect"]` not numeric (`transcripts_dlai.md §3.1`) | "~50-line harness that calls the agent's own Lancelot endpoint" — NOT a LangSmith (`transcripts_yc.md §5`) |
| **ContextBundle (3-layer, append-only)** | `<system>/<developer>/<user>` XML inheritance (Parahelp 6-page prompt, `transcripts_yc.md §6`); append-only enforced for cache (`transcripts_dlai.md §2`); compaction hook at 70–80% fill (`transcripts_dlai.md §2`) | "pure string formatting; ContextVars feed it; no new dep" (`transcripts_yc.md §6`) |
| **Tool permission gate** | enforce `ToolSpec.read_only` + `scopes` (fields exist, unenforced — `lancelot_serve_tools.md §2`); HITL-as-named-status, concurrency forced to 1 when approval possible (`re_startups_map.md §2.2`) | reads existing fields |

### 3.2 COMPOSE (already built — REUSE/EXTEND, do not rebuild)
The byte path is ~half an agent framework's plumbing, battle-tested under load (`00_LENS.md`). REUSE verbatim:
- **Outbound model I/O:** `TransportPool.request/stream/relay/fanout/spawn_child` — retries + Retry-After, POST-never-retried, budget cap, mTLS, idempotency-key, client-disconnect→upstream-cancel (`lancelot_model_io.md §1`). `fanout(first_n, cancel_laggards)` *is* the panel-of-models / parallel-probe primitive (`bad_guide_build_positioning.md §6`; Chroma multi-probe `transcripts_yc_root.md §3a`; Replit CoW parallel-sampling analogue `transcripts_yc.md §1`).
- **Tools + MCP:** `@app.tool → ToolSpec` (auto input+output schema), `app.tool_schemas()`, `app.call_tool()`, `lancelot.mcp.mount` with 4 KB minimal-return WARN — *validated verbatim* by Dex Horthy ("MCP tools returning JSON blobs = anti-pattern", `transcripts_yc_root.md §1c`) and the field's task-oriented-tool doctrine (`transcripts_dlai.md §7`).
- **Streaming substrate:** R3 pump + `Event`/`sse_adapter`/`_clean_field` + TTFT + `defer_status`/`max_stream_lifetime`/`error_frame` (`lancelot_serve_tools.md §1`). The AG-UI 16-event protocol rides on this transport unchanged (`transcripts_dlai.md §12`).
- **Resilience/observability/safety infra:** RED metrics, `assert_safe_url` SSRF guard (covers CGNAT + IPv4-mapped IPv6 — pass `block_private_ips=True` for any fetch/browse tool), `lancelot.secrets.get` (5 backends + SIGHUP), `TenantConcurrencyLimiter`, `RateLimit`, `webhook.sign/verify`, `app.lifetime_task`, `cancel_on_disconnect` (`lancelot_serve_tools.md §6-11`).
- **Cost/trace context:** `cost_usd`/`tokens_in/out`/`agent_id`/`trace_id` ContextVars + auto `traceparent` injection — feeds Langfuse/Phoenix trace trees with zero extra instrumentation (`re_startups_map.md §1.8`).

The COMPOSE test (`autonomous_coding.md`, Mastra): "Remove the workflow engine and the agent loop does not exist. That is the test for a rebuilt primitive vs. a wrapper." Lancelot's byte path passes — remove `TransportPool` and the agent has no model I/O.

### 3.3 REFUSE — the Tier-D do-not-build list (positioning edition)
Each refusal is grounded in the teardowns showing the thing is overhead, not value. This extends `CLAUDE.md`'s existing Tier-D into the *agent-layer* space.

1. **A universal-Runnable / graph-composition layer.** LangChain's "8,000-line runtime exists to track the graph — the tracing infrastructure IS the complexity" and "every 'what does this chain do' becomes 'trace the Runnable graph'… the benefit is wiped out because the interesting units are already well-typed" (`langchain.md`, harvest 8072). Verdict (harvest 8074): "Do NOT build a universal-Runnable interface for Lancelot. Typed function → tool call → observation → loop is 50 lines. Any generic composition layer imposes the LangChain overhead tax for no user-visible win."

2. **A graph-DSL / StateGraph / node-DAG.** ADK ships zero graph DSL and is fine (`google_adk.md §1`); ADK's own `WorkflowAgent`/`FunctionNode`/`ParallelWorker` graph layer "does NOT exist in v1.33.0 or v2.0.0 — unreleased preview… Do not implement" (harvest 7014). LangGraph v1 is "a 200-line StateGraph builder over the Pregel runtime" (harvest 8168) whose value (durable checkpoint/resume) is a *user-side* durable-execution concern, not transport (`durable_exec.md`, harvest 5563). Topology-hash checkpointing is a real durable-seam idea (`langgraph.md`, harvest 985) but it lives **above** the byte path — Lancelot serves the submit/status/cancel HTTP surface; the user owns durability (positioning lock).

3. **Agent-strategies-as-plugins.** Dify's plugin agent-strategy system is "600+ lines and replaces direct Python function calls. The byte path's job is fast transport; the strategy is 5-10 lines of Python the user writes" (`lowcode_frameworks.md`, harvest 9498).

4. **A memory-class hierarchy.** "Do NOT copy any memory class hierarchy. A `messages: list` state key + a single `compaction(messages, threshold) -> messages` hook is the correct primitive" (`langchain.md`, harvest 8177; LangChain's `SummarizationMiddleware` is 658 lines for what is one hook). Typed 4-tier agent memory itself stays user-side (`re_startups_map.md §1.3`, Mem0 is the reference; usable via `lancelot.mcp.mount`).

5. **Per-chunk stream optimization / a generic streaming compressor.** Upstream-rate-limited (~50–200 tok/s); "the framework is never the bottleneck — the LLM is" (`transcripts_yc_root.md §8.1`; `CLAUDE.md` Tier-D). Reintroducing per-chunk cross-thread bridging re-creates the 16–26× cliff (`lancelot_serve_tools.md §"Anti-patterns" 1`).

6. **A wire-compatible model gateway / `/v1/chat/completions` router.** "Portkey, Helicone, LiteLLM all do this. Not Lancelot's lane" (`re_startups_map.md §1.10`). `TransportPool` already owns the routing-mode/Retry-After byte-path analogue for *outbound* calls (`re_startups_map.md §1.2`); building an inbound multi-provider gateway is someone else's commodity.

7. **Fine-tuning / model-specific anything as the moat.** "Fine-tuning is usually a capital trap; the next base release erases your edge. Model-agnostic harnesses age better" (`bad_guide_build_positioning.md §9`; Poetic AI "vaccinated against the bitter lesson" `bad_guide_ai_research.md §9`). Any primitive mentioning a specific model name is a ~12-month liability.

8. **Elaborate multi-agent orchestration with parallel writes.** Walden Yan (Cognition): "multi-agent is a write-conflict problem, not a concurrency problem… parallel agents make conflicting implicit decisions that produce broken output" (`bad_guide_ai_research.md §6`). Rule: `fanout` for **opinions/reads**, single-threaded synthesis before any **write**. `spawn_child` already carries the single-trace context this requires.

9. **A generic CLI (`lancelot run …`).** FastAPI itself delegates to `fastapi-cli` (separate package); not core (`CLAUDE.md` Tier-D, carried forward).

---

## 4. NON-GOALS (what Lancelot's agent layer is NOT)

Stated as positioning, each maps to a refusal above:

- **Not a graph-DSL.** No StateGraph, no node-DAG, no Pregel. "ADK is not a graph, not a crew, not handoff" is the model to emulate (`google_adk.md §1`). Linear where the best human is linear; agentic only where they loop back (Jake Heller, `transcripts_yc_root.md §1a`; "the failure mode: defaulting to agentic because it sounds more sophisticated").

- **Not a router cult.** Model routing (classify→tier) has direct P&L impact (`transcripts_yc.md §8`) but is **user-configured policy**, not framework machinery — the precedent is `LANCELOT_LLM_RATES` (Lancelot ships no rates; user provides the table). A `ProviderProfile` + `route(complexity)→profile` is a thin user-side seam, not a built-in router product. Inbound gateway routing is explicitly someone else's lane (§3.3-6).

- **Not MCP-first.** MCP is supported (`lancelot.mcp.mount`) but **not the center of gravity**, because the corpus is unanimous that MCP-eats-context at scale: 50+ tools = ~72K tokens upfront, blowing the KV-cache prefix every session (harvest 15652); 58 tools ≈ 55K tokens before any conversation (harvest 16300); Perplexity moved internal orchestration "**away from MCP toward direct APIs**" precisely because accumulated schemas compound (`perplexity.md §4`, harvest 12740). For Lancelot's `@app.tool` (schema registered at decoration, not per-request) the problem doesn't manifest — but it would if a future adapter re-serialized schemas per call (harvest 12859). MCP is a *surface*, the byte path is the *product*.

- **Not a replacement for AI logic.** Agent strategies, vector stores, prompt templates, durable workflow engines stay user-side (positioning lock, `CLAUDE.md`). "We own the byte path; the user owns the agent."

- **Not "a faster server."** The honest claim is "beats FastAPI and Robyn on every shape" + the four cost-effectiveness wins — never a blanket throughput claim (`CLAUDE.md`; `bad_guide_build_positioning.md §5,§9`).

---

## 5. NAMING & API SHAPE

**Naming discipline (from the corpus, not invented):** the durable thing is the *mechanism*, never the brand name. OpenClaw's `soul.md` and Claude Code's `CLAUDE.md` are the same pattern — a file read at startup and injected into the system prompt; "the exact file name is not [durable]; the mechanism (behavioral profile file injected at startup) is" (`transcripts_yc.md §"hype/depreciating" 4`). So:

- Lead with **byte-path verbs the model can read**, mirroring the existing surface: `dumps_canonical`, `set_session_affinity`, `@app.batch`, `@app.stream`, `@app.tool`, `mcp.mount`. These already test-pass the primitive-in-one-sentence rule (`bad_guide_build_positioning.md §1`).
- The agent layer should follow the same flat, opinionated naming: `Agent(...)`, `@app.tool(verify=...)`, `lancelot.eval.run(cases, judge)`, `ContextBundle` — **not** `AgentGraphBuilder`, `RunnableSequence`, `WorkflowOrchestrationEngine`. Fewer, more opinionated primitives beat infinite flexibility (Linear collapsed 98 color vars → 3; `bad_guide_build_positioning.md §4`).
- **Opinionated constants ARE the moat** (`bad_guide_build_positioning.md §3-4`): `LANCELOT_STREAM_BUFFER=256`, `max_response_bytes=4096`, `LANCELOT_HEADER_TIMEOUT_MS=15000` "are not just defaults — they are the load-test findings crystallized into config." Any agent-layer default (e.g. stuck-detect thresholds 4/3/3/6, retry 6/4s/240s, context ceiling ~75%) should be documented as "this is the production-tested number," with an escape hatch.
- Positioning line for the buyer's head (Matt Lerner, copy-before-features, `bad_guide_build_positioning.md §7`): **"I need my streaming to not break under load and my LLM bill to stop scaling with traffic."** Not "Rust HTTP framework."

---

## 6. THE COST-EFFECTIVENESS STORY

Two layers: how it's *achieved* (mechanism) and how it's *sold* (framing).

**Achieved (cost falls out of the byte path — §2 in one place):**
- Prompt-cache prefix stability via `dumps_canonical` → >80% cache hit → 5–10× token reduction (`transcripts_dlai.md §2`).
- `@app.batch` → ~16× fewer upstream calls against rate-limited backends (`00_LENS.md`).
- `fanout(first_n, cancel_laggards)` → take first-N good completions, cancel the rest = the panel-of-models cost win (budget panel within 1% of frontier at ~half price — OpenRouter Fusion, `bad_guide_build_positioning.md §6`); "burn tokens, not headcount" (Jared Friedman, `transcripts_yc_root.md §5a`).
- In-band `cost_usd` accumulation + `X-Lancelot-Cost-USD` trailer → cost is *instrumented*, not estimated post-hoc (`re_startups_map.md §1.10`).
- The single-token / answer-first trick on classification tool outputs (`max_tokens=1` + stop words) → ~200× decode reduction on classifier steps (`transcripts_yc_root.md §5a`); `@app.tool` output ordering should document this.

**Sold (the framing rule — do NOT lead with "cheaper"):**
BAD_GUIDE is explicit (`bad_guide_build_positioning.md §5,§7`): "Sell AI that drives revenue (no ceiling), not just cuts cost (ceiling = headcount displaced)." So the headline is reliability/SLA, the cost win is the *proof*:
- **Dream outcome:** "your AI API meets its TTFT SLA under concurrency, and your LLM bill stops scaling linearly with traffic."
- **Perceived likelihood:** "validated under adversarial `oha` load, not just TestClient" — the M4 lesson ("shipped + green unit tests ≠ works under load") is itself the moat against wrapper frameworks (`transcripts_yc.md §10`).
- **Time delay / effort:** "10-minute `pip install lancelot` swap; zero-config const tier; one decorator `@app.batch`" (`bad_guide_build_positioning.md §7`).
- Outcome-priceable because the levers are instrumentable: LLM-cost reduction (cache + batch), TTFT p99, uptime (graceful drain) — quote the honest benchmark numbers (`bad_guide_build_positioning.md §7`). Manus is the existence proof of the cost-effective-by-architecture thesis: 86.5% GAIA at **~$2/task** via planner/executor routing, vs o3 at 20–40× the cost for marginal gain (`transcripts_yc.md §1,§8`).

**Honesty flag (`bad_guide_build_positioning.md §9`):** the `@app.batch` 12× and TTFT-cliff numbers are valid *today* against rate-limited APIs / current inference cost; they may soften on the 1–2yr clock as on-prem inference changes the bottleneck shape. The *durable* claims are the structural ones (transport ownership, cache stability, backpressure). Lead with durable, cite depreciating as current proof.

---

## 7. DISTRIBUTION

The corpus gives a specific, low-cost channel — "agents as distribution, developers as users" (`re_startups_map.md` framing; `transcripts_yc_root.md §6`; `transcripts_yc.md §9`):

1. **Be the API coding models recommend.** Recall.ai/Supabase/Resend get inbound *because* their docs are structured + complete + cover edge cases, so LLMs trained on them emit those APIs as the default answer ("top-3 source of new signups: from ChatGPT" — Resend; `transcripts_yc.md §9`). Open-source = larger training footprint → more model recommendations, zero acquisition cost (`transcripts_yc_root.md §6`).
2. **Docs as context-window engineering.** New docs are "designed to fit inside a coding agent's context window in a single load, no multi-page hyperlinked structure" → higher integration accuracy (`transcripts_yc_root.md §6`). Ship an `llm.txt`/`llm.md` (or generate it from `app.tool_schemas()`) with example calls, not just JSON Schema (`transcripts_yc.md §9`). Informative errors are part of this — the Rust server's Levenshtein-404 already aligns (`transcripts_yc.md §9`).
3. **MCP server as a second (LLM) user surface.** Trigger.dev/Infisical expose docs + "skills" via MCP so coding agents integrate correctly ("two users now: human and LLM"; `transcripts_yc_root.md §6`). `lancelot.mcp.mount` already provides this — but per §4, keep it a surface, not the pitch.
4. **The shareable demo moment.** Frictionless entry beats any campaign (Cursor free no-card fork; Bolt.new matched 7yr ARR in 48h off one tweet; `bad_guide_build_positioning.md §8`). For Lancelot the spike-driver is a real `oha` trace: "`@app.batch` 12× / ~16× fewer upstream calls" or "TTFT p99 under concurrency" as an X thread.
5. **Personal monopoly lane** (`bad_guide_build_positioning.md §8`): "Rust HTTP performance + Claude-Agent-SDK ops + Central-Asian operator" — a crossing with one occupant.

---

## 8. SUMMARY — the brutal four-line creed

1. **The loop is ~10 lines; refuse to compete on it.** Build the loop minimally; spend engineering on the harness primitives the byte path uniquely enables.
2. **The wedge is cost-effective-by-construction, not "faster":** cache-stable serialization (5–10×), batch coalescing (~16× fewer calls), in-band per-call cost accounting, real TTFT+backpressure streaming — all four are competitive white space (`re_startups_map.md §1.10`).
3. **COMPOSE the byte path** (TransportPool, tools/MCP, R3 streaming, SSRF/secrets/tenant/webhook, cost/trace context) — it's already ~half an agent framework, battle-tested under load. **BUILD** only the lean agent half (loop, tool-repair+error-routing, verify gate, ~50-line eval harness, 3-layer append-only ContextBundle, permission gate).
4. **REFUSE the machinery:** no universal-Runnable graph layer (LangChain's 8K-line tax), no graph-DSL/StateGraph, no agent-strategy plugins, no memory-class hierarchy, no per-chunk stream optimization, no inbound model gateway, no fine-tuning moat, no parallel-write multi-agent. **Non-goals:** not a graph-DSL, not a router cult, not MCP-first, not an AI-logic replacement, not "a faster server."

---

*Sources, by weight: `02_core/bad_guide_build_positioning.md`, `02_core/bad_guide_ai_research.md`, `02_core/re_startups_map.md`, `02_core/transcripts_yc_root.md`, `02_core/transcripts_yc.md`, `02_core/transcripts_dlai.md`, `04_byte_path/lancelot_model_io.md`, `04_byte_path/lancelot_serve_tools.md`. Teardown anchors: `langchain.md`, `google_adk.md`, `agno.md`, `crewai.md`, `perplexity.md`, `autonomous_coding.md`, `lowcode_frameworks.md`, `durable_exec.md`, `langgraph.md`, `hermes_core.md`, `browser_use.md`. All line numbers prefixed "harvest" reference the concatenated teardown+core+byte_path stream read for this synthesis; all other cites are file + section.*
