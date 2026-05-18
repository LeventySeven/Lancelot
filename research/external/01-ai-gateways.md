# External Value-Mine: AI Gateways / LLM Proxies vs Lancelot's `app.llm` layer

> Scanned: `teardowns/{LITELLM,PORTKEY,BIFROST,KONG_AI_GATEWAY,OPENROUTER,ORQ,NEXOS_AI}.md`.
> Mandate: default verdict is "skip". Only concrete, plan-beating, reimplementable findings surface.
> Cross-ref'd against `ULTIMATE-PLAN.md §2`, `SYNTHESIS.md §2 #12 / §4.6`, `DESIGN-DECISIONS.md §4 (AI-native layer)`.
> No matching `products/*_PRODUCT_CODE.md` exist for any of these.

---

## 1. Verdict (5 lines)

**YES — there are a small number of concrete additions worth pulling into the plan.** Lancelot's plan
already nails the *hard* structural moat (disconnect→upstream-abort, backpressure, pooled HTTP/2 client,
circuit-breaker, multi-provider failover, POST-never-retried). What it does **not** yet specify is the
*decision math and constants* that turn "we have failover + a circuit breaker" into a tuned router. The
genuinely useful, reimplementable items: **(1)** the exact cooldown/retry constants + the v2 failure-*rate*
(not failure-*count*) cooldown rule with a single-deployment safety valve; **(2)** the 429 `retry-after`-aware
retry that snaps the next attempt to the rate-limit window boundary with a total retry budget; **(3)**
cache-warm sticky routing (pin a conversation to the provider/key that holds its prompt cache); **(4)**
per-attempt key rotation on rate-limit within one provider; **(5)** TTFT-normalized (sec-per-token) latency
routing. Everything else is confirmation or out of scope.

---

## 2. Genuinely useful findings (with cites + why + how to apply)

### Finding 1 — Cooldown/retry constants + failure-RATE cooldown with single-deployment valve
`file: teardowns/LITELLM.md` (lines ~544-551, 1661-1698, 894-922)

Lancelot's plan says "circuit-breaker" and "multi-provider failover" but pins **no constants and no trip
rule**. LiteLLM's production rule is more correct than a naive failure-*count* breaker and is directly
reimplementable:

- Trip a deployment when `failures / (successes + failures) > 0.5` over the window **with a minimum of 5
  total requests** before the breaker can trip at all (`DEFAULT_FAILURE_THRESHOLD_PERCENT=0.5`,
  `DEFAULT_FAILURE_THRESHOLD_MINIMUM_REQUESTS=5`). This avoids tripping a healthy provider that had 3
  failures against 200 successes — the failure mode of every count-based breaker.
- **Single-provider safety valve:** if a model group has only one deployment, suppress cooldown unless the
  failure threshold is hit *and* there have been ≥1000 requests in the window. Rationale: cooling down your
  only provider leaves nothing to fall back to — better to keep trying it. Lancelot's per-host breaker
  must have this exact carve-out or a single-provider app self-DoSes on a transient blip.
- **Cooldown is status-code-selective:** trip on 429/401/408/404 and all 5xx; do **not** trip on
  400/403/422 (those are the caller's fault, not the provider's — tripping wastes the only good provider).
  Also explicitly *exclude* the gateway's own connection errors (`APIConnectionError`) from tripping.
- Constants worth adopting verbatim as defaults: `DEFAULT_COOLDOWN_TIME_SECONDS=5`,
  `DEFAULT_ALLOWED_FAILS=3`, `ROUTER_MAX_FALLBACKS=5` (cap the fallback chain depth — Lancelot's plan has
  an unbounded `.relay()` failover concept; bound it), `DEFAULT_MAX_RETRIES=2`.

**Why it beats the plan:** the plan has the *mechanism* (circuit breaker) but a breaker with the wrong trip
rule is worse than none. The failure-rate + min-requests + single-provider-valve + status-selectivity is
the difference between a breaker that protects you and one that takes down your only working provider.

**How to apply:** make `app.llm`'s per-host breaker a failure-*rate* breaker (sliding window, min 5
requests), code the 400/403/422-don't-trip and single-provider-valve rules into the trip predicate, ship
the four constants above as the documented defaults in the spec's AI-native section.

