# Byte-path mine: Gateways + Deployment (LiteLLM · Portkey · Kong AI · Railway · Render)

> **Sources read in full:** `teardowns/LITELLM.md` (4127 lines), `teardowns/PORTKEY.md` (1143 lines),
> `teardowns/KONG_AI_GATEWAY.md` (809 lines), `teardowns/RAILWAY.md` (6758 lines, grep pass),
> `teardowns/RENDER.md` (5803 lines, grep + targeted reads).
> **Cross-ref:** `research/external/01-ai-gateways.md`, `research/external/04-deployment-observability-dx.md`.
> **Date:** 2026-06-22.

---

## Legend
- **HAVE** — Lancelot already has this at `lancelot/` + CLAUDE.md confirms shipped.
- **NEW / IMPROVE** — additive or an upgrade over current behavior.
- **SKIP** — out of scope, Tier-D, or a non-bottleneck for Lancelot's byte-path.

---

## Section 1 — Outbound LLM transport: `lancelot.llm.TransportPool` vs the gateways

### M1 — Retry-after honoring + total budget cap (HAVE — already shipped, confirm constants)

Lancelot `lancelot/llm.py:376-490` (`_retry_after_seconds`, `request()`):
- Parses `Retry-After` / `retry-after-ms` (lines 376–390).
- Enforces `retry_budget_ms` total budget: bails when `spent_ms + next_sleep > budget` (lines 485–489).
- Default `retry_budget_ms=60000` (60 s) from `LANCELOT_RETRY_BUDGET_MS` env (line 312–317).
- `max_retries=3`, `backoff_base=0.2`, `backoff_max=8.0` (line 229).
- POST/PATCH never retried (`_IDEMPOTENT` frozenset, line 17).

**Cross-ref: LiteLLM** `constants.py:895–921` confirms the field is:
`MAX_RETRY_LIMIT_MS=60_000`, `DEFAULT_MAX_RETRIES=2`.
**Portkey** `retryHandler.ts:49` (`MAX_RETRY_LIMIT_MS=60_000`, `MAX_RETRIES=5`, `RETRY_STATUS_CODES=[429,500,502,503,504]`).

**Delta from gateways:** Portkey also parses `x-ms-retry-after-ms` (Azure-specific header).
Lancelot only parses `retry-after` and `retry-after-ms`.
**IMPROVE:** add `x-ms-retry-after-ms` to `_retry_after_seconds` — one-liner, zero perf cost.
`lancelot/llm.py:376–390`.

**Verdict:** HAVE the mechanism. Micro-improve: add the Azure header.

---

### M2 — Failure-RATE circuit breaker vs failure-COUNT (NEW — not yet in Lancelot)

**Source:** LiteLLM `router.py`, `router_utils/cooldown_cache.py`, `constants.py`.

LiteLLM's production trip rule (`constants.py:908–909`, `router_utils/cooldown_handlers.py`):

```python
DEFAULT_FAILURE_THRESHOLD_PERCENT = 0.5   # 50% failure rate trips cooldown
DEFAULT_FAILURE_THRESHOLD_MINIMUM_REQUESTS = 5   # min 5 requests before trip possible
DEFAULT_ALLOWED_FAILS = 3                 # legacy count-based (v1 only)
DEFAULT_COOLDOWN_TIME_SECONDS = 5
ROUTER_MAX_FALLBACKS = 5
SINGLE_DEPLOYMENT_TRAFFIC_FAILURE_THRESHOLD = 1000  # suppress cooldown on only deployment
```

`_is_cooldown_required(model_id, exception_status, exception_str)` (`router.py/cooldown_handlers.py`):
```python
ignored_strings = ["APIConnectionError"]      # don't trip on Lancelot's own conn errors
# Trip on: 429, 401, 408, 404, all 5xx
# Do NOT trip on: 400, 403, 422 (caller's fault, not provider's)
```

`SINGLE_DEPLOYMENT_TRAFFIC_FAILURE_THRESHOLD`: if a model group has only ONE deployment,
cooldown is SUPPRESSED unless failure rate exceeded AND ≥1000 requests in the window.
Rationale: cooling the only deployment leaves nothing to fall back to — better to keep trying.

