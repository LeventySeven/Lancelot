# Lancelot Byte-Path — Serve / Tools / Streaming / Safety Inventory

**Source:** Direct read of `lancelot/{stream,tool,mcp,metrics,telemetry,net,secrets,tenant,ratelimit,webhook,app}.py`
**Date:** 2026-06-22
**Purpose:** Inventory what the byte path already provides for the agent layer. Classification: REUSE / EXTEND / BUILD-ON-TOP.

---

## 1. Streaming (`lancelot/stream.py` + `app.py:App.stream`)

### What exists

**R3 pump architecture** (native path): async-gen → bounded `mpsc` channel (default 256 slots, `LANCELOT_STREAM_BUFFER`) → hyper `StreamBody`. One asyncio-bridge crossing per stream (not per chunk); GIL released across each `__anext__` await on the on-loop `_drain` coroutine. Same-thread `try_push` for backpressure — only parks (event-driven `reserve().await`) when the channel is full. Client disconnect → next `try_push` sees `Closed` → ≤1 extra chunk produced. SIGTERM → `graceful_loop_teardown` cancels tasks + calls `shutdown_asyncgens` so user generator's `finally`/`aclose` runs.

**SSE surface** (`Event(name, data, id)` → `.render() -> bytes`):
- `_clean_field(s)` strips `CR/LF/NUL + control chars` (chars < 0x20 or 0x7F) from `id:` and `event:` fields. A newline in `id:` smuggles an extra frame; NUL in `id:` silently resets `lastEventId` (WHATWG SSE §9.2.6). **This exact strip should be reused verbatim in any agent stream output.**
- `sse_adapter(gen, done_sentinel="[DONE]")` wraps a user async-gen: `Event`/`bytes`/`str` pass through byte-identical; bare model/dict → `_sse_data_frame(_wire_json(obj))` (same pydantic-core serialize path as unary 200 responses); clean exhaustion → terminal `data: [DONE]\n\n`; exception → no sentinel (a half-stream must not look complete — correct invariant).
- `_SSEStream` proxy: carries `media_type = "text/event-stream"` for the native R3 path's content-type detection; forwards `__aiter__`/`__anext__`/`aclose` so cancellation reaches user generator.

**`@app.stream` kwargs** (all ASGI-seam / uvicorn only; native `App.run()` passes `(method, path, handler)` only):
- `sse=True` + `done_sentinel` — SSE-of-models framing (M15 T7)
- `defer_status=True` — pulls first item BEFORE committing 200 + SSE headers; handler exception before first `yield` → real 4xx/5xx (not 200 + error frame)
- `max_stream_silence=N` — cancels generator if no DATA (not keepalive pings) for N seconds; last-DATA tracked separately from last-PING so a keepalive zombie is detected
- `max_stream_lifetime=N` — absolute wall-clock cap from stream start; emits `event: reconnect\ndata: {"reason":"max_stream_lifetime"}\n\n` then closes (Polar 10-min-cap pattern)
- `error_frame=True` — routes mid-stream raise through scrubbed RFC-9457 `ProblemDetail`; GUARANTEES terminal sentinel on every non-disconnect end

**TTFT metric**: `lancelot.metrics.record_stream_ttft(route_path, method, ttft_s)` emits `lancelot.http.stream.ttft` histogram on first streamed chunk (keepalive pings excluded). Route path is the TEMPLATE, not the concrete URL (cardinality bound).

### Agent-layer classification

**REUSE** — the R3 pump, `Event`/`sse_adapter`/`_SSEStream`, `_clean_field`, TTFT metric, and all `@app.stream` knobs are the streaming substrate for an agent token-stream response. The agent layer yields `Event('delta', {...})` objects and gets SSE framing, TTFT instrumentation, keepalive, and backpressure for free. No rebuild needed.

