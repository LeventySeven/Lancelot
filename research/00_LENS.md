# Lancelot Agents — research lens (read this first)

**Mission.** Build a lean, brutal agent framework *for Lancelot*, grounded in a genuine fresh harvest of the primary sources. The methodology (the user's own words): **"just build agents, brutal."** Not 500+ graphs/nodes. Real, effective, stable, **cost-effective** agents. No complexity for complexity's sake, no chasing 1% wins — only primitives that earn their keep, drawn from real founders / researchers / production systems. The thesis the corpus keeps converging on: *the agent loop is 5-10 lines and is not where anyone competes; the product is the harness around it — context/cache discipline, tool design, error recovery, memory, evals, durable state.* Ship the boring durable core clean and small; make the things big frameworks bury in 3,000-line files into first-class primitives.

**This is an independent build for Lancelot.** (The user has built an agent framework before; that prior project — "Verso" — is mentioned only as a reference for the *type of job and the quality bar*. Do NOT read it, port it, depend on it, or treat its decisions as settled. Mine the PRIMARY sources directly and decide fresh, for Lancelot.)

**The Lancelot wedge.** Lancelot is already a **Rust byte-path framework** (HTTP transport / serialization / streaming / resilience) — so a large chunk of an agent framework's *plumbing is already built and battle-tested under load*, and the agent layer rides a byte path that pure-Python frameworks (Pydantic AI, LangGraph, etc.) structurally cannot offer. Your job, per source: extract the best agent primitives **and** flag where Lancelot's byte path already provides, changes, or obviates them.

---

## The quality bar (from the BAD_GUIDE field manual — non-negotiable)

1. **Primary + production sources only.** The actual framework code / founder / researcher, mechanism-level. No listicle/SEO/summary-of-summary filler.
2. **No basics.** Skip 101s. Deliver the 80/20 only experienced operators know: the non-obvious mechanism, the hard-won constant, the production gotcha.
3. **Mechanism, not vibes.** "CrewAI repairs tool input via `json.loads → ast.literal_eval → json5 → repair_json` then `SequenceMatcher>0.85` fuzzy name fix" — NOT "CrewAI has good error handling." Cite the exact knob/constant/file.
4. **Quality > quantity.** Returning few high-signal items is correct. Padding is failure.
5. **Honesty.** If a primitive is hype, off-thesis, or a depreciating tactical hack (churns on a ~1-2yr model clock), say so. The durable layer (loop / context / memory / evals / orchestration) is what persists across 3+ model generations — weight it.
6. **Every claim cites its source** (the teardown/transcript filename + section).

---

## Lancelot's existing byte path (what is ALREADY BUILT — do NOT propose rebuilding these)

The Python package is `~/Desktop/Lancelot/lancelot/`. Confirmed primitives:

- **Model/transport I/O — `lancelot.llm.TransportPool`**: `request` / `stream` / `fanout(first_n, deadline, cancel_laggards, concurrency)` / `spawn_child(parent, session_affinity)` / `relay`. Retries w/ exp-backoff **+ Retry-After honoring**, token-bucket `rate_limit`, **mTLS** (`reload_mtls` SIGHUP), auto-injects `x-request-id`+`x-agent-id`+**`traceparent`** + Idempotency-Key, idle-guard, client-disconnect→upstream cancel, cost emission. *This is the outbound LLM-call substrate — a big part of Verso's `model/` layer, already done.*
- **Upstream metering — `lancelot.upstream_metrics`**: provider detect (OpenAI/Anthropic/Google/Groq/DeepSeek/Mistral/Together/Fireworks), token extraction across 3 provider shapes, `calc_cost_usd` + env rate table (Lancelot ships NO rates), **error taxonomy** `ok/quota/timeout/permanent/transient/upstream_5xx`, 6 OTel histograms `lancelot.upstream.*`.
- **Cost/trace context — `lancelot.context`** ContextVars: `cost_usd`, `tokens_in/out`, `agent_id`, `tenant_id`, `request_id`, `trace_id`, `queue_wait_ms`. Cost accumulates into the parent; `X-Lancelot-Cost-USD` trailer.
- **Tools — `lancelot.tool`** (`@app.tool` → `ToolSpec` w/ auto JSON schema + auto `output_schema`); **`lancelot.mcp.mount`** (MCP server mount; minimal-return >4KB WARN discipline).
- **Streaming — `lancelot.stream`** (`@app.stream`): Rust R3 pump (async-gen → bounded-mpsc → hyper, GIL dropped per chunk, **backpressure**, graceful drain, disconnect→cancel), SSE keepalive, `[DONE]`, `LANCELOT_STREAM_BUFFER`. **TTFT metric** `lancelot.http.stream.ttft`. Parity-to-1.3× vs Starlette.
- **Cache stability — `lancelot.canonical.dumps_canonical`**: stable sorted-key JSON bytes for **prompt-/KV-cache prefix stability** (the #1 free cost lever, already a primitive).
- **Batch coalescing — `lancelot.batch`** (`@app.batch`): prefix-aware request grouping, **~12× throughput / ~16× fewer upstream calls** vs a rate-limited LLM backend. No analogue in FastAPI/Robyn.
- **Resilience/observability**: RED metrics, body/conn caps, slow-loris guard, per-route timeout, graceful drain, `/livez`+`/readyz`, `/fleet/status`, region routing, **`lancelot.secrets`** (5 backends + SIGHUP), **`lancelot.net`** SSRF guard, **`lancelot.tenant`** per-tenant concurrency, **`lancelot.webhook`** (Standard Webhooks sign/verify), `lancelot.ratelimit`.
- **Serving**: the Rust HTTP/1.1 server (hyper/tokio/pyo3); **const tier** 183k rps prebaked zero-GIL; multiprocess scaling (`LANCELOT_WORKERS`); ASGI seam; uvloop embedded loop.
- **Runtime dep is `pydantic>=2.7` ONLY** (cardinal rule). Everything else lazy/test-only. The agent layer MUST honor this: lazy-import provider SDKs, no new hard deps.

## What Lancelot LACKS (the agent half — the build target)

The agent LOOP (ReAct, named `StopReason`, budget, grace call, stuck detect) · **context engineering** as a primitive (ContextBundle assembly, observation-masking compaction, `inspect()`) · the **model adapter/translation** layer (ProviderProfile / OpenAI-canonical→N-dialects / OMIT sentinel — TransportPool does *transport*, not *message-shape translation*) · **typed memory** (4-tier, RRF retrieval, temporal — `memory_pool` is byte-OOM, not agent memory) · **evals** (unit / judge panel / define_outcome) · the **durable seam** (event-sourced checkpoint/resume/HITL) · agent-level **safety** (permissions allow/deny/ask/defer, confidence router, 5-stage hooks) · **orchestration** (subagent context-isolation, fan-out — though `TransportPool.fanout`/`spawn_child` are byte-path analogues to reuse).

---

## Source corpus (mine the PRIMARY sources directly)

This is a fresh, independent harvest. Read the actual source assigned to you, in full:
- `~/Desktop/guidesfm/BAD_GUIDE.md` — the top-1% founder/CTO/CPO/AI-researcher field manual (taste, positioning, eval craft, "just build agents").
- `~/Desktop/researchfms/core/` — primary transcripts (YC / a16z / DeepLearning.AI / Stanford / Palantir / Microsoft / Google) + the agentic-architecture synthesis docs + RE docs.
- `~/Desktop/researchfms/teardowns/` — reverse-engineered teardowns of real agent frameworks + production agents + infra (model gateways, memory, evals, durable execution, retrieval, serving).

Go to the raw code/transcript for the load-bearing mechanism — the exact knob, constant, file, prompt. Don't summarize secondhand.

---

## Your output contract

1. **Write a note** to the path given in your task (under `~/Desktop/Lancelot/research/`), mechanism-level, every claim cited.
2. **Return the structured digest** per the schema: `copy` (best primitives, mechanism + why + cite), `avoid` (anti-patterns), `byte_path_implication` (does Lancelot's byte path provide/change/obviate this?), `api_shape_notes` (ergonomics worth stealing), `note_path`.

You have FULL tool access (Read, Grep, Bash, WebFetch, etc.). "Pure research output" restricts your OUTPUT to findings — it does NOT restrict your inputs. READ THE FULL SOURCE. Do not guess; if a file is large, read it in pages or grep to the load-bearing sections.
