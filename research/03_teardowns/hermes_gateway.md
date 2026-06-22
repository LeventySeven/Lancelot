# Hermes Provider/Gateway Layer — Mechanism Teardown

**Sources:** `HERMES_DEEP_PROVIDERS.md` (ProviderProfile, four adapters, tool-call parsers, reasoning per provider, credential pool), `HERMES_DEEP_GATEWAY.md` (PlatformAdapter ABC, GatewayRunner dispatch, session keying), `HERMES_DEEP_ACP.md` (ACP IDE stdio adapter). Repo HEAD `8191f66`.

**Focus per brief:** ProviderProfile dataclass + 5 polymorphic hooks + OMIT_TEMPERATURE sentinel. The model-adapter shape that sits ON Lancelot TransportPool.

---

## §1 — ProviderProfile: the declarative model-adapter shape

**Source:** `providers/base.py:38–184` (184-line file, completely captured).

The prior art: Hermes had hardcoded `if provider == 'kimi'` branches inside `run_agent.py` and `agent/auxiliary_client.py`. Replaced by a single dataclass that every transport reads. Architectural thesis: **N boolean flags from the caller → one profile object that knows every quirk inline**.

### 1.1 Verbatim dataclass surface

```python
@dataclass
class ProviderProfile:
    # Identity
    name: str
    api_mode: str = "chat_completions"      # selects which of 4 adapter families handles the request
    aliases: tuple = ()

    # Human-readable (UX strings, functionally inert)
    display_name: str = ""
    description: str = ""
    signup_url: str = ""

    # Auth & endpoints
    env_vars: tuple = ()                    # env var names for API key, in priority order
    base_url: str = ""                      # empty = user-supplied at setup time
    models_url: str = ""                    # explicit catalog endpoint; falls back to {base_url}/models
    auth_type: str = "api_key"              # api_key|oauth_device_code|oauth_external|copilot|aws_sdk|external_process
    supports_health_check: bool = True      # False → health-check skips /models probe

    # Model catalog
    fallback_models: tuple = ()             # picker fallback when live /models fetch fails; tool-call-capable only
    hostname: str = ""                      # base hostname for URL→provider reverse-mapping

    # Client-level quirks (set once at client construction)
    default_headers: dict[str, str] = field(default_factory=dict)   # attribution/WAF-bypass headers

    # Request-level quirks
    fixed_temperature: Any = None           # None=use caller's default, OMIT_TEMPERATURE=strip field, float=override
    default_max_tokens: int | None = None   # provider-specific output cap when caller doesn't specify
    default_aux_model: str = ""             # cheap model for auxiliary tasks (compression, vision, title gen)
```

**`api_mode`** selects the entire wire-format adapter family: `chat_completions` (23/30 providers), `anthropic_messages` (4), `codex_responses` (2), `bedrock_converse` (1). Adding a new OpenAI-compatible provider is 5 lines: just provide identity, `base_url`, and `env_vars`. `providers/base.py:58`.

**`env_vars`** is an ordered tuple of all env names that could carry the API key — captures organic naming drift. ZAI uses `("GLM_API_KEY", "ZAI_API_KEY", "Z_AI_API_KEY")` because the provider changed its name twice. `zai/__init__.py:9`.

**`default_aux_model`** = the cheap model the agent uses for sidebar tasks when the main model is expensive. Every provider with a model family provides one: Anthropic → `"claude-haiku-4-5-20251001"`, Gemini → `"gemini-3-flash-preview"`, Kimi → `"kimi-k2-turbo-preview"`. `anthropic/__init__.py:49`, `gemini/__init__.py:59`, `kimi-coding/__init__.py:54`.

**`supports_health_check: bool`** — the ONLY profile with `False` is Xiaomi MiMo: `/v1/models` returns 401 even with a valid key, so the doctor/health-check probe skips it. This is a server-side discovered quirk encoded as a declarative flag. `xiaomi/__init__.py:11`.