**Lancelot has NONE of this.** `TransportPool` has retry logic but no deployment-level circuit
breaker, no cooldown, no single-provider-valve.

**How additive:** `TransportPool` is a *per-call* pool. A multi-deployment failover router
is a distinct layer on top (the `app.llm` concept referenced in CLAUDE.md and the external mine).
**The right action:** document the constants above in the Lancelot spec for `app.llm`'s router
phase; they don't need to live in `TransportPool` itself (which is single-provider per instance).

**SKIP for TransportPool directly, NEW for the `app.llm` router layer.**

---

### M3 — TTFT-normalized latency routing: seconds-per-completion-token (NEW — document formula)

**Source:** LiteLLM `router_strategy/lowest_latency.py:60–180`, `constants.py`.

```python
# LiteLLM lowest_latency.py:79-110
if kwargs.get("stream", None) is True:
    time_to_first_token_response_time = (
        kwargs.get("completion_start_time", end_time) - start_time
    )

# Normalize to seconds-per-completion-token (not raw latency):
final_value = safe_divide_seconds(response_seconds, completion_tokens)
# TTFT for streaming: same normalization
```

Constants: `max_latency_list_size=10` (sliding window), `ttl=3600` (1h window), `lowest_latency_buffer=0`.

**Lancelot has TTFT metric (`lancelot.http.stream.ttft`) on the server side.** But TransportPool
records upstream-TTFT (`llm.py:521–554`, `_record_stream_usage`). It does NOT use TTFT to
route between providers — only records it.

**This is only relevant if/when Lancelot adds latency-aware provider selection to `app.llm`.**
Record the formula: TTFT-per-completion-token (not raw), window=last 10 samples, 1h TTL.

**SKIP until latency-aware selector is built; document formula for when it is.**

---

### M4 — Cache-warm session stickiness (HAVE the primitive; `app.llm` policy not built)

**Source:** Portkey `tryTargetsRecursively` + Bifrost key-stickiness (corroborated in `external/01-ai-gateways.md §Finding 3`).

Lancelot ALREADY HAS `spawn_child(parent_request, session_affinity=...)` (`llm.py:906–961`).
The `session_affinity` value is forwarded as `x-lancelot-affinity` header to the upstream.
**But this is routing the USER's traffic, not selecting which provider key to use.**

The gateway-level pattern: after a request that writes an Anthropic ephemeral prompt cache
(5-min window), the NEXT request for the same session should go to the **same provider key**
so the cache is warm. TransportPool is a single-provider pool — multi-provider routing is
`app.llm`'s job.

**The correct additive**: in `app.llm`'s key-selection logic (when built), an optional
`sticky_by_session` mode that pins `(session_id, model)` → `(provider, key)` for a TTL
matching the prompt-cache window (Anthropic: 5m ephemeral / 1h explicit; OpenAI: 1h).

**SKIP for now (TransportPool already has the affinity header); design note for `app.llm`.**

---

### M5 — Provider-to-canonical request/response transform 4-method Config pattern

**Source:** LiteLLM `llms/anthropic/chat/transformation.py:243–330`, `constants.py`;
Portkey `src/providers/anthropic/chatComplete.ts:268`, `src/handlers/handlerUtils.ts:476–834`.
Kong `kong/llm/drivers/anthropic.lua:602 LOC`.

All three converge on the same **4-method provider abstraction**:
1. `get_supported_openai_params(model)` — capability list
2. `map_openai_params(params, model)` — translate names
3. `transform_request(model, messages, params, headers)` — build wire body
4. `transform_response(raw_response)` — parse back to canonical (OpenAI shape)

