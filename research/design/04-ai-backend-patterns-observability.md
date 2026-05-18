# Lancelot — AI-Backend Patterns & Observability (Decision-Grade Design Research)

> Companion to `research/SYNTHESIS.md` (esp. §4.5 streaming, §4.6 AI-native layer) and the 9 source-level teardowns in `research/{fastapi,robyn,blazeio}/`.
> Goal: define the AI-backend feature set + observability that makes Lancelot *the* framework for AI apps — concrete API/architecture, reimplementable.
> Labels: **KNOWN** = sourced from a teardown file (`path:line`) or external doc (URL). **INFERRED** = derived from known facts. **RECOMMENDATION** = Lancelot design decision.
> Every external claim carries a URL; every framework claim cross-refs `path:line` into the corpus.

---

## Executive summary — the AI-native API surface (read this first)

Lancelot's AI-app thesis: **the framework owns the LLM I/O hot path end-to-end** — inbound stream, the agent loop, the outbound provider call, and the observability — so the app developer writes `yield token` and gets disconnect-cancellation, backpressure-to-the-model, cost accounting, and OTel spans *for free*. None of FastAPI/Robyn/Blazeio do all four; that union is the differentiator.

1. **`@app.stream` / `SSEResponse` / `AISSEResponse`** — return an async generator; Rust core does SSE/JSONL framing, 15 s timer-wheel keepalive, per-chunk cancel checkpoint, and **client-disconnect → cancel producer → abort upstream LLM call** (stop paying for tokens). Native OpenAI/Anthropic/Vercel-AI-SDK wire-format emitters.
2. **`app.llm` — first-class async LLM client**: per-host HTTP/2 pool, stale-detect, least-in-flight, **POST never auto-retried**, streaming passthrough (`relay()` proxies a provider stream to the client with zero re-buffering), per-provider circuit breaker + concurrency semaphore, token/cost accounting, multi-provider failover (LiteLLM-shaped).
3. **`@app.mcp.tool/.resource/.prompt`** — Robyn's `mcp.py` shape, productionized: Streamable HTTP transport (the 2025-06-18 / Nov-2025 spec), `Mcp-Session-Id`, resumability, `Origin` validation, `auth_required=True`, Pydantic-model tool args → rich JSON-Schema, negotiated `protocolVersion`.
4. **`@app.agent`** — agent-loop endpoint primitive: streams intermediate steps (tool-call deltas, reasoning), conversation/session state, cooperative cancellation, optional **durable mode** (checkpoint each step; resume after crash — Temporal/Vercel-Workflow-shaped).
5. **`background.spawn()`** — fixes FastAPI's `BackgroundTasks`-blocks-connection bug: detached Rust-owned worker pool, request returns immediately, bounded + concurrent, queue depth in `/metrics`.
6. **`structured(Model)` / `output_schema=`** — passthrough to provider constrained decoding (OpenAI `strict` json_schema, Anthropic structured outputs), validates the final object with pydantic-core, streams partial JSON for progressive UIs.
7. **OTel GenAI conventions out of the box** — `gen_ai.*` spans (chat/embeddings/execute_tool/invoke_agent), `gen_ai.client.token.usage` + `operation.duration` + `time_to_first_token` histograms, opt-in prompt/response capture with a redaction hook. Zero-config OTLP export.
8. **`@app.embeddings` + batching/fan-out middleware** — concurrency-limited, auto-batched embedding endpoints; streaming-RAG helper that interleaves retrieval spans with the generation stream.

**Single biggest AI differentiator:** *the framework, in Rust, ties client-disconnect to upstream-LLM-abort and end-to-end backpressure (slow client → stalled handler → stalled upstream read → bounded RAM → cancelled token billing).* Every other framework leaks money and memory here; Lancelot makes "stop paying for tokens nobody is reading" the default, not a recipe.

---

## 1. The LLM streaming hot path

This is the headline AI workload. An AI backend spends the vast majority of its wall-clock time holding a long-lived connection open while tokens trickle from an upstream model. The framework's job: get the first token to the client fast, never buffer the whole completion, propagate backpressure all the way to the upstream socket, and the instant the client leaves, stop generating (and stop paying).

### 1.1 Transport choice: SSE vs WebSocket vs raw HTTP chunked

**KNOWN — the three options and what each costs:**

| Transport | Direction | Framing | Reconnect/resume | Proxy/CDN behaviour | Verdict for token streaming |
|---|---|---|---|---|---|
| **SSE** (`text/event-stream`) | server→client only | `data: <payload>\n\n`, optional `event:`/`id:`/`retry:`/`:comment` | native: browser `EventSource` auto-reconnects, `Last-Event-ID` header replays | needs `X-Accel-Buffering: no` to defeat nginx buffering (`fastapi/01:521`); otherwise transparent over HTTP/1.1+HTTP/2 | **Default.** It is what OpenAI, Anthropic, and the Vercel AI SDK all use on the wire. |
| **WebSocket** | bidirectional | opaque frames | none (app must build it) | many corp proxies/CDNs drop or buffer WS; needs explicit upgrade | Only when the client must *also* stream up mid-generation (live voice, interactive tool approval). Heavier per-conn. |
| **HTTP chunked** (`Transfer-Encoding: chunked`, no SSE framing) | server→client | raw `%X\r\n<bytes>\r\n` | none | transparent | The substrate under SSE; expose it raw as `ChunkedResponse` for JSONL / NDJSON / binary (audio) where SSE's `data:` envelope is unwanted. |

**RECOMMENDATION:** SSE is the default streaming transport; `ChunkedResponse` (raw chunked, JSONL/NDJSON) is the structured-step transport; WebSocket is a first-class but explicitly-opted-in third option. Cross-ref `SYNTHESIS §4.5`: "typed returned `SSEResponse`/`ChunkedResponse` objects (not exception-as-response)". WS endpoints in Robyn already get full DI but no response model so they're cheaper per message (`fastapi/01:193`) — keep that property for WS token push.

### 1.2 The provider wire formats (must be emittable and relayable verbatim)

Lancelot must be able to (a) **emit** these formats so an app can be a drop-in OpenAI/Anthropic-compatible endpoint, and (b) **relay** them — proxy an upstream provider stream straight to the downstream client with minimal copies.