### 1.2 OMIT_TEMPERATURE sentinel

```python
# providers/base.py:21
OMIT_TEMPERATURE = object()
```

Three-state `fixed_temperature`:
- `None` → use caller's `temperature` (or omit if caller didn't supply one)
- `OMIT_TEMPERATURE` → `kwargs.pop("temperature", None)` unconditionally — the server manages temperature internally
- numeric float → override caller's value

Object identity check: `if profile.fixed_temperature is OMIT_TEMPERATURE: kwargs.pop("temperature", None)`. Kimi uses this because their server errors on any client-supplied temperature. `kimi-coding/__init__.py:53`, docstring at `providers/base.py:73`.

This sentinel is **architecturally critical** because it disambiguates "not set" from "actively omit". Standard Python sentinel pattern — `object()` means the singleton survives aliasing.

---

## §2 — The five polymorphic hooks

All five are methods on `ProviderProfile`. Default implementations handle the common case; plugins override only what they need. The design thesis: "The hardcoded path required N branches for each new provider; the dataclass requires writing one method." `providers/base.py:119–128`.

### 2.1 `get_hostname() → str` (`providers/base.py:82–93`)

Derives hostname from `base_url` via `urlparse` when `self.hostname` is empty. Used by `agent/model_metadata.py` for URL→provider reverse mapping. No plugin overrides — the default suffices everywhere.

### 2.2 `prepare_messages(messages)` (`providers/base.py:95–101`)

Default: pass-through. Only Qwen overrides this.

**Qwen override** (`qwen-oauth/__init__.py:13–52`):
1. Normalizes message content from bare string to list-of-dicts (Qwen rejects plain strings)
2. Injects `cache_control: {"type": "ephemeral"}` on the **last part of the system message** → pins Qwen's prompt cache to the system prompt

Called "AFTER codex field sanitization, BEFORE developer role swap" per the docstring at line 97. This is the only hook that fires mid-pipeline, not at request-construction time.

### 2.3 `build_extra_body(*, session_id=None, **context) → dict` (`providers/base.py:103–110`)

Default: empty dict. Injects provider-specific blobs into `extra_body`. Live examples:

- **Nous** (`nous/__init__.py:14–16`): always returns `{"tags": nous_portal_tags()}` — product-line metric tags
- **Qwen** (`qwen-oauth/__init__.py:54–57`): always returns `{"vl_high_resolution_images": True}` — Qwen vision models reject high-res images without this flag
- **OpenRouter** (`openrouter/__init__.py:42–66`): two distinct features:
  - Provider preferences passthrough: `body["provider"] = prefs` when `provider_preferences` in context (controls `providers_allowed`, `providers_ignored`, `providers_order`, etc.)
  - Pareto Code router: `body["plugins"] = [{"id": "pareto-router", "min_coding_score": score_f}]` ONLY for `openrouter/pareto-code` model. Score is float, clamped `[0.0, 1.0]`
- **Gemini** (`gemini/__init__.py:22–49`): translates `reasoning_config` to `thinking_config`. The OpenAI-compat /openai subpath gets double-nested: `{"extra_body": {"google": {"thinking_config": ...}}}` because the OpenAI SDK passes `extra_body` to body root AND Google's `/openai` strips one nesting level

### 2.4 `build_api_kwargs_extras(*, reasoning_config=None, **context) → Tuple[dict, dict]` (`providers/base.py:112–130`)

Returns `(extra_body_additions, top_level_kwargs)`. The transport merges the first into `extra_body` and the second directly into `api_kwargs`.

**This split exists specifically for reasoning controls.** Different providers put reasoning config in different places — see the docstring at lines 119–128:

> "This split exists because some providers put reasoning config in extra_body (OpenRouter: extra_body.reasoning) while others put it as top-level api_kwargs (Kimi: api_kwargs.reasoning_effort)."