**Anthropic-specific transforms (load-bearing for Lancelot users):**
- Tool name sanitization: `^[a-zA-Z0-9_-]{1,128}$` — `/` and `.` → `_`; reverse map stashed to rewrite tool names on the response (LiteLLM `transformation.py:1800–1920`).
- `thinking` drop when last assistant message lacks `thinking_blocks` (prevents 400 from Anthropic).
- System-message separation: OpenAI `role:system` → Anthropic top-level `system` field.
- `tool_choice: "required"` → `{"type": "any"}`.
- `reasoning_effort` → `thinking.budget_tokens` (LiteLLM mapping: `minimal→1024`, `low→4000`, `medium→8000`, `high→16000`, `xhigh→32000`).

**Lancelot's `TransportPool` does NOT transform provider requests** — it sends whatever
body the user constructs. This is INTENTIONAL (CLAUDE.md: "AI logic stays user-side").
Lancelot owns the byte path; the user owns the agent.

**SKIP:** the translation layer is user-side. Document the Anthropic quirks above as
operator-docs / `docs/operator/` additions (free, low effort, high value for `app.llm` users).

---

### M6 — Per-attempt key rotation within a provider on 429 (NEW — design note)

**Source:** Bifrost's `keyProvider(usedKeyIDs) → Key` pattern (cited in `external/01-ai-gateways.md §Finding 4`).

On a 429 that is per-key (not per-provider), rotating to another key of the SAME provider
is cheaper and faster than falling over to a different provider.

**Lancelot's TransportPool is single-key per instance.** Multi-key within one provider
is an `app.llm` concern.

**Pattern to implement:** a `KeyPool(keys: list[str], strategy="weighted_random")`
alongside the deployment pool. On 429, the retry path calls `key_pool.next(excluding=tried_keys)`
before escalating to provider fallback.

**SKIP for TransportPool (single-key by design); design note for `app.llm`.**

---

### M7 — Mid-stream error as `finish_reason:"error"` chunk (IMPROVE — cheap correctness)

**Source:** `external/01-ai-gateways.md` (Minor section, referencing OPENROUTER.md).

Once SSE has started (HTTP 200 sent), status code CANNOT change. Mid-stream provider failures
must be emitted as a terminal SSE chunk:

```json
data: {"choices":[{"delta":{},"finish_reason":"error","index":0}],"error":{"message":"upstream failed","type":"server_error"}}
```

**Lancelot's R3 streaming pump (`src/stream.rs`, `lancelot/stream.py`):** on upstream failure
after stream start, behavior is a CancelledError → the stream terminates without sending a
final error chunk. This can confuse OpenAI-SDK-compatible clients that expect the
`finish_reason` field.

**IMPROVE:** in `stream.py` / `src/stream.rs` pump, on error after first chunk, emit a
synthetic terminal SSE chunk with `finish_reason:"error"` before closing. Cheap: ~10 lines.

---

### M8 — SSE keepalive must be a spec-valid comment (HAVE, verify format)

**Source:** `external/01-ai-gateways.md` (Minor section).

SSE spec: a keepalive MUST be a comment line `": ...\n\n"` (colon-prefix),
NOT a fake `data:` chunk. Data chunks corrupt the token stream if parsed by compliant clients.

**Lancelot has `stream_interval` + SSE keepalive (`@app.stream(sse_keepalive=N)`).**
Verify that the keepalive frame emitted by `src/stream.rs` uses the `: lancelot-keepalive\n\n`
comment format, not a `data:` frame. If it does, it is HAVE. If not, one-line fix in
`lancelot/stream.py` or `src/stream.rs`.

**lancelot_check_hint:** `grep -n "keepalive\|sse_keepalive\|comment" src/stream.rs lancelot/stream.py`.

---

## Section 2 — Routing / strategy internals (gateway patterns)

### M9 — Portkey's recursive fallback-of-loadbalance-of-conditional config tree (SKIP)

**Source:** Portkey `src/handlers/handlerUtils.ts:476–834` (`tryTargetsRecursively`).
Portkey `StrategyModes`: `FALLBACK`, `LOADBALANCE`, `CONDITIONAL`, `SINGLE`.
`CONDITIONAL` uses MongoDB-operator syntax (`$eq/$ne/$gt/$in/$regex`) over
`metadata.X / params.Y / url.Z`.

