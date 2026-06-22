# Lancelot Model-Layer I/O — Byte-Path Inventory

> **Purpose**: Mechanism-level audit of what the existing Lancelot byte path already provides for an agent's model-I/O layer.  For each primitive: REUSE / EXTEND / BUILD-ON-TOP, with exact file + line citations.

---

## 1. Transport — `lancelot/llm.py` (`TransportPool`)

### 1.1 Outbound call discipline

**POST is NEVER retried** (`_IDEMPOTENT = frozenset({"GET","HEAD","PUT","DELETE","OPTIONS"})`, `llm.py:17`).  The docstring cites the Supermemory Oct-2025 retry-cascade incident as the concrete motivation (`llm.py:4-8`).  Retry budget wall-clock cap (`retry_budget_ms`, default 60 000 ms / env `LANCELOT_UPSTREAM_RETRY_BUDGET_MS`) prevents a runaway `Retry-After` (e.g. 3×30 s) from holding a call open past the operator's drain window (`llm.py:308-318`).

**Status for agent layer**: REUSE as-is.  An agent loop's primary LLM call is a POST — the non-retry rule is correct and already implemented.  The retry budget is the right knob for the agent's per-step wall-clock deadline.

### 1.2 Streaming I/O with cancel propagation

`pool.stream()` (`llm.py:588`) sets `read=None` timeout (`self._stream_timeout = httpx.Timeout(timeout, read=None)`, `llm.py:325`) — the correct shape for a streaming LLM call where a read deadline would kill any >30 s generation.  Client-disconnect → upstream cancel (`_CancellableStreamCtx`, `llm.py:120-195`) races the connection-open AND every subsequent chunk (`_race_chunk_vs_cancel`, `llm.py:36-72`) against an `asyncio.Event`.

`pool.relay()` (`llm.py:686`) is an async-context-manager that wraps open + forward + `aclose()` for the reverse-proxy (inference proxy) pattern: `async with pool.relay("POST", url, request=request) as upstream: async for chunk in upstream: yield chunk`.  The docstring notes the common mistake of returning a `StreamingResponse` from inside the `async with` block (exits CM before iteration — `llm.py:705-707`).

**Status for agent layer**: REUSE.  The relay pattern covers the LLM-streaming relay case.  The cancel-propagation chain (client→agent→upstream) is already present; the agent layer just needs to thread the request object through.

### 1.3 SSE scan for streamed usage (`_scan_last_data_frame`, `llm.py:75-117`)

Retains the LAST `data:` SSE frame containing `b"usage"` or `b"usageMetadata"` during streaming (scan-on-receive, `_SSE_SCAN_CAP = 65536` carry-over buffer cap, `llm.py:21`).  On clean-end, parses with `extract_tokens` + `calc_cost_usd` and accumulates into `cost_usd` ContextVar.  `track_usage=True` on `pool.stream()` activates it (`llm.py:612-620`).

Three provider shapes handled: OpenAI `stream_options={"include_usage":true}` near-terminal frame, Anthropic `message_delta`, Google `usageMetadata` (`llm.py:85-92`).

**Status for agent layer**: REUSE.  Pass `track_usage=True` on the LLM stream call.  The cost ContextVar accumulates automatically across the request's sub-calls.

### 1.4 Idle timeout on stalled upstream streams

`UpstreamIdleTimeout(idle_secs)` + `idle_guarded_chunks(response, idle_secs)` (`llm.py:25-30`, `llm.py:634-684`).  Pool-level env `LANCELOT_UPSTREAM_STREAM_IDLE_SECS` (default 0 = off).  Prevents a zombie SSE holding a connection past graceful drain.

**Status for agent layer**: EXTEND — set `LANCELOT_UPSTREAM_STREAM_IDLE_SECS` per-deployment; no code change needed.

### 1.5 Idempotency-Key injection (`llm.py:752-762`)

Stable `lancelot-{uuid4}` key stamped on FIRST try, reused across retries so the upstream can dedupe a network-lost retry against the original.  Caller-supplied key wins.  Pattern cited as Turbopuffer/Stainless (`llm.py:434-439`).

**Status for agent layer**: REUSE.  Non-idempotent (POST/PATCH) never gets a key — correct.

### 1.6 Connection-pool knobs (`llm.py:259-285`)

