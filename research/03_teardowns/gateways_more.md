# Model Gateways — Mechanism Teardown (Bifrost · Kong AI · Nexos · Orq)

**Sources:** `BIFROST.md` (Go gateway, maximhq/bifrost), `KONG_AI_GATEWAY.md` (Lua/OpenResty plugins), `NEXOS_AI.md` (closed-source hosted, infrastructure fingerprint), `ORQ.md` (Orq.ai hosted platform, Speakeasy SDK + infrastructure probe). Rounds 1+2 for each.

**Focus per brief:** how model gateways relate to `TransportPool`; 4-method Config (get_supported / map_openai_params / transform_request / transform_response, OpenAI canonical); error-typed fallback lists; `require_parameters=true`; cache-sticky routing; DualCache; tool-name sanitization; Retry-After. What does Lancelot already do vs what a thin adapter adds.

---

## §1 — The OpenAI-canonical doctrine and provider adapters

Every gateway in this batch — Bifrost (Go), Kong (Lua), Nexos (closed), Orq (closed) — uses the same structural bet: **clients speak OpenAI; the gateway translates inbound and outbound per provider**. This is not a contested design decision; it is the settled norm.

### 1.1 Bifrost: the strictest per-provider type system

Bifrost's 24 provider packages each implement a `Provider` interface with fully typed Go structs per request/response. Bifrost's adapter is **NOT a ProviderProfile declarative object** — it is a full Go package (~2-5K LOC per provider) with typed struct pairs and complete converters:

- **Interface contract** (verified, `core/providers/`): `to_format` / `from_format` divided by operation type. Not the 4-method Config shape from Hermes; instead a **direct struct→struct converter** gated by operation type (chat, embedding, rerank, audio, image, video, batch, etc.).
- **No OMIT sentinel needed** — Go's `nil` pointer handles the "omit vs zero" distinction natively (optional fields on structs are pointers).
- **Anthropic adapter** (`providers/anthropic/utils.go`, 3,064 LOC): key mechanisms — `MapBifrostEffortToAnthropic(effort)` maps `reasoning_effort: low|medium|high` → Anthropic thinking budget tokens; `ConvertAnthropicFinishReasonToBifrost` maps `end_turn|max_tokens|tool_use|stop_sequence` → canonical; `cache_control: { type: "ephemeral", ttl: "5m"|"1h" }` markers on messages and tools; `system` field extracted separately. (BIFROST §R1.10)
- **Bedrock model quirk handling**: Bedrock-hosted Mistral, Cohere, and Titan don't support `system` role — Kong handles this explicitly in `bedrock_unsupported_system_role_patterns` (regex list, `kong/llm/drivers/bedrock.lua`), rewrites system prompt as a `Human:...` message exchange. Bifrost handles it in the Bedrock provider adapter too. **This class of per-model quirk must be encoded somewhere** — it cannot be papered over by the OpenAI canonical shape. (KONG §R1.10, KONG §R2.11)
- **Claude 1/2 legacy format** (`kong/llm/drivers/anthropic.lua:17-46`): `Human: %s\n\nAssistant:` single-string format still in Kong source for Bedrock Claude 1.x/2.x. Deprecated by Anthropic in late 2024. Any gateway that needs to support old Bedrock endpoints must handle this. Flag as **depreciating hack** — churn on ~1yr model clock.
- **Zero-alloc JSON manipulation** — Bifrost uses `tidwall/sjson` + `tidwall/gjson` to set/delete provider-specific fields (`cache_control`, `extra_body` for OpenAI o-series) without unmarshaling the full request struct. (BIFROST §R1.7, §R2.23). Equivalent in Python: `orjson` field-level manipulation vs full parse.

**Byte-path implication:** Lancelot's `TransportPool` handles transport (auth headers, body bytes, retry, cancellation). It does NOT do message-shape translation (OpenAI → Anthropic block conversion, system extraction, tool schema renaming). The **adapter layer is the missing seam above TransportPool** — it produces the correct wire body and parses the response back to canonical. Hermes's `ProviderProfile` is the right design shape for this; Bifrost's Go packages are the right depth of implementation.

### 1.2 Kong: 4-function contract (verified)

