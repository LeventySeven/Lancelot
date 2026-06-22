# LLM Gateway Routing — Mechanism-Level Distillation

**Sources:** `teardowns/LITELLM.md` (LiteLLM 1.85.0, Python, 592K LOC), `teardowns/PORTKEY.md` (Portkey gateway 1.15.2, TypeScript/Hono, 51K LOC), `teardowns/OPENROUTER.md` (OpenRouter API + multi-agent supplement, April 2026)

---

## What this cluster is

Three production LLM gateways. All three share the same fundamental bet: **OpenAI format is the lingua franca, route everything through one endpoint, normalize responses out.** The differences are in routing expressiveness, guardrail depth, and deployment targets.

**Lancelot lens:** the byte-path (TransportPool, upstream_metrics, streaming) already covers what these gateways do at the *transport* layer. What they add on top — provider-shape translation, routing strategies, guardrail hooks, cost tracking — is the **model adapter / translation layer** that Lancelot's `00_LENS.md` explicitly names as a missing piece.

---

## 1. Provider Shape Translation — The Load-Bearing Mechanism

### The canonical pattern (LiteLLM + Portkey, both verified from source)

Every provider gets a `Config` class with exactly four methods:

```python
# LiteLLM — llms/anthropic/chat/transformation.py (2,600 LOC), verified
class AnthropicConfig:
    def get_supported_openai_params(self, model) -> list[str]:
        # Returns subset of OpenAI params this model accepts
        # e.g. Anthropic drops: n, frequency_penalty, presence_penalty, seed, logprobs
        # Anthropic adds: thinking, reasoning_effort, cache_control, context_management

    def map_openai_params(self, non_default_params, optional_params, model, drop_params):
        # OpenAI param name → provider-native name/shape
        # e.g. stop → stop_sequences, user → metadata.user_id (dotted-path target!)
        # tool_choice: "required" → {type: "any"}, "auto" → {type: "auto"}, "none" → {type: "none"}
        # reasoning_effort: "high" → thinking: {type: "enabled", budget_tokens: 16000}

    def transform_request(self, model, messages, optional_params, litellm_params, headers) -> dict:
        # Build final HTTP body
        # Non-obvious: must inject a dummy tool when messages contain tool_call blocks
        # but no tools are in optional_params — otherwise Anthropic 400s
        # Must drop thinking param if last assistant message has tool_calls but no thinking_blocks
        # Tool names sanitized to ^[a-zA-Z0-9_-]{1,128}$ with forward+reverse map

    def transform_response(self, ...) -> ModelResponse:
        # Parse provider response → OpenAI shape
```

**Portkey does the same thing in TypeScript** (`src/providers/<name>/chatComplete.ts`). Per-provider config declares each OpenAI param with `{ param, default, min, max, transform }`. The transform fn converts inline. Anthropic's `ProviderConfig` in Portkey (`src/providers/anthropic/chatComplete.ts:268`, 832 LOC total) is the best single-file reference for every Anthropic-specific quirk.

### Critical Anthropic-specific transforms (cited: LITELLM.md §A.2, PORTKEY.md §R2.8)