`httpx.Limits(max_connections, max_keepalive_connections, keepalive_expiry)` surfaced as constructor kwargs + env (`LANCELOT_UPSTREAM_MAX_CONNECTIONS=100`, `LANCELOT_UPSTREAM_MAX_KEEPALIVE=20`, `LANCELOT_UPSTREAM_KEEPALIVE_EXPIRY=5.0`).  The docstring notes httpx's silent defaults cap fanout under load (Turbopuffer/Bifrost pattern, `llm.py:259-262`).

**For agent fanout**: at high concurrency (fanout N parallel sub-LLM calls) the default `max_connections=100` is the cap; tune via env for wide-fanout agents.

### 1.7 fanout (`llm.py:800-903`)

`pool.fanout(requests, first_n=N, deadline=2.0, cancel_laggards=True, concurrency=K)` — first-N-wins parallel probe.  Results in COMPLETION ORDER (not input order — the whole point is first-N wins).  Partial-failure shape: successes are `httpx.Response`, failures are exception instances.  Rate gate (token bucket) runs BEFORE concurrency semaphore (`llm.py:857-865`).

**Status for agent layer**: REUSE.  This is the speculative-decode / parallel-retrieval primitive.  The agent layer's "ask N backends, take the first K responses" pattern maps directly.

### 1.8 spawn_child (`llm.py:905-963`)

`_ChildHandle` snapshots the parent's `traceparent`/`request_id`/`agent_id`/`session_affinity` at construction time — before ContextVars can be reset by the parent's completion.  Every child call gets `.setdefault(k, v)` header injection.  Cancel propagation: child forwards parent's `_cancel_event` if no explicit `request=` override (`llm.py:992-995`).

**Status for agent layer**: REUSE for sub-agent dispatch.  This is the context-isolation pattern for nested agent calls — the snapshot-at-construction guarantees the child carries the right parent identifiers even in fire-and-forget asyncio tasks.

### 1.9 Rate limit token bucket (`_TokenBucket`, `llm.py:198-221`)

Asyncio single-threaded, no lock (correct — state only mutates between awaits).  Starts FULL (burst = `rate`), refills at `rate`/s capped at `rate`.  Applied in `pool.request()`, `pool.fanout()` (pre-concurrency-semaphore), cited as Hatchet pattern.

**Status for agent layer**: REUSE.  Shared API key across parallel sub-agents needs the outbound rate cap.

### 1.10 mTLS + SIGHUP reload (`llm.py:350-373`)

`pool.reload_mtls()` rebuilds the httpx client from the same cert/key file paths.  In-flight calls use the old cert (TLS state lives on the connection); new connections pick up the rotated cert.  Old client closes after rebuild (`llm.py:368-373`).

**Status for agent layer**: REUSE.  Agent calls to secure internal backends (embedding services, private model servers) can use mTLS without additional code.

---

## 2. Upstream Observability — `lancelot/upstream_metrics.py`

### 2.1 Provider detection (`upstream_metrics.py:74-96`)

URL-regex matching (compiled patterns) against 8 provider domains: openai, anthropic, google, groq, deepseek, mistral, together, fireworks.  Falls back to `"unknown"` (still a useful metric bucket).  Pure rules — no AI logic, no network call.

**Status for agent layer**: REUSE.  The agent loop can call `detect_provider(url)` for conditional prompt-format selection, though the canonical use is the OTel metric label.

### 2.2 Token extraction (`upstream_metrics.py:108-129`)

`extract_tokens(body) -> (int, int)`.  Handles:
- OpenAI: `usage.prompt_tokens` / `usage.completion_tokens`
- Anthropic: `usage.input_tokens` / `usage.output_tokens`
- Google: `usageMetadata.promptTokenCount` / `usageMetadata.candidatesTokenCount`

Falls back to 0 on any missing field — never raises.

**Status for agent layer**: REUSE for token budget tracking.  The agent loop's `remaining_budget = budget - tokens_in.get() - tokens_out.get()` pattern works because this function feeds the context ContextVars automatically.

### 2.3 Error taxonomy (`upstream_metrics.py:154-185`)

`classify_status(status, body) -> str`:
- `ok` — 2xx
- `quota` — 429 (explicit: client should back off)
- `timeout` — 408 / 504
- `permanent` — 400/401/403/404/422 (do NOT retry)
- `transient` — 502/503/521 (retry eligible, already handled by TransportPool)
- `upstream_5xx` — catch-all 5xx