Kong's `kong/llm/drivers/` matches the 4-method Config shape almost exactly (KONG §R2.8):

```lua
transformers_to[route_type]             -- to_format / map_openai_params + transform_request
transformers_from[route_type]           -- transform_response
transformers_from["stream/" .. rt]      -- stream variant
_M.configure_request(conf)              -- set URL/headers
_M.subrequest(body, conf, ...)          -- for transformer LLMs calling an introspection model
```

The `route_type` enum (`llm/v1/chat`, `llm/v1/completions`, `llm/v1/embeddings`, `preserve`) is the **"get_supported" equivalent** — each driver exports only the route_types it handles.

`preserve` mode = passthrough (no body transform). Used for arbitrary endpoints outside the standard enum. Equivalent to Hermes's `api_mode = "passthrough"` in some sense, though simpler. (KONG §R2.6)

**`merge_config_defaults(request, options, request_format)`** (`shared.lua:217`) — overlays `conf.model.options.{max_tokens, temperature, top_p, top_k}` onto the request if not already set. This is the **`require_parameters=true` analogue**: you can pin `max_tokens` at the plugin level so clients can't omit it. (KONG §R2.6, Kong §R1.19)

**`allow_override: true`** — explicit toggle to let clients override the model name per-request. Default false. If the plugin config specifies a model and the request differs → 400 Bad Request. This is the enforcement gate for `require_parameters`. (KONG §R2.6)

---

## §2 — Error-typed fallback lists

### 2.1 Bifrost: two-level error classification with fallback gating

Bifrost's fallback orchestrator (`core/bifrost.go:4501-4586`) gates fallback on error class (BIFROST §R2.8):

- **Retryable → fallback eligible:** rate-limit (429), server-error (5xx), network error.
- **Fatal → no fallback:** bad request (4xx except 429), auth failure, model not found.

`shouldContinueWithFallbacks(fallback, fallbackErr)` — if a fallback itself returns a fatal error, the chain stops immediately rather than trying the next. **Returns the PRIMARY error** on total failure, not the last fallback's error — preserves user-visible root cause. (BIFROST §R2.8)

`retryableStatusCodes = map[int]bool{ 500: true, 502: true, 503: true, 504: true, 429: true }` (BIFROST §R2.7).

**`rateLimitPatterns`** — 22 string patterns for message-based 429 detection:
```go
"rate limit", "rate_limit", "ratelimit", "too many requests",
"quota exceeded", "quota_exceeded", "request limit", "throttled", "throttling",
"rate exceeded", "limit exceeded", "requests per", "rpm exceeded", "tpm exceeded",
"tokens per minute", "requests per minute", "requests per second",
"api rate limit", "usage limit", "concurrent requests limit", "burst_rate", "rate increased"
```
Captures providers that return 200-with-error-body or 500-with-rate-limit-message. Case-insensitive substring check. (BIFROST §R2.7)

**Byte-path implication:** Lancelot's `upstream_metrics` already has `error taxonomy: ok/quota/timeout/permanent/transient/upstream_5xx`. The `rateLimitPatterns` list is exactly what `upstream_metrics` needs for classifying providers that send 200 OK with a rate-limit message. **Add these 22 patterns to the quota detection in `lancelot.upstream_metrics`.** Currently TransportPool retries on 429; it needs message-based 429 detection too.

### 2.2 Bifrost: governance Decision enum (10 codes)

`plugins/governance/resolver.go:20-33`:
```go
DecisionAllow, DecisionVirtualKeyNotFound, DecisionVirtualKeyBlocked,
DecisionRateLimited, DecisionBudgetExceeded, DecisionTokenLimited,
DecisionRequestLimited, DecisionModelBlocked, DecisionProviderBlocked,
DecisionMCPToolBlocked
```
Distinguishes token vs request rate limits separately; VK not-found vs VK blocked; model vs provider vs MCP tool. **9 distinct denial reasons** — much richer than a simple 429. (BIFROST §R2.14)

**Byte-path implication:** When Lancelot adds a governance/budget layer (not yet built per the lens), this enum is the right shape for denial codes — not a flat HTTP status.

### 2.3 Bifrost first-chunk error detection (CRITICAL)