1. **System messages** must be extracted from `messages[]` and placed at top-level `system:` field. Both implementations do this.
2. **Tool names regex** `^[a-zA-Z0-9_-]{1,128}$` — any `/` or `.` in tool names must be mapped to `_` on the way out, and reverse-mapped on the way back. The forward map is applied to outgoing tool defs AND message tool_use/tool_result blocks. Reverse map stashed in litellm_params and applied on response. (LiteLLM source: `anthropic/chat/transformation.py:1850-1880`.)
3. **Dummy tool injection** — if messages contain `tool_use` blocks but `tools` is not in the request, inject `add_dummy_tool(custom_llm_provider="anthropic")`. Otherwise Anthropic 400s with "tools field required when messages contain tool_use." (LiteLLM source: `transformation.py:1800-1810`.)
4. **`thinking` auto-drop** — if `thinking` is set in params but the last assistant message with tool_calls has NO thinking_blocks, drop `thinking`. Not dropping it causes an Anthropic error "Expected thinking or redacted_thinking, but found tool_use." (LiteLLM source: `transformation.py:1810-1825`.)
5. **Advisor-block strip** — strip `advisor` content blocks from message history if the advisor tool is not currently in the tools list. (LiteLLM source: `transform_request` lines 315-321.)
6. **`user: "u_42"` → `metadata.user_id: "u_42"`** — the Anthropic config writes to a dotted-path in the body (`param: 'metadata.user_id'`). (Portkey source: `AnthropicChatCompleteConfig:629`.)
7. **temperature** is bounded 0..1 for Anthropic, not 0..2. Any OpenAI value >1 gets coerced. (Portkey source: `chatComplete.ts:624`.)
8. **`reasoning_effort` → `thinking.budget_tokens`** mapping (LiteLLM `REASONING_EFFORT_TO_BUDGET` dict, verified):
   - `"none"` → 0 (disable thinking), `"minimal"` → 1024, `"low"` → 4000, `"medium"` → 8000, `"high"` → 16000, `"xhigh"` → 32000, `"default"` → 4000. (LiteLLM source: `transformation.py:1133-1142`.)
9. **`tool_choice` mapping**: `"required"` → `{type:"any"}`, `"auto"` → `{type:"auto"}`, `"none"` → `{type:"none"}`, `{type:"function",function:{name:"X"}}` → `{type:"tool",name:"X"}`. (LiteLLM source: `transformation.py:1120-1127`; Portkey source: `chatComplete.ts:416-434`.)
10. **`content_blocks` passthrough** — Portkey adds `!strictOpenAiCompliance && { content_blocks: response.content.filter(i => i.type !== 'tool_use') }` to the response. Anthropic-aware clients can read the raw blocks (thinking blocks, etc.); strict OpenAI clients ignore the extra field. (Portkey source: `§R2.9`, lines 706-728.)

### Anthropic streaming state (cited: PORTKEY.md §R2.10)

