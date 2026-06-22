# Lancelot Model Layer — Synthesis Brief

> **Layer**: `model` — the thin adapter that sits **ON TOP of `lancelot.llm.TransportPool`**.
> **Altitude**: "just build agents, brutal." The smallest primitive that earns its keep. Anti-overkill AND anti-underkill.
> **The one-line scope lock**: *TransportPool already does transport / retry-with-jitter / cost / rate / mTLS / cancel / traceparent. The model adapter adds exactly three things — **translation** (OpenAI-canonical → N dialects), **routing** (`init_model("provider:model")`), and **error-typed cross-model failover**. Nothing else.*

This brief is for the Lancelot byte-path builder ("Lancelot"). It decides what to BUILD, what to REUSE from the existing byte path, and what to REFUSE. Every call is cited to a harvest note or to verified Lancelot source.

---

## 0. The boundary, stated once and enforced everywhere

The corpus is unanimous: **every production gateway and agent framework bets that OpenAI Chat-Completions is the lingua franca; clients speak it, the adapter translates inbound and outbound per provider.** This is "not a contested design decision; it is the settled norm" (`gateways_more.md §1`, verifying Bifrost/Kong/Nexos/Orq; `gateways_routing.md §1`, verifying LiteLLM/Portkey/OpenRouter).

The split between transport and translation is the whole brief:

| Concern | Owner | Evidence |
|---|---|---|
| HTTP body bytes, auth headers, connection pool, TLS/mTLS | **TransportPool (built)** | `lancelot_model_io.md §1`; `llm.py` |
| Retry w/ exp-backoff + **jitter** + Retry-After + budget cap; POST never retried | **TransportPool (built)** | `llm.py:481` (`+ random.random()*backoff_base` — jitter is PRESENT, the gateway "verify jitter" flags are RESOLVED CLEAN); `llm.py:432`,`:478` |
| Token extraction, cost calc, 6-reason error taxonomy, OTel histograms | **upstream_metrics (built)** | `lancelot_model_io.md §2`; `upstream_metrics.py:156` |
| cost/token/trace ContextVars; KV-cache prefix stability; request coalescing; fanout; spawn_child | **context / canonical / batch / llm (built)** | `lancelot_model_io.md §§3-5,1.7-1.8` |
| **Message-shape translation** (system extraction, tool/role rewrite, reasoning placement) | **model adapter (BUILD)** | `hermes_gateway.md §1-2`; `gateways_routing.md §1`; `google_adk.md §13` |
| **`init_model("provider:model")` routing + lazy SDK dispatch** | **model adapter (BUILD)** | `langchain.md:31,47`; `vercel_ai_sdk.md §6` |
| **Error-typed cross-model failover (NOT same-model retry)** | **model adapter (BUILD)** | `devin_warp.md §1` (verbatim Warp finding); `hermes_core.md §1` |
| **Per-provider tool-schema sanitization** (Anthropic nullable-union, strict mode) | **model adapter (BUILD)** | `hermes_gateway.md §4.7`; `openai_agents.md §10`; verified `tool.py` does NOT sanitize |

Anything not in the bottom three rows is **already built** or is a different layer's job. The model adapter is ~5 small modules; the durable part is the *shape*, the depreciating part is the per-model constants.

---

## 1. WHAT LANCELOT MUST BUILD

### 1.1 `ProviderProfile` — the declarative adapter dataclass (the spine)

**This is the load-bearing primitive.** Hermes's `ProviderProfile` (`hermes_gateway.md §1`, `providers/base.py:38-184`) is the exact design the task names, and it is the right one. The architectural thesis (verbatim): *"N boolean flags from the caller → one profile object that knows every quirk inline."* The prior art it replaced was `if provider == 'kimi'` branches scattered through the transport — the anti-pattern Lancelot must not recreate.

A profile is a frozen dataclass + a small number of polymorphic hooks with pass-through defaults. **The single most important mechanism** is the `build_api_kwargs_extras` split returning `(extra_body_additions, top_level_kwargs)` — *"this split exists because some providers put reasoning config in extra_body (OpenRouter) while others put it as top-level api_kwargs (Kimi). Without this split, every new provider would require a new conditional branch in the transport"* (`hermes_gateway.md §2.4`). That sentence is the entire reason the dataclass exists.