Bifrost peeks at the first SSE chunk for embedded errors (BIFROST §R2.16):
```go
// Anthropic HTTP 200 with event: error\ndata: {"type":"error","error":{"type":"overloaded_error",...}}
checkedStream, drainDone, firstChunkErr := providerUtils.CheckFirstStreamChunkForError(ctx, streamChan)
if firstChunkErr != nil {
    <-drainDone  // don't leak the drained goroutine
    bifrostError = firstChunkErr
}
```
Without this, an Anthropic `overloaded_error` embedded in a 200 SSE stream bypasses retry/fallback. Lancelot's `@app.stream` is a Rust R3 pump — the chunk-peek would need to happen in Python before the pump, or the adapter layer intercepts the stream generator. **Flag: Lancelot's C2 cancellation and fallback logic must handle stream-embedded provider errors.** Currently unhandled. (BIFROST §R2.16)

---

## §3 — Cache-sticky routing and session affinity

### 3.1 Bifrost: KVStore session stickiness

The worker's `keyProvider(usedKeyIDs)` closure — for the **session stickiness** case — always returns the SAME key for a `(model, sessionID)` tuple stored in Redis KVStore. (BIFROST §R1, §R2.9 implied)

Use case: **Anthropic prompt cache continuity**. Same conversation through the same API key → same Anthropic backend node (per Anthropic's cache affinity) → cache warm. Without this, fallback key rotation defeats ephemeral cache hits.

Combined with Bifrost's `cache_control: { type: "ephemeral", ttl: "5m"|"1h" }` markers: Bifrost preserves these markers on messages and tools, and session stickiness ensures the same key sees consecutive turns in a conversation, maximizing Anthropic cache HIT rate. (BIFROST §R1.11)

**Byte-path implication:** Lancelot already has `Response.set_session_affinity(key)` for KV-cache-aware routing. This IS the same primitive described in every teardown — "cache-aware LB, not available out-of-box." Lancelot has it. What Lancelot lacks is the **Anthropic `cache_control` marker injection** at the adapter layer. That marker + session affinity together produce the full cache-continuity story.

### 3.2 Kong: `_KEYBASTION` weak-keyed identity cache

```lua
local _KEYBASTION = setmetatable({}, {
  __mode = "k",   -- weak keys: auto-evicted when config GC'd
  __index = ai_shared.cloud_identity_function,  -- lazy-compute on miss
})
```
Caches GCP service account access tokens, AWS STS sessions per `conf` (the plugin config table). Auto-evicted on config change. `MaxConnDuration: 300s` in Bifrost forces connection recycling every 5 min for the same reason — NAT/LB silent drops on stale connections. (KONG §R2.6, BIFROST §R2.5)

**Byte-path implication:** `lancelot.secrets` + `TransportPool.reload_mtls()` cover key/cert rotation. Cloud identity token refresh (GCP ADC, AWS STS) is not yet in `TransportPool` — legitimate future addition if Vertex/Bedrock become first-class providers.

---

## §4 — `require_parameters=true` (tool-agent critical)

### 4.1 Kong: model lock-down on `normalize-request`

```lua
if request_table.model and request_table.model ~= model_t.name then
  return bail(400, "cannot use own model - must be: " .. model_t.name)
end
```
If the plugin config pins a model and the client specifies a different one → 400 immediately. This is `require_parameters` enforcement at the gateway level. (KONG §R2.6)

### 4.2 Orq: `PolicyRetryConfig` with `onCodes`

```ts
retryConfig?: { count: number, onCodes?: number[] }
```
Simple — retry up to `count` times if HTTP status is in `onCodes`. `onCodes` defaults are inferred to be `[429, 500, 502, 503, 504]`. (ORQ §R1.4)

### 4.3 Tool-aware routing in Nexos Smart Router

Nexos Smart Router explicitly filters out models that don't support the tools requested (NEXOS §R1.4, §R2.14):

> "Tool-aware: the router won't pick a model that doesn't support the tools enabled for that request (e.g., if the agent has GitHub MCP enabled, the router picks a tool-capable model like Claude or GPT, not a tool-less open-source model)."

This requires a **`(model, capabilities)` table** — which models support function calling, vision, audio, web search. This is the `get_supported` function of the 4-method Config: enumerate supported operation types per model so the router can filter ineligible candidates.

**Byte-path implication:** `@app.tool` + `lancelot.mcp.mount` define Lancelot's tool surface. When the model adapter layer is built, it needs `get_supported_operations` per model so tool-requiring deployments don't route to incapable models.

---

## §5 — DualCache: semantic cache

### 5.1 Bifrost: direct-hash + embedding cosine, both modes

`plugins/semanticcache/main.go` (OSS, NOT enterprise-only — Round 1 correction):

```go
Config struct {
    Provider       schemas.ModelProvider   // embedding provider
    EmbeddingModel string                  // e.g. text-embedding-3-small
    TTL            time.Duration           // default 5min
    Threshold      float64                 // cosine threshold, DEFAULT 0.8 (NOT 0.95)
    Dimension      int                     // >0 = semantic; ==1 = direct-hash-only
    ConversationHistoryThreshold int       // default 3 — skip for convs with >3 messages
    CacheByModel   *bool                   // default true
    CacheByProvider *bool                  // default true
    ExcludeSystemPrompt *bool              // default false
}
```

**Two modes:**
- **Semantic** (`Dimension > 0`, `Provider` set): embed the prompt → cosine ANN search → HIT if similarity ≥ threshold.
- **Direct-only** (`Dimension = 1`, `Provider = ""`): only exact-hash match (xxhash). Zero embedding cost.

**`ConversationHistoryThreshold: 3`** — skip caching for conversations with >3 messages. Long multi-turn conversations have basically unique state — caching is pointless overhead. This is the non-obvious constant that prevents semantic cache from degrading for agents. (BIFROST §R2.13)

**Threshold 0.8, not 0.95** — the Round-1 0.95 was wrong. 0.8 is much looser; false-positive cache hits are real at this threshold. For agents where a subtly different prompt could generate a meaningfully different response, 0.8 is risky. Tune up.

**Byte-path implication:** `lancelot.canonical.dumps_canonical` + `lancelot.batch` are already the cache-stability and coalescing primitives. A semantic cache plugin above TransportPool would use `dumps_canonical` for exact-match and an embedding call for semantic-match. Key design: TTL + model-aware cache key + conversation-length guard. `ConversationHistoryThreshold: 3` is the constant to adopt.

### 5.2 Kong Enterprise: semantic cache metrics seam

Kong's OSS observability layer pre-declares the cache metrics fields even in the OSS release (`kong/llm/drivers/shared.lua:30-60`):
```lua
CACHE_STATUS, VECTOR_DB, EMBEDDINGS_PROVIDER, EMBEDDINGS_MODEL
```
So the enterprise `ai-semantic-cache` plugin just fills fields already in the log schema — zero log-format churn. This is the right design for optional features: **declare the metric fields at framework level; fill them only when the optional plugin runs**.

**Byte-path implication:** Lancelot's RED metrics already have `TTFT`, `queue_wait_ms`, `cost_usd`. Adding `cache_status: HIT|MISS`, `cache_backend` fields to the metric schema now (even unfilled) costs nothing and makes future semantic cache non-disruptive.

---

## §6 — Tool-name sanitization

Neither Bifrost, Kong, Nexos, nor Orq explicitly document tool-name sanitization in the sources inspected. The Hermes teardown (`hermes_gateway.md`) covers the `get_tool_name_sanitizer` hook. This cluster adds:

**Bifrost governance:** `DecisionMCPToolBlocked` — one of the 10 governance decision codes. Implies MCP tool calls can be blocked by the governance plugin (allow/deny list of tool names). This is an authorization layer on top of tool-name pass-through, not sanitization — but an adjacent primitive. (BIFROST §R2.14)

**Orq: `EvaluatorRef.isGuardrail=true` on tool call inputs** — Orq's evaluators can be gated on `executeOn: "input"` to intercept a tool call before execution, giving a block/allow verdict. This is functionally tool-call guardrailing. (ORQ §R1.6)

**Byte-path implication:** `lancelot.mcp.mount` already emits a WARN when tool output exceeds 4KB (minimal-return discipline). Tool-name sanitization (stripping special chars for providers that reject non-alphanumeric names) and tool-call blocking belong in the adapter layer, not TransportPool.

---

## §7 — Retry-After honoring and backoff

### 7.1 Bifrost: ±20% jitter, NOT Portkey's `randomize: false`

```go
backoff := min(RetryBackoffInitial * (1 << attempt), RetryBackoffMax)  // exp, initial 500ms, max 5s
jitter := backoff * (0.8 + 0.4*rand.Float64())   // ±20% jitter
```
Attempts 0,1,2,3,4 → sleep 0, 500ms, 1s, 2s, 4s (each ±20%). Go 1.20+ `rand.Intn` is thread-safe, zero-syscall. (BIFROST §R2.7)

`DefaultMaxRetries = 0` — **retries are OFF by default**. Operator must opt in. (BIFROST §R2.3)

**Key rotation on rate-limit:** key marked `usedKeyIDs[key.ID] = true` ONLY on rate-limit errors. Network errors (whole provider endpoint is bad) keep the key in the pool — rotating keys doesn't help when it's a network issue. (BIFROST §R2.7)

**Byte-path implication:** Lancelot's `TransportPool` already honors `Retry-After` and does exponential backoff with the token-bucket `rate_limit`. The ±20% jitter should be verified in `TransportPool`; Bifrost's exact constants (500ms initial, 5s max) are a good baseline.

### 7.2 Bifrost: `MaxConnDuration: 300s` forced recycling

```go
DefaultMaxConnDurationInSeconds = 300  // 5 min: force connection recycling to prevent NAT/LB silent drops
```
Even with TCP keepalive, NAT boxes silently drop idle TCP connections after a few minutes. Bifrost caps total connection lifetime regardless of keepalive. (BIFROST §R2.5)

**Separate streaming client:** zero `ReadTimeout`, zero `MaxConnDuration` — long SSE streams never get killed. (BIFROST §R2.5)

**TCP keepalive:** `Idle: 10s, Interval: 5s, Count: 3` → dead conn detected in ≤25s, before `MaxIdleConnDuration: 30s` would reuse it. `RetryIfErr = network.StaleConnectionRetryIfErr` — fasthttp auto-retries stale-conn races. (BIFROST §R2.6)

**Byte-path implication:** Lancelot's Rust HTTP/1.1 server (hyper/tokio) and `TransportPool`'s outbound client share the same concern. `TransportPool`'s `idle_guarded_chunks` and idle-guard already address the streaming idle case. **Verify that `TransportPool` outbound connections have an explicit `MaxConnDuration` equivalent (not just idle timeout)**; a 5-min absolute lifetime cap prevents NAT drops. Currently not confirmed in TransportPool source.

---

## §8 — Orq: Deployment-as-atomic-unit and Policy resource

### 8.1 Policy as a separate resource (not buried in a prompt)

```ts
Policy = {
  modelsConfig: { mode: "fallback"|"weighted"|"round_robin", models: ModelRef[] },
  retryConfig: { count, onCodes? },
  limits: { budget?, requests?, tokens? },
  evaluators: EvaluatorRef[],
  timeout: number,
}
```
One policy can be attached to many deployments. Audit/governance teams manage policies independent of prompt engineering. (ORQ §R1.3)

`ModelsConfig.mode` offers three routing strategies: fallback, weighted, round_robin. Combined with `RoutingRules` (conditional routing on request metadata) and per-policy `Limits`. **This is the cleanest expression of the routing layer I've seen** — all three dimensions (which-strategy, retry-config, resource-limits) in one resource.

**Byte-path implication:** Lancelot's `@app.batch` + `TransportPool.fanout` cover coalescing and fan-out. What Lancelot lacks is the **policy-as-a-resource** pattern: a named, reusable object that bundles `(modelsConfig + retryConfig + limits + evaluators)` and can be attached to routes. This is a clean Lancelot primitive: `@app.route('/chat', policy='enterprise-policy')`.

### 8.2 Orq: JWT-based API keys

Auth is `Authorization: Bearer <JWT>` with `iss=orq.ai`, `bearerFormat: "JWT"` in `openapi.yaml`. Three-stage validation: (1) JWT format check, (2) issuer check, (3) signature/expiry. O(1) auth check — workspace/project claims in the token, no DB lookup per request. (ORQ §R2.2)

**Byte-path implication:** `lancelot.context` carries `tenant_id`, `agent_id`, `request_id`. A JWT-based API key scheme can embed all of these in the token, making the ASGI seam's context extraction O(1). Not a TransportPool concern — an auth middleware concern. Flag for when Lancelot needs multi-tenant auth.

---

## §9 — Kong: the 8-stage filter pipeline

```lua
STAGES = {
  SETUP=0, REQ_INTROSPECTION=1, REQ_TRANSFORMATION=2, REQ_POST_PROCESSING=3,
  RES_INTROSPECTION=4, RES_TRANSFORMATION=5,
  STREAMING=6,       -- REPEATED_PHASES[6]=true (runs per chunk)
  RES_PRE_PROCESSING=7, RES_POST_PROCESSING=8
}
```
Filters are globally registered, deduplicated per-request via `ai_executed_filters` set. Multiple plugins (ai-prompt-guard + ai-proxy + ai-prompt-decorator) sharing `parse-request` only run it once. (KONG §R2.2, §R2.13)

Plugin priority: `ai-prompt-guard (771) > ai-proxy (770)`. Guard runs BEFORE the proxy decides to call the LLM — denied prompts never hit the provider. (KONG §R2.4)

**Regex guard: obscures itself.** Returns `"bad request"` not which pattern matched. `ngx.re.find(str, pattern, "jo")` — `j` = JIT-compile, `o` = compile-once-cache. Regex syntax error → 500 (admin error). Pattern match → 400 with obscured body. (KONG §R2.5)

**`ai-request-transformer`** — synchronous LLM call mid-pipeline to rewrite the request body. System prompt + user prompt = original request; the LLM's response is the new request body. `response_regex_match` strips LLM preamble. Adds 200-1500ms per request — use sparingly. (KONG §R1.17, §R2.12)

**Byte-path implication:** Lancelot's `@app.middleware("http")` is the equivalent extensibility hook. The 8-stage pattern is more granular but overkill for Lancelot's scope. What IS worth copying: (1) the deduplication of shared filters via a per-request executed-filter set, and (2) **guard runs before provider call** — Lancelot's `@app.batch` and MCP mount already have hook points; a guardrail hook should be in `REQ_TRANSFORMATION` stage, before the outbound call.

---

## §10 — Kong's TPOT metric

`llm_tpot_latency` = time per output token, auto-derived as `llm_e2e_latency / llm_completion_tokens_count`. Metric registration system requires `metrics_register(key)` before use — catches typos at runtime, makes metrics discoverable. (KONG §R2.9)

**Byte-path implication:** Lancelot already has `lancelot.http.stream.ttft` (time-to-first-chunk). TPOT = `(e2e_latency - ttft) / completion_tokens`. It can be derived from existing metrics without new instrumentation. Worth exposing for agent-hosting deployments where per-token latency matters.

---

## §11 — Nexos: closed-source signals

Despite being fully closed-source, Nexos's infrastructure fingerprint yields mechanism-level findings:

- **Auth stack: ORY Hydra + ORY Kratos** (self-hosted, Go, GDPR-friendly). API keys are JWTs minted by Hydra with `iss=nexos.ai`. `hydra.nexos.ai`, `kratos.nexos.ai`, `kratos-selfservice-ui.nexos.ai` confirmed via `crt.sh`. (NEXOS §R2.2)
- **Object storage: Cloudflare R2** (`d-r2.nexos.ai`). Zero egress fees — correct cost choice for LLM gateway (large response bodies, many reads per upload). (NEXOS §R2.3)
- **Region-aware routing: `region-router.nexos.ai`** subdomain confirms multi-region steering. EU primary, US secondary. (NEXOS §R2.1)
- **Stream timeout: 300,000ms (5 minutes)** — default timeout per model, not per endpoint. Long default supports o-series / extended thinking models. Configurable per model. (NEXOS §R1.6)
- **Guardrail priority: Excluded → Custom → Company** — three-tier. Exclusions override custom, custom overrides company defaults. Clear policy hierarchy for enterprise layered governance. (NEXOS §R1.6)

**Byte-path implication:** `lancelot.secrets` + multi-region region routing (`@app.get('/x', region=...)`) are already built. The 5-minute stream timeout is relevant: Lancelot's `@app.stream` timeout is `LANCELOT_DRAIN_SECS` (10s default for graceful drain) — but **per-stream timeout (not drain) needs to be separately configurable for long-running reasoning models**. Flag for the model adapter layer.

---

## §12 — Honest assessment: what Lancelot already has vs what's missing

### Already have (do NOT rebuild)
- **Transport**: `TransportPool.request/stream/fanout/spawn_child` + `Retry-After` + token-bucket rate-limit + mTLS + `traceparent` propagation.
- **Error taxonomy**: `lancelot.upstream_metrics` with `ok/quota/timeout/permanent/transient/upstream_5xx`.
- **Session affinity**: `Response.set_session_affinity(key)` — the cache-sticky routing primitive.
- **KV-cache prefix stability**: `lancelot.canonical.dumps_canonical`.
- **Streaming pump**: Rust R3 pump with backpressure, TTFT, disconnect→cancel.
- **Cost/trace context**: `lancelot.context` ContextVars.
- **Secrets rotation**: `lancelot.secrets` with SIGHUP.
- **MCP mount**: `lancelot.mcp.mount` with minimal-return discipline.
- **Tool surface**: `@app.tool` + auto-schema.

### The thin adapter that sits ABOVE TransportPool (not yet built — the focus)
1. **ProviderProfile / 4-method Config** — `(get_supported, map_openai_params, transform_request, transform_response)`. Each provider is a declared adapter object, not scattered `if provider == X` branches. Hermes's `ProviderProfile` dataclass is the right pattern. ~200 LOC per provider for the top 5 (OpenAI passthrough, Anthropic messages→blocks+cache_control, Bedrock SigV4+EventStream, Vertex GCP-token, Azure URL).
2. **`OMIT_TEMPERATURE` sentinel** — three-state `fixed_temperature: None | OMIT | float`. Copy from Hermes exactly. Kimi-class providers that error on any client-supplied temperature need this.
3. **Bedrock system-role quirk** — `bedrock_unsupported_system_role_patterns` regex list for models that don't accept `system` role. Encode as a profile flag or per-model list.
4. **Anthropic `cache_control` marker injection** — add `cache_control: { type: "ephemeral", ttl: "5m"|"1h" }` on messages and tools for cache-continuity. Session affinity is already there; this adds the provider-side marker.
5. **Stream-embedded error peek** — `CheckFirstStreamChunkForError` pattern (Bifrost §R2.16). Retry/fallback logic must handle Anthropic's 200-OK-with-error-SSE. Add a first-chunk interceptor in the adapter's stream wrapper.
6. **Message-based 429 detection** — 22 `rateLimitPatterns` from Bifrost §R2.7. Feed into `upstream_metrics` quota classification.
7. **`ConversationHistoryThreshold: 3`** for semantic cache skip. When Lancelot adds semantic cache, skip caching for conversations with >3 messages.
8. **Per-stream timeout** (5-min for reasoning models) — distinct from drain timeout. Configurable per-model in the provider profile.

### Anti-patterns to refuse
- **Build Bifrost's 1000-worker-per-provider queue model in Python** — Go channels + goroutines don't translate. Lancelot's backpressure is Rust-side + asyncio; per-provider worker pools would be Python threads (GIL, anti-scales). Bifrost's architecture is a Go architecture.
- **Claim semantic cache threshold 0.95** — Bifrost's actual default is **0.8**. 0.95 was Round-1 misinformation. Don't hardcode 0.95 into Lancelot's cache design.
- **Claude 1/2 legacy prompt format** (`Human: ... Assistant:`) — depreciating hack, off-thesis. Don't implement for Lancelot's provider profiles. Only needed for Bedrock Claude 1.x/2.x endpoints that no new product should be targeting.
- **LLM-as-request-transformer** (Kong `ai-request-transformer`) — adds 200-1500ms per request. Not a byte-path win; explicitly Tier-D per the cardinal rule "no new logic that doesn't change the byte path."
- **Brotli compression** — Tier-D per CLAUDE.md. The proxy's job.

---

*Note path: `/Users/seventyleven/Desktop/Lancelot/research/03_teardowns/gateways_more.md`*