### Finding 2 — 429-aware retry: snap next attempt to the rate-limit boundary, under a total budget
`file: teardowns/PORTKEY.md` (line 49, retryHandler); corroborated `teardowns/BIFROST.md` (line 211)

Portkey's `retryRequest`: `MAX_RETRIES=5`, `RETRY_STATUS_CODES=[429,500,502,503,504]`, and a hard total
retry budget `MAX_RETRY_LIMIT_MS=60_000`. On a 429 it parses the provider's
`retry-after` / `retry-after-ms` / `x-ms-retry-after-ms` headers; if the advised wait `>= 60s` or exceeds
the remaining budget it **bails immediately** (no point burning the budget on a wait that overshoots it);
otherwise it **zeros the backoff for remaining attempts and sleeps exactly the advised time** so the next
attempt lands precisely at the rate-limit window boundary. Retries are deterministic (`randomize:false`).
Timeout uses an `AbortController` and converts an abort into a structured 408.

**Why it beats the plan:** the plan says "POST-never-retried, circuit-breaker" but specifies *nothing*
about retryable GET/idempotent or the 429 case. Naive exponential backoff against a provider that just told
you exactly when to come back (`retry-after`) either over-waits (latency) or under-waits (immediate
re-429). Honoring the provider's own `retry-after` and capping with a single total budget is the correct,
provider-cooperative behavior and is ~30 lines.

**How to apply:** in `app.llm`'s retry path (which the plan already restricts to non-POST/idempotent):
parse the three `retry-after*` headers, snap the sleep to that value, enforce a single `MAX_RETRY_LIMIT_MS`
total budget across all attempts (not per-attempt), bail when the advised wait would blow the budget. Keep
the plan's "POST never retried" rule — this only changes *how* the already-allowed retries are scheduled.

### Finding 3 — Cache-warm sticky routing (pin a conversation to its prompt-cache provider/key)
`file: teardowns/OPENROUTER.md` (line 597); same idea independently in `teardowns/BIFROST.md` (lines 87, 111)

OpenRouter: after a request with cached content, *subsequent requests for the same model are routed to the
same provider to keep the prompt cache warm* — an explicit override of the cost-weighted load balancer.
Bifrost implements the same at the key level: **session stickiness keyed by `(model, sessionID)` in a
KVStore** so the same conversation always picks the same upstream key, maximizing the Anthropic
ephemeral-prompt-cache HIT rate.

**Why it beats the plan:** the plan's `app.llm` does multi-provider failover + load-balancing but says
nothing about cache affinity. With Anthropic/OpenAI prompt caching (5m/1h ephemeral), routing turn-2 of a
conversation to a *different* provider/key throws away a cache write the user already paid for and re-pays
full input price. This is real money on multi-turn agent/chat workloads — exactly Lancelot's headline AI
workload. None of Lancelot's three source frameworks (FastAPI/Robyn/Blazeio) had this; it's net-new.