**KNOWN — OpenAI Chat Completions stream** (data-only SSE; <https://developers.openai.com/api/docs/guides/streaming-responses>, <https://developers.openai.com/api/reference/resources/chat/subresources/completions/streaming-events>):
```
data: {"id":"chatcmpl-…","object":"chat.completion.chunk","created":1677652288,"model":"gpt-…","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}

data: {"id":"chatcmpl-…","object":"chat.completion.chunk","model":"gpt-…","choices":[{"index":0,"delta":{"content":"Once"},"finish_reason":null}]}

data: {"id":"chatcmpl-…","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: [DONE]
```
- Flat chunk; token at `choices[0].delta.content`. First chunk carries `delta.role`. Tool calls arrive as `delta.tool_calls[i].function.arguments` string fragments. Terminator is the literal `data: [DONE]\n\n`. Usage is *not* in the stream unless `stream_options:{"include_usage":true}` is set (then a final chunk before `[DONE]` has `usage`).

**KNOWN — Anthropic Messages stream** (named SSE events; <https://platform.claude.com/docs/en/docs/build-with-claude/streaming>). Exact sequence from the live doc:
```
event: message_start
data: {"type":"message_start","message":{"id":"msg_…","type":"message","role":"assistant","content":[],"model":"claude-…","stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":25,"output_tokens":1}}}

event: content_block_start
data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}

event: ping
data: {"type":"ping"}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello"}}

event: content_block_stop
data: {"type":"content_block_stop","index":0}

event: message_delta
data: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"output_tokens":15}}

event: message_stop
data: {"type":"message_stop"}
```
- **Token-accounting subtlety (KNOWN, load-bearing):** `input_tokens` is reported in `message_start.message.usage`; `output_tokens` is reported *cumulatively in the final* `message_delta.usage`. A cost meter must read both, from two different events.
- Delta variants: `text_delta` (`.text`), `input_json_delta` (`.partial_json`, for tool args — emitted one key/value at a time, chunked), `thinking_delta` (`.thinking`) + `signature_delta` (`.signature`) for extended thinking.
- `ping` events appear anywhere and carry no data — a relay must pass them through (they *are* the upstream's keepalive) or strip them and inject its own.
- Error mid-stream: `event: error\ndata: {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}`.

**KNOWN — Vercel AI SDK "data stream protocol"** (SSE; header `x-vercel-ai-ui-message-stream: v1`; <https://ai-sdk.dev/docs/ai-sdk-ui/stream-protocol>). Typed JSON parts on `data:`: `{"type":"start","messageId":…}`, `text-start`/`text-delta`(`.delta`)/`text-end` (all carry an `id`), `reasoning-start/-delta/-end`, `tool-input-start`(`toolCallId`,`toolName`)/`tool-input-delta`(`inputTextDelta`)/`tool-input-available`(`input`)/`tool-output-available`(`output`), `data-<suffix>` custom parts, `source-url`/`source-document`/`file`, `{"type":"error","errorText":…}`, `start-step`/`finish-step`, `{"type":"finish"}`, terminated by `data: [DONE]`. This is the format a Next.js `useChat()` frontend expects — Lancelot shipping a native emitter for it makes "Lancelot backend + Vercel AI SDK frontend" a one-liner.

**RECOMMENDATION:** ship three codec emitters in the Rust core — `openai`, `anthropic`, `vercel_ai` — selectable per response: `AISSEResponse(gen, codec="openai")`. The Python generator yields a *normalized delta* (`{"text": "..."}` or a `ToolCallDelta`/`Usage` object); Rust frames it into the chosen wire format. This is the dual of the relay path (§4.4): one normalized internal representation, N wire formats, framing in Rust (per `SYNTHESIS §4.5`: "Move the byte framing into the Rust core").

### 1.3 The streaming architecture Lancelot must implement

This is the single most-vetted subsystem in the corpus. The correct design is a *synthesis* of FastAPI's architecture and Blazeio's per-token cost discipline; Robyn's streaming is explicitly the one thing not to copy.

**KNOWN — FastAPI's decoupled-producer architecture (steal the architecture verbatim, `fastapi/01:475-556`):**
1. Endpoint generator runs in **its own task**, decoupled from the socket writer, connected by a bounded `anyio` memory stream `max_buffer_size=1` (`fastapi/01:486`). The buffer of 1 *is* the backpressure: the producer can be at most one item ahead of the socket; a slow client stalls the producer which stalls its upstream read.
2. A **separate keepalive task** sits between producer and consumer and injects `: ping\n\n` (`KEEPALIVE_COMMENT = b": ping\n\n"`, `fastapi/01:535`) whenever the producer is idle > `_PING_INTERVAL = 15.0 s` (`fastapi/01:497,535`). **Critical (PEP-789):** the timer wraps the *memory-stream receive*, never the generator's `__anext__` — wrapping the generator would `CancelledError`/`GeneratorExit` it mid-stream and kill the LLM call (`fastapi/01:527`).
3. The producer context manager is entered on the **request-scoped `AsyncExitStack`**, not left to async-generator GC finalization (`fastapi/01:528`). This is the fix for the notorious "yield-dependency cleanup runs in the wrong task/time during streaming" bug class — the DB/LLM-client dependency stays open *through* the stream and closes after, via structured teardown.
4. **`await anyio.sleep(0)` after every yielded chunk** (`fastapi/01:529`, refs FastAPI #14680) — guarantees a scheduler checkpoint so client-disconnect cancellation is actually delivered even when the producer is faster than the consumer.
5. Forced headers: `Cache-Control: no-cache` + `X-Accel-Buffering: no` (`fastapi/01:520-521,535`) — the second defeats nginx response buffering, which otherwise destroys time-to-first-token.

**KNOWN — Blazeio's per-token cost discipline (steal the substrate, `blazeio/02 §0`, `SYNTHESIS §4.5`):** one preallocated `bytearray(102400)` + cached `memoryview` (one copy per `recv`, zero steady-state alloc); **boolean send-backpressure gate — no `Future` allocated per token** (Blazeio flips a 2-event flag and sync-early-returns when the client keeps up); pause-reading-by-default pull model; prepare-time writer rebind (no per-chunk branch). Blazeio's coalescing is size-only at 30 KiB which holds thousands of tokens — `SYNTHESIS §4.5` corrects this to **time+size (~25 ms / 2 KiB)** so latency stays bounded.

**KNOWN — Robyn's anti-pattern (do NOT copy, `robyn/01:10`, `robyn/02`, `SYNTHESIS §0/§4.5`):** per-token `gen.__next__()` in `spawn_blocking` + `with_gil` *per chunk*, sync-generator-only, `String` copy per token. For a 1000-token stream that is ~1000 thread round-trips + 1000 GIL acquisitions. This is "the one thing Robyn must not be copied on."

**RECOMMENDATION — Lancelot's streaming pump (DESIGN, per `SYNTHESIS §4.5`):**
- Transport substrate = Blazeio's `BufferedProtocol` receive + boolean backpressure + pull-by-default, **reimplemented in Rust** (the parser/writer are Rust per `SYNTHESIS §0` "Blazeio's parser is the spec for Lancelot's Rust parser").
- Architecture = FastAPI's decoupled producer + keepalive + per-chunk checkpoint + request-scoped teardown — but the **producer↔socket channel is a bounded SPSC ring in Rust** (`SYNTHESIS §4.5`: "bounded SPSC ring (size-1 default)"), the **keepalive is a Tokio timer-wheel** (no Python keepalive task), and the **SSE/JSONL framing is Rust** (no per-event Python list-join).
- Native **async-generator** pump only — **reject sync generators for streaming** (eliminates `iterate_in_threadpool`'s per-chunk thread hop, `fastapi/01:549`, `SYNTHESIS §4.5`).
- Coalescing trigger = time **and** size (~25 ms / ~2 KiB), set by benchmark not guess (`SYNTHESIS §4.5,§4.8`).
- Per-stream framework allocation target ≈ 1–3 (vs FastAPI's ≥3-AsyncExitStacks + 2-memory-streams + 2-tasks per stream, `fastapi/01:543`). Collapse the 4-layer middleware onion into one request-scope wrapper (`fastapi/01:593`, `SYNTHESIS §4.4`).

### 1.4 Client-disconnect → cancel → abort upstream (the money differentiator)

**KNOWN — why this matters:** an LLM backend pays per output token. If the client closes the tab mid-generation and the server keeps reading the upstream stream to completion, you pay for tokens nobody will ever see, and you hold a request slot + upstream connection for the full generation. FastAPI relies on the `anyio.sleep(0)` checkpoint to *eventually* notice the disconnect when `receive()` reports it (`fastapi/01:529,550`). Blazeio's Rust-less core can't watch the socket independently. Robyn's Rust core owns the socket but its streaming model doesn't wire disconnect→producer-cancel→upstream-abort.

**RECOMMENDATION (DESIGN, `SYNTHESIS §4.5`: "Client-disconnect → cancel → abort upstream LLM call"):** the Rust core watches the downstream socket out-of-band (it already owns it). On `FIN`/`RST`/half-close it **immediately**:
1. cancels the Python producer task (`asyncio.Task.cancel()` → `CancelledError` at the next checkpoint; the per-chunk checkpoint guarantees prompt delivery), then
2. the producer's `finally`/`async with` unwinds the request-scoped exit stack, which **closes the in-flight `app.llm` upstream connection** (§4) — aborting the provider request, so token billing stops at the current token, and the pooled connection is reset (not returned half-read).

This is the end-to-end chain `SYNTHESIS §4.5` calls out: *slow/gone client → stalled handler → stalled upstream read → bounded RAM regardless of completion length → cancelled token billing*. It is strictly better than FastAPI's checkpoint-timing approach because the Rust core delivers the cancel the instant the kernel reports the disconnect, not on the next Python scheduler tick. **Make this the default behaviour, with an opt-out** `@app.stream(detach_on_disconnect=True)` for the rare "finish generating and cache it even if the client left" case.

### 1.5 Time-to-first-token (TTFT) optimization

TTFT is the dominant perceived-latency metric for chat UIs. Sources of TTFT tax and Lancelot's mitigations:

| TTFT tax | Source | Lancelot mitigation |
|---|---|---|
| nginx/CDN response buffering | needs `X-Accel-Buffering: no` (`fastapi/01:521` KNOWN) | forced on every streaming response by the Rust codec; also emit `Cache-Control: no-cache`. |
| First-chunk waits for full first item | coalescing buffers the first token | RECOMMENDATION: **flush the first chunk immediately**, coalesce only chunks 2..N (first-token-fast, then amortize). |
| Middleware `send`-wrapper stack | FastAPI's 3+ `send` closures per chunk (`fastapi/01:590` INFERRED ~3000 extra calls/1000-token stream) | collapsed single request-scope wrapper (`SYNTHESIS §4.4`). |
| Upstream connection setup | TLS+TCP handshake on a cold connection | RECOMMENDATION: `app.llm` keeps a **warm pre-connected pool** to known providers (§4.3); first request pays no handshake. |
| DI/validation before the first `yield` | per-request graph walk | resolve-once DI compiler (`SYNTHESIS §4.4`); a streaming endpoint's deps resolve before the generator is even called, off the TTFT path only if cheap — RECOMMENDATION: allow `@app.stream(eager_deps=False)` to defer non-critical deps past first token. |

Instrument `gen_ai.client.operation.time_to_first_chunk` / `gen_ai.server.time_to_first_token` (§5) so TTFT regressions are caught, not guessed.

### 1.6 Backpressure when the client is slower than the model

**KNOWN — the failure mode:** a fast model (or a cached/replayed stream) produces tokens faster than a slow mobile client drains them. Naive servers buffer the unsent tail in RAM → unbounded memory per slow client → OOM under concurrency. **KNOWN — the correct model (Blazeio, `blazeio/01 §0.2,0.6`, `blazeio/02 §0.3`, `SYNTHESIS §4.5`):** pull-by-default + bounded buffer means the *server stops reading the upstream model* when the client is behind. The `max_buffer_size=1` channel (`fastapi/01:486,527`) makes the producer block on `send()` until the socket drains one item → the producer's `async for` on the upstream stalls → the upstream TCP window closes → the provider stops sending → **bounded RAM regardless of completion length and client speed**.

**RECOMMENDATION:** SPSC ring size = 1 by default (lowest latency + tightest backpressure, `SYNTHESIS §4.5,§4.8`); make it tunable per-endpoint (`@app.stream(buffer=N)`) for the "burst-tolerant, latency-insensitive" case. The boolean send-gate (no Future per token, `blazeio/02 §0.3`) is the steady-state fast path; the ring only materializes a waiter when actually blocked.

### 1.7 Multiplexing many concurrent streams

**KNOWN — the scaling reality (`SYNTHESIS §0,§4.2`, `robyn/01 §0.2`):** one Python process has one GIL; the only real Python CPU scaling is N processes over one `SO_REUSEPORT` socket. Per process, hundreds of *concurrent idle-ish streams* are cheap *if* each stream is not holding the GIL and not allocating per token. FastAPI's per-stream cost is high: ≈8 coroutine frames + ≥3 `AsyncExitStack`s + 2 memory streams + 2 producer tasks, all resident for the whole generation (`fastapi/01:543` INFERRED). At 500 concurrent streams that is a lot of resident Python.

**RECOMMENDATION (DESIGN):**
- Per-stream Python footprint = 1 producer task + 1 generator frame + 1 request-scope wrapper (collapsed middleware, `SYNTHESIS §4.4`). The ring buffer, keepalive timer, and socket are **Rust-side**, shared infrastructure, not per-stream Python objects.
- GIL is dropped across the entire `.await` (Robyn's choreography verbatim, `robyn/01 §0.3-0.5`, `SYNTHESIS §1.2`); 500 streams awaiting tokens hold zero GIL collectively.
- Keepalive timers for all streams in a process live in **one Tokio timer-wheel**, not 500 Python `anyio.fail_after` tasks (`SYNTHESIS §4.5`).
- Multiprocess + cgroup-aware worker count (`SYNTHESIS §4.2`) is the horizontal axis; a single process targets thousands of concurrent token streams bounded by FD limits + ring memory, not GIL.

### 1.8 How the reference implementations actually do it (2026)

- **FastAPI + SSE / `sse-starlette`-class:** the decoupled-producer + 15 s keepalive + per-chunk-checkpoint architecture above (`fastapi/01:475-556` KNOWN). This *fork* makes `yield` from an endpoint just stream (native SSE/JSONL, not a userland recipe). The architecture is right; the implementation is Python tasks/streams that Lancelot pushes into Rust.
- **LangServe / LangChain `astream_events`:** exposes a chain as an HTTP endpoint that streams a typed event log (`on_chat_model_stream`, `on_tool_start/end`, `on_retriever_*`) over SSE — i.e. it streams the *intermediate agent steps*, not just the final tokens. Takeaway for Lancelot's `@app.agent` (§3): the unit of streaming for agents is the *typed step event*, not the raw token. (`<https://ai-sdk.dev/docs/ai-sdk-ui/stream-protocol>` shows the same step-event idea generalized.)
- **Vercel AI SDK backends:** emit the `x-vercel-ai-ui-message-stream: v1` typed-part SSE protocol (§1.2 KNOWN) so the React `useChat` hook renders text/tool/reasoning/data parts incrementally. Lancelot ships this as a native codec.
- **LiteLLM proxy:** sits as an OpenAI-compatible front, **relays** the upstream provider stream through, normalizing every provider into the OpenAI chunk shape, and adds router/fallback/cost (§4.5). Lancelot's `app.llm.relay()` is the same pattern as a framework primitive, not a separate proxy process. (<https://docs.litellm.ai/docs/routing-load-balancing>)
- **OpenAI/Anthropic's own endpoints:** the wire formats in §1.2 (KNOWN, official docs). Anthropic interleaves `ping` events as its keepalive; OpenAI relies on chunk cadence + the terminal `[DONE]`. Lancelot's keepalive (15 s `: ping\n\n`) matches the SSE-comment convention both tolerate.

**Reference-implementation scorecard (KNOWN, distilled):**

| | disconnect→abort upstream | end-to-end backpressure | per-token alloc | typed-item stream | keepalive | client+MCP |
|---|---|---|---|---|---|---|
| FastAPI fork (`fastapi/01`) | partial (checkpoint-timed) | yes (`max_buffer_size=1`) | Python tasks/streams (heavy) | yes (`stream_item_field`) | yes (15 s) | no client / no MCP |
| Robyn (`robyn/01-03`) | Rust owns socket but not wired to abort | actix-managed | **per-token GIL + spawn_blocking** (AVOID) | no | n/a | weak `ai.py` / strong `mcp.py` (no stream) |
| Blazeio (`blazeio/02-03`) | no Rust watcher | yes (boolean gate) | **near-zero (boolean gate, no Future/token)** | no (manual framing) | no | strong client (HTTP/1.1, CERT_NONE) / no MCP |
| **Lancelot (target)** | **yes, Rust-immediate** | **yes (Rust SPSC ring)** | **near-zero (Rust ring + boolean gate)** | **yes (pydantic-core/item)** | **yes (Tokio timer-wheel)** | **first-class client + MCP** |

No existing framework fills the whole row. Lancelot's value is the union — and specifically the first column (disconnect→abort), which *no one* does in Rust wired through to billing.

### 1.9 The exception-as-response anti-pattern (reject for streaming)

**KNOWN (`blazeio/02 §7`, `SYNTHESIS §3` AVOID, `blazeio/03:100`):** Blazeio's primary return mechanism is `raise Abort(...)` / `raise Eof()` — control flow via exceptions, body written as a side-effect of the raise. It is clever but: non-local, untestable without a fake transport, slower when raised, and the source of Blazeio's 3-way header hand-checks. For an LLM stream this is actively dangerous — an exception thrown to signal "stream done" is indistinguishable from an exception that means "the model errored mid-generation", and the `finally`/teardown ordering around a raised control-flow signal is exactly the PEP-789 hazard FastAPI fixed with structured exit stacks (`fastapi/01:528`). **RECOMMENDATION:** Lancelot streaming returns a *typed response object* (`SSEResponse`/`AISSEResponse`/`ChunkedResponse`); the framework writes it. Steal Blazeio's central `try/except/finally` funnel + `finally`-guaranteed cleanup (`SYNTHESIS §3`), reject success-via-`raise`. A streamed error is an explicit `error` event in the chosen codec (OpenAI: an error chunk; Anthropic: `event: error`; Vercel: `{"type":"error","errorText":…}`, §1.2 KNOWN), not a Python exception bubbling through the framing layer. This keeps a handler unit-testable (assert on the yielded items, no transport).

### 1.10 WebSocket vs SSE for *agent* streams specifically

**KNOWN/INFERRED:** the §1.1 table says SSE-default. The agent case (§3) sharpens it. SSE is server→client only — fine for "stream the agent's tokens/steps down". The moment the protocol needs **mid-generation client→server messages** (interactive tool approval: model proposes a destructive tool call, UI must approve before execution; or live voice where the user interrupts), SSE cannot carry the upstream message and you need either (a) a side-channel `POST` correlated by `conversation_id` (works, simplest, the MCP pattern — §2.2's separate POST for client messages), or (b) a WebSocket. **RECOMMENDATION:** default agents to **SSE + correlated side-channel POST** (`@app.agent` streams down via SSE; client sends approvals/interrupts via `POST /agent/{conversation_id}/signal` — reuses the §3.3 session store as the rendezvous). Offer WebSocket as an explicit `@app.agent(transport="ws")` for true bidirectional/low-latency-interrupt cases; WS endpoints get DI but no response model so per-message cost is lower (`fastapi/01:193` KNOWN) — good for high-frequency interrupt signalling. Do **not** make WS the default: corp proxies/CDNs frequently break it and SSE's native reconnect + `Last-Event-ID` resumability (§1.1, §3.4) is exactly what a dropped agent stream needs.

---

## 2. MCP (Model Context Protocol) — first-class server surface

MCP is the standard by which agents (Claude Desktop, IDE agents, OpenAI/Anthropic agent runtimes) discover and call tools/resources/prompts a server exposes. A web framework that *is* an MCP server with three decorators is a genuine differentiator — Robyn already proved the shape; Lancelot productionizes it.

### 2.1 Current spec state (2026)

**KNOWN (<https://modelcontextprotocol.io/specification/2025-06-18/basic/transports>, <https://modelcontextprotocol.info/blog/mcp-next-version-update/>):**
- Two standard transports: **stdio** (subprocess; client launches server, newline-delimited JSON-RPC over stdin/stdout, logs on stderr) and **Streamable HTTP** (the `2025-06-18` revision; *replaces* the deprecated `2024-11-05` HTTP+SSE two-endpoint transport).
- The `2025-06-18` revision added: structured tool outputs, OAuth-based authorization, elicitation (server→user prompts), security hardening.
- **A newer revision shipped 2025-11-25** (RC 2025-11-11), with the Transport Working Group smoothing stateless/session-handling rough edges for production hosting. As of 2026-05 that is the current spec; `MCP-Protocol-Version` negotiation (below) makes version a runtime value, not a constant.
- Robyn hardcodes `protocolVersion = "2024-11-05"` (`robyn/03:557` KNOWN) — that is the *deprecated* transport's version. Lancelot must implement Streamable HTTP and negotiate the version.

### 2.2 Streamable HTTP transport — exactly what a production endpoint needs

**KNOWN (full spec, <https://modelcontextprotocol.io/specification/2025-06-18/basic/transports>):**

*Single endpoint* (e.g. `POST|GET|DELETE /mcp`) handling all three methods:

- **POST** (client→server message): client `Accept:` MUST list **both** `application/json` and `text/event-stream`. Body is one JSON-RPC request/notification/response.
  - Input is a *response* or *notification* → server returns **`202 Accepted`, no body** (or a 4xx with a JSON-RPC error, no `id`).
  - Input is a *request* → server returns **either** `Content-Type: application/json` (single JSON object) **or** `Content-Type: text/event-stream` (an SSE stream that eventually carries the JSON-RPC response, possibly preceded by server-initiated requests/notifications, then SHOULD close). Client MUST support both.
- **GET** (open a server→client SSE stream): client `Accept: text/event-stream`. Server returns `text/event-stream` **or `405 Method Not Allowed`** (no server-push stream offered). Used for unsolicited server requests/notifications.
- **DELETE** (explicit session termination): client sends `Mcp-Session-Id`; server MAY reply `405` if it doesn't allow client-initiated termination.

*Session management:*
- Server MAY assign a session at init by putting **`Mcp-Session-Id`** (cryptographically secure, visible ASCII 0x21–0x7E) on the HTTP response carrying `InitializeResult`.
- If assigned, client MUST echo `Mcp-Session-Id` on **every** subsequent request. Server that requires it returns **`400 Bad Request`** for non-init requests lacking it.
- Server MAY terminate a session anytime → then returns **`404 Not Found`** for that session id; client receiving 404 MUST start a new session (new `InitializeRequest`, no session id).

*Resumability/redelivery:* server MAY put `id:` on SSE events (globally unique per session/stream). Client resumes a broken stream via **`GET` with `Last-Event-ID:`**; server MAY replay messages after that id **only on the stream that disconnected** (per-stream cursor). Server MUST send each JSON-RPC message on exactly one stream (no broadcast).

*Protocol version:* client MUST send **`MCP-Protocol-Version: <negotiated>`** on every HTTP request after init. Missing header + no other signal → server assumes `2025-03-26`. Invalid/unsupported → **`400 Bad Request`**.

*Security (MUST):* validate the **`Origin`** header on all connections (DNS-rebinding defence); when local, bind `127.0.0.1` not `0.0.0.0`; implement auth on all connections.

### 2.3 Robyn's `mcp.py` — the reusable shape (KNOWN, copy wholesale)

**KNOWN (`robyn/03 §9`, `robyn/03:495-561`):** `robyn/mcp.py` is ~461 dependency-free lines (stdlib `inspect`/`json`/`re`/`dataclasses`) that turn any app into an MCP server. The reusable pieces:
- **Decorator registry**: `@app.mcp.resource(uri,name,description,mime_type)`, `@app.mcp.tool(name,description,input_schema)`, `@app.mcp.prompt(name,description,arguments)` — defaults inferred from the function (`uri=f"function://{fn.__name__}"`, `name`, `description=fn.__doc__`) (`robyn/03:537-540`).
- **Auto JSON-Schema from signature** (`_generate_schema_from_function`, `mcp.py:24-57`): type map `str→string, int→integer, float→number, bool→boolean`, generics → `object`, default `string`; required iff no default (`robyn/03:502`).
- **URI-template resource routing** (`_match_uri_template`, `mcp.py:202-232`): `{param}` → regex `(.+)` if param ∈ `{path,file_path,directory}` else `([^/]+)`, anchored — path-like params greedily match slashes, segment params don't (`robyn/03:527`). Correct; copy verbatim.
- **JSON-RPC dispatcher** (`MCPHandler.handle_request`, `mcp.py:152-185`): methods `initialize`, `resources/list|read`, `tools/list|call`, `prompts/list|get`. Error envelope codes `-32700` parse / `-32601` method-not-found / `-32602` invalid-params / `-32603` internal (`robyn/03:513-525`). Spec-compliant; copy.
- **Signature-aware handler invocation** (`mcp.py:249-339`): `iscoroutinefunction`→await; uri-params → `handler(**uri_params)`; whole-params → `handler(params)`; none → `handler()` (`robyn/03:529`).
- **Lazy mount** via `@property def mcp` (`__init__.py:652-680`): the `/mcp` route is only added if the user touches `app.mcp` — **zero cost if unused** (`robyn/03:542`). Copy verbatim.

### 2.4 The five gaps Lancelot must close (KNOWN gaps, `robyn/03:553-560`)

1. **No streaming transport** — Robyn's MCP is request/response `POST /mcp` only; the spec defines Streamable HTTP with SSE. Lancelot already has `SSEResponse` (§1) — wire MCP request handling to return `text/event-stream` when the tool/resource handler is an async generator or long-running, per §2.2.
2. **Shallow schema inference** — generics collapse to `"object"` (`mcp.py:47-49`). Lancelot has a pydantic-core schema path (`SYNTHESIS §4.4`, FastAPI's `get_pydantic_openapi_schema` analog) — route MCP tool args through it so a tool taking a Pydantic model gets a rich JSON-Schema (and structured-output enforcement, §3.4).
3. **No auth on `/mcp`** — it's an open POST. Lancelot's auth middleware (`robyn/03 §10.1`, `TokenGetter`/`AuthenticationHandler`) → `@app.mcp(auth_required=True)` installs the BEFORE_REQUEST auth middleware; honor MCP's OAuth-authorization addition from the `2025-06-18`+ spec for bearer flows.
4. **Hardcoded `protocolVersion`** (`mcp.py:190` = `2024-11-05`) — make it a negotiated value: read `MCP-Protocol-Version`, validate against a supported set, `400` on unsupported, echo negotiated version in `InitializeResult` (§2.2).
5. **Stringified results** (`str(content)`, `mcp.py:281,306`) — support structured tool output (the `2025-06-18` addition), JSON content, and binary content types per spec.

### 2.5 RECOMMENDATION — Lancelot's first-class MCP surface

```python
# zero cost unless app.mcp is touched (Robyn's lazy @property mount, robyn/03:542)
@app.mcp.tool(auth_required=True)              # gap-3 closed
async def search_docs(query: str, top_k: int = 5) -> list[Doc]:
    "Search the knowledge base."               # → tool.description
    ...                                         # Pydantic return → structured output (gap-2,5)

@app.mcp.resource("file://{file_path}")         # path-like param → (.+) regex (robyn/03:527)
async def read_file(file_path: str) -> bytes: ...

@app.mcp.prompt()
def summarize(text: str) -> str: ...
```

- **Transport**: one Rust-routed endpoint `/{mcp_path}` (default `/mcp`) serving POST/GET/DELETE per §2.2. `Origin` validation, `Mcp-Session-Id` (cryptographic UUID, session store in the Rust core keyed by id with TTL), `MCP-Protocol-Version` negotiation, `Last-Event-ID` resumability backed by the same Rust SPSC-ring + event-id cursor used for SSE (§1.3) — *the MCP SSE path reuses the streaming substrate; do not build a second one*.
- **stdio mode**: `lancelot mcp-stdio app:app` runs the same registry over newline-delimited JSON-RPC on stdin/stdout for desktop-agent embedding (KNOWN this is the *preferred* transport for local clients, §2.1).
- **Schema**: tool/resource/prompt args inferred from type hints; Pydantic models → full JSON-Schema via the same registry that builds OpenAPI (`SYNTHESIS §4.4` "one type representation"), so MCP schema and OpenAPI never drift.
- **Auth**: `@app.mcp(auth_required=True)` (app-wide) or per-decorator; bearer + the spec's OAuth flow.
- **The keeper insight (`robyn/03:560`, `SYNTHESIS §1` steal #11):** this is *the* single most directly reusable thing for the AI goal — ~460 dep-free lines proving "framework as MCP server". Ship it **core, not experimental**, with all five gaps closed.


---

## 3. Agent / tool-loop backends

An agent endpoint is a long-running request that runs an LLM↔tool loop, streaming *intermediate steps* (the model's plan, each tool call, each tool result, reasoning), maintaining conversation state, cancellable, and ideally resumable across a crash. This is the hardest AI-backend workload and where FastAPI's design actively hurts.

### 3.1 Serving an agent loop over HTTP

**KNOWN — the loop shape (LangServe `astream_events`, Vercel AI SDK `WorkflowAgent`):** the agent runs `while not done: call model → if tool_calls: execute tools → append results → loop`. The endpoint streams a *typed event log*: `step_start`, `model_delta` (tokens), `tool_call` (name + args, possibly streamed as `input_json_delta`/`tool-input-delta`), `tool_result`, `step_finish`, `final`. LangServe streams `on_chat_model_stream`/`on_tool_start`/`on_tool_end`/`on_retriever_*`; Vercel AI SDK streams `start-step`/`tool-input-*`/`tool-output-available`/`finish-step` (§1.2 KNOWN). The common abstraction: **the streamed unit is a typed step event, not a raw token** — tokens are *one kind* of step event.

**RECOMMENDATION — `@app.agent` primitive (DESIGN):**
```python
@app.agent(codec="vercel_ai", session=True)        # or codec="anthropic"/"openai"/"jsonl"
async def assistant(ctx: AgentContext, msg: str):
    async for step in ctx.run(model="gpt-…", tools=[search_docs, calc], messages=ctx.history + [msg]):
        yield step          # AgentStep: ModelDelta | ToolCall | ToolResult | Reasoning | Final
```
- `ctx.run(...)` is the framework-owned loop: it calls `app.llm` (§4), detects tool calls, invokes the registered `@app.mcp.tool`/plain tools (one tool-arg coercion engine shared with HTTP/WS/MCP per `robyn/03:314`), feeds results back, re-calls, and yields a typed `AgentStep` per event.
- The endpoint is a normal Lancelot async-generator stream (§1.3): same SPSC ring, same keepalive, same **client-disconnect → cancel loop → abort in-flight `app.llm` call** (§1.4) — so abandoning an agent run stops paying for tokens *and* halts further tool execution immediately.
- `codec=` selects the wire emitter; `AgentStep` is the normalized internal representation (§1.2 dual).

**Loop termination & failure semantics (RECOMMENDATION — DESIGN, the reimplementable detail):**
- **Termination conditions** (loop exits on the first true): model returns no tool calls (natural finish, emit `Final`); `max_steps` reached (default 10, configurable — bounds runaway loops); `max_tokens`/`max_cost` budget exceeded (read from the §4.5 accounting, hard stop with a typed `BudgetExceeded` `Final`); client disconnect (§1.4, immediate cancel); explicit `ctx.stop()` from a tool. Never an unbounded `while True` — a model that loops calling the same tool must hit `max_steps`.
- **Tool failure**: a tool raising is **caught by the loop**, converted to a `ToolResult(error=...)` step, and **fed back to the model** as the tool result content (the model can recover/retry/abandon) — *not* propagated as an HTTP 500. Only a framework-level failure (LLM provider hard-down after failover exhausted, §4.5) terminates the stream with an `error` codec event (§1.9).
- **LLM call failure inside the loop**: goes through §4's retry/breaker/failover *before* the loop sees it; if all providers exhausted, emit `error` event + structured teardown (close tool connections via the request-scoped exit stack, `fastapi/01:528`).
- **Parallel tool calls**: when the model emits N tool calls in one step, execute them via `app.gather(concurrency=…)` (§6.2) — bounded fan-out on the GIL-free await path, results re-assembled in call order, each its own `execute_tool` span (§5.5).
- **The tool registry** (KNOWN shape from `robyn/03 §9.1`, the MCP auto-schema): a tool is `(name, description=docstring, input_schema=JSON-Schema-from-signature, fn, is_async)` resolved **once at registration** (`SYNTHESIS §1` steal #1) — the loop never re-introspects a signature per step. Pydantic-model params → rich schema (§2.4 gap-2). This is identical to the MCP tool registry (§2.3): **one registry, consumed by both `@app.mcp.tool` and `@app.agent` tools** — register a tool once, it is callable by remote MCP clients *and* the in-process agent loop.

### 3.2 Structured output / function calling / constrained-decoding passthrough

**KNOWN (2026 provider state, <https://developers.openai.com/api/docs/guides/structured-outputs>, <https://towardsdatascience.com/hands-on-with-anthropics-new-structured-output-capabilities/>, <https://www.buildmvpfast.com/blog/structured-output-llm-json-mode-function-calling-production-guide-2026>):**
- **OpenAI Structured Outputs**: `response_format:{type:"json_schema", json_schema:{strict:true, schema:…}}` — constrained decoding guarantees the output adheres to the JSON Schema; <0.1% parse-failure. Plain "JSON mode" is now a footnote.
- **Anthropic Structured Outputs** (top models, 2026): exact-schema adherence; ~<0.2% parse-fail. Subtlety (KNOWN, load-bearing): the SDK **silently strips** `minimum`/`maximum`/`minLength`/`pattern` from the schema (the constrained decoder can't enforce them), moves them to the field `description`, then **validates post-generation and retries** on violation. Tool-use returns structured data in a single block at end-of-stream — *cannot be parsed progressively*; Structured Outputs (not tool-use) is what enables partial JSON.

**RECOMMENDATION (DESIGN):** `structured(Model)` / `@app.post(output_schema=Model)`:
- Lancelot derives the JSON Schema from the Pydantic model via the *same* registry that builds OpenAPI/MCP schema (`SYNTHESIS §4.4` "one type representation" — schema generated once at import, never per request).
- It is passed through to the provider's native constrained-decoding param (`response_format`/structured-outputs) — Lancelot does **not** reimplement constrained decoding; it is a *passthrough* with schema generation + final validation.
- The final object is validated with **pydantic-core `validate_json(bytes)` single-pass** (`SYNTHESIS §1` steal #7, `fastapi/03:0`, `robyn/03:5`) — not a double-parse. On validation failure, surface a typed error (and, opt-in, the Anthropic-style "retry once with the violated constraints in the prompt").
- **Progressive structured streaming**: when the provider streams partial JSON (OpenAI strict mode does; Anthropic structured-outputs does), emit Vercel-AI-SDK `data-*` parts or partial-object SSE so a UI can render the object as it fills. This is the "stream a typed object incrementally" capability that `fastapi/01:539` calls out as exactly what you want for streaming structured agent steps.

### 3.3 Conversation / session state

**KNOWN — the anti-pattern (`robyn/03 §0.6`, `SYNTHESIS §3`):** Robyn's `ai.py` uses *process-local, stringly-typed* memory — useless under its own `--fast` multi-process fork default (each worker has a different store). Blazeio mangles request state onto `current_task()` — invisible to debuggers, leaks (`blazeio/03:283`). Both are disqualifying for conversation state.

**RECOMMENDATION (DESIGN):**
- Per-request scope = real `contextvars.ContextVar` threaded through middleware (`SYNTHESIS §1` steal #14, `robyn/01 §0.7`; require Python ≥3.11 to drop Robyn's degraded 3.10 fallback). Per-turn agent state lives here.
- **Cross-request conversation state must be out-of-process** (the multiprocess model, `SYNTHESIS §4.2`, makes in-proc memory wrong by construction). `session=True` on `@app.agent` wires a pluggable `SessionStore` ABC: default Redis (or SQLite for single-node dev), keyed by `conversation_id`, storing the message history + tool-result cache with a TTL. The `gen_ai.conversation.id` OTel attribute (§5) is this key — observability and state share the identifier.
- The "await a late-registered service" startup-order-independence is a nice pattern (`blazeio/03:283` `wait_for_key`), but implement it as a plain dict + `asyncio.Event`, **not** task-attribute mangling (`blazeio/03:283` AVOID) — e.g. the handler awaits the warm `app.llm` pool before first use.

### 3.4 Cancellation, resumability, durable execution

**KNOWN — cancellation:** covered by §1.4 (Rust-core disconnect → cancel producer/loop → abort upstream). The per-chunk `anyio.sleep(0)` checkpoint (`fastapi/01:529`) guarantees the loop's `CancelledError` is delivered promptly even between tool calls; structured request-scoped teardown (`fastapi/01:528`) closes tool connections (DB, HTTP) deterministically.

**KNOWN — durable execution (the 2026 pattern, <https://temporal.io/blog/building-durable-agents-with-temporal-and-ai-sdk-by-vercel>, <https://vercel.com/blog/a-new-programming-model-for-durable-execution>, <https://ai-sdk.dev/v7/docs/agents/workflow-agent>):** an agent loop that survives a worker crash needs each step's result *checkpointed* so a replacement worker can resume. Two philosophies:
- **Temporal**: deterministic workflow + event-history replay — crash at step 5 of 10 → new worker replays history, completed Activities return recorded results (not re-executed), resumes at step 5. Strict determinism required.
- **Vercel Workflow DevKit**: tools marked `'use step'` become durable steps with automatic retries (default 3); `sleep`/hooks suspend & resume cleanly. >100M runs / >500M steps since Oct-2025 beta. Lower ceremony, DX-first.

**RECOMMENDATION (DESIGN — opt-in durable mode):** `@app.agent(durable=True, store=...)`:
- The framework-owned loop already has the natural step boundary (model call → tool calls → results). In durable mode, **after each step the loop persists `(conversation_id, step_index, step_result)` to a `DurableStore`** (the same `SessionStore` backend) *before* yielding the step downstream.
- On resume (`POST` with `conversation_id` + a resume token, or MCP `Last-Event-ID`), the loop **replays from the store**: completed steps return recorded results (Temporal-style — do not re-call the model/tool, do not re-bill), execution continues from the first uncheckpointed step. The SSE `id:` per step *is* the resume cursor — reusing §1.3's event-id mechanism and §2.2's MCP resumability. One resumability mechanism, three consumers (SSE reconnect, MCP `Last-Event-ID`, durable-agent crash recovery).
- Lancelot does **not** build a Temporal-class deterministic VM; it provides *step-level checkpoint + replay* for the agent loop specifically (the 90% case), and an escape hatch to call out to Temporal/Workflow for arbitrary durable orchestration. Determinism burden is contained because the loop, not user code, owns the step boundary.

### 3.5 Background long tasks — fixing FastAPI's connection-blocking bug

**KNOWN — the bug (`fastapi/01:662-688`, `SYNTHESIS §3` AVOID):** FastAPI/Starlette `BackgroundTasks` run **after** the response body is sent but **inside the same request coroutine**, **sequentially**, *before* the coroutine returns (`await self.background()` inside `Response.__call__`, `starlette/responses.py:171,285`). Consequences: a 30 s background embedding job keeps the request slot + (keep-alive) connection occupied 30 s after the client got their 200; N tasks = sum of latencies; sync tasks consume the 40-token threadpool (`fastapi/01:683`). The canonical "return fast, do the slow LLM/embedding work in the background" pattern is silently broken — a frequent production surprise for AI backends specifically (embeddings/ingestion are the textbook background job).

**RECOMMENDATION (DESIGN, `SYNTHESIS §3` AVOID, `fastapi/01:688`):** `background.spawn(fn, *a, **kw)` enqueues into a **Rust-core-owned detached worker pool**, separate from request workers, with its own bounded queue + configurable concurrency. The request coroutine returns *immediately* after the body is sent; tasks run truly detached and concurrently. Provide opt-in `background.spawn(..., await_in_request=True)` for the rare case the old semantics are wanted. Surface queue depth + in-flight + reject count in `/metrics` (§5). This is a small surface change that makes Lancelot dramatically better than FastAPI for the most common AI background workload (`fastapi/01:688`: "a place Lancelot can be dramatically better than FastAPI").


---

## 4. The outbound LLM client (`app.llm`)

**KNOWN — why this is the most important AI subsystem (`blazeio/03:297`, `SYNTHESIS §1` steal #12):** an AI backend spends most of its wall-clock in outbound calls to LLM/embedding/tool APIs. Every millisecond and every allocation in the client multiplies by concurrency. FastAPI ships *no* client (bring `httpx`); Robyn ships *no* client; Blazeio ships the right shape but with security defects. Lancelot must ship a first-class one — it is half the AI pitch.

### 4.1 Connection pooling (KNOWN — Blazeio's `__SessionPool__`, copy the shape)

`blazeio/03 §4.4` (`Client/__init__.py:456-532`):
- Pool keyed by `(host, port, *proxy)` (`:512`); `app.sessions: dict[key, list[instance]]`.
- **Stale-detection before hand-out** (`:525-531`): if the pooled connection has no transport / `is_timed_out` / `transport.is_closing()` / `__wait_closed__` → reconnect. Kills the classic "pooled socket the server already closed" bug — *essential* for LLM providers that idle-close at 60–120 s.
- **Least-in-flight selection** (`:520-521`): `waiters = [i.context.waiter_count for i in instances]; pick min` — spreads concurrent LLM calls evenly across the per-host connection set. The `ioCondition` exposes live `waiter_count` (impossible with stdlib `asyncio.Condition`, `blazeio/03:220`) — this introspection *is* the load balancer.
- **Idle reaping** (`clean_instance`, `:471-483`): every `keepalive_interval = 30 s`, connections idle ≥ `timeout = 60 s` are closed + socket aborted. Warm connections for burst traffic, reaped *before* the provider's own idle timeout silently kills them.
- `max_contexts` default **2 per host** (`:458`) — KNOWN-conservative; INFERRED for high-concurrency LLM workloads raise it (Lancelot default: per-provider configurable, default ~32).
- `TCP_NODELAY=1` (`blazeio/03:353`) — essential: no 40 ms Nagle delay on the smallish JSON POST every LLM request is.

### 4.2 HTTP/2 + TLS verification (KNOWN gaps Lancelot must fix)

**KNOWN defects in Blazeio (`blazeio/03:353`, `:106`, `SYNTHESIS §1` steal #12, §3 AVOID):**
- Blazeio is **HTTP/1.1-only**, one in-flight request per physical connection (`blazeio/03:373`: `async with instance.context` serializes use). For LLM providers that support HTTP/2, multiplexing many concurrent streams over *one* connection is dramatically more efficient than N HTTP/1.1 connections.
- Blazeio defaults **`ssl.CERT_NONE`, `check_hostname=False`** (`blazeio/03:353`, `Dependencies/__init__.py:687-696`) — a security defect; calling `api.openai.com` with cert verification off is unacceptable.
- Blazeio **resolves DNS every connect** (`blazeio/03:106` AVOID) and uses a **racy external-thread request timeout** (`SYNTHESIS §1` steal #12).

**RECOMMENDATION (DESIGN, `SYNTHESIS §1` steal #12, §4.6):** Blazeio's pool design **+ HTTP/2 default** (one connection, many concurrent LLM streams multiplexed — also removes the HTTP/1.1 head-of-line serialization) **+ `verify=True` default** (opt-out only, never the default) **+ DNS cache** (resolve once, TTL'd) **+ in-band `asyncio.timeout`** (not Blazeio's racy watchdog thread). The pool, parser, and writer are the Rust core (`SYNTHESIS §0` "Blazeio's parser is the spec for Lancelot's Rust parser"); HTTP/2 framing is native to `hyper` (the chosen core, `SYNTHESIS §4.1`).

### 4.3 Per-provider rate-limit, circuit breaking, retries

**KNOWN — POST is never auto-retried (`blazeio/03:390`, `SYNTHESIS §1` steal #12):** Blazeio's retry loop (`_connector`, `:259-271`, `max_retries=3`) retries **only idempotent methods** (`NON_BODIED_HTTP_METHODS = {GET,HEAD,OPTIONS,DELETE}`) — a POST is *never* auto-retried. **This is correct and load-bearing for LLM clients: silently replaying a POST = duplicate completions = double billing.** Lancelot keeps this invariant absolutely.

**KNOWN — LiteLLM's reliability model (2026, <https://docs.litellm.ai/docs/proxy/reliability>, <https://docs.litellm.ai/docs/router_architecture>, <https://www.getmaxim.ai/articles/retries-fallbacks-and-circuit-breakers-in-llm-apps-a-production-guide/>):** cooldowns + fallbacks + timeouts + retries (fixed + exponential backoff) across deployments/providers; `allowed_fails` = consecutive-failure budget before a model is marked unhealthy and the fallback chain activates (a budget, *not* a full half-open circuit breaker); ordered fallback (`order=1` fails → try `order=2` …, each level its own retries); **context-window fallback** (input too long → fall to a larger-context model); general fallback (rate-limited/error/unavailable).

**RECOMMENDATION (DESIGN):** per-provider policy object on `app.llm`:
- **Retry policy:** retry only on connection errors *before any bytes were sent* and on `429`/`503` *with* `Retry-After` honored — **never replay a POST that may have reached the model** (the POST-never-retried invariant; a connection reset *after* the request was written is NOT retried). Exponential backoff with jitter, capped attempts (default 2), respecting `Retry-After`/`x-ratelimit-reset` headers.
- **Circuit breaker (proper, with half-open):** per `(provider, model)` — `closed` → `open` after K consecutive failures or failure-rate over a window; `open` → fast-fail (no socket) for a cooldown; `half-open` → allow one probe, success closes, failure re-opens. (LiteLLM's `allowed_fails` is only a budget; Lancelot ships the real state machine.) Surface state in `/metrics` (§5).
- **Concurrency semaphore per provider** — bound in-flight requests so a burst doesn't trip the provider's own rate limit; queued requests get a fast `503 Retry-After` past a bounded wait (matching the framework-wide "pool full → 503, never unbounded queue" rule, `SYNTHESIS §3` AVOID, `fastapi/01:405`).
- **Rate-limit accounting:** parse `x-ratelimit-remaining-{requests,tokens}` / `-reset` response headers; proactively throttle before the provider 429s.

### 4.4 Streaming passthrough / relay (zero-copy proxy)

**KNOWN — Blazeio's relay primitive (`blazeio/03:411`, `:401`, `SYNTHESIS §1` steal #12):** `.pipe_to(pipe)` splices a response stream straight into another writer — *proxy an upstream LLM stream to the downstream client without materializing it* (`Modules/streaming.py:170-181` `forward_to_client`). `handle_chunked` (`:401`) yields **every decoded chunk immediately, no whole-body buffering**; `BufferedProtocol` receive = one `bytearray(102400)` per connection, the one unavoidable copy is `bytes(memoryview[:n])` into the stream deque (`blazeio/03:334`). This is the lowest-overhead possible relay.

**RECOMMENDATION (DESIGN):** `app.llm.relay(upstream_response) -> AISSEResponse` — a framework primitive that:
- reads the upstream provider stream via the pooled connection (§4.1), and **writes the bytes straight to the downstream SPSC ring (§1.3) with zero re-parsing** when the downstream codec == upstream codec (e.g. relaying OpenAI→client as OpenAI: pure byte splice, one copy total);
- when codecs differ (relay Anthropic upstream → emit OpenAI downstream, the LiteLLM pattern), parse the upstream into the normalized internal delta (§1.2) and re-frame in Rust — still streaming, still bounded, no whole-body buffer;
- inherits §1.4: downstream disconnect → cancel relay → close upstream pooled connection → stop billing. This is "Lancelot's `app.llm.relay()` is the LiteLLM-proxy pattern as a framework primitive, not a separate process" (§1.8).

### 4.5 Token/cost accounting & multi-provider failover

**KNOWN — where the numbers are (§1.2):** OpenAI usage only in-stream if `stream_options.include_usage` (final chunk); Anthropic `input_tokens` in `message_start`, `output_tokens` cumulative in final `message_delta`. Non-stream responses carry `usage` in the body. A correct meter must know each provider's quirk.

**RECOMMENDATION (DESIGN):**
- `app.llm` parses usage from every response (stream or not), per-provider-aware, and emits the OTel `gen_ai.client.token.usage` histogram + a `gen_ai.usage.input_tokens`/`output_tokens`/`cache_read.input_tokens`/`reasoning.output_tokens` span attribute set (§5) — *the client is the instrumentation point*, the app writes no telemetry code.
- A pluggable price table (`{(provider,model): (in_per_mtok, out_per_mtok, cache_read_per_mtok)}`) → derived `gen_ai.cost.usd` attribute + a cost counter metric. Cost is a first-class metric, not a log line.
- **Multi-provider failover** (LiteLLM-shaped, §4.3): a `Model` alias maps to an ordered list of `(provider, model, params)`; on circuit-open / context-window-exceeded / hard error, fall to the next. **Failover never replays a POST that may have hit the first model** (POST-never-retried invariant) — failover only triggers on *pre-send* failures or explicit retryable provider errors (429/5xx with no partial completion). Streaming failover: if the first byte hasn't arrived, switching is safe; once tokens have streamed downstream, the stream cannot be transparently failed over (surface a typed error; do not silently restart and double-bill).

### 4.6 RECOMMENDATION — `app.llm` API surface

```python
app.llm = LLMClient(
    providers={
      "openai":    Provider(base="https://api.openai.com/v1", key=env, http2=True, verify=True,
                             max_conns=32, breaker=Breaker(k=5, cooldown=20)),
      "anthropic": Provider(base="https://api.anthropic.com", key=env, http2=True),
    },
    models={ "fast": ["openai:gpt-…", "anthropic:claude-…-haiku"] },   # ordered failover
    prices=PRICE_TABLE,
)

# non-stream — usage+cost+span automatic
resp = await app.llm.chat("fast", messages=[...], output_schema=Answer)   # §3.2 structured passthrough

# stream — disconnect→abort→stop-billing automatic (§1.4)
async def handler():
    async with app.llm.stream("fast", messages=[...]) as up:
        async for delta in up:           # normalized delta (§1.2)
            yield delta

# relay — zero-copy proxy upstream→client (§4.4), LiteLLM-as-a-primitive
@app.stream(codec="openai")
async def proxy(req):
    return app.llm.relay(await app.llm.open("openai", req.json()))
```
Invariants baked in: HTTP/2 + verify=True default; POST never auto-retried; per-provider breaker+semaphore; pooled+stale-detected+least-in-flight; usage/cost/spans automatic; disconnect→abort. `orjson` bytes-in/bytes-out (no `.encode()` round-trip, `blazeio/03:238` DESIGN). This is `SYNTHESIS §4.6`: "a *proper* async LLM client (not Robyn's `ai.py`)".


---

## 5. AI observability — OpenTelemetry GenAI semantic conventions

The single most valuable thing a framework can do for AI observability is **make the instrumentation a property of the framework, not the app**. Because `app.llm` (§4) owns every outbound call and `@app.agent`/`@app.stream` own the loop, Lancelot can emit spec-compliant telemetry the developer never writes.

### 5.1 Current state of the conventions (2026)

**KNOWN (<https://opentelemetry.io/docs/specs/semconv/gen-ai/>, <https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-spans/>, <https://opentelemetry.io/blog/2026/genai-observability/>):** the OTel GenAI semantic conventions are the emerging standard for LLM observability. As of 2026 most attributes are **Development** (formerly "experimental") status; client spans began exiting experimental in early 2026. They are *not yet fully stable* — so Lancelot must gate the newest conventions behind the opt-in env var the spec defines (below) and be ready to track the stabilization. The transition plan will add a stable version before GA. Treating them as the target is correct; pinning exact attribute *stability* is the freshness risk — re-verify on each release.

### 5.2 Spans (KNOWN — exact, <https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-spans/>)

| Span | Name template | Kind | Status |
|---|---|---|---|
| Inference/chat | `"{gen_ai.operation.name} {gen_ai.request.model}"` | `CLIENT` (`INTERNAL` if same-process model) | Development |
| Embeddings | `"{gen_ai.operation.name} {gen_ai.request.model}"` | `CLIENT` | Development |
| Retrieval | `"{gen_ai.operation.name} {gen_ai.data_source.id}"` | `CLIENT` | Development |
| Execute tool | `"execute_tool {gen_ai.tool.name}"` | `INTERNAL` | Development |

**Required attributes:** `gen_ai.operation.name` (enum: `chat`, `embeddings`, `execute_tool`, `generate_content`, `text_completion`, `retrieval`, `create_agent`, `invoke_agent`, `invoke_workflow`); `gen_ai.provider.name` (enum: `openai`, `anthropic`, `aws.bedrock`, `azure.ai.openai`, `gcp.vertex_ai`, `gcp.gemini`, `gcp.gen_ai`, …); `gen_ai.tool.name` (execute_tool only). `error.type` (Stable) when errored.

**Conditionally required:** `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.data_source.id` (retrieval), `gen_ai.output.type`, `gen_ai.request.choice.count` (if ≠1), `gen_ai.request.seed`, `gen_ai.request.stream` (bool), `server.address`/`server.port` (Stable).

**Recommended (chat):** `gen_ai.conversation.id`, `gen_ai.request.{temperature,max_tokens,top_p,top_k,frequency_penalty,presence_penalty,stop_sequences}`, `gen_ai.response.{finish_reasons,id,time_to_first_chunk}`, `gen_ai.usage.{input_tokens,output_tokens,cache_creation.input_tokens,cache_read.input_tokens,reasoning.output_tokens}`. **Embeddings:** `gen_ai.embeddings.dimension.count`, `gen_ai.request.encoding_formats`. **Execute tool:** `gen_ai.tool.{call.id,description,type}` (`type` ∈ function|extension|datastore).

**Sampling-critical** (must be set at span-create if set at all): `gen_ai.operation.name`, `gen_ai.provider.name`, `gen_ai.request.model`, `server.address`, `server.port`.

**Opt-in / sensitive content** (instrumentations MUST NOT capture by default, MUST offer opt-in): `gen_ai.input.messages`, `gen_ai.output.messages`, `gen_ai.system_instructions`, `gen_ai.tool.definitions`, `gen_ai.retrieval.documents` (`id`+`score`), `gen_ai.retrieval.query.text`, `gen_ai.tool.call.{arguments,result}`. Recorded as structured attrs or JSON strings; an optional **external-storage hook** receives content objects + the span (invoked regardless of sampling) so the app can enrich/redact before recording. Opt-in env: `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental`.

### 5.3 Metrics (KNOWN — exact, <https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-metrics/>)

| Metric | Type | Unit | Status | Bucket boundaries |
|---|---|---|---|---|
| `gen_ai.client.token.usage` | Histogram | `{token}` | Recommended | `[1,4,16,64,256,1024,4096,16384,65536,262144,1048576,4194304,16777216,67108864]` |
| `gen_ai.client.operation.duration` | Histogram | `s` | **Required** | `[0.01,0.02,0.04,0.08,0.16,0.32,0.64,1.28,2.56,5.12,10.24,20.48,40.96,81.92]` |
| `gen_ai.client.operation.time_to_first_chunk` | Histogram | `s` | Recommended (streaming only) | same as duration |
| `gen_ai.client.operation.time_per_output_chunk` | Histogram | `s` | Recommended (streaming only) | same as duration |
| `gen_ai.server.request.duration` | Histogram | `s` | Recommended | same as duration |
| `gen_ai.server.time_per_output_token` | Histogram | `s` | Recommended (success only) | `[0.01,0.025,0.05,0.075,0.1,0.15,0.2,0.3,0.4,0.5,0.75,1.0,2.5]` |
| `gen_ai.server.time_to_first_token` | Histogram | `s` | Recommended (success only) | `[0.001,0.005,0.01,0.02,0.04,0.06,0.08,0.1,0.25,0.5,0.75,1.0,2.5,5.0,7.5,10.0]` |

`gen_ai.client.token.usage` required attrs: `gen_ai.operation.name`, `gen_ai.provider.name`, **`gen_ai.token.type`** (`input`/`output` — one data point per type). Duration metrics: `gen_ai.operation.name` + `gen_ai.provider.name` required, `error.type`/`gen_ai.request.model`/`server.port` conditional.

### 5.4 MCP semantic conventions (KNOWN)

OTel defines GenAI conventions for MCP too (<https://opentelemetry.io/docs/specs/semconv/gen-ai/mcp/>). Lancelot's MCP layer (§2) emits MCP spans (method, transport, session) so an MCP server's tool calls are observable with the same `gen_ai.*` vocabulary as direct LLM calls.

### 5.5 RECOMMENDATION — what Lancelot instruments out of the box (DESIGN)

The framework owns the instrumentation points; the app emits zero telemetry code:
- **Every `app.llm` call** → an inference/embeddings `CLIENT` span with the full required+recommended attribute set (model, provider, params, finish_reasons, usage), the `gen_ai.client.operation.duration` histogram, and `token.usage` (split `input`/`output` with `gen_ai.token.type`). Streaming calls add `time_to_first_chunk` + `time_per_output_chunk` (the §1.5 TTFT metric — measured, not guessed). Cost (§4.5) → a derived `gen_ai.cost.usd` attribute + counter.
- **Every `@app.agent` run** → an `invoke_agent` span; each loop step a child span; each tool call an `execute_tool` `INTERNAL` span with `gen_ai.tool.{name,call.id,type}`. The agent span carries `gen_ai.conversation.id` = the session key (§3.3) so traces, state, and observability share one identifier.
- **Server-side streaming** → `gen_ai.server.{time_to_first_token,time_per_output_token,request.duration}` on the inbound endpoint, so you see *your* TTFT to *your* client (distinct from upstream provider TTFT).
- **Retrieval** (§6) → `retrieval` span with `gen_ai.data_source.id`, opt-in `gen_ai.retrieval.{query.text,documents}`.
- **Content capture is OFF by default** (spec MUST). `@app.observe(capture_content=True, redact=fn)` opts in; the redaction hook is the spec's external-storage hook — it receives the content + span and PII-scrubs *before* recording. This is the PII/redaction concern handled correctly: capture is opt-in, redaction is a first-class hook, never a default that leaks prompts.
- **Eval hooks**: `@app.eval(name=...)` registers a sampled post-response callback (gets request, response, usage, latency, span) for online evals/scoring/LLM-judge — runs in the detached background pool (§3.5) so it never adds latency to the response path. Eval verdicts attach to the span as attributes.
- **Export**: zero-config OTLP (env `OTEL_EXPORTER_OTLP_ENDPOINT`); gate newest conventions behind `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental` (§5.1) and pin the convention version Lancelot targets in a constant (re-verify each release — the convention is not yet stable, the freshness risk).
- **Framework metrics** alongside GenAI: streaming-pump backpressure stalls, SPSC-ring occupancy, per-provider breaker state, background-pool queue depth (§3.5), upstream pool size/idle-reap count. The AI-specific operational signals FastAPI/Robyn/Blazeio expose *none* of.


---

## 6. RAG / vector & structured endpoints

RAG is the most common production AI pattern: embed the query, vector-search a store, assemble context, stream a grounded generation. The framework can't own the vector DB, but it can own the *concurrency and batching primitives* that make RAG endpoints fast and the *streaming-RAG* shape that interleaves retrieval with generation.

### 6.1 The embedding/vector-search endpoint pattern

**KNOWN — the shape:** `embed(query) → vector` (an `app.llm`-class outbound call to an embeddings model); `vector_store.search(vector, top_k) → docs` (outbound to a vector DB); optional rerank; assemble prompt; generate. Each arrow is an outbound call that benefits from the §4 pool. The embeddings span uses `gen_ai.operation.name=embeddings` + `gen_ai.embeddings.dimension.count` (§5.2 KNOWN).

**KNOWN — the concurrency problem:** ingestion/indexing embeds *thousands* of chunks. Done naively (one HTTP call per chunk, awaited serially) it is catastrophically slow; done with unbounded concurrency it trips the provider rate limit and exhausts the pool. The framework primitive that helps is **bounded-concurrency batching** — exactly the gap `app.llm`'s per-provider semaphore (§4.3) + a batching helper fills.

### 6.2 Framework primitives that help (RECOMMENDATION — DESIGN)

- **Batching middleware / helper** `app.llm.embed_batch(texts, *, batch_size=…, concurrency=…)`: chunks `texts` into provider-max batches (embeddings APIs take arrays), fans them out under the per-provider concurrency semaphore (§4.3), preserves order, returns vectors. One call, bounded fan-out, automatic backpressure, one OTel embeddings span per batch with summed `gen_ai.usage.input_tokens`.
- **Concurrency-limit primitive** `@app.limit(key="openai-embed", max=8)`: a declarative semaphore around any handler/section (shares the §4.3 admission model: over-limit → bounded wait → `503 Retry-After`, never an unbounded internal queue, `SYNTHESIS §3` AVOID `fastapi/01:405`). Applies to embeddings, reranking, any rate-limited dependency.
- **Fan-out / gather helper** `app.gather(*coros, concurrency=N, return_exceptions=...)`: structured-concurrency scatter-gather with a concurrency cap and per-task error isolation — the RAG "embed query + N parallel sub-retrievals + rerank" fan-out, on the GIL-free await path (`SYNTHESIS §1.2`: GIL dropped across `.await`, so N concurrent retrievals hold zero GIL).
- **`@app.embeddings` endpoint decorator**: an OpenAI-`/v1/embeddings`-compatible endpoint in one decorator (request validated by pydantic-core single-pass, `SYNTHESIS §1` steal #7), auto-batched, instrumented — so a Lancelot app can *be* an embeddings provider, symmetric with the MCP "framework as MCP server" idea (§2).
- **`@app.embedding_cache`**: content-hash-keyed cache (the §3.3 `SessionStore` backend) — identical text is never re-embedded (embeddings are deterministic per model); large cost saver for RAG over slowly-changing corpora. Cache key includes the model id (different models → different vectors).

### 6.3 Streaming RAG (RECOMMENDATION — DESIGN)

The good RAG UX streams: show "searching…", then retrieved sources, then the grounded answer token-by-token. This is a *typed-step stream* (§3.1) where the steps are `retrieval_started` → `sources` (the docs, as a Vercel-AI-SDK `source-document`/`source-url` part, §1.2 KNOWN) → `model_delta`* → `final`.

`@app.rag` helper composes the pieces on the streaming substrate (§1.3):
```python
@app.rag(codec="vercel_ai")
async def ask(ctx: RAGContext, q: str):
    yield ctx.step("retrieving")
    vec  = await app.llm.embed("text-embedding-…", q)        # embeddings span (§5)
    docs = await ctx.store.search(vec, top_k=5)               # retrieval span, gen_ai.data_source.id
    yield ctx.sources(docs)                                   # → source-document parts (§1.2)
    async with app.llm.stream("fast", messages=ctx.grounded_prompt(q, docs)) as up:
        async for delta in up: yield delta                    # token stream, disconnect→abort (§1.4)
```
- Retrieval is a span (`gen_ai.operation.name=retrieval`, opt-in `gen_ai.retrieval.{query.text,documents}` with `id`+`score`, §5.2 KNOWN) — interleaved into the trace alongside the generation span, all under the request span.
- The whole thing is one Lancelot stream: SPSC-ring backpressure, 15 s keepalive across the (potentially slow) retrieval gap, and disconnect→cancel→abort-upstream — so a user who navigates away during retrieval *or* generation stops all downstream work + billing immediately (§1.4).
- The vector store is a pluggable `VectorStore` ABC (pgvector / Turbopuffer / Qdrant / etc.) — Lancelot owns the *concurrency, streaming, and observability*, not the index.


---

## 7. Concrete AI-native API surface proposal (opinionated, reimplementable)

This is the proposed surface: the decorators/objects/middleware. Everything below is **RECOMMENDATION (DESIGN)** synthesized from the KNOWN evidence above and `SYNTHESIS §4.5-4.6`. The design rule throughout: *reflect/compile at registration, execute-only at request time* (`SYNTHESIS §1` steal #1, `fastapi/01:0.1`), framing/timers/pools/cancellation in the Rust core, the Python surface is thin and typed.

### 7.1 Streaming

```python
# Plain async-gen stream → SSE. Rust frames, keepalives, checkpoints, disconnect-aborts.
@app.stream                                   # media_type defaults text/event-stream
async def chat(req) -> AsyncIterator[str]:
    async with app.llm.stream("fast", messages=req.json()["messages"]) as up:
        async for delta in up:
            yield delta

# Explicit response objects (typed return, NOT exception-as-response — SYNTHESIS §4.5)
return SSEResponse(gen, keepalive=15.0, codec=None)        # raw SSE, your own data:
return AISSEResponse(gen, codec="openai")                  # emit OpenAI chunk wire format
return AISSEResponse(gen, codec="anthropic")               # emit Anthropic event wire format
return AISSEResponse(gen, codec="vercel_ai")               # emit x-vercel-ai-ui-message-stream:v1
return ChunkedResponse(gen, framing="jsonl")               # raw chunked NDJSON / binary
```
Knobs: `@app.stream(buffer=1, keepalive=15.0, coalesce=("25ms","2KiB"), detach_on_disconnect=False, eager_deps=True, codec=...)`. Defaults encode the §1 decisions (SPSC ring 1, 15 s `: ping\n\n`, time+size coalescing, disconnect→abort ON). First chunk flushed immediately; chunks 2..N coalesced (§1.5). Rust owns: SSE/JSONL framing, timer-wheel keepalive, per-chunk cancel checkpoint, disconnect watch → cancel → abort upstream (§1.3-1.4). Sync generators rejected for streaming (`SYNTHESIS §4.5`).

### 7.2 The LLM client

```python
app.llm = LLMClient(providers={...}, models={"fast": [...ordered failover...]}, prices=PRICE_TABLE)
await app.llm.chat(model, messages=..., output_schema=Model, **params)   # non-stream, structured passthrough
async with app.llm.stream(model, messages=...) as up: async for d in up: ...   # stream, disconnect→abort
up = await app.llm.open(provider, raw_body); app.llm.relay(up)           # zero-copy proxy (LiteLLM-as-primitive)
await app.llm.embed(model, text) / app.llm.embed_batch(model, texts, batch_size=, concurrency=)
```
Baked invariants (all KNOWN-sourced §4): HTTP/2 + `verify=True` default; **POST never auto-retried**; per-(provider,model) circuit breaker w/ half-open; per-provider concurrency semaphore (over-limit → `503 Retry-After`); pooled + stale-detected + least-in-flight (`blazeio/03 §4.4`); DNS cache; in-band timeout; usage/cost/OTel-span automatic; `orjson` bytes-in/out.

### 7.3 MCP

```python
@app.mcp.tool(auth_required=True)
async def search(query: str, top_k: int = 5) -> list[Doc]: "docstring → description"
@app.mcp.resource("file://{file_path}") async def read(file_path: str) -> bytes: ...
@app.mcp.prompt() def summarize(text: str) -> str: ...
app.mcp.configure(path="/mcp", protocol_versions=[...], auth_required=True)
# lancelot mcp-stdio app:app    # same registry over stdio for desktop agents
```
Lazy `@property` mount (zero cost unused, `robyn/03:542`). Streamable HTTP transport per §2.2: one endpoint POST/GET/DELETE, `Mcp-Session-Id`, `MCP-Protocol-Version` negotiation, `Last-Event-ID` resumability (reuses §1.3 ring + event-id), `Origin` validation, structured/binary results, Pydantic-model args → rich JSON-Schema (shared with OpenAPI registry). All five Robyn gaps closed (`robyn/03:553-560`).

### 7.4 Agents & durability

```python
@app.agent(codec="vercel_ai", session=True, durable=False)
async def assistant(ctx: AgentContext, msg: str):
    async for step in ctx.run(model="fast", tools=[search, calc], messages=ctx.history + [msg]):
        yield step                                  # ModelDelta|ToolCall|ToolResult|Reasoning|Final
```
`ctx.run` = framework-owned LLM↔tool loop on the streaming substrate (§3.1): typed step events, shared tool-arg coercion engine (`robyn/03:314`), disconnect→cancel-loop→abort. `session=True` → out-of-process `SessionStore` keyed by `gen_ai.conversation.id`. `durable=True` → step-checkpoint + replay (§3.4); resume cursor = the SSE `id:` (one resumability mechanism for SSE-reconnect + MCP `Last-Event-ID` + crash-recovery).

### 7.5 Background, structured output, RAG, observability

```python
background.spawn(ingest, doc)                       # detached Rust pool, request returns NOW (§3.5)
@app.post(output_schema=Answer) ...                 # provider constrained-decoding passthrough (§3.2)
@app.rag(codec="vercel_ai") async def ask(ctx, q): ...      # streaming RAG (§6.3)
@app.embeddings async def embed(req): ...           # OpenAI-/v1/embeddings-compatible, auto-batched
@app.limit(key="openai-embed", max=8) ...           # declarative concurrency semaphore (§6.2)
await app.gather(*coros, concurrency=N)             # capped structured fan-out (§6.2)
@app.observe(capture_content=True, redact=scrub)    # opt-in content capture + redaction hook (§5.5)
@app.eval(name="groundedness", sample=0.1) ...      # sampled post-response eval in bg pool (§5.5)
```

### 7.6 What the framework owns vs what the app owns

| Owned by Lancelot (Rust core + thin Python) | Owned by the app |
|---|---|
| SSE/JSONL/wire-codec framing; keepalive timer-wheel; per-chunk cancel checkpoint | the generator body / prompt logic |
| disconnect → cancel producer/agent-loop → **abort upstream LLM (stop billing)** | which model, which tools |
| outbound pool (HTTP/2, stale-detect, least-in-flight, breaker, semaphore, POST-never-retry) | the price table values, provider keys |
| token/cost accounting; OTel `gen_ai.*` spans+metrics; redaction hook plumbing | redaction function, eval scoring logic |
| MCP transport/session/resumability/auth; agent step-loop + checkpoint/replay | tool/resource/prompt implementations |
| backpressure (SPSC ring), GIL choreography, multiprocess scaling, bg worker pool | business logic, the vector store choice |

### 7.7 Why this wins (the differentiator restated)

FastAPI has the streaming *architecture* but Python-implemented, no client, no MCP, no cost/obs, and the `BackgroundTasks` footgun. Robyn has the Rust core + the MCP shape but the streaming anti-pattern and a disqualifying `ai.py`. Blazeio has the per-token cost discipline + the client shape but HTTP/1.1-only, `CERT_NONE`, no typed layer, no obs. **No one ties client-disconnect to upstream-abort + end-to-end backpressure + automatic cost/obs.** Lancelot, by owning the LLM I/O hot path in Rust, makes the expensive correct behaviours (don't pay for unread tokens, don't OOM on slow clients, don't double-bill on retry, emit spec OTel) the *default* — the app developer writes `yield token` and gets all of it. That is the AI-native moat.

---

## 8. Freshness & open questions

- **MCP spec moves fast**: §2 is the `2025-06-18` Streamable HTTP transport + the `2025-11-25` revision (current 2026-05). `MCP-Protocol-Version` negotiation makes version runtime; re-verify the transport spec each release. (KNOWN: <https://modelcontextprotocol.io/specification/2025-06-18/basic/transports>, <https://modelcontextprotocol.info/blog/mcp-next-version-update/>)
- **OTel GenAI conventions are Development status, not stable** (§5.1). Pin the targeted convention version in a constant; gate behind `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental`; re-verify attribute names/stability each release. (KNOWN: <https://opentelemetry.io/docs/specs/semconv/gen-ai/>)
- **Provider wire formats drift**: §1.2 captured 2026-05 (OpenAI `[DONE]` + optional `stream_options.include_usage`; Anthropic named events + split usage; Vercel AI SDK `v1`). The codec emitters/relay parsers are the freshness-sensitive code; everything else (substrate, pool, cancellation) is durable.
- **Open (to the brainstorm):** SPSC ring size, keepalive interval, coalescing thresholds — set by benchmark not guess (`SYNTHESIS §4.8`). Per-provider default `max_conns` under HTTP/2 multiplexing (one connection may suffice; benchmark vs N). Whether durable-agent checkpoint/replay (§3.4) is v1 or v2. Whether `@app.embeddings`/`@app.rag` are core or a `lancelot.ai` extra. hyper-vs-actix for the core affects HTTP/2 client ergonomics (`SYNTHESIS §4.1,§4.8`).

*Generated 2026-05-18. Cross-refs into `research/{fastapi,robyn,blazeio}/*` and `research/SYNTHESIS.md`; external claims carry URLs. `Lancelot/` is gitignored.*
