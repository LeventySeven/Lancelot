# Streaming — layer brief (Lancelot agent framework)

**Altitude:** "just build agents, brutal." The smallest streaming primitive that earns its keep — anti-overkill AND anti-underkill.
**Date:** 2026-06-22
**Synthesizer inputs:** `03_teardowns/{vercel_ai_sdk,pydantic_ai,claude_agent_sdk,openai_agents,letta,mastra,deep_research,browser_use,manus}.md` · `02_core/{transcripts_dlai,agentic_deep,bad_guide_build_positioning}.md` · `04_byte_path/{lancelot_serve_tools,lancelot_model_io}.md` · direct read of `lancelot/stream.py`.

---

## TL;DR — the one-paragraph decision

Lancelot already owns the hard 80% of agent streaming: the **R3 pump** (async-gen → bounded mpsc → hyper, GIL dropped per `__anext__`, backpressure event-driven, disconnect→cancel), **SSE framing** (`Event`/`sse_adapter`/`_clean_field`), **TTFT metric**, `[DONE]` sentinel, and five `@app.stream` knobs (`sse`, `defer_status`, `max_stream_silence`, `max_stream_lifetime`, `error_frame`). This is *faster and more correct* than the TS stacks (Vercel/Mastra ship `ReadableStream` on the event-loop thread; Lancelot drops the GIL) and is the marquee per the byte-path notes (`lancelot_serve_tools.md §1`). The agent layer must **NOT rebuild any of it.** What it must BUILD is thin and sits entirely on top: **(1) one normalized event taxonomy** (~12 typed parts, true-delta convention, reasoning as its own channel) — copied from the *intersection* of Vercel's 16-part union (`vercel_ai_sdk.md §2.4`), Pydantic AI's 4-event `part_start/delta/end/final` (`pydantic_ai.md §9`), and AG-UI's 16 events (`transcripts_dlai.md §12`), NOT the 86-chunk (Mastra) or 27-chunk (Vercel UI) explosions; **(2) a stream normalizer** — the 5 production fixes every framework hand-rolls (tool-calls-win-over-text, empty-first-chunk suppression, mid-stream `finish_reason:length` → targeted error, blank-text strip, deterministic sub-message IDs); **(3) a `[DONE]`/terminal guarantee + cooperative cancel** (`athrow`) wrapping the agent loop; and **(4) resumability DEFERRED** to an optional `seq_id`-cursor backend (Letta's Redis-Streams pattern, `letta.md §8`), gated behind background-run support — do not build it day-1. All of this is pure-Python on the existing pump; **zero new runtime deps** (cardinal rule holds).

---

## 1. What the corpus actually converges on (the load-bearing signals)

### 1.1 Streaming IS the AI-UX metric — TTFT, not throughput
- "TTFT > 200–300 ms → perceived lag"; "< ~15 tok/s feels slow; 30+ feels responsive"; **average latency is a lying metric — track P95/P99 + TTFT** (`transcripts_dlai.md §1`, Groq/Nebius). Lancelot's `lancelot.http.stream.ttft` histogram (fires on first non-keepalive chunk, route-template label) already captures the exactly-right number — confirmed in three independent notes (`transcripts_dlai.md §1/§11`, `agentic_deep.md §16`, `lancelot_serve_tools.md §4`). **This is the single biggest reason the streaming layer is a thin wrapper, not a rebuild: the metric that matters is already wired.**

### 1.2 The event taxonomy has CONVERGED across every serious framework
Five independent designs, same shape — a small set of **typed parts**, each carrying **true incremental deltas** (not cumulative snapshots), with **start/delta/end** lifecycle and a terminal **finish carrying usage + finish-reason**:

| Source | Part/event count | The durable core (what they all share) | cite |
|---|---|---|---|
| Vercel `LanguageModelV2StreamPart` | **16** (provider layer) | `stream-start`, `text-start/delta/end`, `reasoning-start/delta/end`, `tool-input-start/delta/end`, `tool-call`, `tool-result`, `finish` (usage+reason), `error`, `raw` | `vercel_ai_sdk.md §2.4` |
| Pydantic AI | **4** | `part_start` / `part_delta` / `part_end` / `final_result`; delta kind ∈ `{text, thinking, tool_call}`; each delta has `.apply(part)` | `pydantic_ai.md §9` |
| AG-UI (CopilotKit) | **16** | `run_started/finished/error`, `text_message_start/delta/end`, `tool_call_start/delta/end/result`, `state_snapshot/delta`, `raw` | `transcripts_dlai.md §12` |
| Letta | (typed sub-messages) | `reasoning_message`, `tool_call_message`, `assistant_message`, `ping`, named control frames `event: cancelled/error` | `letta.md §15, §A.shape` |
| Mastra | **86** (too many — see refuse) | same lifecycle core buried under HITL/background/multi-agent variants | `mastra.md §6` |

**The intersection is ~10–12 part types.** That is the build target. The reasoning/thinking channel is universal (Vercel `reasoning-*`, Pydantic `thinking`, AG-UI absent-but-`raw`-able, Claude `ThinkingConfig.display`). The terminal frame always carries usage + a **normalized finish reason** (Vercel's 7-value enum `stop|length|content-filter|tool-calls|error|other|unknown`, `vercel_ai_sdk.md §2.3`).

### 1.3 True-delta is a hard convention, not a nicety
- Vercel: `text-delta` looks up the active part by `id` and **appends** delta; a `text-delta` without a preceding `text-start` is a **hard error** ("`text-start` MUST precede `text-delta`", `vercel_ai_sdk.md §3.3`). The strict start→delta→end ordering with per-id tracking is "what makes streaming composable."
- Letta derives sub-message IDs deterministically as `message_id + type_index` on every **type transition** (same type → same id, deltas coalesce; boundary → new id) — **no fresh UUID per delta** (`letta.md §15`). This is the cheap correct way to give every part a stable id for client-side reconstruction.
- Pydantic streams partial structured output via pydantic-core `allow_partial='trailing-strings'` — `{"name":"Ada","age":` validates to `{name:'Ada'}` mid-stream (`pydantic_ai.md §9`). Lancelot already serializes via pydantic-core on the unary path; the same `from_json(..., allow_partial=...)` is available for free.

### 1.4 Append-only event stream IS the KV-cache win (the cost lever)
- Manus's #1 metric: input:output ratio is **100:1**; cache optimization matters 100× more than output. The event stream is **append-only — never edit/remove prior events** → prefix bytes identical turn-to-turn → guaranteed cache hit (`manus.md §1/§2`, `agentic_deep.md §2.1`, `transcripts_dlai.md §2`). Lancelot's `dumps_canonical` gives the byte-stable serialization; the **streaming layer's job is to model the agent's output as an append-only typed-event log** so that the on-the-wire stream and the persisted transcript are the same append-only structure. This is the streaming/context seam — get it right and compaction (a sibling layer) plugs in cleanly.

### 1.5 The five normalizer fixes everyone hand-rolls (this is the real value-add)
These are not glue; every framework ships them and the ones that miss them degrade silently:
1. **Tool-calls win over text in a mixed turn.** When a response has BOTH text and tool calls (Anthropic preamble), execute tools and **discard the text this turn** (recover it only if the next response is empty). Missing this → agent confused by preambles, burns retries (`pydantic_ai.md §3`, `_agent_graph.py:1201-1210`).
2. **Suppress the empty first chunk.** Anthropic's first streaming chunk is empty (`message_delta.content is None` + model startswith `claude-`) — drop it (`letta.md §15`).
3. **Classify mid-stream `finish_reason == 'length'` as a targeted error, not a retry.** If the last tool call's args fail JSON-parse because the stream truncated at `max_tokens`, raise a *specific* "raise max_tokens" signal instead of burning the retry budget (`pydantic_ai.md §2`, `check_incomplete_tool_call`).
4. **Strip blank/whitespace-only text deltas** before emit (coalescing artifact; Mastra ships `BatchPartsProcessor` to coalesce text-deltas, `mastra.md §5`).
5. **Coerce tool-arg token stream → content deltas with a tolerant partial parser**, emitting a delta only when the extracted field *value changed* (`letta.md §14`, `OptimisticJSONParser`; also Vercel's array/enum partial-emit `vercel_ai_sdk.md §5.1`). For agents that reply via a `send_message`-style tool, this is the bridge to an OpenAI-compatible text stream.

### 1.6 Terminal-frame guarantee + cooperative cancel (defends against your own bugs)
- **`[DONE]` synthesis:** sniff passing chunks (line-anchored `startswith("data: [DONE]")` / `"\ndata: [DONE]"` to avoid matching `[DONE]` inside content); if the generator exits having seen neither `[DONE]` nor an error frame, **synthesize** stop_reason + `event: error` + `data: [DONE]` (`letta.md §12`). Lancelot's `sse_adapter` already emits `[DONE]` on clean exhaustion and **correctly emits NO sentinel on exception** (a half-stream must not look complete) — but the *agent-loop* wrapper drives many generator yields across many tool calls, so the "detect-missing-terminal-and-synthesize" guard is worth adding at that layer.
- **Cooperative mid-stream cancel via `athrow`:** business-logic cancel (a separate "cancel this run" request) should call `gen.athrow(RunCancelledException)` — injects at the suspension point so the agent's `try/except/finally` runs (persist partial state, close upstream). Distinguish from `asyncio.CancelledError` (client disconnect — re-raise for ASGI teardown). Disconnect → `event: cancelled`, 200 (`letta.md §10`). Lancelot's R3 pump + `TransportPool.cancel_on_disconnect` cover the transport half; `athrow` is the agent-loop half.

### 1.7 Resumability — real, but DEFER it
- Letta's full durable-stream backbone: monotonic `seq_id` per chunk, batched `XADD` via Redis pipeline, `maxlen=10000 ~MAXLEN` O(1) trim, 3h TTL on `sse:run:{run_id}`, rebuffer-on-flush-failure; reader resumes via `starting_after` cursor with dual `last_redis_id`/`cursor_seq_id` (`letta.md §8`). Gemini/OpenAI deep-research expose the same idea at the API surface: SSE `last_event_id` resume + `background=true`+`store=true` (`deep_research.md §10`).
- **This is the right design for *background* runs** (agent keeps working after the client disconnects) — but it needs a stateful store (Redis) and only pays off once background runs are a real requirement. The byte-path note's verdict is explicit: "**Defer until background-run support is a real requirement**" (`letta.md §8`). Day-1: ship the **`seq_id` field on every event part** (cheap, makes the stream cursor-ready) and a pluggable `StreamStore` Protocol with an in-memory default; wire Redis later without changing the event shape.

---

## 2. DECISION — BUILD / REUSE / REFUSE

### 2.1 REUSE (from the existing byte path — do not touch)
- **R3 pump** (`lancelot/stream.py` + `src/stream.rs`): async-gen → bounded mpsc → hyper, GIL-released per chunk, backpressure event-driven, disconnect→cancel, `LANCELOT_STREAM_BUFFER` (default 256). *The marquee. Any agent stream is a generator of `Event`s fed to this pump.*
- **`Event(name, data, id)` + `.render()` + `_clean_field`**: SSE framing with CR/LF/NUL strip on `id:`/`event:` (smuggle-frame / lastEventId-reset defense, WHATWG §9.2.6). **Reuse `_clean_field` verbatim** on every agent event field.
- **`sse_adapter(gen, done_sentinel="[DONE]")`**: byte-identical framing across the uvicorn ASGI seam AND the native R3 pump — the invariant that keeps both transports in lockstep. The agent event emitter MUST yield through this path.
- **`@app.stream` knobs**: `sse=True`, `defer_status=True` (real 4xx/5xx before first yield, not 200+error-frame), `max_stream_silence=N` (kills data-silent zombies; tracks last-DATA vs last-PING separately), `max_stream_lifetime=N` (absolute cap → `event: reconnect`), `error_frame=True` (mid-stream raise → scrubbed RFC-9457 `ProblemDetail`, guarantees terminal sentinel). The agent stream endpoint sets `sse=True, error_frame=True, defer_status=True`.
- **`lancelot.http.stream.ttft`**: TTFT histogram on first chunk. Free for the agent stream. (Operator doc names the 200–300 ms / 15–30 tok/s thresholds — `transcripts_dlai.md §1`.)
- **`TransportPool.stream()` / `relay()`** with `read=None` timeout, client→agent→upstream cancel chain, `track_usage=True` SSE-usage scan (`_scan_last_data_frame` → `cost_usd` ContextVar), `idle_guarded_chunks` (`LANCELOT_UPSTREAM_STREAM_IDLE_SECS`) — the *upstream* streaming half. The agent loop reads provider SSE through this; the normalizer (§2.2.2) translates it to Lancelot events.
- **pydantic-core partial JSON** (`from_json(allow_partial='trailing-strings')`) for streaming structured-output preview — already the serialize path.

### 2.2 BUILD (the thin agent-streaming layer, pure-Python on the pump)

**2.2.1 — `lancelot.agent.events`: one normalized event taxonomy (~10–12 typed parts).**
The intersection of Vercel-16 / Pydantic-4 / AG-UI-16, true-delta, reasoning as its own channel. NOT the 86/27 explosions. Concretely (frozen dataclasses, each `→ Event` via `.to_sse()`):

```
# lifecycle
run.start    {run_id, agent_id}                         # → SSE id carries seq_id
run.finish   {finish_reason, usage:{in,out,cost_usd}}   # normalized 7-value reason
error        {code, message}                            # scrubbed; never raw exc text

# content (true-delta; start/delta/end; id = base_id + type_index)
message.start    {id}
message.delta    {id, text}                             # APPEND, never cumulative
message.end      {id}
reasoning.delta  {id, text}                             # SEPARATE channel (thinking)

# tools (start/delta/end enables overlapped exec — see §2.2.3)
tool.start    {tool_call_id, name}
tool.delta    {tool_call_id, args_fragment}            # partial-parse, emit on value-change
tool.result   {tool_call_id, output}                    # already truncated/shaped (sibling layer)

# control
status   {label}                                        # taskNameActive-style; transient, not persisted
ping     {seq_id}                                       # keepalive carries resume cursor
```
- **Normalized `finish_reason`**: map every provider's native reason → `{stop, length, content_filter, tool_calls, error, other, unknown}`; keep `raw_finish_reason` alongside (`vercel_ai_sdk.md §2.3`).
- **Every part carries a monotonic `seq_id`** (set at emit). Cheap; makes the stream resume-cursor-ready without committing to a backend (§2.2.5).
- **Deterministic ids**: `id = generate_id(base_message_id, type_index)`; same type coalesces, type-boundary mints a new id — Letta's pattern, no per-delta UUID (`letta.md §15`).
- **`status`/`transient` parts** (Vercel `data-*` / `transient:true`, `vercel_ai_sdk.md §3.2`; v0 `taskNameActive`, `vercel_ai_sdk.md §7.1`): stream-but-don't-persist, for progress UI. One flag on the part.

**2.2.2 — `lancelot.agent.stream.normalize(provider_sse) -> Iterator[Event]`: the stream normalizer.**
The 5 fixes from §1.5, applied as a per-provider pass over the upstream SSE (read via `TransportPool.stream(track_usage=True)`):
1. tool-calls-win-over-text (discard preamble text on a mixed turn; recover only if next turn empty).
2. suppress Anthropic empty first chunk.
3. mid-stream `finish_reason:length` + unparseable trailing tool-args → emit a `error{code:"max_tokens_truncated"}` event, do NOT burn the loop's retry budget.
4. strip blank/whitespace-only text deltas (+ optional coalescing of micro-deltas).
5. tool-arg token stream → `tool.delta` via tolerant partial parse, emit only on field-value change (`OptimisticJSONParser` pattern); the same partial-parser feeds streaming structured-output preview.
Plus the **terminal guarantee** (§1.6): track `saw_finish`/`saw_error`; if the upstream generator exits seeing neither, synthesize `run.finish{error}` + `[DONE]`. (Lives here because only the agent-loop wrapper drives multi-turn generators; `sse_adapter` already handles the single-generator case.)
This is the home of the depreciating-but-necessary provider quirks — isolate them here so the rest of the layer is provider-agnostic (matches the byte-path note: "the repair lives in the message-shape translation layer Lancelot doesn't yet have", `browser_use.md §4`). Honesty: these specific shapes churn on the model clock (`bad_guide_build_positioning.md §9`) — keeping them in one ~150-line normalizer is the containment strategy.

**2.2.3 — overlapped tool execution on `tool.delta`-end (optional, cheap).**
When `tool.start→delta→end` completes (full args parsed), kick `app.call_tool` immediately — overlap execution with continued generation (`vercel_ai_sdk.md §2.4`, `execute-tools-from-stream.ts:219`). The R3 pump already gives backpressure; this is a `create_task` at the right moment. Defer if the loop is simpler without it; it's an optimization, not a correctness primitive.

**2.2.4 — cooperative cancel: `athrow` path on the agent stream.**
A "cancel run" request → `gen.athrow(RunCancelled)` so the agent's `finally` runs; emit `event: cancelled`, 200. Distinguish from `CancelledError` (disconnect → re-raise). (`letta.md §10`.) ~20 lines wrapping the loop generator.

**2.2.5 — resume-readiness (NOT the Redis backend) — `StreamStore` Protocol + in-memory default.**
Ship the seam, defer the implementation:
```python
class StreamStore(Protocol):
    async def append(run_id: str, events: list[Event]) -> None      # batched
    async def replay(run_id: str, after_seq: int) -> AsyncIterator[Event]
    # default: InMemoryStreamStore (bounded ring, TTL); Redis-Streams impl later
```
- `seq_id` already on every event (§2.2.1). `ping` carries `last_seq_id` so a reconnecting client knows where it got to (`letta.md §9`).
- The `maxsize=1`-queue keepalive pattern (`letta.md §9`) for **long tool-call gaps** (generator simply not yielding for tens of seconds): copy this on the Python side — the Rust pump's keepalive fires on byte-silence, but a non-yielding generator inside a long tool call needs the Python-side `asyncio.wait_for(queue.get(), timeout=keepalive)` injector. `maxsize=1` preserves backpressure.
- **Redis-Streams `seq_id`-cursor backend (`letta.md §8`) is BUILD-LATER**, gated behind a real background-run requirement. When it lands, copy the writer/reader verbatim; the event shape doesn't change.

### 2.3 REFUSE (off-thesis / overkill / depreciating)
- **The 86-chunk (Mastra) and 27-chunk (Vercel UI Message Stream) taxonomies.** The UI-reconstruction state machine, `data-*` generative-UI parts at full fidelity, source/file/document parts, the 4-state-per-tool client machine — that is a *frontend rendering protocol*, user-side, not framework core. Ship the ~12-part intersection; let users map it to AG-UI / Vercel UI parts in their own client (`transcripts_dlai.md §12` confirms AG-UI rides on our SSE — "no new primitive needed in Lancelot's core"). Anti-underkill check: 12 parts (not 4) because reasoning-channel + tool-lifecycle + status are load-bearing for real agents; anti-overkill check: not 86 because background/multi-agent/network variants are sibling-layer concerns.
- **GBNF / grammar-constrained generation** for non-native-tool models (`letta.md §13`): depreciating on the model clock (frontier models have native tool calls). Do not build.
- **Per-chunk Pydantic validation on streams.** Already Tier-D in CLAUDE.md; re-confirmed (`claude_code.md`: "validate input once at request time, not per chunk"). The partial-parse preview (§2.2.2.5) is best-effort display, NOT validation.
- **`smooth-stream` (word/line re-chunking + delay) as core** (`vercel_ai_sdk.md §8`): a UX animation tunable, user-side. The pump delivers raw deltas; a `smooth_stream` transform can be an optional helper, never the default, never in the hot path.
- **Regressing the pump to per-`__anext__` cross-thread bridging.** Tier-D, the 16–26× cliff. Any agent-stream wrapper stays same-thread on the `try_push` path (`lancelot_serve_tools.md §1` anti-pattern #1).
- **Mandatory Redis / Postgres for streaming.** The day-1 store is in-memory; Redis is opt-in for background runs only. (Letta mandates Postgres+pgvector — explicitly flagged as overkill, `letta.md` anti-patterns.)
- **Emitting `str(exc)` into stream error frames / tool results** (`mcp.py` does this; `lancelot_serve_tools.md` anti-pattern #3): scrub before it reaches the wire — it leaks internals into the context window and to the client.
- **A bespoke checkpoint subsystem bolted onto streaming.** Durability/HITL belong to the sibling durable-execution layer (Pydantic-AI snapshot-per-node, OpenAI `RunState`, Mastra workflow-snapshot). The streaming layer only needs the *append-only event log* + `seq_id` so the durable layer can persist it cleanly. Don't duplicate it here.

---

## 3. Recommended API sketch (the shape users touch)

```python
from lancelot import App
from lancelot.agent import Agent, events as ev

app = App()
agent = Agent(model="anthropic:claude-...", tools=[...])   # tools via @app.tool registry

@app.stream("/chat", sse=True, error_frame=True, defer_status=True, cancel_on_disconnect=True)
async def chat(req):
    # agent.stream() yields normalized lancelot.agent.events parts;
    # internally: TransportPool.stream(track_usage=True) -> normalize() -> typed Events
    async for part in agent.stream(req.json["messages"]):
        yield part            # Event subclass; sse_adapter frames it byte-identically
    # clean exhaustion -> sse_adapter emits data: [DONE]
```

Client sees (true-delta SSE):
```
id: 1
event: run.start
data: {"run_id":"...","agent_id":"..."}

id: 2
event: reasoning.delta
data: {"id":"m0:0","text":"Let me check"}

id: 5
event: message.delta
data: {"id":"m0:1","text":"The weather is"}

id: 8
event: tool.start
data: {"tool_call_id":"t0","name":"get_weather"}

id: 12
event: tool.result
data: {"tool_call_id":"t0","output":{...}}      # already shaped/truncated by sibling layer

event: run.finish
data: {"finish_reason":"stop","usage":{"in":812,"out":143,"cost_usd":0.004}}

data: [DONE]
```

Cancel (separate request) → `agent` calls `gen.athrow(RunCancelled)` → handler finally persists partial state → `event: cancelled` + 200.

Resume-ready (deferred backend): every event has `seq_id`; reconnect with `?after_seq=8` → `StreamStore.replay(run_id, 8)`. In-memory default day-1; Redis-Streams opt-in.

---

## 4. Exact default constants (cite-grounded; expose, don't hardcode silently)

| Constant | Value | Rationale / cite |
|---|---|---|
| `LANCELOT_STREAM_BUFFER` (mpsc depth) | **256** (existing, clamp [1,4096]) | throughput knee, R3 pump (`lancelot_serve_tools.md §1`) |
| stream-debounce / delta-coalesce window | **0.1 s** (100 ms), configurable, `None`=per-token | universal streaming constant (`pydantic_ai.md §13`); expose as param, do NOT hardcode (Pydantic-AI anti-pattern note) |
| keepalive interval (Python long-tool-gap injector) | **operator-set** (e.g. 15 s); `max_stream_silence` default 30 min kills zombies | `letta.md §9` (`maxsize=1` queue); Lancelot `max_stream_silence` already exists |
| partial-JSON mode (preview + structured output) | `allow_partial='trailing-strings'` | pydantic-core (`pydantic_ai.md §9`) |
| normalized finish-reason enum | `stop \| length \| content_filter \| tool_calls \| error \| other \| unknown` (+ keep `raw_finish_reason`) | `vercel_ai_sdk.md §2.3` |
| `[DONE]` sentinel | `"[DONE]"` (existing `_DEFAULT_DONE_SENTINEL`) | `lancelot/stream.py`; OpenAI-compat |
| reasoning display default | passthrough provider default; `omitted` allowed | `claude_agent_sdk.md §9` (Opus 4.7+ defaults thinking `omitted` to cut context pressure) |
| `StreamStore` default | `InMemoryStreamStore` (bounded ring + TTL); Redis opt-in | `letta.md §8` deferred-backend verdict |
| Redis-Streams (when built) | `maxlen=10000 ~MAXLEN`, TTL **3h**, key `sse:run:{run_id}`, batched `XADD` | `letta.md §8` — copy verbatim, build-later |
| TTFT operator thresholds (doc only) | TTFT **200–300 ms**; **15/30 tok/s** | `transcripts_dlai.md §1` |

---

## 5. Why this is "brutal" (the altitude check)

- **It builds ~3 small modules** (`events.py` ~120 LOC of frozen dataclasses, `stream/normalize.py` ~150 LOC of provider quirks, a ~40 LOC cancel/terminal wrapper) on top of a battle-tested pump — not a streaming engine. The agent loop is the 5–10 lines everyone agrees it is (`agentic_deep.md §1`); streaming is the typed log it appends to.
- **It refuses the two biggest over-builds** (86-chunk taxonomy, full UI-reconstruction protocol) and the depreciating hack (GBNF), and **defers** the heavy resumability backend (Redis) behind a real requirement — while shipping the cheap seam (`seq_id`, `StreamStore` Protocol) so it's not a rewrite later.
- **It honors the cardinal rule:** pure-Python, pydantic-only runtime; provider SDKs stay lazy in the normalizer; Redis (if ever) is lazy-imported.
- **It leads with the validated win:** GIL-released backpressure + TTFT — a streaming substrate pure-Python frameworks (Vercel/Mastra/Letta on `ReadableStream`/asyncio-on-the-GIL) structurally cannot match (`lancelot_serve_tools.md §1`, `mastra.md` byte-path table). The agent layer's streaming story is "the same R3 pump that beats Starlette, now speaking one clean agent-event protocol with reasoning, tools, normalization, and a terminal guarantee."

---

## 6. Open questions (for Lancelot to decide)
1. **Event id scheme** — adopt Letta's `base_id + type_index` exactly, or UUID7 per part (Pydantic AI uses UUID7 for run/conversation ids, `pydantic_ai.md §11`)? Recommend `base_id + type_index` for coalescing + zero-alloc; reserve UUID7 for run_id.
2. **Overlapped tool execution (§2.2.3)** day-1 or deferred? It's an optimization; the loop is correct without it. Recommend: ship the `tool.start/delta/end` lifecycle (so it's *possible*), defer the `create_task`-on-end until benchmarked.
3. **Where the normalizer's provider quirks live** — one `normalize.py` with a per-provider dispatch, or per-provider `ProviderProfile` objects (the model-adapter layer the corpus keeps asking for, `browser_use.md §4`, `smolagents.md`, `togetherai.md`)? They're the same code; recommend folding the streaming normalizer INTO the ProviderProfile so transport + message-shape + stream-shape translation are one provider-specific unit.
4. **Reasoning channel persistence** — persist `reasoning.delta` into the append-only log (cache cost) or treat as transient like `status`? Recommend transient-by-default (don't pay to store thinking), persist only if the model's signature-hash needs to round-trip (`claude_agent_sdk.md §9`).