**Why SKIP:** Portkey's tree is a config-driven gateway product surface. Lancelot is a
framework, not a config-driven gateway. The routing tree is the OPERATOR's code (LangGraph,
user-written logic), not Lancelot's job. CLAUDE.md: "AI logic stays user-side."

---

### M10 — Kong's 8-stage filter pipeline dedup via `ai_executed_filters` map (SKIP)

**Source:** Kong `kong/llm/plugin/base.lua:7–20` (STAGES enum), `normalize-request.lua:238 LOC`.

Kong deduplicates shared filters across multiple AI plugins via
`ngx.ctx.ai_executed_filters[name] = true`. Prevents double-parsing when
`ai-proxy + ai-prompt-guard + ai-prompt-decorator` all register `parse-request`.

**Why SKIP:** Lancelot's middleware chain is Python-ASGI (not multi-plugin-lua).
The dedup concern doesn't apply. Kong's phase-based nginx cycle is not the model.

---

### M11 — Kong `_KEYBASTION` weak-keyed identity cache (DESIGN PATTERN — adopted in Lancelot secrets)

**Source:** Kong `kong/llm/plugin/shared-filters/normalize-request.lua:22–25`.

```lua
local _KEYBASTION = setmetatable({}, {
  __mode = "k",  -- weak keys: auto-evict when config table GC'd
  __index = ai_shared.cloud_identity_function,  -- lazy mint on miss
})
```

GCP service account access tokens / AWS STS sessions cached per-config-object.
Auto-evicted when config reloads. No explicit TTL management needed.

**Lancelot equivalent:** `lancelot/secrets.py` (`install_sighup_handler()`, per-backend
lazy import). The SIGHUP rotation pattern is HAVE. The Kong pattern is a GC-based variant;
Python doesn't have weak-keyed dicts in the same form (use `weakref.WeakKeyDictionary`
for per-config caches if needed). **SKIP as a direct port; note pattern for identity caches.**

---

## Section 3 — Deployment / container byte-path (Railway + Render)

### M12 — Render deploy ladder: SIGTERM timing ceiling (HAVE in spirit, operators need doc)

**Source:** `teardowns/RENDER.md:249–257` (zero-downtime deploy); `external/04-deployment-observability-dx.md §F1`.

Render deploy ladder (KNOWN from docs):
```
git clone → buildCommand (≤120m) → preDeployCommand (≤30m) →
[per instance:] startCommand (≤15m to healthy) → health-check pass →
networking cutover → +60s → SIGTERM old → ≤maxShutdownDelaySeconds → SIGKILL
```

Constants:
- `maxShutdownDelaySeconds` default **30s**, max **300s** (`RENDER.md:256`).
- Start-command timeout: **15 minutes** (`RENDER.md:172`).
- Running-service health: 15 consecutive seconds failed → deroute; 60s → restart.
- SIGTERM arrives **+60s after new instance is live** (`RENDER.md:255`).

**Risk for Lancelot:** if `LANCELOT_DRAIN_SECS` (default 10s) is raised to e.g. 25s,
but Render's `maxShutdownDelaySeconds` is left at 30s, an operator with a long drain
timeout could still get SIGKILL'd mid-drain. Drain must complete inside
`maxShutdownDelaySeconds`.

**HAVE:** `LANCELOT_DRAIN_SECS`, `/readyz` → 503 on SIGTERM, `initiate_shutdown()`.
**IMPROVE (docs):** Add to `docs/deploy.md` and `docs/operator/` one rule:
`render.maxShutdownDelaySeconds ≥ LANCELOT_DRAIN_SECS + 10`. The `lancelot init`
scaffolder should write `maxShutdownDelaySeconds = max(40, LANCELOT_DRAIN_SECS + 10)`
into `render.yaml` (see `external/04` §F1).

---

### M13 — Railway `drainingSeconds` + `overlapSeconds` (HAVE in spirit, scaffold gap)

**Source:** `teardowns/RAILWAY.md:649–650, 741–742` (`ServiceConfig`).