Note: the `permanent` set explicitly includes 422 — the typical "malformed tool call" response from LLMs.  The agent loop's retry logic should key on this taxonomy: `permanent` → fix the input (or escalate); `quota` → back off; `transient` → retry (already handled by TransportPool); `timeout` → the step budget is blown.

**Status for agent layer**: REUSE directly.  This is the agent loop's error-branch selector.

### 2.4 Cost calc (`upstream_metrics.py:132-151`)

`calc_cost_usd(provider, model, tokens_in, tokens_out)`.  Rate table shape: `{provider: {model: {"input_per_1k": float, "output_per_1k": float}}}` with `"default"` / `"*"` fallback key.  Table set via `set_rate_table(...)` or `LANCELOT_LLM_RATES` env JSON.  Lancelot ships the table EMPTY — rates change weekly.

**Status for agent layer**: REUSE.  The agent cost-budget check is: `cost_usd.get() > budget_usd` after each step.

### 2.5 OTel metric surface (`upstream_metrics.py:235-291`)

`record_upstream(url, status, duration_s, *, body, ttft_s, provider, model)` emits:
- `lancelot.upstream.requests` — counter by `llm.provider` / `llm.model` / `llm.class` / `http.status_code`
- `lancelot.upstream.duration` — histogram (s)
- `lancelot.upstream.ttft` — histogram (s), only when `ttft_s` is passed
- `lancelot.upstream.tokens.input` / `.output` — counters
- `lancelot.upstream.cost.usd` — histogram

All lazy-initialized: returns the attrs dict even when OTel isn't installed — cost calc is always available.

**Status for agent layer**: REUSE.  No agent-specific metrics are needed beyond what is already emitted.

---

## 3. Context ContextVars — `lancelot/context.py`

All ContextVars (`context.py:20-105`) are safe to read outside a request (default sentinels: `"-"` for string IDs, `0.0`/`0` for numeric, `None` for objects).  asyncio Tasks each get their own context copy (copy-on-Task semantics), so concurrent sub-agent tasks do NOT bleed across each other.

| ContextVar | Default | Set by | Read by |
|---|---|---|---|
| `request_id` | `"-"` | ASGI seam (inbound `X-Request-ID`) | logs, TransportPool.`_inject_traceparent` |
| `trace_id` / `trace_parent` | `"-"` / `None` | ASGI seam (inbound `traceparent`) | logs, TransportPool |
| `cost_usd` | `0.0` | `_emit_upstream` / `_record_stream_usage` | ASGI seam → `X-Lancelot-Cost-USD` + metric |
| `tokens_in` / `tokens_out` | `0` | user or upstream_metrics | agent budget check |
| `agent_id` | `"-"` | ASGI seam (inbound `X-Agent-Id` header) | logs, TransportPool header injection |
| `tenant_id` | `"-"` | user middleware (e.g. TenantConcurrencyLimiter) | metrics cardinality-bounded label |
| `queue_wait_ms` | `0.0` | TenantConcurrencyLimiter semaphore acquire | ASGI seam → `lancelot.http.queue_wait_ms` |
| `principal` | `None` | user auth dependency | user handler |

### 3.1 What the agent layer LACKS in context

The current ContextVars are per-HTTP-request scoped, not per-agent-step.  An agent loop that runs 10 steps inside one HTTP handler will accumulate `cost_usd` correctly (it keeps incrementing), but there is NO per-step cost isolation.  The agent layer needs to snapshot `cost_usd.get()` BEFORE each step and subtract to get per-step cost.  This is BUILD-ON-TOP (pure user code, no new ContextVar needed).

There is also no `conversation_id` ContextVar — this is the missing link between consecutive HTTP requests in a multi-turn agent session.  The agent layer must pass this in its own session store.  BUILD from scratch.

---

## 4. Canonical JSON — `lancelot/canonical.py`

`dumps_canonical(obj) -> bytes`: `json.dumps(sort_keys=True, separators=(",",":"), ensure_ascii=False, allow_nan=False).encode("utf-8")` (`canonical.py:28-35`).

Stability guarantee: the same dict produces the same byte string across Python runs.  `allow_nan=False` raises on NaN/Infinity — correct for LLM prompt payloads (a silent NaN in a prompt payload produces a non-RFC-8259 JSON that some providers reject silently).

**Status for agent layer**: REUSE.  Use as the hash input for prompt-cache key computation.  The typical pattern is `hashlib.sha256(dumps_canonical({"model": m, "messages": msgs, "tools": tools})).hexdigest()` → cache key.  Already the stated use case in the lens file.

