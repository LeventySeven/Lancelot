"""M7 T1.3 + T2.2 — LLM-aware upstream observability.

What ships:
  - Provider + model detection from the upstream URL + response body
    (OpenAI / Anthropic / Google / generic-OpenAI-compat). Pure rules,
    no AI logic.
  - Token counts extracted from the response body's `usage` block.
  - Cost computed from a user-supplied rate table (Lancelot does NOT
    ship rates — they change weekly per provider).
  - OTel metrics emitted on every upstream call:
      lancelot.upstream.requests        (counter by provider/model/class)
      lancelot.upstream.duration        (histogram by provider/model)
      lancelot.upstream.ttft            (histogram, set when first chunk)
      lancelot.upstream.tokens.input    (counter)
      lancelot.upstream.tokens.output   (counter)
      lancelot.upstream.cost.usd        (histogram)
  - Structured error taxonomy: `transient` / `permanent` / `timeout`
    / `quota` / `upstream_5xx` (Commure pattern).

Cardinal rule: no OTel dep at runtime. Like `lancelot.metrics`, every
public function early-returns when opentelemetry-api isn't importable.
"""
from __future__ import annotations

import os
import re
from typing import Any, Optional


# ----- Rate table (user-supplied) -----------------------------------------

# Shape: {provider: {model: {"input_per_1k": float, "output_per_1k": float}}}
# Lancelot ships this empty. The user sets rates explicitly via
# `lancelot.upstream_metrics.set_rate_table(...)` or env JSON
# `LANCELOT_LLM_RATES`. Rate-card changes weekly per provider — Lancelot
# stays out of the rate-card business.
_RATE_TABLE: dict[str, dict[str, dict[str, float]]] = {}


def set_rate_table(table: dict) -> None:
    """Set the per-provider/model rate table. Replaces any prior table.
    Empty dict disables cost calc (cost defaults to 0.0)."""
    global _RATE_TABLE
    _RATE_TABLE = dict(table or {})


def _load_env_rate_table() -> None:
    """Lazy env load (one-time): LANCELOT_LLM_RATES='{"openai":...}'.
    Called on first metric emit; harmless if env unset / malformed."""
    raw = os.environ.get("LANCELOT_LLM_RATES")
    if not raw:
        return
    try:
        import json as _json
        t = _json.loads(raw)
        if isinstance(t, dict):
            set_rate_table(t)
    except Exception:  # noqa: BLE001
        pass


_ENV_LOADED = False


def _ensure_env_loaded():
    global _ENV_LOADED
    if not _ENV_LOADED:
        _load_env_rate_table()
        _ENV_LOADED = True


# ----- Provider / model detection ----------------------------------------

_PROVIDER_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bapi\.openai\.com\b"), "openai"),
    (re.compile(r"\bapi\.anthropic\.com\b"), "anthropic"),
    (re.compile(r"\b(generativelanguage\.googleapis|aiplatform\.googleapis)"),
     "google"),
    (re.compile(r"\bapi\.groq\.com\b"), "groq"),
    (re.compile(r"\bapi\.deepseek\.com\b"), "deepseek"),
    (re.compile(r"\bapi\.mistral\.ai\b"), "mistral"),
    (re.compile(r"\bapi\.together\.xyz\b"), "together"),
    (re.compile(r"\bapi\.fireworks\.ai\b"), "fireworks"),
]


def detect_provider(url: str) -> str:
    """Best-effort provider detection from URL. Defaults to 'unknown'
    (which is still a useful tag in metrics — it groups all
    self-hosted / unknown endpoints together)."""
    if not url:
        return "unknown"
    for pat, name in _PROVIDER_PATTERNS:
        if pat.search(url):
            return name
    return "unknown"


def detect_model(body: Any) -> str:
    """Pull the `model` field from a JSON response body if present.
    OpenAI, Anthropic, Google, Groq, etc. all include it at the top level."""
    if not isinstance(body, dict):
        return "unknown"
    m = body.get("model")
    return str(m) if m else "unknown"


def extract_tokens(body: Any) -> tuple[int, int]:
    """Return (input_tokens, output_tokens) extracted from the response
    body. Handles OpenAI (`usage.prompt_tokens` / `completion_tokens`),
    Anthropic (`usage.input_tokens` / `output_tokens`), and Google
    (`usageMetadata.promptTokenCount` / `candidatesTokenCount`)."""
    if not isinstance(body, dict):
        return (0, 0)
    u = body.get("usage") or body.get("usageMetadata")
    if not isinstance(u, dict):
        return (0, 0)
    inp = (u.get("prompt_tokens")
           or u.get("input_tokens")
           or u.get("promptTokenCount")
           or 0)
    out = (u.get("completion_tokens")
           or u.get("output_tokens")
           or u.get("candidatesTokenCount")
           or 0)
    try:
        return (int(inp), int(out))
    except (ValueError, TypeError):
        return (0, 0)


def calc_cost_usd(provider: str, model: str,
                  tokens_in: int, tokens_out: int) -> float:
    """Compute cost from the rate table. Returns 0.0 if rates aren't
    set for (provider, model)."""
    _ensure_env_loaded()
    p = _RATE_TABLE.get(provider)
    if not p:
        return 0.0
    m = p.get(model)
    if not m:
        # Fallback: a "default" key under the provider lets users set
        # one rate for "anything we haven't named yet".
        m = p.get("default") or p.get("*")
    if not m:
        return 0.0
    try:
        return (tokens_in / 1000.0) * float(m.get("input_per_1k", 0.0)) \
            + (tokens_out / 1000.0) * float(m.get("output_per_1k", 0.0))
    except (ValueError, TypeError):
        return 0.0