This is the **architectural reason the ProviderProfile dataclass exists at all.** Without this split, every new provider would require a new conditional branch in the transport.

Concrete reasoning wire shapes per provider:

| Provider | extra_body path | top_level path | Notes |
|---|---|---|---|
| OpenRouter | `extra_body.reasoning` (full dict) | — | full passthrough; defaults to `{enabled:True, effort:"medium"}` |
| Kimi | `extra_body.thinking.type` | `reasoning_effort` | asymmetric dual location |
| DeepSeek V4 | `extra_body.thinking.type` | `reasoning_effort` | FORCES `{type:"enabled"}` even without request (V4 bug fix — see §4) |
| Nous | `extra_body.reasoning` | — | OMITS the field entirely when disabled (vs sending `{enabled:False}`) |
| AI Gateway | `extra_body.reasoning` | — | same as OpenRouter |
| Copilot | `extra_body.reasoning.effort` | — | per-model gate via `github_model_reasoning_efforts(model)` |
| Ollama | — | `extra_body.think` (bool) | `think=False` to disable on Ollama |

**xAI Grok prompt-cache pinning** (OpenRouter hook, `openrouter/__init__.py:78–81`): when routing Grok models via OpenRouter (`model.startswith(("x-ai/grok-", "xai/grok-"))`), injects `extra_headers["x-grok-conv-id"] = session_id`. This pins xAI's per-backend-instance KV cache to the same backend server across turns — a discovered server-side behaviour.

### 2.5 `fetch_models(*, api_key=None, timeout=8.0) → list[str] | None` (`providers/base.py:132–184`)

Default: REST GET to `models_url` or `{base_url}/models`, parses `{"data": [{"id": ...}]}` or plain list, carries `self.default_headers` so per-client headers reach the catalog probe. Returns `None` on failure → callers fall back to `_PROVIDER_MODELS` static list.