**Key invariant to preserve:** `sse_adapter` emits already-framed `bytes` so BOTH transports (uvicorn ASGI seam + native R3 pump) are byte-identical. Any agent-layer stream wrapper must stay on this path — do NOT reintroduce per-chunk cross-thread bridging (`call_soon_threadsafe` per chunk = TIER-D, 16–26× cliff).

---

## 2. Tools (`lancelot/tool.py` + `app.py:App.tool/tool_schemas/call_tool`)

### What exists

**`ToolSpec`** (dataclass): `name`, `description`, `func`, `read_only: bool`, `scopes: list`, `input_schema: dict` (JSON Schema `{type:object, properties, required}`), `output_schema: dict | None`, `_validator` (Pydantic `create_model` ad-hoc validator).

**`_build_tool_schema(func, name, description)`**: uses `inspect.signature` + `typing.get_type_hints(include_extras=True)` → per-param `TypeAdapter(ann).json_schema()` → `{type:"object", properties, required}`. Output schema: `TypeAdapter(return_annotation).json_schema()` if annotation is not `None`/empty/unschemable — `None` when missing (deliberate: absence is meaningful). Single source of truth shared with HTTP route validation.

**`app.call_tool(name, /, **kwargs)`** — positional-only `name` (PEP 570) so tools with a `name=` parameter don't collide; validates kwargs through `spec._validator(**kwargs).model_dump()` then calls `spec.func(**validated)`.

**`app.tool_schemas()`** returns `[{name, description, input_schema[, output_schema]}]` — exactly Anthropic/OpenAI/Google/MCP wire shape. `output_schema` included only when non-None.

### Agent-layer classification

**REUSE** — `ToolSpec`, `make_tool_spec`, `app.tool_schemas()`, and `app.call_tool()` are the tool registration and invocation substrate. An agent loop calls `app.tool_schemas()` to build its tool-list prompt and `app.call_tool(name, **args)` to execute. Pydantic validation on both input and output schemas already wired.

**EXTEND** — the agent layer needs two additions on top of this substrate:

1. **Tool permission gate** (`read_only`, `scopes` fields already exist on `ToolSpec` but are not enforced anywhere in the tool/mcp stack). The agent safety layer reads these and enforces `allow/deny/ask` before `call_tool`.
2. **Tool result truncation** — `mcp.py` has a WARN at 4096 bytes but does NOT truncate. The agent layer should enforce a hard truncation + `handle-and-fetch` pattern for tool outputs bound for the context window.

---

## 3. MCP (`lancelot/mcp.py`)

### What exists

**`lancelot.mcp.mount(app, path='/mcp', *, allowed_origins=None, max_response_bytes=4096)`** — top-level canonical entry. Internally `make_handler(app, ...)` returns a JSON-RPC 2.0 async handler.

**Session management**: `secrets.token_hex(16)` session id issued on `initialize`, returned in `mcp-session-id` response header. Session set is in-memory (no persistence — correct for single-worker; multi-worker needs a shared store, documented gap).

**Protocol gaps closed vs Robyn reference**: `Mcp-Protocol-Version` validated (400 on unsupported); `Origin` allow-list (403 on mismatch); `Mcp-Session-Id` issued on initialize.

**Minimal-return discipline** (M7 T1.5 / Dex Horthy pattern): `max_response_bytes=4096` default — WARN-only (never truncates). Log message explicitly names the handle-and-fetch pattern: "consider returning an id/handle and exposing a separate `fetch_details` tool."

**`tools/call` error shape**: exception → `{"isError": True, "content": [{"type":"text","text":str(exc)}]}` — the MCP spec's isError convention. NOT propagated as JSON-RPC error code.

**PROTOCOL_VERSION = "2025-06-18"** (spec date constant).

### Agent-layer classification

**REUSE** — `lancelot.mcp.mount` is the MCP server mount. Agent tools registered via `@app.tool` are auto-exposed on the MCP endpoint. An agent framework that exposes its tool registry as MCP gets this for free.

**BUILD-ON-TOP** — session affinity for multi-worker MCP (the `Mcp-Session-Id` set is per-process); tool execution trace injection (`traceparent` into MCP tool calls); resource and prompt endpoints (currently stubs returning `{}` capabilities).