Railway's deploy config fields:
```
drainingSeconds: Int    # graceful shutdown window for old instance
overlapSeconds:  Int    # new instance overlap before old drains
healthcheckPath: String # HTTP path probed in HEALTHCHECK step
healthcheckTimeout: Int # seconds before healthcheck fails the deploy
```

Deploy pipeline order (KNOWN, `RAILWAY.md:1401`):
```
CONFIGURE_NETWORK → CREATE_CONTAINER → HEALTHCHECK → DRAIN_INSTANCES
```
Network is pre-provisioned BEFORE container starts. Critical: if `healthcheckPath` is
not set, Railway infers success from port-binding only.

**Lancelot HAVE:** `/livez` (Rust constant-tier, zero-GIL), `/readyz` (503 on drain).
**IMPROVE (scaffold):** `lancelot init` should emit `railpack.json` + `Procfile` with
`drainingSeconds` matching `LANCELOT_DRAIN_SECS` and `healthcheckPath: "/readyz"`.

lancelot_check_hint: `ls docs/ && cat docs/deploy.md` for current scaffolding.

---

### M14 — Railway Railpack Python framework detection for zero-config start (NEW — adoption win)

**Source:** `teardowns/RAILWAY.md:272–323, 5366` (Railpack Python provider);
`external/04-deployment-observability-dx.md §F2`.

Railpack detects FastAPI by import and emits:
`"startCommand": "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"`.
Unknown frameworks fall back to `python main.py`.

A Lancelot app with no `Procfile` / `railpack.json` hits the `python main.py` fallback
(only works if `__main__` calls `app.run()`). This is a FRICTION point for Railway deploys.

**IMPROVE (low effort, high leverage):**
1. `lancelot init` emits `Procfile: web: python -m lancelot run` + `railpack.json` pinning
   `deploy.startCommand = "python -m lancelot run"`.
2. Long-term: open a PR to Railpack's Python provider adding a Lancelot detection arm
   (mirror the FastAPI arm: detect `from lancelot import App` → emit the start command).

**This is an adoption funnel item, not a byte-path win per se, but it ensures the operator
experience on Railway is correct out of the box.**

---

### M15 — `$PORT` binding and cold-start surface (HAVE, confirm)

**Source:** `teardowns/RENDER.md:1779` (`$PORT` defaults to 10000), `teardowns/RAILWAY.md:5366`.

Both platforms: bind `0.0.0.0:$PORT` or the service is unreachable.

**Lancelot HAVE:** `LANCELOT_PORT` env → `run(port=...)` → Rust hyper binds on that port.
`LANCELOT_HOST` env covers `0.0.0.0`. `PORT` env is also read (`LANCELOT_PORT || PORT`
from CLAUDE.md M12 milestone). Confirmed in `lancelot/settings.py` and `src/lib.rs`.

**SKIP — already correct.**

---

### M16 — SOCI lazy image pull for cold-start reduction (PLATFORM-PROVIDED, SKIP)

**Source:** `teardowns/RAILWAY.md:3927, 4191–4197` (SOCI snapshotter + overlayfs).

Railway uses `overlayfs + SOCI (Seekable OCI)` — lazy layer pulls so the container can
start before the full image is fetched. This is platform infrastructure, not something
Lancelot controls. The actionable implication: smaller Lancelot container images → less
SOCI has to pull → faster cold starts. Already addressed by the distroless/multi-stage
Dockerfile guidance in CLAUDE.md M12.

**SKIP — platform-provided, no Lancelot code change.**

---

## Section 4 — Observability (gateway-sourced findings)

### M17 — Kong `llm_tpot_latency` = time-per-output-token (HAVE equivalent, naming note)

**Source:** Kong `kong/llm/plugin/observability.lua:3`; `kong/llm/drivers/shared.lua:30–60`.

Kong emits `llm_tpot_latency = llm_e2e_latency / llm_completion_tokens_count`
under namespace `proxy` → flows into Prometheus / Datadog / Splunk via `kong.log.set_serialize_value`.