Adopt these fields (subset of Hermes, pruned to byte-path-relevant):

```python
OMIT = object()   # sentinel: three-state "not set" vs "actively strip" vs "override"

@dataclass(frozen=True)
class ProviderProfile:
    name: str
    api_mode: str = "chat_completions"   # selects the wire-format family: chat_completions | anthropic_messages | gemini_generate
    aliases: tuple[str, ...] = ()
    base_url: str = ""                    # empty → user supplies at init
    env_vars: tuple[str, ...] = ()        # ordered API-key env names (captures naming drift: ZAI = 3 names)
    default_headers: dict = field(default_factory=dict)
    fixed_temperature: Any = None         # None=caller's | OMIT=strip | float=override
    default_max_tokens: int | None = None
    default_aux_model: str = ""           # cheap model for sidebar tasks (see §1.4)
    fallback_chain: tuple[str, ...] = ()  # cross-model failover targets (see §1.3)
    # polymorphic hooks (pass-through defaults; override only what differs):
    def prepare_messages(self, messages): return messages          # role/content normalization
    def transform_request(self, body) -> dict: return body         # system extract, tool rewrite, OpenAI→native
    def transform_response(self, raw) -> "ModelResponse": ...       # native → canonical ordered parts
    def build_extra_body(self, **ctx) -> dict: return {}
    def build_api_kwargs_extras(self, **ctx) -> tuple[dict, dict]: return {}, {}  # THE split
    def sanitize_tool_schema(self, schema) -> dict: return schema   # see §1.5
```

**`OMIT` sentinel — copy verbatim** (`hermes_gateway.md §1.2`). Three-state `fixed_temperature`: `None` → caller's value; `OMIT` → `kwargs.pop("temperature", None)` unconditionally; float → override. Object-identity check (`is OMIT`). Kimi-class providers error on *any* client-supplied temperature — without `OMIT` you cannot express "actively strip" distinct from "not set." This is "architecturally critical" and is the canonical Python sentinel pattern.

**Ship the top ~5 profiles only**, in priority order: OpenAI (passthrough — ~5 lines), Anthropic (`messages` + `system` split + `cache_control` injection), Gemini (`contents`/`role:model`/`systemInstruction`), plus OpenAI-compatible shims (Groq/DeepSeek/Together/Fireworks/Mistral/OpenRouter = ~5 lines each, just identity + `base_url` + `env_vars`). Adding an OpenAI-compatible provider must be ~5 lines (`hermes_gateway.md §1.1`). Bedrock (SigV4) and Vertex (GCP token) are deferred — they need crypto/cloud-identity not yet in TransportPool (`gateways_more.md §3.2`).

**Plugin discovery**: a simple last-writer-wins registry (`get_profile(name)`), lazy-imported on first use, user override by registering a profile with the same `name` (`hermes_gateway.md §3`). Do NOT build the 3-pass bundled/user/legacy filesystem scanner — one dict + `register_profile()` is the brutal version.

Honest envelope: the *shape* is durable (survives 3+ model generations — `hermes_gateway.md §8`); the per-model wire constants (beta-header strings, thinking-budget token values, model-substring detection) are depreciating tactical hacks — **copy the architecture, never the constants**.

### 1.2 `init_model("provider:model")` — the string router (the ergonomic users touch)

The corpus converges on one ergonomic. LangChain's `init_chat_model("openai:gpt-4o")` — *"the string is `<provider>:<model_name>`; the function lazy-imports the partner package"* (`langchain.md:31`), explicitly *"what to copy for Lancelot: the `<provider>:<model>` string format and the lazy-import dispatch"* (`langchain.md:47`). Vercel's `createProviderRegistry(..., {separator:':'})` resolving `'openai:gpt-4o'`, default separator `':'`, and `resolveModel(str|Model)` accepting either form (`vercel_ai_sdk.md §6`, `:143`). ADK's `LLMRegistry` does the same via regex `model→class` (`google_adk.md §13`).

Build the LangChain/Vercel shape (string-split, not regex — simpler, no ambiguity):

```python
model = init_model("anthropic:claude-sonnet-4")   # → ModelClient bound to the Anthropic profile + a TransportPool
# resolve_model(str | ModelClient) accepts either uniformly (Vercel's resolveLanguageModel rule)
```

