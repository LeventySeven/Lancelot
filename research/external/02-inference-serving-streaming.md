# External Scan — Inference Engines & Provider APIs (serving/streaming layer only)

> Scope: 9 teardowns (VLLM, TGI, TENSORRT_LLM, LLAMACPP, OLLAMA, LMSTUDIO, GROQ,
> TOGETHERAI, SAMBANOVA). Looking ONLY for serving/streaming patterns that beat the
> ULTIMATE-PLAN streaming/AI tier. Model internals (KV cache, kernels, quant,
> schedulers, prefix tries) deliberately ignored — not applicable to a web framework.
> Verdict default: **skip**. Plan already has: Rust SSE codec (openai/anthropic/vercel_ai),
> per-token boolean backpressure, bounded SPSC ring, timer-wheel keepalive, coalescing
> thresholds, disconnect→cancel→`upstream.abort()` moat, const-tier `/livez`, Prometheus
> `/metrics` + OTLP + GenAI spans.

---

## 1. Verdict (5 lines)

1. **The streaming architecture in the plan is already at or above every engine here.** vLLM's entire abort flow (`disconnect → CancelledError → engine_core.abort()`) is *exactly* the plan's moat — independent confirmation it's the correct design, nothing to add.
2. Nothing changes the plan's core. Three small **wire-conformance** additions are worth folding into the SSE codec spec (they are correctness, not architecture): the `stream_options.include_usage` gate, the dual reasoning/content chunk split, and the OpenAI `[DONE]`-after-`usage` ordering.
3. One genuinely-new *mechanism*: TGI checks **client-disconnect at admission time** (`response_tx.is_closed()` before any work), not only mid-stream. The plan only cancels mid-stream — adding a pre-handler disconnect check is a cheap, distinct win.
4. One health-probe nuance worth copying: llama-server distinguishes **`200 "no slot available"` (saturated but alive) vs `503 "loading"`** — maps onto the plan's load-shed/`Retry-After` and `/readyz` split.
5. Net: **no plan change required**; ≤4 concrete spec-level additions below, all small.

---

## 2. Genuinely-useful findings

### F1 — Admission-time client-disconnect check (NEW mechanism, not in plan)
`teardowns/TGI.md` §5 (`backends/v3/src/queue.rs`): the very first line of the admission loop is
```
if entry.response_tx.is_closed() { drop (client disconnected); continue }
```
**Why it beats the plan:** the plan's moat is *mid-stream* disconnect → `upstream.abort()`. But a request can disconnect while still **queued/admitted but before the handler runs** (slow upstream, blocking-pool backlog, load-shed queue). The plan never describes dropping those — it would still spend a GIL acquire + handler entry + first upstream connection on a dead client.
**How to apply:** in the bounded blocking-pool admission path *and* the request lifecycle just before the `with_gil{call handler}` step, add a Rust-side `if conn.is_disconnected() { drop, emit metric, skip }` gate. It's the same `is_closed()`/poll the disconnect-watcher already needs for the moat — reused one step earlier. Add a `lancelot_requests_dropped_predispatch_total` counter (mirrors TGI's `tgi_request_failure{err}`). Cheap; closes a real waste path under load.

### F2 — `stream_options: {"include_usage": bool}` gates the final usage chunk (WIRE conformance)
`teardowns/SAMBANOVA.md` (l.322, 471), `teardowns/VLLM.md` §8.3, `teardowns/LLAMACPP.md` §SSE: the OpenAI-compat contract is that the terminal `usage` chunk (the one with `choices:[{delta:{},finish_reason:"stop"}], "usage":{...}`) is emitted **only when the request set `stream_options.include_usage=true`**; otherwise the stream goes straight from the last content delta to `data: [DONE]` with no usage object.
**Why it beats the plan:** the plan's `AISSEResponse(codec="openai")` spec doesn't mention this gate. Emitting usage unconditionally (or never) is a silent OpenAI-incompat that breaks SDK clients that count tokens. This is a one-flag branch in the Rust SSE codec, not architecture — but it must be in the codec spec or it gets missed.
**How to apply:** SSE codec reads `stream_options.include_usage` off the parsed request; if true, before `[DONE]` emit one extra chunk carrying `usage` (and `choices:[]` per current OpenAI shape) and otherwise omit it. Frame order is fixed: `…last delta` → (optional `usage` chunk) → `data: [DONE]\n\n`.

### F3 — Dual reasoning/content SSE chunk split, same `created` (WIRE conformance for `codec="anthropic"` + OpenAI reasoning)
`teardowns/OLLAMA.md` l.465: when a response carries both `thinking` and `content`, the encoder emits **two consecutive SSE chunks** — first a reasoning-only chunk (`content:""`, `tool_calls:null`), then the content/tool_calls chunk (`reasoning:""`) — both sharing the **same `created` timestamp**. `teardowns/LLAMACPP.md` §15 gives the Anthropic-codec analogue: `content_block_start` → `content_block_delta`/`input_json_delta` → and the Responses-codec analogue: `response.reasoning_text.delta` vs `response.output_text.delta` as *distinct event types*.
**Why it beats the plan:** the plan names three codecs (`openai|anthropic|vercel_ai`) but the codec spec doesn't capture that reasoning and content are **separate framed events**, not interleaved into one delta. A naive codec that puts reasoning into the same `delta.content` field is wire-wrong for every modern reasoning model.
**How to apply:** codec spec must model an output item as a tagged union `{Reasoning(text) | Content(text) | ToolCallDelta{…}}` and the per-codec framer maps each variant to its event: OpenAI → two chunks/one `created`; Anthropic → `content_block_*` with block index; Responses → `response.reasoning_text.delta` vs `response.output_text.delta`. This is the right internal shape for the Rust codec; fold into the codec interface seam before Wave-1 freezes it.