### 4.1 What canonical does NOT provide

It is a byte-encoder only.  It does NOT:
- Normalize message role ordering (if callers accidentally reorder `messages`, the hash changes).
- Strip or normalize ephemeral fields (e.g. `request_id` in a message body would bust the cache).
- Handle Pydantic model serialization (caller must call `.model_dump()` first, or use pydantic-core `model_dump(mode="json")`).

These are BUILD-ON-TOP concerns for the agent's `ContextBundle.to_cache_key()` method.

---

## 5. Batch Coalescing — `lancelot/batch.py`

### 5.1 What it provides

`@app.batch(batch_window_ms, max_batch, key=lambda item: item.system_prompt)` — the ASGI-layer request coalescer.  Groups concurrent HTTP requests by key, waits up to `batch_window_ms`, then calls the handler ONCE with `list[T]`.  Fan-back by index.  Whole-batch error semantics: all waiters see the same exception.

M15 T9 extensions (`batch.py:56-93`): `size_fn` / `max_size` budget trigger — a batch flushes when `size_fn(pending) >= max_size` (token budget) in addition to the count trigger.  Defer-to-tail: an item that would push a non-empty batch over budget defers the current pending to flush first, then starts fresh.  `max_keys` cap (default 1024) → `BatchOverloadError` → ASGI seam maps to 503 + `Retry-After` (`batch.py:33-43`).

**Critical bug that was fixed**: timer-flush self-cancel (`batch.py:153`): `if timer is not None and not timer.done() and timer is not asyncio.current_task(): timer.cancel()`.  The guard `timer is not asyncio.current_task()` was the fix — without it, the timer's own flush task would cancel itself at the `timer.cancel()` line, propagating `CancelledError` into the user handler.

### 5.2 For the agent layer

`@app.batch` is an HTTP-layer primitive: it coalesces INCOMING requests before they reach the handler.  This is the right primitive for an inference endpoint that batches multiple user requests into one upstream LLM call.

It is NOT the right primitive for an agent loop's internal "batch tool calls" pattern (an agent deciding to call 3 tools in one LLM request).  That pattern is BUILD-ON-TOP and lives in the agent loop's tool-call dispatch.