`init_model` splits on the first `:`, looks up the `ProviderProfile`, lazy-imports nothing heavier than the profile (provider SDKs are NOT imported — Lancelot speaks HTTP via TransportPool directly; this is a Lancelot advantage over LangChain, which imports the partner package). The returned `ModelClient` wraps `TransportPool.request/stream` and applies the profile's transforms. Keep the `do_`-prefix discipline from Vercel (`vercel_ai_sdk.md §2.1`): the low-level call is internal; users call `model.complete()` / `model.stream()`, never the raw transform.

**Default constant**: separator `:` (Vercel `provider-registry.ts:143`).

### 1.3 Error-typed cross-model failover (the single most load-bearing routing fact)

This is the one mechanism every production source independently converged on, and the one that is genuinely missing. Warp's verbatim finding (`devin_warp.md §1`, `blog.md:225-227`): *"We originally attempted to retry with the same model, and found that this often produced repeat failures"* → switched to **cross-model** fallback. Three failure modes ALL trigger the same cross-model hop: **outage, rate-limit, malformed tool call** (`blog.md:169`). Published chain: `Claude 4 Sonnet → Claude 3.7 Sonnet → Gemini 2.5 Pro → GPT-4.1`. Fires on ~2% of runs (`devin_warp.md:295`).

The byte-path note already scoped this exactly (`lancelot_model_io.md` is silent on it but `devin_warp.md:316-320` is explicit): *"Error taxonomy `ok/quota/timeout/permanent/transient/upstream_5xx` — the taxonomy is right; the failover action (hop to next provider) is not yet wired. The missing piece is the 'on classification X, retry with pool[i+1]' logic."*

Build the smallest thing that earns its keep — a profile-list walk gated on the **existing** error taxonomy:

```python
model = init_model("anthropic:claude-sonnet-4", fallback=["anthropic:claude-haiku-4", "google:gemini-flash"])
# on classify_status(...) in {"quota","timeout","transient","upstream_5xx"} OR malformed tool-call (422 → "permanent"
#   but tool-call-shaped) → hop to fallback[i]. On {"permanent" non-tool} (400/401/403) → abort, no hop.
```

Reuse `upstream_metrics.classify_status` (verified `upstream_metrics.py:156`) as the branch selector — do NOT build a parallel classifier. The action layer is the new part: ~40 LOC walking the list.

**Two non-negotiable caveats from the corpus:**
1. **Cache-busting order** (`devin_warp.md:328`, Devin `§R4.3`): *"switching models busts the cache."* The chain must try **same-family first** (Anthropic→Anthropic) before cross-provider (Anthropic→Google). A model switch invalidates the KV-cache prefix that `dumps_canonical` + session affinity worked to keep warm. Default the chain to same-family hops where the profile declares a family.
2. **Return the PRIMARY error on total failure**, not the last fallback's (`gateways_more.md §2.1`, Bifrost `shouldContinueWithFallbacks`) — preserves the user-visible root cause. And **stop the chain on a fatal error** mid-walk (auth/model-not-found) rather than trying the next.

**`require_parameters` (flagged CRITICAL in the task)** is the *gate* on this: when failing over to a fallback provider, the request must carry the parameters that provider needs, and a tool-using request must not route to a tool-incapable model. The minimal version: a `requires_tools: bool` on the request + a `supports_tools: bool` on the profile, so the failover walk skips incapable candidates (Nexos Smart Router: *"the router won't pick a model that doesn't support the tools enabled"* — `gateways_more.md §4.3`). Kong's enforcement gate (`gateways_more.md §4.1`): if a profile pins a model and the request differs → reject. Keep it a boolean flag, not a capability matrix (the matrix is overkill until there's a real catalog).

### 1.4 `AuxiliaryClient` — the cheap-model sidebar (separate cost bucket)

A pervasive, durable pattern: a **separate** LLM client for background/auxiliary work (compression, title-gen, vision, schema-derivation), NOT the expensive main model. Hermes uses it everywhere (`hermes_core.md §4`, `auxiliary_client.py:3445 LOC`); every profile declares a `default_aux_model` — Anthropic→haiku, Gemini→flash, Kimi→turbo (`hermes_gateway.md §1.1`). Corroborated by `nia.md:302` (cheap model, temp=0.0, for schema derivation), `evals_obs.md:114` (*"cheap model for breadth, expensive model for depth"*), `langchain.md:69` (SummarizationMiddleware uses a cheaper `summary_model`).