**Lancelot HAVE:** `lancelot.http.stream.ttft` histogram (TTFT, not TPOT).
TPOT (time-per-output-token) = e2e_latency / completion_tokens, a different and complementary metric.
TTFT is more actionable for user-perceived streaming UX. TPOT is useful for throughput capacity planning.

**IMPROVE (optional):** add `lancelot.upstream.tpot_ms` histogram (derive from
`upstream_metrics.record_upstream(url, 200, duration_s, body=body)` which already has
duration + token counts). One line in `upstream_metrics.py`. Nice-to-have, not urgent.

---

### M18 — LiteLLM `model_prices_and_context_window.json` as pricing source (REFERENCE)

**Source:** LiteLLM `model_prices_and_context_window.json` (40,076 lines, 2,303 entries, `constants.py:916`).

This file is the **de facto industry pricing source** — LangChain, OpenRouter's cost calculator,
and many dashboards mirror its structure. Key fields per model:
`input_cost_per_token`, `output_cost_per_token`, `cache_read_input_token_cost`,
`supports_function_calling`, `supports_prompt_caching`, `max_input_tokens`, etc.

**Lancelot HAVE:** `lancelot.upstream_metrics.set_rate_table()` + `LANCELOT_LLM_RATES` env JSON.
Lancelot deliberately does NOT ship a price table (CLAUDE.md: "Lancelot does NOT ship rates").
This is correct — the LiteLLM JSON is the right external source for operators who want cost tracking.

**SKIP — architectural decision confirmed correct; reference for operators.**

---

### M19 — Spend-flush decoupling: Redis incr → Postgres on `DEFAULT_FLUSH_INTERVAL_SECONDS=5` (REFERENCE)

**Source:** LiteLLM `proxy_server.py:2099–2112`, `constants.py:900`.

LiteLLM's spend tracking: `redis.incrbyfloat(key, cost)` per call → background flush to Postgres
every 5s (`DEFAULT_FLUSH_INTERVAL_SECONDS`). The Redis counter is source-of-truth for budget checks.

**Lancelot has:** `cost_usd` ContextVar, `X-Lancelot-Cost-USD` header, `lancelot.http.cost.usd` histogram.
No persistent spend DB (correct — Lancelot is a framework, not a gateway with multi-tenant billing).

**SKIP — out of framework scope.**

---

## Summary of additive actions

| ID | Action | Effort | File | Real byte-path? |
|---|---|---|---|---|
| M1-delta | Add `x-ms-retry-after-ms` (Azure) to `_retry_after_seconds` | 1 line | `lancelot/llm.py:376–390` | Yes — correct retry |
| M2 | Document failure-RATE breaker constants for `app.llm` router design | Spec only | `docs/plans/` | Yes — prevents self-DoS |
| M7 | Emit `finish_reason:"error"` terminal SSE chunk on mid-stream failure | ~10 lines | `lancelot/stream.py` / `src/stream.rs` | Yes — client compat |
| M8 | Verify keepalive is SSE comment (`": ...\n\n"`), not data chunk | Grep + maybe 1 line | `src/stream.rs` | Yes — correctness |
| M12 | Doc rule: `render.maxShutdownDelaySeconds ≥ LANCELOT_DRAIN_SECS + 10` | Docs only | `docs/deploy.md` | Yes — prevents SIGKILL mid-drain |
| M13 | Scaffold `railpack.json`/`Procfile` with `drainingSeconds` + `healthcheckPath=/readyz` | Scaffold | `lancelot init` | Yes — operator experience |
| M14 | `lancelot init` emits `Procfile: web: python -m lancelot run` | Scaffold | `lancelot init` | Adoption + correctness |
| M17 | Optional: `lancelot.upstream.tpot_ms` histogram | 1 line | `lancelot/upstream_metrics.py` | Nice-to-have |

**Not built, not needed:** provider translation layer (user-side), config-tree gateway DSL,
Kong plugin dedup, Portkey MongoDB routing, semantic cache, virtual keys, spend DB — all
correctly out of Lancelot's framework scope.