Overrides:
- **Anthropic**: uses `x-api-key` + `anthropic-version: 2023-06-01` (Anthropic doesn't speak Bearer auth on REST endpoints)
- **Bedrock**: returns `None` always — model listing is via boto3, not REST
- **OpenRouter**: module-level `_CACHE` since catalog is 200+ models and immutable per-session. The actual picker uses `fetch_openrouter_models()` (a separate path) for tool-call capability filtering
- **Copilot-ACP**: returns `None` — models come from the ACP subprocess
- **Custom/Ollama**: returns `None` if `base_url` is empty; otherwise calls super

---

## §3 — Plugin discovery (three-pass, last-writer-wins)

**Source:** `providers/__init__.py:140–191`.

Lazy on first call to `get_provider_profile()`. Three passes in fixed order:

1. **Bundled plugins** at `<repo>/plugins/model-providers/<name>/` — sorted deterministically, `_` and `.` dirs skipped
2. **User plugins** at `$HERMES_HOME/plugins/model-providers/<name>/` — same shape, overrides bundled
3. **Legacy per-file** at `providers/<name>.py` via `pkgutil.iter_modules` — back-compat layer

Last-writer-wins: second `register_provider()` with same `name` silently replaces the first. A user plugin at `$HERMES_HOME/plugins/model-providers/anthropic/` with the same `name` replaces the bundled Anthropic profile with no repo edits.

**Import mechanics:** `submodule_search_locations=[str(plugin_dir)]` makes each plugin dir a proper Python package. Hyphen-to-underscore in module names (`openai-codex` → `plugins.model_providers.openai_codex`). Failure isolation: broken plugin logs WARN, is removed from `sys.modules`, does not crash the registry.

**Cold-start cost:** all plugins are imported lazily on first `get_provider_profile()` call. Each plugin file is 12–115 lines; only imports what it needs at method level.

---

## §4 — Production quirks worth internalizing

These are server-side discovered behaviours, each of which required a specific line of code or an explicit dataclass flag to handle.

### 4.1 DeepSeek V4 thinking trap

DeepSeek V4 defaults to thinking-ON when `extra_body.thinking` is unset. Once the model returns `reasoning_content`, ALL subsequent assistant tool-call messages must echo it back or the API returns HTTP 400 `reasoning_content must be passed back`. Hermes forces explicit `{"type": "enabled"}` or `{"type": "disabled"}` to get deterministic behaviour. `deepseek/__init__.py:1–18, 60–62`.

The `reasoning_content` pad in `chat_completion_helpers.py:749–761`: `msg["reasoning_content"] = reasoning_text or " "`. The space is deliberate — DeepSeek V4 Pro rejects empty string `""`.

### 4.2 Anthropic thinking signature invariant

Anthropic signs thinking blocks against the full turn content. **Any upstream mutation** (context compression, message merging, orphan stripping, session truncation) invalidates the signature → HTTP 400. Third-party Anthropic-compatible endpoints (MiniMax, Azure, Bedrock, Kimi, DeepSeek/anthropic) cannot validate signatures and reject them outright. Solution: three-way dispatch in `anthropic_adapter.py:1866–1952`:

1. Native Anthropic, latest assistant: preserve signed, downgrade unsigned to text
2. Third-party OR non-latest: strip all thinking blocks
3. Kimi/DeepSeek `/anthropic`: strip signed, preserve unsigned (synthesized from `reasoning_content`)

Adaptive thinking must force `display: "summarized"` on 4.7+ because the new default `display: "omitted"` silently hides reasoning text. `anthropic_adapter.py:2213–2216`.

### 4.3 MiniMax rejects specific Anthropic betas

MiniMax's `/anthropic` endpoint (both intl `api.minimax.io` and China `api.minimaxi.com`) rejects:
- `fine-grained-tool-streaming-2025-05-14` beta header
- `context-1m-2025-08-07` beta header

Every tool-use request fails without stripping these. `anthropic_adapter.py:499–512, 547–567`. And MiniMax requires `Authorization: Bearer` not `x-api-key`. `anthropic_adapter.py:473`.

### 4.4 Codex: deterministic call_id for prompt-cache stability

`_deterministic_call_id(fn_name, arguments, index) = f"call_{sha256(f'{fn_name}:{arguments}:{index}'.encode())[:12]}"`. OpenAI's Responses API caches based on exact request prefix — random UUIDs would defeat the cache on every retry. `codex_responses_adapter.py:143–152`.

### 4.5 Gemini tool-call ID instability

Gemini returns no persistent function-call ID. The adapter mints `call_{uuid4().hex[:12]}` on every response. The conversation history is the only stable reference. IDs are keyed on `(part_index, name, thought_signature)` tuple for parallel call disambiguation. `gemini_native_adapter.py:1999–2006`.

Gemini also emits full `args` JSON on every SSE delta (not incremental). The adapter computes incremental deltas by prefix-stripping: `emitted_arguments = args_str[len(last_arguments):]`. `gemini_native_adapter.py:618–678`.

### 4.6 xAI schema sanitization

xAI's Responses API rejects `pattern` and `format` JSON Schema keywords with HTTP 400. Both must be stripped. Also strips slash-form enum values. Applied in `auxiliary_client.py:710–724` and in the main codex_responses path. Documented as server-side-discovered.

### 4.7 Anthropic tool schema sanitization (two layers)

1. **Nullable unions** (`{"anyOf": [{"type": "string"}, {"type": "null"}]}`): Pydantic and MCP commonly emit these; Anthropic rejects them. `strip_nullable_unions()` collapses to the non-null branch.
2. **Top-level `oneOf`/`allOf`/`anyOf`**: rejected at the schema root. Stripped and replaced with plain object schema.

Tools from MCP servers or Pydantic discriminated unions arrive at the Anthropic API with a **different** schema than what's in the registry. `anthropic_adapter.py:1406–1444`.

### 4.8 Codex encrypted reasoning replay

Codex Responses API returns `encrypted_content` on reasoning items. Must be replayed on every subsequent turn for cross-turn coherence. `include: ["reasoning.encrypted_content"]` must be in every request. Earlier decision to strip encrypted reasoning on xAI was reversed — xAI explicitly relies on cross-turn coherence. `codex_responses_adapter.py:247–264, 287–309`.

### 4.9 `reasoning_content` round-trip for Kimi/DeepSeek/Anthropic protocol

Both Kimi's `/coding` endpoint and DeepSeek's `/anthropic` endpoint require `reasoning_content` on every assistant tool-call message. The adapter synthesizes an unsigned thinking block `{"type": "thinking", "thinking": reasoning_content}` **at position 0** (Anthropic protocol: thinking blocks must precede text/tool_use). If `reasoning_content` is absent, pads with `" "` (single space). `anthropic_adapter.py:1664–1688`, `chat_completion_helpers.py:742–786`.

---

## §5 — Credential pool (`agent/credential_pool.py`, 1999 lines)

A multi-credential pool for same-provider failover. Each provider can have N credentials.

### 5.1 Cooldown constants

```python
EXHAUSTED_TTL_401_SECONDS = 5 * 60     # transient auth failure — recover fast
EXHAUSTED_TTL_429_SECONDS = 60 * 60    # rate limit
EXHAUSTED_TTL_DEFAULT_SECONDS = 60 * 60
```

Provider-supplied `reset_at` timestamps override these. `credential_pool.py:75–82`.

### 5.2 Rotation strategies

`fill_first` (default), `round_robin`, `random`, `least_used`. Selection at `credential_pool.py:1223–1254`. `round_robin` rotates by moving current entry to the end and re-prioritizing everyone + persists on every rotation.

### 5.3 Soft-lease semaphore

`_active_leases: {credential_id: int}` — per-process concurrent-request counter per credential. Selection prefers entries below `_max_concurrent`; falls back to least-leased rather than blocking. `credential_pool.py:1286–1324`.

### 5.4 OAuth refresh token sync

OAuth refresh tokens are single-use. When another process refreshes the token and writes to disk (e.g. Claude Code CLI), the pool detects `file_refresh != entry.refresh_token` and syncs the new token pair from disk. Implemented for three OAuth providers (Anthropic Claude Code, OpenAI Codex, xAI). `credential_pool.py:451–600`.

---

## §6 — Lancelot byte-path implications

**`lancelot.llm.TransportPool`** does transport (HTTP, mTLS, retry, rate limit, traceparent), NOT message-shape translation. The ProviderProfile layer is entirely absent from Lancelot today.

### What to build on top of TransportPool

The ProviderProfile shape maps cleanly onto the Lancelot agent layer:

| ProviderProfile concept | Lancelot status | Notes |
|---|---|---|
| `api_mode` selection | Missing | TransportPool is OpenAI-shaped; no adapter dispatch |
| `OMIT_TEMPERATURE` sentinel | Missing | No message-construction layer |
| `prepare_messages()` hook | Missing | No pre-send message normalization |
| `build_extra_body()` | Missing | Per-request body augmentation not present |
| `build_api_kwargs_extras()` split | Missing | The `(extra_body, top_level)` tuple is key for reasoning |
| `default_max_tokens` enforcement | Partial | TransportPool has no per-provider defaults |
| `default_aux_model` | Missing | Auxiliary/cheap model for side tasks |
| `supports_health_check` | Missing | Lancelot has `/livez`+`/readyz` but no provider health gate |
| `fallback_models` / picker | Missing | No model catalog concept |
| Tool schema sanitization (Anthropic nullable unions, Gemini subset) | Missing | @app.tool generates schemas but no per-provider sanitizer |
| Thinking signature management | Missing | |
| `reasoning_content` round-trip pad | Missing | |
| Credential pool (multi-credential failover) | Partial | TransportPool has token-bucket rate_limit but no multi-credential rotation |

### Copy/adapt recommendations

1. **`ProviderProfile` dataclass + 5 hooks + `OMIT_TEMPERATURE`** — the model-adapter shape itself. ~184 lines. Slots in cleanly as a `profile:` parameter on a `ModelAdapter` class that wraps `TransportPool.request()`. The split `(extra_body, top_level)` return from `build_api_kwargs_extras` is the most important mechanism.

2. **Tool schema sanitizers** — `strip_nullable_unions` (Anthropic) and `sanitize_gemini_schema` (Gemini schema subset) — pure functions, no dependencies, drop in as `lancelot.tool.schema_sanitizer`. Anthropic's sanitizer is needed the moment `@app.tool` schemas reach Anthropic.

3. **DeepSeek V4 thinking trap workaround** — the explicit enable/disable force + the `" "` pad for `reasoning_content`. Discovered bug, not documented by DeepSeek. Copy the pattern verbatim.

4. **Anthropic thinking signature management** — three-way dispatch. Not needed until Lancelot has an agent layer that handles multi-turn tool calls to Anthropic.

5. **Deterministic call_id via SHA-256** — copy `_deterministic_call_id(fn_name, arguments, index)` verbatim; keeps prompt-cache stable on retries.

6. **`EXHAUSTED_TTL` constants** — `401→5min, 429→1h` — durable retry discipline. Lancelot's TransportPool error taxonomy (`ok/quota/timeout/permanent/transient/upstream_5xx`) already aligns; add the per-error cooldown durations to `lancelot.upstream_metrics`.

---

## §7 — What NOT to copy

- **PlatformAdapter ABC + 20 platform adapters** (Telegram, Discord, Slack, WeChat, etc.) — out of scope. Lancelot is a byte-path framework; messaging platform adapters are application-layer concerns.
- **GatewayRunner** — full agent dispatch loop with session keying, auto-TTS, busy-session debounce, cron watchers. This is the gateway orchestrator, not the byte path.
- **ACP stdio adapter** — editor ↔ agent JSON-RPC protocol. IDE integration, not relevant to Lancelot's positioning.
- **Credential pool as implemented** — the rotation/OAuth-refresh machinery is tied to Hermes' CLI authentication flows. Lancelot should offer a simpler `CredentialRotator` (just `fill_first` + `EXHAUSTED_TTL` constants) since it owns the transport, not the auth UI.
- **`_busy_text_debounce_seconds = 0.35`, `_busy_text_hard_cap_seconds = 1.0`** — messaging-platform burst management, not relevant.
- **Session keying** (`build_session_key`) — cross-platform chat session identity. Agent-layer concern, not byte-path.

---

## §8 — Honest assessment

**Durable primitives** (survive 3+ model generations): the `ProviderProfile` dataclass shape, the `OMIT_TEMPERATURE` sentinel pattern, the `(extra_body, top_level)` split for reasoning, tool schema sanitization per-provider, the `reasoning_content` round-trip pad, deterministic call_id for cache stability. These encode structural wire-format contracts that have accumulated over years and will remain relevant as long as LLMs have heterogeneous APIs.

**Depreciating tactical hacks** (churn on ~1-2yr model clock): the exact beta header names (`interleaved-thinking-2025-05-14`, `fine-grained-tool-streaming-2025-05-14`, `fast-mode-2026-02-01`), specific model substring detection strings (`"4-7"`, `"4.6"`, `"opus-4-6"`), the exact `THINKING_BUDGET` token values (8000/16000/32000/64000), Codex CloudFlare cookie machinery. Copy the architecture, not the model-specific constants.

**Not hype:** the ProviderProfile + 5-hook design solves a real production problem. Hermes manages 33 provider profiles across 4 API mode families; without the dataclass, the transport code would have become unmaintainable. The mechanisms are clean, well-commented, and source-verifiable.