Anthropic SSE events are inhomogeneous: `message_start`, `content_block_start`, `content_block_delta`, `content_block_stop`, `message_delta`, `message_stop`. OpenAI wants homogeneous `chat.completion.chunk` with `delta`. The gateway needs `AnthropicStreamState` carrying:
- `toolIndex: number` — running index into `tool_calls[]` as `content_block_start` events open new `tool_use` blocks (without this, clients can't stitch argument deltas)
- `model: string` — Anthropic only sends model on `message_start`; every chunk needs it
- `usage: { prompt_tokens, cache_creation_input_tokens?, cache_read_input_tokens? }` — accumulated at `message_start`, used at `message_delta` to compute final `total_tokens`

The cache-token-aware `total_tokens` on the final usage chunk: `prompt_tokens + cache_creation_input_tokens + cache_read_input_tokens + output_tokens`. (Portkey source: `§R2.10`, lines 763-770.) Without this, clients computing cost from streaming miss the cache tokens.

### Gemini-specific transforms (cited: LITELLM.md §A.3)

- `messages[role=system]` → top-level `systemInstruction: {parts: [{text}]}` (not part of `contents`)
- `role: "assistant"` → `role: "model"` in `contents[]`
- `role: "tool"` → `role: "user"` with `content: [{functionResponse: {...}}]`
- `tools` → `[{functionDeclarations: [{name, description, parameters}]}]`
- `tool_choice: "required"` → `toolConfig.functionCallingConfig.mode: "ANY"`
- `web_search_options: {}` → `tools: [{googleSearch: {}}]`
- `service_tier: "auto"` → Gemini `serviceTier: "priority"` (special-case, not trivial)
- `response_format: {type: "json_schema"}` → `generationConfig.responseMimeType: "application/json" + responseSchema`

### Bedrock-specific (cited: LITELLM.md §A.4)

Bedrock Converse API nests `temperature/top_p/max_tokens/stop` under `inferenceConfig`. Tools go under `toolConfig.tools[].toolSpec`. Bedrock is the only adapter that requires **AWS SigV4 signing per request** (crypto, not just Bearer token).

---

## 2. Routing Strategies — The Exact Algorithms

### LiteLLM Router (Python, `router.py` 10,897 LOC, cited: LITELLM.md §B)

Ten+ strategies, all backed by a shared `DualCache` (in-memory + Redis with different TTLs):

| Strategy | Algorithm | State | Note |
|---|---|---|---|
| `simple-shuffle` | Weighted random by `weight`/`rpm`/`tpm` (first non-null) | None (stateless) | Footgun: checks `healthy_deployments[0]` only to detect which field to use |
| `least-busy` | Lowest in-flight count | `DualCache: {group}_request_count` | Not atomic — read-modify-write race under concurrency |
| `lowest-latency` | Lowest avg seconds/completion-token over last 10 | `DualCache: {group}_map[id].latency[]` | TTFT for streaming; normalized by completion_tokens |
| `usage-based-routing-v2` | Lowest TPM/RPM ratio vs configured limit; Redis `INCRBY` atomic | Redis: `{group}:{id}:{YYYY-MM-DD-HH-mm}:rpm` | Production-grade; v1 has the same race as least-busy |
| `cost-based-routing` | Lowest cumulative cost in window | DualCache | Minute-bucketed |
| `tag-based-routing` | First deployment with overlapping `metadata.tags` | Stateless | Useful for env/region routing |

**Key constant**: `DEFAULT_FAILURE_THRESHOLD_PERCENT = 0.5` (50% failure rate triggers cooldown), minimum `DEFAULT_FAILURE_THRESHOLD_MINIMUM_REQUESTS = 5` before cooldown can fire. If the model group has only 1 deployment, cooldown is suppressed unless `SINGLE_DEPLOYMENT_TRAFFIC_FAILURE_THRESHOLD = 1000` requests have been seen. (LiteLLM source: `constants.py` + `cooldown_handlers.py`, `§B.7`.)

**Fallback types** (separate lists, not one list):
- `fallbacks` — general errors
- `context_window_fallbacks` — specifically for `ContextWindowExceededError`
- `content_policy_fallbacks` — for content moderation errors
- `ROUTER_MAX_FALLBACKS = 5` (from `os.getenv("ROUTER_MAX_FALLBACKS", 5)`)

**TTFT normalization** (cited: `§B.5`): latency is stored as seconds-per-completion-token, not total latency. This way a deployment producing 1000 tokens in 5s scores the same as one producing 100 tokens in 1s — you're measuring token throughput, not wall-clock total.

### Portkey recursive routing (TypeScript, `handlerUtils.ts:476-834`, ~360 LOC, cited: PORTKEY.md §R2.3)

The entire routing engine is `tryTargetsRecursively`. Four strategy modes:
- **FALLBACK** — iterate targets in order; continue to next if response status is in `onStatusCodes` OR (no list + not `.ok`); abort if `x-portkey-gateway-exception: true` (gateway bug, don't loop on our own errors)
- **LOADBALANCE** — `Math.random() * totalWeight`, walk targets subtracting weights until hit. Pure per-request RNG, no state. Converges only at volume.
- **CONDITIONAL** — MongoDB-style query over `{ metadata, params, url }` with `$eq/$ne/$gt/$gte/$lt/$lte/$in/$nin/$regex/$and/$or`. Context is **2 levels deep only** (`metadata.tier` works, `metadata.user.tier` does NOT). (Portkey source: `§R2.4`, `conditionalRouter.ts:156 LOC`.)
- **SINGLE** — wrap one target in inherited config shell

**Nested strategies** — a config can be `fallback-of-(loadbalance-of-(conditional-of-(...)))`. The `targets` field is `z.array(z.lazy(() => configSchema))` (recursive Zod schema). (Portkey source: `§R2.17`.) This is the main differentiator vs LiteLLM's flat router.

**Config inheritance** — child target overrides parent for `retry`/`cache`; `overrideParams` merges field-by-field (child wins per-field). `x-portkey-gateway-exception: true` response header is the signal "this was our fault, don't fall back through it." (Source: `§R2.3` step 7.)

**`randomize: false`** on `async-retry` — no jitter. Retries are deterministic. Under high concurrency with same `retry-after`, retries pile up synchronously at the same moment. (Source: `§R2.11`.) Flag this when using at scale.

### OpenRouter routing (server-side closed, documented via API, cited: OPENROUTER.md §3)

Default load balancer (3 steps):
1. Filter providers with recent outages (last 30s window) — move to end, don't exclude
2. Inverse-square price weighting among remaining: `probability ∝ 1/price²`. A $1/M provider gets **9x** more traffic than a $3/M provider.
3. Remaining as ordered fallbacks

Override via `provider` object: `order` (explicit provider slug list), `sort: "price"|"throughput"|"latency"`, `sort: {by, partition}` (partition:none removes model-grouping and ranks globally), `preferred_min_throughput`/`preferred_max_latency` (percentile thresholds, 5-min window, SOFT — never blocks the request), `max_price` (HARD — returns 503 if no provider meets it), `data_collection: "deny"`, `zdr: true`, `require_parameters: true`, `quantizations`.

**Auto Exacto** — implicit on any request with `tools`: reorders provider candidates by real-production tool-calling success rate. Explicit `sort` overrides it. (Source: `§6`.)

**Prompt cache sticky routing** — after a cache-breakpoint request, subsequent requests for the same model are pinned to the same provider to keep the KV-cache warm. This overrides the price-weighted LB for session duration. (Source: `§9`.)

**Fallback `models` array** — top-level `models: ["primary", "fallback1", "fallback2"]`. Triggers on: context exceeded, content moderation refusal, 429, 502/503, any provider error. No documented hop limit. Billing uses the model that actually ran. (Source: `§5`.)

---

## 3. Cost Tracking — The Canonical Pricing Layer

### LiteLLM `model_prices_and_context_window.json` (cited: LITELLM.md §4, §A.2)

40,076 lines, 2,303 model entries. This file is the **de facto pricing source for the whole industry** — LangChain, OpenRouter's calculator, and others mirror it. Schema (selected critical fields):

```json
{
  "gpt-4o": {
    "input_cost_per_token": 2.5e-6,
    "output_cost_per_token": 1e-5,
    "cache_read_input_token_cost": 1.25e-6,
    "cache_read_input_token_cost_priority": 2.125e-6,
    "input_cost_per_token_batches": 1.25e-6,
    "output_cost_per_reasoning_token": ...,
    "supports_function_calling": true,
    "supports_prompt_caching": true,
    "supports_reasoning": false,
    "max_input_tokens": 128000,
    "max_output_tokens": 16384,
    "mode": "chat"
  }
}
```

**Critical: a model not in the JSON returns cost=0 silently.** The map lags new model releases. (Source: `LITELLM.md` Honest gaps.)

**Cost calculator** (`cost_calculator.py`) reads this map and computes: `prompt_tokens × input_cost_per_token + completion_tokens × output_cost_per_token + cache_read_input_tokens × cache_read_input_token_cost + reasoning_tokens × output_cost_per_reasoning_token`.

**Model-group cache-key collapse** — when routing via LiteLLM Router with `caching_groups`, the cache key uses the caching group name (not the actual deployment model). Two Azure GPT-4 deployments share cache hits. (Source: `§3 caching/caching.py:276`.)

### OpenRouter per-request cost (cited: OPENROUTER.md §6, cost formula)

```python
def estimate_cost(model_obj, prompt_tokens, completion_tokens, cached_tokens=0, web_search_calls=0):
    p = model_obj["pricing"]
    non_cached = prompt_tokens - cached_tokens
    return (non_cached * float(p["prompt"])) + \
           (cached_tokens * float(p.get("input_cache_read", p["prompt"]))) + \
           (completion_tokens * float(p["completion"])) + \
           (web_search_calls * float(p.get("web_search", 0)))
```

Retrieved from `GET /api/v1/generation?id={genId}` after the fact. For multi-agent systems: tag each call with `trace.agent_role` and aggregate in Langfuse/Braintrust. (Source: `§6 cost tracking`.)

---

## 4. Cache Key Discipline — The Non-Obvious Invariant

### LiteLLM cache key generation (cited: LITELLM.md §3 caching)

Cache key = hash of ALL OpenAI-API params (model + messages + temperature + top_p + max_tokens + tools + tool_choice + response_format + seed). Three clever behaviors:
1. **`preset_cache_key` stash** — computed once and stashed in `litellm_params` to avoid re-hashing after provider-side param renames (e.g. Bedrock translates `max_tokens` → `max_new_tokens` — without the stash, the hash differs and you get a miss)
2. **Model-group collapse** — uses `caching_group` name if present so identical requests to different deployments hit the same cache entry
3. **File-based cache keys for transcription** use file checksum, not file bytes

### Portkey cache key (cited: PORTKEY.md §R2.12)

`SHA-256(JSON.stringify(requestBody) + '-' + url)`. Plain JS object (not a Map — no LRU, no size cap, runs to OOM on long-lived Node unless Redis is wired via `REDIS_CONNECTION_STRING` env). Streams are never cached (`requestBody.stream === true` → skip put). No canonicalization — a 1-token difference causes a miss. Cache TTL default: 24h. (Source: `§R2.12`.)

### OpenRouter cache (cited: OPENROUTER.md §9)

Prompt caching via `cache_control: {type: "ephemeral", ttl: "5m"|"1h"}` on message content parts. 1-hour TTL costs 2x write price on Anthropic. OpenRouter pins subsequent requests to the same provider to keep KV-cache warm (overrides price-weighted LB). Minimum token requirements: OpenAI 1024, Anthropic 1024-4096 (model-dependent).

---

## 5. Retry / Fallback — The Exact Constants

### Portkey retry (cited: PORTKEY.md §R2.11, `retryHandler.ts:220 LOC`)

```
MAX_RETRY_LIMIT_MS = 60_000      # total retry budget
MAX_RETRIES = 5
RETRY_STATUS_CODES = [429, 500, 502, 503, 504]
POSSIBLE_RETRY_STATUS_HEADERS = ['retry-after-ms', 'x-ms-retry-after-ms', 'retry-after']
```

On 429 with `followProviderRetry: true`:
- Parse `retry-after` header (seconds → ms, or direct ms for `retry-after-ms`)
- If `retryAfter >= MAX_RETRY_LIMIT_MS` OR exceeds remaining budget → skip remaining retries, bail
- Else zero out `async-retry`'s backoff queue (`rateLimiter._timeouts = Array.from(...)`.map(() => 0)`) and sleep exactly `retryAfter` ms before throwing for the next attempt

**`randomize: false`** — no jitter. Can thundering-herd under load. (Source: `§R2.11`.)

AbortController timeout → synthetic 408 response with `type: "timeout_error"`. ConnectTimeoutError → 503.

### LiteLLM cooldown (cited: LITELLM.md §B.7)

Cooldown triggers on: 429, 401, 408, 404, all 5xx. Does NOT cooldown on: 400, 403, 422 (our fault). Duration default: `DEFAULT_COOLDOWN_TIME_SECONDS = 5`. Threshold: 50% failure rate (`DEFAULT_FAILURE_THRESHOLD_PERCENT = 0.5`) with minimum 5 requests before cooldown can fire. Single-deployment suppression: 1000 requests minimum before the only deployment can be cooled down.

---

## 6. Mid-Stream Error Handling (cited: OPENROUTER.md §7, PORTKEY.md §R2.23)

Once streaming starts, HTTP status is already 200. Both OpenRouter and Portkey embed errors IN the stream:

**OpenRouter format:**
```json
{"choices": [{"delta":{"content":""}, "finish_reason":"error"}], "error": {"code": ..., "message": ...}}
```
Detection: check `chunk["error"]` OR `choice["finish_reason"] == "error"`.

**Portkey's stream limitation:** output guardrails run AFTER the full stream completes. If a guardrail fires DENY, the client has already received the chunks — the deny is advisory, status code cannot be changed retroactively. (Source: `§R2.23`.)

**OpenRouter cancellation asymmetry** — disconnect stops processing for OpenAI/Anthropic/Fireworks but does NOT stop processing (and billing continues) for AWS Bedrock/Groq/Google. (Source: `§7`.)

---

## 7. SSRF Protection (cited: PORTKEY.md §R2.16)

Portkey's `requestValidator` is the most thorough SSRF guard in the public OSS gateway space. Why it matters: the gateway accepts arbitrary `custom_host` per request — a one-line SSRF without validation.

Blocked:
- Schemes: `file://, data:, gopher:, ftp://, ftps://`
- Cloud metadata endpoints: `169.254.169.254` (AWS/Azure/GCP IMDS), `metadata.google.internal`, `metadata.azure.com`, `instance-data`
- Private IPv4 ranges (integer comparison, not string): 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 127.0.0.0/8, 169.254.0.0/16, 100.64.0.0/10, multicast/reserved
- TLDs: `.local, .localdomain, .internal, .intranet, .lan, .home, .corp, .test, .invalid, .onion, .localhost`
- Hex IPs: `/^0x[0-9a-f]{1,8}$/i` (`0xC0A80101` = 192.168.1.1)
- Octal/hex IP parts: `/^0[0-9a-fx]/i`
- IPv6-mapped IPv4: `::ffff:127.0.0.1`, `::127.0.0.1`
- IDN homographs: `[^a-z0-9.-]` basic guard

Trusted hosts via `TRUSTED_CUSTOM_HOSTS` env (comma-separated). Default: `localhost, 127.0.0.1, ::1, host.docker.internal`. (Source: `§R2.16`.)

Lancelot has `lancelot.net` SSRF guard already — compare this list to verify coverage.

---

## 8. Multi-Agent Cost Patterns (cited: OPENROUTER.md §6, §7, §8)

**Per-agent budget partitioning** — OpenRouter has no per-agent budget in the API. Workaround: create separate management API keys per agent class with credit limits via `POST /api/v1/keys {name, limit, limitReset: "monthly"}`. Monitor with `GET /api/v1/key → {limit_remaining}`. (Source: `§6`.)

**Orchestrator system prompt cache** — an 8000-token system prompt cached with Anthropic 1-hour TTL:
- Without cache: 8000 × 100 calls × $0.000003 = $2.40/hour
- With cache: (8000 × $0.00000375 write once) + (99 × 8000 × $0.0000003 reads) = $0.267/hour
- **89% cost reduction** on static context

**Tool-executor cost gap** — GPT-5.4 Nano at $0.20/$1.25/M vs Sonnet 4.6 at $3/$15/M = **15x input / 12x output** difference. For high-volume tool dispatch, the model choice for the sub-agent dominates cost more than the orchestrator choice.

**Zero-completion insurance** — OpenRouter deducts 0 credits if response has 0 completion tokens + null/blank finish_reason OR finish_reason=="error". Detection in agent loop: `usage.completion_tokens == 0 and not choice.finish_reason`. Retry immediately (transient cold-start). (Source: `§8`.)

**Streaming for tool detection** — use streaming for agent loops; earlier tool-call detection lets the orchestrator dispatch sub-agents sooner. Token cost identical; wall-clock time reduced. (Source: `§6 batch vs streaming`.)

---

## 9. Observability / Tracing Seams

### OpenRouter Broadcast (OTLP, cited: OPENROUTER.md §16)

Every request gets a span. Custom metadata via `trace` field:
```json
{
  "trace": {
    "trace_id": "...", "trace_name": "...", "span_name": "...",
    "generation_name": "orchestrator",
    "parent_span_id": "...",
    "agent_role": "tool_executor"   // → trace.metadata.agent_role
  },
  "session_id": "..."               // → session.id
}
```
Custom keys under `trace` go into `trace.metadata.*` namespace. `gen_ai.*` semantic conventions for model/token/cost. Broadcast destinations: Langfuse, Braintrust, Datadog, Grafana, PostHog, ClickHouse, Snowflake, S3, webhooks — all OTLP-compatible.

### LiteLLM metadata injection (cited: LITELLM.md §8)

Every request carries `metadata.user_api_key_user_id`, `metadata.user_api_key_team_id`, `metadata.model_group`, `metadata.tags` from auth time onwards. Every observability integration picks these up automatically — single source of truth for routing + billing + logging. (Source: `§7` observability section.)

### Lancelot's existing seam

`lancelot.context` already has `agent_id`, `tenant_id`, `cost_usd`, `request_id`, `trace_id`. `TransportPool` auto-injects `x-request-id`/`x-agent-id`/`traceparent` on outbound calls. The `W3C traceparent` propagation in TransportPool already gives the distributed trace join that these gateways build custom systems for.

---

## 10. What NOT to Build (Anti-Patterns)

1. **Semantic cache as a gateway primitive** — LiteLLM ships it (Qdrant/Redis-vector, embedding model `text-embedding-ada-002` by default, similarity_threshold configurable); Portkey gates it to the hosted control plane. For Lancelot: `lancelot.canonical.dumps_canonical` already covers the simpler + more reliable **byte-stable prefix** approach for prompt-cache stability. Semantic cache adds a costly embedding call on every cache miss and has false-positive risks. Not worth it; use canonical JSON + Anthropic/OpenAI native KV-cache.

2. **Flat LLM proxy route table** (all 15+ endpoints) — only worth building if you're being an API gateway. Lancelot is a framework that builds applications; users plug in TransportPool directly.

3. **Storing a pricing table in the framework** — LiteLLM's `model_prices_and_context_window.json` is the standard (40K lines, 2300 entries). Lancelot's `upstream_metrics` already correctly defers to `LANCELOT_LLM_RATES` env JSON and ships NO rates. This is correct.

4. **`randomize: false` retries** — Portkey's deterministic retry (no jitter) causes thundering herd when many clients hit the same 429. Always jitter. Lancelot's `TransportPool` has `retry_with_backoff` — verify it jitters.

5. **In-memory-only cache without LRU/size cap** — Portkey's `inMemoryCache` is a plain JS object. Leaks to OOM on long-lived processes. Any response cache needs a size cap or Redis backend.

6. **Routing by in-flight count without atomic counters** — LiteLLM's `least-busy` has a read-modify-write race under concurrency. Use Redis `INCRBY` (LiteLLM's `usage-based-routing-v2` pattern) for accurate in-flight tracking.

---

## Lancelot Byte-Path Implications

| Gateway primitive | Lancelot status | Action |
|---|---|---|
| Provider shape translation (messages/tools/system) | **MISSING** — TransportPool does transport, not message-shape | Build as `ProviderProfile` / adapter layer |
| Tool-name sanitization with forward/reverse map | **MISSING** | Needed for Anthropic tool calls from any agent loop |
| `reasoning_effort` → `thinking.budget_tokens` mapping | **MISSING** | Needed for unified reasoning control |
| Cost tracking per model | Partially present (`upstream_metrics.calc_cost_usd`) | Already uses env rate table; extend to cache tokens |
| Routing strategies (least-busy, latency-based) | **MISSING** (TransportPool routes but no strategy selection) | Could add as `pool.select_strategy()` |
| Retry with Retry-After header honoring | **PRESENT** (`TransportPool` respects Retry-After) | Verify jitter is present |
| Prompt cache sticky routing | **MISSING** (no session affinity to specific provider endpoint) | `Response.set_session_affinity(key)` is HTTP-level; provider-level stickiness is different |
| SSRF guard | **PRESENT** (`lancelot.net` SSRF guard) | Verify against Portkey's full blocked-TLD/IPv6-mapped list |
| OTLP tracing seam | **PRESENT** (`traceparent`, W3C, `TransportPool` forwards) | Better than OpenRouter's custom `trace` field; already done |
| Model capability detection (`supported_parameters`) | **MISSING** | Build as part of ProviderProfile |
| Cache key stability (canonical JSON) | **PRESENT** (`lancelot.canonical.dumps_canonical`) | Already the right approach |
| Mid-stream error detection | **PRESENT** (R3 pump + disconnect → upstream cancel) | Verify the `finish_reason=="error"` pattern is handled |