### F4 — Health-probe state machine: saturated-but-alive vs not-ready (OPS nuance)
`teardowns/LLAMACPP.md` §Health Endpoint:
```
GET /health → 200 {"status":"ok"}                 (slots idle)
            → 200 {"status":"no slot available"}   (all busy, STILL accepting)
            → 503 {"status":"loading model"}       (not ready)
```
**Why it beats the plan:** the plan's const-tier `/livez`/`/readyz`/`/startupz` is correct, but this is the explicit semantic the plan should adopt for the *saturated* case: a fully-loaded blocking pool / shed queue must keep `/livez` = 200 (process is alive — do not let K8s kill it under load, the classic liveness-vs-readiness footgun) while `/readyz` flips to 503 so the LB stops sending new work. Liveness 503 only for "not yet booted / engine dead" (maps to vLLM's `EngineDeadError→503`).
**How to apply:** lock the const-tier probe contract: `/livez` 200 whenever the supervisor + tokio runtime are up (independent of pool saturation); `/readyz` returns 503 when the bounded blocking pool is in load-shed (the same signal that drives `503 Retry-After`); `/startupz` 503 until lifespan/`app.state` init completes. One sentence in the ops spec; prevents a production outage class.

---

## 3. One-line-per-file "nothing useful beyond the plan"

- `teardowns/VLLM.md` — serving is plain FastAPI+uvicorn(uvloop); abort flow (`disconnect→CancelledError→abort()`) is *identical to the plan's moat* → confirmation, not new. `stream_interval` token-coalescing = plan's coalescing trigger already. **Nothing new** (only F2 wire detail referenced).
- `teardowns/TGI.md` — Rust `axum` router; queue/radix/budget logic is model-internal (ignored). Only F1 (admission-time disconnect check) is portable; `tgi_batch_concat{reason}` metric tagging + per-batch OTLP `follows_from` spans are nice but the plan already has OTel GenAI spans. **F1 only.**
- `teardowns/TENSORRT_LLM.md` — serving is just `trtllm-serve` (OpenAI-compat FastAPI over the C++ Executor). No framework-layer content. **Nothing useful.**
- `teardowns/LLAMACPP.md` — C++ httplib server; slot FSM is model-internal. Contributes F3 (Anthropic/Responses event names) + F4 (health states) only; router-proxy header-stripping (`server`, `transfer-encoding`, `content-length`, `keep-alive`) is generic reverse-proxy hygiene, already implied. **F3/F4 only.**
- `teardowns/OLLAMA.md` — Go `net/http`; runner→server transport is NDJSON over chunked (not `data:` SSE) with a `cap=100` channel as backpressure — i.e. a worse, ad-hoc version of the plan's bounded SPSC ring. Contributes F3 (dual reasoning/content chunk, shared `created`) only. **F3 only.**
- `teardowns/LMSTUDIO.md` — bespoke **WebSocket-RPC** protocol (not HTTP/SSE) with a discriminated-union packet stream + client `{type:"cancel"}` control msg + granular tool-call lifecycle events (`toolCallGenerationStart/NameReceived/ArgumentFragmentGenerated/End`). Architecturally a different transport than the plan's SSE-first AI tier; the tool-call lifecycle granularity is already covered by F3's tagged-union codec model. **Nothing new** (reinforces F3).
- `teardowns/GROQ.md` — Cloudflare-fronted; `service_tier` capacity-bidding + head-of-queue priority is a *business/gateway* concern, not a framework primitive; `x-groq-region`/ULID `x-request-id` are SDK/gateway headers (we write our own client). **Nothing useful.**
- `teardowns/TOGETHERAI.md` — Cloudflare-fronted; vLLM-shape request surface + custom routing/billing flags + `x-api-received` receive-timestamp header — all gateway/provider concerns, not serving architecture. **Nothing useful.**
- `teardowns/SAMBANOVA.md` — AWS+Highwinds; multi-layer gateway (Flask outer → Go middle → vLLM-frontend) is provider topology, not framework design. Only the `stream_options.include_usage` reference (F2) is portable. **F2 only.**

---

## 4. Report — does this change the plan?

**No plan change required.** The streaming/AI tier is already best-in-class versus every engine scanned (vLLM independently validates the exact disconnect→abort moat). Four small spec-level additions, none architectural:

1. **F1** — add a pre-dispatch client-disconnect check (reuse the moat's disconnect watcher one step earlier; drop dead-client requests before the GIL/handler). New `*_dropped_predispatch_total` metric.
2. **F2** — SSE codec must gate the terminal `usage` chunk on `stream_options.include_usage`; fixed frame order `…delta → [usage?] → [DONE]`.
3. **F3** — codec internal model = tagged union `{Reasoning|Content|ToolCallDelta}`; per-codec framers map each to distinct events (OpenAI two-chunk/same-`created`; Anthropic `content_block_*`; Responses `*.reasoning_text.delta` vs `*.output_text.delta`). Fold into the codec seam before Wave-1 freeze.
4. **F4** — lock probe semantics: `/livez`=200 even when pool-saturated (only 503 if un-booted/engine-dead); `/readyz`=503 on load-shed; `/startupz`=503 until lifespan done.

Generated 2026-05-19. Source-cited to `teardowns/*.md`. `Lancelot/` is gitignored.