The `size_fn` / `max_size` token-budget flush is directly useful as the batch flush trigger for token-budget-aware batching (flush when the batch would exceed the model's context window).

---

## 6. Agent-Layer Primitive Audit: REUSE / EXTEND / BUILD-ON-TOP

| Agent primitive | Lancelot byte-path status | Verdict | Action |
|---|---|---|---|
| LLM HTTP call (unary) | `TransportPool.request()` — retry, Retry-After, budget cap, cancel, metrics | **REUSE** | Thread `request=req` for C2 cancel |
| LLM streaming relay | `TransportPool.relay()` | **REUSE** | Use inside `@app.stream` handler |
| Token/cost accounting | `upstream_metrics.record_upstream` + `cost_usd` ContextVar + `track_usage=True` | **REUSE** | Call after each step; read `cost_usd.get()` for budget check |
| Streamed token accounting | `_scan_last_data_frame` + `_record_stream_usage` (`track_usage=True`) | **REUSE** | Pass `track_usage=True` to `pool.stream()` |
| Error taxonomy | `classify_status(status)` → `ok/quota/timeout/permanent/transient/upstream_5xx` | **REUSE** | Map to agent retry/escalate/bail branches |
| Parallel sub-agent calls | `TransportPool.fanout()` | **REUSE** | First-N-wins pattern for speculative calls |
| Sub-agent context isolation | `TransportPool.spawn_child(req, session_affinity=...)` | **REUSE** | Snapshot parent headers before asyncio.Task reset |
| Distributed trace propagation | `_inject_traceparent` + `context.trace_parent` | **REUSE** | Automatic; no agent code needed |
| Idempotency-Key on retries | `_inject_idempotency` on idempotent verbs | **REUSE** | Automatic |
| Idle stream guard | `idle_guarded_chunks` + `UpstreamIdleTimeout` | **EXTEND** | Set `LANCELOT_UPSTREAM_STREAM_IDLE_SECS` |
| mTLS to private model server | `TransportPool(mtls_cert=, mtls_key=, mtls_ca=)` | **REUSE** | No code change |
| Outbound rate cap | `_TokenBucket` + `rate_limit=` kwarg | **REUSE** | Set on pool construction |
| Cache-stable prompt bytes | `dumps_canonical(obj)` | **REUSE** | Use as hash input for cache key |
| HTTP-layer request coalescing | `@app.batch(batch_window_ms, max_batch, size_fn=...)` | **REUSE** | For multi-user batching on inference endpoints |
| Provider + model detection | `detect_provider(url)` + `detect_model(body)` | **REUSE** | For conditional format adaptation |
| Message-shape translation | NOT present | **BUILD** | ProviderProfile / `messages` → per-provider format |
| Agent loop (ReAct, budget, stuck) | NOT present | **BUILD** | The loop itself |
| Context window management | NOT present | **BUILD** | ContextBundle / observation masking / compaction |
| Per-step cost isolation | NOT present (per-request accumulator only) | **BUILD-ON-TOP** | Snapshot `cost_usd.get()` before/after each step |
| Conversation / session identity | NOT present | **BUILD** | `conversation_id` in session store |
| Typed memory (4-tier, RRF) | NOT present | **BUILD** | MemoryPool is byte-OOM, not agent memory |
| Evals | NOT present | **BUILD** | — |
| Durable checkpoint/resume | NOT present | **BUILD** | — |
| Safety / permissions | NOT present | **BUILD** | — |

---

## 7. Non-Obvious Production Gotchas (mechanism-level)

**a. `relay()` inside `async with` anti-pattern** (`llm.py:705-707`): `return StreamingResponse(upstream)` from inside an `async with pool.relay(...) as upstream:` block exits the CM BEFORE the response body iterates — the upstream is `aclose()`d immediately, producing an empty body.  Correct pattern: `async for chunk in upstream: yield chunk` inside the block, inside an `@app.stream` handler.

**b. `track_usage=False` default** (`llm.py:612`): SSE usage frame retention is opt-in.  The default is zero-overhead (no frame buffering).  Turn on per-pool or per-call; do NOT enable globally on high-concurrency relay endpoints where the LLM never emits a usage frame (the carry-over buffer `_SSE_SCAN_CAP=65536` is small but non-zero per in-flight stream).

**c. POST is NEVER retried** — the agent loop MUST NOT wrap the LLM call in its own retry at the HTTP layer; retries at the loop level (re-issuing a new POST) are correct.  Idempotent verbs (tool-call parameter fetch, GET-based retrieval) retry automatically.

**d. `classify_status(422)` → `"permanent"`** — the standard FastAPI/OpenAI response for a malformed tool call is 422.  The agent loop must treat 422 as a prompt-fix problem, not a transient retry.

**e. `max_connections=100` silently caps fanout** (`llm.py:277-285`): if a wide fanout (fanout N=200 sub-agent calls) hits this pool-level cap, additional calls queue silently inside httpx.  Scale `LANCELOT_UPSTREAM_MAX_CONNECTIONS` to match peak fanout width.

**f. asyncio copy-on-Task semantics**: `spawn_child` is needed ONLY when the child `asyncio.Task` may outlive the parent's request scope (fire-and-forget, background audit, etc.).  For inline `await pool.request(...)` within the same task, ContextVars propagate automatically — `spawn_child` is not needed and adds header-merge overhead.

**g. `dumps_canonical` does not handle Pydantic models** — call `.model_dump(mode="json")` first before passing to `dumps_canonical`.  Passing a Pydantic model directly raises `TypeError`.

---

## 8. What Is NOT the Byte Path's Job (clarity on the boundary)

- **Message-format translation** (`system`/`user`/`assistant` → Anthropic's `messages` + `system` field split, Google's `contents` → `role: "user"/"model"`, tool-result shapes) — TransportPool does HTTP transport, not message-shape normalization.  This is the ProviderProfile / model-adapter layer the agent must build.
- **Prompt construction and compression** — `dumps_canonical` gives stable bytes but does not assemble, truncate, or prioritize context.
- **Token counting ahead-of-call** — `extract_tokens` reads the response body; there is no pre-call token estimator (intentional: no tiktoken dep).  The agent must use provider-provided counts post-call or estimate via its own lightweight counter.
- **Conversation memory** — `context.py` has request-scoped ContextVars; there is no cross-request conversation store.
- **Agent identity beyond `agent_id`** — `agent_id` is a string tag for metrics/logs, not a capability profile.

---

*Sources: `lancelot/llm.py`, `lancelot/upstream_metrics.py`, `lancelot/context.py`, `lancelot/canonical.py`, `lancelot/batch.py`.*