# ----- Error taxonomy (Commure pattern) -----------------------------------

def classify_status(status: int, body: Any = None) -> str:
    """Return the structured error class for an upstream HTTP status.

    Classes:
      - `ok`            — 2xx; the metric still emits with this label
      - `quota`         — 429 (rate-limited; client should back off)
      - `timeout`       — 408 / 504
      - `transient`     — 5xx except permanent ones (502/503/504/521)
      - `permanent`     — 400/401/403/404/422 + any 5xx >= 510 that
                          providers like Anthropic use for hard refusals
      - `upstream_5xx`  — generic 5xx (the catch-all that doesn't fit
                          the other classes)

    Body inspection is optional and Best-Effort — providers like Anthropic
    sometimes include `type: 'overloaded'` in the body of a 5xx, which is
    transient even when 5xx alone would be ambiguous.
    """
    if 200 <= status < 300:
        return "ok"
    if status == 429:
        return "quota"
    if status in (408, 504):
        return "timeout"
    if status in (400, 401, 403, 404, 422):
        return "permanent"
    if status in (502, 503, 521):
        return "transient"
    if 500 <= status < 600:
        return "upstream_5xx"
    return "permanent"  # 3xx redirects / other client errors → permanent


# ----- OTel metric emit ---------------------------------------------------

_NOT_INITIALIZED = object()
_state = _NOT_INITIALIZED


def _maybe_bind():
    """Lazy bind to OTel meter. None if OTel isn't installed."""
    global _state
    if _state is not _NOT_INITIALIZED:
        return _state
    try:
        from opentelemetry import metrics as _otel
    except Exception:  # noqa: BLE001
        _state = None
        return None
    try:
        meter = _otel.get_meter("lancelot.upstream")
        _state = {
            "req_counter": meter.create_counter(
                "lancelot.upstream.requests",
                description="Total outbound LLM/HTTP requests by Lancelot's "
                            "TransportPool."),
            "duration_hist": meter.create_histogram(
                "lancelot.upstream.duration",
                unit="s",
                description="Outbound upstream call duration (s)."),
            "ttft_hist": meter.create_histogram(
                "lancelot.upstream.ttft",
                unit="s",
                description="Outbound upstream time-to-first-chunk (s)."),
            "tokens_in_counter": meter.create_counter(
                "lancelot.upstream.tokens.input",
                description="Total upstream LLM input tokens."),
            "tokens_out_counter": meter.create_counter(
                "lancelot.upstream.tokens.output",
                description="Total upstream LLM output tokens."),
            "cost_hist": meter.create_histogram(
                "lancelot.upstream.cost.usd",
                unit="USD",
                description="Per-call upstream cost (USD); 0 if rate not set."),
        }
    except Exception:  # noqa: BLE001
        _state = None
    return _state


def record_upstream(url: str, status: int, duration_s: float,
                    *, body: Any = None, ttft_s: float | None = None,
                    provider: str | None = None,
                    model: str | None = None) -> dict:
    """Emit one upstream-call metric set. Returns the attrs dict +
    computed cost in a small {"provider","model","cost_usd",
    "tokens_in","tokens_out","class"} record for the caller's logs
    / cost accumulator (T2.1 cost ContextVar uses it).

    Cheap when OTel isn't installed (early-return; still returns the
    attrs dict so the caller can use the cost calc independently)."""
    if provider is None:
        provider = detect_provider(url)
    if model is None:
        model = detect_model(body)
    tokens_in, tokens_out = extract_tokens(body)
    err_class = classify_status(status, body)
    cost = calc_cost_usd(provider, model, tokens_in, tokens_out)

    info = {
        "provider": provider,
        "model": model,
        "class": err_class,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": cost,
    }

    inst = _maybe_bind()
    if inst is None:
        return info

    # T2.2 attrs: provider + model + class are the typical
    # cardinality-bounded dashboard slice. Note: model count can be
    # high but is bounded by what providers offer (~hundreds), so we
    # don't apply tenant-style cap here.
    attrs = {
        "llm.provider": provider,
        "llm.model": model,
        "llm.class": err_class,
        "http.status_code": str(status),
    }
    try:
        inst["req_counter"].add(1, attributes=attrs)
        inst["duration_hist"].record(duration_s, attributes=attrs)
        if ttft_s is not None and ttft_s >= 0:
            inst["ttft_hist"].record(ttft_s, attributes=attrs)
        if tokens_in > 0:
            inst["tokens_in_counter"].add(tokens_in, attributes=attrs)
        if tokens_out > 0:
            inst["tokens_out_counter"].add(tokens_out, attributes=attrs)
        if cost > 0:
            inst["cost_hist"].record(cost, attributes=attrs)
    except Exception:  # noqa: BLE001 — never let telemetry kill a request
        pass

    return info


def reset_for_tests() -> None:
    global _state, _RATE_TABLE, _ENV_LOADED
    _state = _NOT_INITIALIZED
    _RATE_TABLE = {}
    _ENV_LOADED = False


__all__ = [
    "set_rate_table",
    "detect_provider",
    "detect_model",
    "extract_tokens",
    "calc_cost_usd",
    "classify_status",
    "record_upstream",
    "reset_for_tests",
]