**How to apply:** add an optional stickiness key to `app.llm` (default = a hash of the conversation /
explicit `session_id`); when load-balancing across providers or rotating keys, a sticky request bypasses
the balancer and reuses the last `(session, model)→provider/key` mapping (small TTL'd map, TTL ≥ the
provider's prompt-cache TTL). Cost-weighted balancing still applies to *new* sessions.

### Finding 4 — Per-attempt key rotation within one provider on rate-limit
`file: teardowns/BIFROST.md` (lines 87, 211, 253)

Bifrost's worker builds a `keyProvider(usedKeyIDs) → Key` closure: on a rate-limit retry it is called with
the set of already-tried key IDs and returns a *fresh* key from the pool, so retries naturally rotate keys
*before* falling over to a different provider. Default key selector is `WeightedRandom` (integer math,
`int(weight*100)`, ~10ns, zero-alloc).

**Why it beats the plan:** Lancelot's plan models failover as *provider→provider* and load-balancing
across *keys* but does not describe rotating to a different API key of the *same* provider on a 429. In
practice a 429 is usually per-key, not per-provider — rotating to another key of the same provider is
cheaper and faster than failing over to a worse/more-expensive provider. This is a strictly better first
recovery step before the provider-level fallback chain.

**How to apply:** make `app.llm`'s key pool per-provider and have the retry layer exclude already-tried
key IDs and draw a new one (weighted-random over remaining keys) on 429 *before* escalating to the
provider fallback chain. Cheap: a tried-set passed through the retry closure.

### Finding 5 — TTFT-normalized (seconds-per-completion-token) latency for any latency-aware routing
`file: teardowns/LITELLM.md` (lines 485-513, 1625-1645)

If/when Lancelot adds latency-aware provider selection, LiteLLM's normalization is the non-obvious correct
metric: don't compare raw response time — normalize to **seconds-per-completion-token**
(`response_seconds / completion_tokens`), and for streaming use **TTFT** (`completion_start_time -
start_time`) not total time. Window = last 10 samples, 1h TTL. Otherwise a provider that returned a long
(good) answer looks "slower" than one that returned a short stub.

**Why it beats the plan:** the plan doesn't include latency-based routing at all, but its headline workload
is streaming chat where perceived speed *is* TTFT. If Lancelot adds any "pick the fastest provider"
behavior, the naive total-time metric is actively wrong; this is the right formula, free to adopt.

**How to apply:** only if a latency-aware selector is added to `app.llm` — record per-provider TTFT
normalized by completion tokens over a small sliding window; otherwise note as the correct formula for the
spec and skip building it in v0.

### Minor, optional (one-liners, only if cheap)
- **`: OPENROUTER PROCESSING` SSE-comment keepalive** (`OPENROUTER.md:526`): emit an SSE *comment* line
  during long upstream stalls to stop client/proxy idle-timeout disconnects. Lancelot's plan already has a
  Rust timer-wheel keepalive; just confirm the keepalive frame is a spec-valid SSE **comment** (`: ...\n\n`),
  not a fake data chunk — comments are ignored by compliant clients and don't corrupt the token stream.
- **Mid-stream error shape** (`OPENROUTER.md:529-543`): once SSE has started you cannot change the HTTP
  status; emit the error as a final chunk with `choices[0].finish_reason:"error"` and an `error` object at
  HTTP 200. Lancelot's Rust SSE codec should adopt this exact convention so OpenAI-compatible clients don't
  break on upstream mid-stream failures. Cheap correctness detail, not in the plan.

---

## 3. Checked, nothing new (one line each)

- **LITELLM.md — routing-strategy zoo** (simple-shuffle/least-busy/usage-v2/cost/tag): standard
  load-balancing; Lancelot's plan deliberately scopes `app.llm` to failover+pooled-client, not a 10-strategy
  router. Constants/cooldown extracted above; the strategy menu itself is out of v0 scope — skip.
- **LITELLM.md — 8-backend cache + semantic cache + `DualCache` two-TTL**: useful general pattern but
  request/response caching is not in Lancelot's `app.llm` plan and adds a vector-store dependency; the
  two-TTL (short in-mem / long Redis) idea is generic and already implied by any cache design — skip.
- **LITELLM.md — `model_prices_and_context_window.json` (2,303 entries)**: this *is* the industry pricing
  source; if Lancelot ever does cost accounting, mirror this file's structure rather than invent one. Noted,
  not a plan change (plan's cost/token accounting via OTel GenAI already covers the need) — skip.
- **PORTKEY.md — recursive Target tree + MongoDB-operator conditional router + 21-plugin guardrails**:
  powerful config DSL but it's a *product surface*, not a structural advantage; Lancelot is a framework,
  not a config-driven gateway — explicitly out of scope — skip.
- **BIFROST.md — per-provider buffered-channel + worker-pool, aggressive TCP keepalive
  (Idle:10s/Interval:5s/Count:3), separate zero-timeout streaming client**: the keepalive/stale-conn
  detection is good practice but Lancelot's plan already has a pooled HTTP/2 client with
  stale-detect-before-handout (from Blazeio steal #12) and a separate streaming path — confirmation, not new.
  The Go worker-pool concurrency model doesn't map onto the tokio/PyO3 model — skip.
- **BIFROST.md — semantic cache plugin, governance/virtual-keys, Starlark code-mode MCP sandbox**:
  enterprise/product features, not framework primitives — skip.
- **KONG_AI_GATEWAY.md — Lua plugin-priority chain, LLM-as-request/response-transformer,
  `ai-prompt-decorator/template`**: Kong-runtime-specific; the only transferable bit (SSE line-buffered
  parser holding partial chunks across invocations) is already in Lancelot's plan as the Rust SSE codec —
  confirmation only — skip.
- **OPENROUTER.md — inverse-square (`1/price²`) cost-weighted load balancing + uptime-30s deprioritize +
  `:nitro`/`:floor` shortcuts + NotDiamond auto-router**: elegant but it's a *managed-aggregator* policy;
  Lancelot's `app.llm` routes among the *developer's own* keys/providers, not a 60-provider marketplace —
  the price-weighting math doesn't apply (the dev picks providers). Noted as interesting, not applicable — skip.
- **OPENROUTER.md — stream-cancellation provider matrix** (cancel stops billing for OpenAI/Anthropic/
  Fireworks but NOT Bedrock/Groq/Google): this *confirms* Lancelot's disconnect→`upstream.abort()` moat is
  real and valuable, and flags that for some providers abort won't stop billing (document the caveat). No
  plan change — confirmation + a doc note.
- **OPENROUTER.md — response-healing, reasoning normalization, BYOK encryption**: product features /
  out of scope — skip.
- **ORQ.md — Deployment-as-resource (server-stored prompt+model+policy), Policy engine
  (fallback/weighted/round_robin + retry `{count,onCodes}` + budget/token limits), evaluator/guardrail
  framework, A2A agent streaming event taxonomy**: this is a SaaS control-plane product model, the opposite
  of Lancelot's "code-first framework" positioning; `PolicyRetryConfig {count, onCodes}` is a strictly
  weaker version of Finding 1/2 — skip.
- **ORQ.md — `headroom` context-optimization layer (prune/summarize to fit token window)**: adjacent to
  Lancelot's agent loop but context-compaction is not in the `app.llm` outbound-LLM scope — note for a
  possible future agent-memory research pass, not this layer — skip.
- **NEXOS_AI.md — Smart Router (ML classifier picks model), Deep Research primitive, two-validator
  guardrails, 300000ms (5min) default stream timeout**: docs-only teardown, no source; the only concrete
  number — a 5-minute default stream timeout to support long reasoning calls — is a sane default to note for
  Lancelot's per-route/idle timeout config, but the plan already mandates configurable per-route/idle/total
  timeouts so it's a default-value note, not a mechanism — skip.

---

## 4. Report back

**Did anything change the plan? YES — 5 concrete additions (all to `app.llm`, none structural rewrites):**

1. **Failure-RATE circuit breaker** (not count): trip on `fail/(fail+succ) > 0.5` with min-5-requests,
   plus the single-provider safety valve (suppress cooldown for the only provider unless ≥1000 reqs) and
   status-selective tripping (no trip on 400/403/422 or own connection errors). Adopt LiteLLM's defaults:
   cooldown 5s, allowed_fails 3, max fallback chain depth 5, max retries 2. *(LITELLM.md)*
2. **429-aware retry scheduling**: honor provider `retry-after`/`retry-after-ms`/`x-ms-retry-after-ms`,
   snap the next attempt to that boundary, enforce a single 60s *total* retry budget, bail when the advised
   wait overshoots it. Keeps the plan's "POST never retried" rule. *(PORTKEY.md, corroborated BIFROST.md)*
3. **Cache-warm sticky routing**: pin a `(session, model)` to its last provider/key (TTL ≥ prompt-cache
   TTL) so multi-turn conversations keep the Anthropic/OpenAI prompt cache warm; bypass the balancer for
   sticky requests. Net-new vs the entire FastAPI/Robyn/Blazeio corpus. *(OPENROUTER.md, BIFROST.md)*
4. **Per-attempt key rotation within a provider on 429**: rotate to another key of the same provider
   (weighted-random over untried keys) *before* escalating to the provider fallback chain. *(BIFROST.md)*
5. **TTFT-normalized latency metric** (seconds-per-completion-token, streaming uses TTFT) — only if/when a
   latency-aware selector is added; otherwise record as the correct formula in the spec. *(LITELLM.md)*

Plus two cheap SSE-codec correctness details: keepalive must be a spec-valid SSE **comment** line, and
mid-stream errors must be emitted as a final `finish_reason:"error"` chunk at HTTP 200 (status can't change
once streaming starts). And a confirmation: the OpenRouter cancellation matrix proves the
disconnect→`upstream.abort()` moat is real *and* tells us to document that abort won't stop billing on
Bedrock/Groq/Google.

Everything else across all 7 files is either confirmation of decisions already in the plan, or
product/control-plane surface that is deliberately out of Lancelot's framework scope.