**The byte-path subtlety that makes this a real BUILD, not just `spawn_child`** (`hermes_core.md §4` byte-path lens): `spawn_child` snapshots the parent and **accumulates cost into the parent's `cost_usd` ContextVar** (verified `lancelot_model_io.md §1.8`, `§3.1`). The aux client needs cost to land in a **separate** bucket so sidebar spend doesn't pollute the main session's cost accounting. So:

```python
aux = model.auxiliary()                    # cheap model from profile.default_aux_model, OWN cost bucket
summary = await aux.complete(messages)     # does NOT add to context.cost_usd; emits its own metric label
```

Implementation: a thin `ModelClient` over the same TransportPool but bound to `default_aux_model` and a distinct cost-attribution label (`upstream_metrics` already supports `llm.class` / model labels — reuse them). ~30 LOC.

### 1.5 Per-provider tool-schema sanitization (the moment `@app.tool` reaches a non-OpenAI model)

**Verified gap**: `lancelot/tool.py` emits raw Pydantic `TypeAdapter.json_schema()` with **no** `additionalProperties:false`, **no** nullable-union strip, **no** strict-mode pass (verified — `tool.py:63,83`, grep for `sanitiz`/`strict`/`additionalProperties` returns nothing). This breaks the moment a tool schema reaches Anthropic or OpenAI strict mode.