---

## 4. Metrics (`lancelot/metrics.py`)

### What exists

**Lazy OTel binding**: `_maybe_bind()` — single `_NOT_INITIALIZED` sentinel, tries `from opentelemetry import metrics` once; on failure `_state = None` and every subsequent call is a single `None` check. Never retries. Instruments:

- `lancelot.http.requests` (counter)
- `lancelot.http.request.duration` (histogram, unit=s)
- `lancelot.http.requests.in_flight` (UpDownCounter)
- `lancelot.http.stream.ttft` (histogram, unit=s) — TTFT only on streaming
- `lancelot.http.cost.usd` (histogram, unit=USD) — skipped at zero (most routes don't accumulate cost)
- `lancelot.http.queue_wait_ms` (histogram, unit=ms) — records 0.0 baseline even when not waiting (saturated semaphore IS the signal)

**Cardinality guard** (`_bounded_tenant`): `LANCELOT_TENANT_LABEL_MAX` (default 1000) caps distinct tenant label values; over-cap → `_overflow_` bucket. Never evicts (cardinality cap, not LRU). GIL-safe (set mutation between awaits).

**Attributes on every metric**: `http.route` (TEMPLATE), `http.method`, `http.status_class` (Nxx), `http.status_code` (exact — enables adverse-path slicing: 499 client-disconnect, 504 per-route timeout, 503 upstream).

**SCOPE NOTE**: metrics fire ONLY on the Python ASGI seam (`lancelot/asgi.py`). The native Rust `_native.run` server emits NO OTel metrics — `grep -rI 'opentelemetry|record_request|histogram' src/` is empty. Operators on the native const tier (183k rps, zero-GIL) get no RED/TTFT observability from it by design.

### Agent-layer classification

**REUSE** — `record_request` and `record_stream_ttft` cover HTTP-level observability. The agent layer rides these for free on every endpoint.

**EXTEND** — add agent-specific histograms following the same lazy-OTel pattern:
- `lancelot.agent.loop_iterations` (counter + histogram)
- `lancelot.agent.tool_calls` (by tool name — cardinality-bounded via the same `_bounded_tenant` pattern)
- `lancelot.agent.context_tokens` (histogram, from `lancelot.context.tokens_in/out`)

The `cost_usd` ContextVar already accumulates per-request across sub-LLM calls and is emitted via `lancelot.http.cost.usd`. An agent that accumulates cost across tool-calling loop iterations gets this for free if all calls go through TransportPool.

---

## 5. Telemetry (`lancelot/telemetry.py`)

### What exists

**`Event(kind, attrs)`** dataclass + global `_SINK = None` (zero-cost default). `set_sink(callable)` → plugs any `callable(Event)` for Langfuse/Phoenix/Helicone. `emit(event)` — try/except swallows sink errors (never breaks a request).

**`cost_event_from_openai_response(response_json)`**: parses usage block supporting THREE provider shapes:
- OpenAI: `usage.prompt_tokens` / `usage.completion_tokens` + `usage.prompt_tokens_details.cached_tokens`
- Anthropic: `usage.input_tokens` / `usage.output_tokens` + `usage.cache_read_input_tokens`
- `gen_ai.cost.usd` is always `None` here (rates are user-configured via `lancelot.upstream_metrics.set_rate_table()`; Lancelot never ships rates)

### Agent-layer classification

**REUSE** — `set_sink` / `emit` / `Event` are the telemetry hook. An agent framework that wants Langfuse/Phoenix span integration plugs a sink here. The cost_event_from_openai_response parser handles the provider shape translation that agent frameworks otherwise hand-roll.

**EXTEND** — add `gen_ai.span` event kind (currently a comment, not implemented) for agent loop spans: start/end/tool-call/error.

---

## 6. Net / SSRF Guard (`lancelot/net.py`)

### What exists

**`assert_safe_url(url: str) -> None`** — raises `SSRFError(ValueError)` for blocked URLs. Full coverage:
- Scheme: only `http`/`https` allowed
- Hostname: blocks `localhost` + `.localhost` subdomains explicitly
- IP checks (via `socket.getaddrinfo` on EVERY resolved IP — normalizes obfuscated literals like `0x7f000001`, `127.1`, `2130706433` that `ipaddress.ip_address()` would reject): `is_private`, `is_loopback`, `is_link_local`, `is_reserved`, `is_multicast`, `is_unspecified`
- IPv4-mapped IPv6 (`::ffff:a.b.c.d`) — unwrapped + re-checked (critical for `::ffff:100.64.0.1` CGNAT which `is_private` misses)
- CGNAT (`100.64.0.0/10`, RFC 6598) — the ONE range no stdlib `ipaddress` flag covers; explicit check against `_CGNAT` network object

**DNS cache**: 30s TTL (`LANCELOT_SSRF_DNS_TTL_SECS`). Narrowing (not eliminating) DNS-rebinding TOCTOU gap — validate → httpx connects later; fully rebinding-proof needs pinned-IP transport (documented as out-of-scope, adequate for webhook/tool-fetch threat model).

**`SSRFError(ValueError)`**: subclasses `ValueError` so it surfaces as bad-input through Pydantic validation layers.

**`is_safe_public_url(url: str) -> bool`**: bool variant for predicate use.

### Agent-layer classification

**REUSE** — Any agent tool that accepts a user-supplied URL (fetch tool, webhook tool, browser tool) MUST call `assert_safe_url(url)` before passing it to `TransportPool` or any HTTP client. The SSRF guard is already stdlib-only (no new dep), covers all the production-relevant edge cases, and is a `ValueError` subclass so it integrates with Pydantic validation.

Critical for agent tools with fetch/browse capabilities — pass `block_private_ips=True` on `TransportPool` which lazy-imports and calls this.

---

## 7. Secrets (`lancelot/secrets.py`)

### What exists

**`lancelot.secrets.get(name) -> str | None`** with pluggable backends: `EnvBackend` (default), `FileBackend` (k8s secret-volume pattern), `VaultBackend` (lazy `hvac`), `InfisicalBackend` (lazy `infisical_sdk`), `AwsSecretsManagerBackend` (lazy `boto3`). All non-env backends lazy-import their SDK (cardinal rule maintained).

**FileBackend path-traversal guard**: `os.path.realpath(os.path.join(base, name))` + `commonpath` check — blocks `..` escape and absolute `name` (same `realpath + commonpath` pattern as `StaticFiles`).

**SIGHUP rotation**: `install_sighup_handler()` → `signal.signal(SIGHUP, _handler)` → `rotate()` → `backend.refresh()` (dict.clear on cached backends; lazy re-fetch on next `get()`). Runs in main thread (signal handler constraint); the work (dict.clear) is fast enough.

**Audit hook**: `set_audit_hook(fn)` → `fn(name: str, found: bool)` called on every `get()`. Hook failure is swallowed (audit MUST NOT break the caller). Operator wires to SIEM/Splunk.

**Thread safety**: `threading.Lock()` around `configure()`. `get()` captures backend reference locally before the lock-free audit hook call (safe under configure() race).

### Agent-layer classification

**REUSE** — `lancelot.secrets.get("OPENAI_API_KEY")` is the correct pattern for agent provider credential access. Every provider SDK lazy-loaded via an agent tool should read credentials this way (not raw `os.environ.get`). SIGHUP rotation means credential rotation without restart.

**EXTEND** — agents calling multiple providers need one `get()` call per credential, per tool execution. No change to the secrets module needed; the audit hook gives SIEM visibility on which tool read which credential.

---

## 8. Tenant (`lancelot/tenant.py`)

### What exists

**`TenantConcurrencyLimiter`** (pure-ASGI middleware): per-tenant `asyncio.Semaphore` keyed by `X-Tenant-Id` header (configurable). Default cap: `LANCELOT_TENANT_CONCURRENCY=10`. Anon bucket: `_anon_`. Measures `queue_wait_ms = time.perf_counter() delta` → writes to `lancelot.context.queue_wait_ms` ContextVar + `lancelot.context.tenant_id` before calling inner app. ASGI `finally` block resets both ContextVars (correct isolation across requests).

**Semaphore dict**: grows unbounded by design (cardinality control is operator's job — set `label_header` to an auth-pinned tenant id, not raw user input). Pair with `_bounded_tenant` in metrics for cardinality safety.

**`cap=0` path**: disables semaphore acquire but still records 0.0 `queue_wait_ms` baseline (saturated semaphore IS the signal; the baseline is what dashboards alert against).

### Agent-layer classification

**REUSE** — multi-tenant agent deployments add `TenantConcurrencyLimiter` as ASGI middleware. Per-tenant concurrency control, queue_wait_ms, and tenant_id ContextVar propagation are all automatic. The 0.0 baseline design is a non-obvious correctness decision worth copying.

**EXTEND** — agent tool-execution budget per tenant (token budget cap per tenant per time window) builds on top of this: the tenant_id ContextVar is already set by the time the handler runs, so a tool execution budget gate reads it from context.

---

## 9. Rate Limiting (`lancelot/ratelimit.py`)

### What exists

**`RateLimit`** (pure-ASGI middleware): token bucket, INBOUND rate limiting. `rate="100/minute"` or `(count, window_seconds)` tuple. Keys: `ip` (scope['client'][0]), `api_key` (X-API-Key header → Authorization Bearer → IP fallback), or `callable(scope) -> str`.

**Key correctness notes from the module**:
- Per-PROCESS buckets (default `InProcessTokenBucketBackend`): W workers → effective global limit is W× looser. For per-worker safety valve: fine. For true global limit: supply `backend=` (Redis-backed `TokenBucketBackend`).
- `key='ip'` behind a proxy = proxy's IP, not real client's. Safe over-throttle (never a bypass). For real client IP: `key=callable` reading validated `X-Forwarded-For` from a TRUSTED proxy.
- `burst` defaults to `count` (one window burst allowed).

**On shed**: 429 + `Retry-After` (delta-seconds, ceil'd) + `RateLimit-Limit` / `RateLimit-Remaining` / `RateLimit-Reset` headers. Body: `{"detail":"Rate limit exceeded"}`.

**`TokenBucketBackend` protocol**: one method `take(key, capacity, refill_per_sec) -> (allowed, remaining, reset_after_seconds)`.

**`_parse_rate`**: accepts `'100/minute'`, `'5/30s'` (explicit seconds), all unit aliases. Returns `(count, window_s)`.

### Agent-layer classification

**REUSE** — `RateLimit(rate="100/minute", key="api_key")` is the inbound throttle for agent API endpoints. Pair with `TenantConcurrencyLimiter` for full admission control.

**BUILD-ON-TOP** — agent-specific rate limiting on tool-call density (not just HTTP rate) needs to build on top of the `InProcessTokenBucketBackend` or a custom `TokenBucketBackend`. The `take()` interface is simple enough to wrap in a tool-execution decorator.

---

## 10. Webhook (`lancelot/webhook.py`)

### What exists

**Standard Webhooks scheme** (standardwebhooks.com — same as Render/Svix). STDLIB-ONLY: `hmac/hashlib/base64/time/uuid`.

**`sign(payload, secret, *, msg_id=None, timestamp=None) -> dict[str,str]`**: returns `{webhook-id, webhook-timestamp, webhook-signature}` headers. HMAC-SHA256 over `f"{msg_id}.{ts}.".encode() + payload`. Signature format: `v1,<base64>`. `msg_id` stable across retries (idempotency key); must not contain `.`.

**`verify(payload, headers, secret, *, tolerance_s=300) -> bool`**: constant-time `hmac.compare_digest`; timestamp freshness window (`tolerance_s=0` disables); accepts multiple space-separated `v1,` signatures (key rotation). Returns `False` on any structural failure (fail-closed). Replay dedup on `webhook-id` is caller's job.

**Key length guard**: 24–64 bytes decoded. `verify()` returns `False` on misconfigured key (same as a missing key — fail-closed). Prior bug (empty key → fail-open via `hmac.new(b"", ...)`) was caught in code review and fixed (`MIN_KEY` check added).

**Secret format**: `"whsec_" + base64.b64encode(os.urandom(32)).decode()` generates a valid 256-bit secret.

### Agent-layer classification

**REUSE** — any agent that sends callbacks to user-supplied URLs should sign them with `lancelot.webhook.sign`. Any agent that receives results from async tools via webhook should verify with `lancelot.webhook.verify`. The Standard Webhooks scheme is the correct default (replay protection, key rotation, timestamp freshness).

---

## 11. App (`lancelot/app.py`)

### Key serve/safety seams relevant to agent layer

**`App.lifetime_task(coro_fn)`** (M15 T5): registers a zero-arg coroutine factory to run for the app's lifetime. Lifespan-startup calls `asyncio.ensure_future(fn())`; strong-referenced in `_lifetime_tasks` (no GC). On `drain()` / shutdown: cancelled + awaited bounded by `_drain_timeout` (`LANCELOT_DRAIN_SECS`), suppressing `CancelledError`. Returns `coro_fn` unchanged (decorator pattern). Agent use: heartbeat, metric flusher, queue poller, context refresh loop.

**`App.batch` (`@app.batch`)** (M15 T9 budget knobs):
- `size_fn(items: list) -> int` + `max_size` budget trigger (fires when `size_fn(pending) >= max_size`, complementing `max_batch` count trigger)
- A lone item exceeding `max_size` still processes by itself (correct — no starvation)
- `max_keys` hit → 503 + Retry-After (not 500)
- Key function: `key=lambda item: item.system_prompt` for prefix-aware coalescing to share KV cache

**`const_route` auth-bypass guardrail** (`_audit_const_route_middleware_bypass`): warns (or `LANCELOT_CONST_STRICT=1` → hard error) when `const_route` is registered AND middleware is present. The const tier runs pre-middleware in Rust (zero-GIL), bypassing auth. Relevant for agent endpoints that ALSO have a `/health` const route — health MUST be exempt, but agent endpoints must not accidentally be const-routed.

**`cancel_on_disconnect: bool = False`** (app-level default for unary handlers, M15 T6): routes inherit; per-route `cancel_on_disconnect=` kwarg overrides. Agent endpoints that start long-running LLM calls should set `cancel_on_disconnect=True` (or set it app-wide) so a client disconnect cancels the upstream call via TransportPool's disconnect-cancel path.

**`App(json_max_depth=128)`**: DoS guard on JSON nesting depth. 128 = Robyn's value. Lower for shallow-schema agent tool inputs.

**Native server lifespan bridge** (`_make_lifespan_callbacks`): routes `@app.on_event`, `lifespan=` CM, mounted sub-app lifespans, and `@app.lifetime_task` daemons all through the single `_lifespan` ASGI coroutine via an in-memory asyncio-queue channel. Startup runs BEFORE socket bind (raising startup → refuse to serve, exit non-zero — correct fail-safe). Shutdown runs AFTER connection drain.

**`LANCELOT_LIFESPAN=0`**: escape hatch to disable lifespan (restores old no-op for environments where it breaks).

---

## Cross-cutting: what the byte path DOES and DOES NOT provide for agents

### DOES provide (use as-is)

| Primitive | Where | Agent use |
|---|---|---|
| R3 pump + backpressure | `stream.py` + Rust `src/stream.rs` | Token stream to client |
| `Event` + `sse_adapter` + `_clean_field` | `stream.py` | SSE framing for agent output |
| TTFT histogram | `metrics.py` | AI-streaming UX metric |
| `ToolSpec` + `make_tool_spec` | `tool.py` | Tool registration + validation |
| `app.tool_schemas()` + `app.call_tool()` | `app.py` | Tool list for prompt, tool execution |
| `lancelot.mcp.mount` | `mcp.py` | MCP server for agent tools |
| 4 KB warn discipline | `mcp.py` | Large tool-return detection |
| `assert_safe_url` / `SSRFError` | `net.py` | Fetch/browse tool URL safety |
| `lancelot.secrets.get` + SIGHUP rotate | `secrets.py` | Provider credential access |
| `TenantConcurrencyLimiter` | `tenant.py` | Per-tenant concurrency + queue_wait_ms |
| `RateLimit` | `ratelimit.py` | Inbound token bucket |
| `webhook.sign` / `webhook.verify` | `webhook.py` | Async tool callback signing |
| `app.lifetime_task` | `app.py` | Supervised background daemons |
| `@app.batch` + `size_fn`/`max_size` | `app.py` | LLM batch coalescing + budget |
| `cancel_on_disconnect` | `app.py` | Unary LLM call cancellation on disconnect |
| `cost_usd` ContextVar (+ `X-Lancelot-Cost-USD` trailer) | `context` | Per-request cost accumulation |
| `traceparent` auto-inject | `TransportPool` | Distributed trace across LLM calls |

### DOES NOT provide (agent layer builds these)

| Missing | Byte-path analogy that partly helps |
|---|---|
| Agent loop (ReAct / StopReason / budget / stuck-detect) | `@app.lifetime_task` for daemon, `@app.batch` for coalescing |
| Context window management (ContextBundle / compaction / inspect()) | `lancelot.canonical.dumps_canonical` for cache-stable serialization |
| Model adapter / message-shape translation (OpenAI → Anthropic → Google) | `TransportPool` does transport; `telemetry.cost_event_from_openai_response` parses 3 shapes |
| Tool permission gate (enforce `read_only`/`scopes` on `ToolSpec`) | `ToolSpec.read_only` + `ToolSpec.scopes` fields exist, unenforced |
| Typed memory (4-tier, RRF retrieval) | `lancelot.context` ContextVars are per-request, not persistent |
| Evals (unit / judge panel / define_outcome) | RED + TTFT metrics are operational, not eval-grade |
| Durable seam (event-sourced checkpoint / resume / HITL) | `webhook.sign/verify` is the callback primitive |
| Agent safety (permissions / confidence router / 5-stage hooks) | `TenantConcurrencyLimiter` + `RateLimit` + `SSRFError` are infrastructure-level safety |
| Subagent context isolation | `TransportPool.spawn_child` snapshots traceparent/request_id |

---

## Anti-patterns to avoid

1. **Rebuilding the streaming pump in Python.** The R3 on-loop `_drain` + `try_push` design (one bridge crossing/stream, same-thread push, backpressure event-driven) is what makes streaming fast. Any agent streaming adapter that adds a per-chunk `call_soon_threadsafe` / `into_future` re-creates the 16–26× regression cliff (TIER-D in CLAUDE.md).

2. **Fetching user-supplied URLs without `assert_safe_url`.** The CGNAT range (100.64.0.0/10) and IPv4-mapped IPv6 SSRF vectors are not caught by naive `is_private` checks; `net.py` covers them.

3. **Emitting `str(exc)` in tool error returns.** `mcp.py` does this. For agent tool errors bound for the model, this leaks internal details into the context window. The agent layer should scrub exception text before returning it as tool output.

4. **Deploying the agent server 1-process-many-threads.** The Rust server anti-scales with threads (GIL). The `TenantConcurrencyLimiter` and `RateLimit` per-process buckets are designed for multiprocess scaling. `SO_REUSEPORT` LB is Linux-only.

5. **Putting tool outputs in context verbatim.** The `max_response_bytes=4096` warn threshold in `mcp.py` flags this but doesn't enforce it. The agent layer must truncate / handle-and-fetch for large tool outputs.