Two pure functions, no deps, drop into the profile's `sanitize_tool_schema` hook:
1. **Anthropic** (`hermes_gateway.md §4.7`): `strip_nullable_unions` collapses `{"anyOf":[{"type":"string"},{"type":"null"}]}` (Pydantic/MCP emit these; Anthropic rejects) to the non-null branch; strip top-level `oneOf/allOf/anyOf`. *"Tools from MCP servers or Pydantic discriminated unions arrive with a different schema than what's in the registry."*
2. **OpenAI strict** (`openai_agents.md §10`): `ensure_strict_json_schema` — optional fields become `required` with `T|null`, all objects get `additionalProperties:false`, no `$ref` without inlining. *"The exact rules are non-obvious and painful to derive from OpenAI's docs — lift the function as-is."* (Swarm's naive `Optional[int]→"string"` is the named anti-pattern — do NOT copy.)

These run inside the adapter's request transform, keyed by `api_mode`. ~80 LOC total. The durable kind (structural wire contracts); ships only when a non-OpenAI profile is added.

### 1.6 `ModelResponse` — ordered content parts (the canonical intermediate)

The adapter's `transform_response` must produce a provider-neutral shape, and the corpus agrees on what it is. Vercel's V2 breakthrough (`vercel_ai_sdk.md §2.2`): replace the old `{text, toolCalls, reasoning}` split with **`content: list[Text | Reasoning | ToolCall | ToolResult]`, order-preserving** — this is what lets reasoning models and interleaved tool calls (Anthropic emits text-then-tool) work uniformly. Plus a normalized `finish_reason` 7-value enum (`stop/length/content-filter/tool-calls/error/other/unknown`) with the raw string preserved separately (`vercel_ai_sdk.md §2.3`). ADK's `BaseLlm` contract is the same idea (`google_adk.md §13`): one `LlmResponse(partial=False)` non-streaming; N partials + final aggregate streaming.

Build a minimal frozen `ModelResponse` with ordered parts + normalized `finish_reason` + `usage` + `raw`. This is the boundary the (separately-owned) agent loop reasons over. Keep it small — do NOT build Vercel's full 16-part provider-stream union and 27-part UI-stream protocol now (that's the streaming/agent layer's call; `vercel_ai_sdk.md §11` puts it on the stream pump Lancelot already has).

---

## 2. WHAT LANCELOT REUSES (byte path — do NOT rebuild)

All verified against `lancelot_model_io.md` and Lancelot source. The model adapter is a thin client over these.

| Reused primitive | What it gives the adapter | Citation |
|---|---|---|
| `TransportPool.request()` | unary LLM call: retry (jittered), Retry-After, budget cap, **POST-never-retry**, cancel, metrics | `llm.py:410,432,481`; `lancelot_model_io.md §1.1` |
| `TransportPool.stream()` + `relay()` | streaming LLM call w/ `read=None` timeout + client-disconnect→upstream-cancel; relay for inference-proxy | `llm.py:588,686`; `lancelot_model_io.md §1.2` |
| `TransportPool.fanout(first_n, deadline, cancel_laggards)` | the **panel-of-models** primitive — budget panel beats solo frontier at ~half price (`bad_guide_build_positioning.md §6`); first-N-wins, completion order | `llm.py:800`; `lancelot_model_io.md §1.7` |
| `TransportPool.spawn_child(parent, session_affinity)` | sub-agent dispatch w/ parent header snapshot (cost DOES accumulate to parent — aux client is the opposite case, §1.4) | `llm.py:906`; `lancelot_model_io.md §1.8` |
| `upstream_metrics.classify_status` | the failover branch selector (§1.3) — `ok/quota/timeout/permanent/transient/upstream_5xx` | `upstream_metrics.py:156` |
| `upstream_metrics.extract_tokens` / `calc_cost_usd` | token + cost across OpenAI/Anthropic/Google shapes; env rate table (ships NO rates) | `upstream_metrics.py:108,132` |
| `upstream_metrics.record_upstream` + 6 OTel histograms | `lancelot.upstream.{requests,duration,ttft,tokens.*,cost.usd}` — no new metrics needed | `upstream_metrics.py:235` |
| `context.{cost_usd,tokens_in,tokens_out,agent_id,tenant_id,trace_id}` | cost/trace plumbing; `X-Lancelot-Cost-USD` trailer; auto traceparent injection | `context.py`; `lancelot_model_io.md §3` |
| `canonical.dumps_canonical` | KV-cache prefix stability — the #1 free cost lever (`bad_guide_build_positioning.md §4`); use as `sha256(dumps_canonical({model,messages,tools}))` cache key | `canonical.py:28`; `lancelot_model_io.md §4` |
| `batch (@app.batch, size_fn/max_size)` | HTTP-layer request coalescing (~12×/~16×) for multi-user inference endpoints; token-budget flush trigger | `batch.py`; `lancelot_model_io.md §5` |
| `Response.set_session_affinity(key)` | cache-sticky routing — "cache-aware LB, not available out-of-box"; Lancelot HAS it (`gateways_more.md §3.1`) | `lancelot_model_io.md`; `gateways_more.md §3` |
| `net` SSRF guard / `secrets` (5 backends + SIGHUP) / mTLS (`reload_mtls`) | private-model-server access; cred rotation; per-request `custom_host` SSRF defense | `lancelot_model_io.md §1.10`; `gateways_routing.md §7` |
| `tool (@app.tool → ToolSpec + output_schema)` | tool declaration + auto JSON schema; the adapter wraps its schema in `sanitize_tool_schema` (§1.5) | `tool.py:90` |

**Jitter is already present** (`llm.py:481`) — the gateway notes' "verify TransportPool jitters" flags (`gateways_routing.md §10.4`, `gateways_more.md §7.1`) are resolved CLEAN. Do not add a jitter knob.

---

## 3. WHAT LANCELOT REFUSES (off-thesis / depreciating / overkill)

Grounded refusals, each with the source that condemns it:

1. **`FallbackModel`-as-the-resilience-primitive** (Pydantic AI's try-models-in-order). *"TransportPool already provides retry + backoff + rate-limit + Retry-After — that is the correct resilience layer. Wrapping multiple models is a policy decision, not infrastructure"* (`pydantic_ai.md §15`). The §1.3 failover IS the policy layer; do not duplicate transport-level resilience inside it.

2. **Semantic cache as a model-layer primitive.** *"`dumps_canonical` already covers the simpler + more reliable byte-stable prefix approach. Semantic cache adds a costly embedding call on every miss and has false-positive risks"* (`gateways_routing.md §10.1`). Bifrost's actual default threshold is **0.8, not 0.95** and *"false-positive cache hits are real at this threshold"* (`gateways_more.md §5.1`). Refuse; the byte path's canonical-JSON + native provider KV-cache wins.

3. **A shipped pricing table.** LiteLLM's `model_prices_and_context_window.json` (40K lines, 2303 entries) *"is the de facto pricing source"* but *"a model not in the JSON returns cost=0 silently"* and *"the map lags new model releases"* (`gateways_routing.md §3,§10.3`). `upstream_metrics` already correctly defers to `LANCELOT_LLM_RATES` env JSON and ships NO rates — *"this is correct."* Refuse.

4. **The full 22-reason `FailoverReason` enum.** Hermes's taxonomy (`hermes_core.md §1`) is strictly more precise, but Lancelot's `upstream_metrics` already has a 6-reason taxonomy that the failover walk keys on. **Extend by ~3 actionable splits** at most (`context_overflow` vs `payload_too_large`; `billing`/rotate-now vs `rate_limit`/backoff-first) — do NOT port 22 reasons + the priority pipeline. The brutal version keys failover on the 6 that exist. (The verbatim `_RATE_LIMIT_PATTERNS` message lists ARE worth adopting into `upstream_metrics` for 200-OK-with-rate-limit-body detection — `gateways_more.md §2.1`, 22 Bifrost patterns — but that's an `upstream_metrics` extension, not the adapter.)

5. **Bifrost's per-provider worker-pool/queue architecture.** *"Go channels + goroutines don't translate. Per-provider worker pools would be Python threads (GIL, anti-scales). Bifrost's architecture is a Go architecture"* (`gateways_more.md §12`). Refuse — it contradicts Lancelot's multiprocess-not-threads law.

6. **Subprocess-delegated agent loop** (Claude Agent SDK). *"Single-vendor lock (Anthropic CLI only), binary black box, no multi-provider"* (`claude_agent_sdk.md` anti-pattern 1). The adapter is multi-provider by construction; refuse the subprocess bridge.

7. **Pydantic AI's 9-class toolset wrapping hierarchy** (`FilteredToolset`/`RenamedToolset`/...). *"Implement the 3 useful primitives; skip the class hierarchy"* (`pydantic_ai.md §15`). The model layer needs none of it.

8. **LLM-as-request-transformer** (Kong `ai-request-transformer`). *"Adds 200-1500ms per request. Not a byte-path win; explicitly Tier-D"* (`gateways_more.md §10`). Refuse.

9. **Depreciating per-model constants** — exact beta-header names (`fine-grained-tool-streaming-2025-05-14`), thinking-budget token values (8000/16000/32000), model-substring detection (`"4-7"`, `"opus-4-6"`), Claude 1/2 legacy `Human:/Assistant:` format. *"Copy the architecture, not the model-specific constants"* (`hermes_gateway.md §8`; `gateways_more.md §1.1` flags the Claude-1/2 format as a *"depreciating hack, off-thesis"*). The profile *shape* holds these; they live in the profile body and churn freely.

10. **Brotli / generic streaming compressor** — Tier-D per CLAUDE.md, the proxy's job (`gateways_more.md §10`).

---

## 4. Recommended public API sketch (the shape users touch)

```python
from lancelot.model import init_model, ProviderProfile, OMIT, register_profile

# --- the 90% path: one line ---
model = init_model("anthropic:claude-sonnet-4")
resp = await model.complete(messages=[...], tools=[...])     # ModelResponse(content=[Text|ToolCall|...], finish_reason, usage)
async for part in model.stream(messages=[...]): ...          # rides the existing R3 pump + TTFT metric

# --- failover (error-typed, same-family-first, cache-aware) ---
model = init_model(
    "anthropic:claude-sonnet-4",
    fallback=["anthropic:claude-haiku-4", "google:gemini-flash"],  # hop on quota/timeout/transient/5xx/malformed-tool
)

# --- panel-of-models (reuses TransportPool.fanout) ---
# (the agent layer calls pool.fanout directly; the model layer just supplies translated bodies)

# --- cheap sidebar work, separate cost bucket ---
aux = model.auxiliary()                       # profile.default_aux_model; cost NOT added to context.cost_usd
title = await aux.complete(messages=[summarize_prompt])

# --- escape hatch: provider-native knobs pass through untouched (Vercel providerOptions pattern) ---
resp = await model.complete(messages=[...], provider_options={"anthropic": {"cache_control": {"type": "ephemeral"}}})

# --- adding a provider is ~5 lines for OpenAI-compatible ---
register_profile(ProviderProfile(name="groq", api_mode="chat_completions",
                                 base_url="https://api.groq.com/openai/v1",
                                 env_vars=("GROQ_API_KEY",), default_aux_model="llama-3.1-8b-instant"))
```

---

## 5. Exact default constants (cited)

| Constant | Value | Source |
|---|---|---|
| model-string separator | `:` | `vercel_ai_sdk.md §6` (`provider-registry.ts:143`); `langchain.md:31` |
| `fixed_temperature` sentinel | `OMIT = object()` (identity check) | `hermes_gateway.md §1.2` |
| failover hop trigger (classify_status) | `{quota, timeout, transient, upstream_5xx}` + malformed-tool-call | `devin_warp.md §1`; `upstream_metrics.py:156` |
| failover NO-hop (abort) | `permanent` non-tool (400/401/403), `model_not_found` (404) | `gateways_more.md §2.1`; `hermes_core.md §1` |
| failover order | **same-family first, then cross-provider** (cache preservation) | `devin_warp.md:328` (Devin `§R4.3`) |
| failover error returned on total failure | the **PRIMARY** error, not last fallback's | `gateways_more.md §2.1` |
| cross-model failover hit rate (sizing) | ~2% of runs | `devin_warp.md:295` |
| `api_mode` families to ship | `chat_completions` (default) / `anthropic_messages` / `gemini_generate` | `hermes_gateway.md §1.1` |
| aux-client default temp (schema/sidebar tasks) | `0.0` | `nia.md:302` |
| reasoning-effort dial (DURABLE shape, DEPRECATING values) | `none/minimal/low/medium/high/xhigh` → budget tokens — **do not hardcode the token map** | `gateways_routing.md §1` (LiteLLM `REASONING_EFFORT_TO_BUDGET`); `hermes_gateway.md §8` |
| Anthropic temperature bound | clamp to `[0,1]` not `[0,2]` (profile-level) | `gateways_routing.md §1.7` |
| tool-name sanitize regex (Anthropic) | `^[a-zA-Z0-9_-]{1,128}$` (forward+reverse map) | `gateways_routing.md §1.2` |
| per-stream timeout for reasoning models | ~300s (5 min), distinct from drain timeout | `gateways_more.md §11` (Nexos); `gateways_routing.md` |

---

## 6. Open questions for Lancelot (decisions, not blockers)

1. **`api_mode` count at v1.** Brief recommends 3 families (`chat_completions`/`anthropic_messages`/`gemini_generate`). Is Anthropic-native enough at launch, or is OpenAI-compatible-only (everyone speaks it via a shim) the truly-brutal v0? Lean: ship OpenAI-compat + Anthropic-native (the two shapes that actually differ on the wire); add Gemini-native when a user needs it.
2. **Where the `ModelResponse` ordered-parts type lives** — `lancelot.model` (owned here) vs shared with the agent-loop layer. It's the seam between this layer and the loop; recommend defining it here and having the loop import it (single source of truth).
3. **`require_parameters` depth.** Brief recommends a boolean `supports_tools` gate (anti-overkill). If a real model *catalog* lands later (live `/models` fetch — `hermes_gateway.md §2.5`), upgrade to a capability set. Not before.
4. **Aux-client cost label.** Confirm `upstream_metrics` can carry a distinct cost-attribution label for aux calls without a new ContextVar (it has `llm.class`/model labels — likely sufficient; verify at build time).
5. **Streaming translation boundary.** This brief deliberately stops at `ModelResponse` (unary + a thin stream wrapper) and leaves the rich provider-stream-part union (Vercel's 16 parts) + first-chunk-error-peek (`gateways_more.md §2.3`, Anthropic 200-OK-with-error-SSE) to the streaming/agent layer that owns the R3 pump. Confirm that division with the stream-layer synthesizer.

---

*Sources: `03_teardowns/{hermes_gateway,hermes_core,gateways_routing,gateways_more,devin_warp,vercel_ai_sdk,pydantic_ai,openai_agents,google_adk,langchain,strands,agno,nia,evals_obs,claude_agent_sdk}.md`; `02_core/{transcripts_yc_root,bad_guide_build_positioning}.md`; `04_byte_path/lancelot_model_io.md`; verified Lancelot source `lancelot/{llm,upstream_metrics,context,canonical,tool}.py`.*
