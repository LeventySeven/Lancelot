"""M2-P3 — auto OTel GenAI telemetry. Every `lancelot.llm` call emits
a `gen_ai.cost` event with token breakdown + USD cost (per the OTel
GenAI semconv: `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`,
`gen_ai.usage.cached_input_tokens`, `gen_ai.response.model`,
`gen_ai.cost.usd`).

Lancelot does NOT depend on opentelemetry-api at runtime. Users plug an
OTel exporter (or Langfuse/Phoenix/Helicone) into the sink themselves
via `set_sink(callable)`. Default sink = None (no-op, zero perf cost).

The price table is conservative: only well-published model prices.
Unknown models => `gen_ai.cost.usd = None` (NEVER invent a price)."""
from dataclasses import dataclass

# USD per 1M tokens (input, cached_input, output). cached_input price is
# typically half of input (Anthropic prompt-caching read; OpenAI cached).
# Numbers are deliberately conservative + dated; users override via the
# price table if they want exact billing.
PRICE_TABLE: dict[str, tuple[float, float, float]] = {
    # OpenAI
    "gpt-4o": (2.5, 1.25, 10.0),
    "gpt-4o-mini": (0.15, 0.075, 0.6),
    "gpt-4o-mini-2024-07-18": (0.15, 0.075, 0.6),
    "gpt-4-turbo": (10.0, 5.0, 30.0),
    "o1": (15.0, 7.5, 60.0),
    "o1-mini": (3.0, 1.5, 12.0),
    # Anthropic (per 1M tokens)
    "claude-3-5-sonnet-20241022": (3.0, 0.3, 15.0),
    "claude-3-5-haiku-20241022": (0.8, 0.08, 4.0),
    "claude-3-opus-20240229": (15.0, 1.5, 75.0),
    # Common Bedrock / Groq aliases
    "llama-3.1-70b-instruct": (0.88, 0.88, 0.88),
    "llama-3.1-8b-instruct": (0.05, 0.05, 0.08),
}


@dataclass
class Event:
    kind: str               # "gen_ai.cost"  | future: "gen_ai.span"
    attrs: dict             # OTel-shape attributes


_SINK = None  # default no-op (zero perf cost)


def set_sink(sink):
    """Plug a callable(Event) → None. Pass None to disable."""
    global _SINK
    _SINK = sink


def get_sink():
    return _SINK


def emit(event: Event) -> None:
    """Lancelot internal: fire the event to the sink if set."""
    if _SINK is not None:
        try:
            _SINK(event)
        except Exception:  # noqa: BLE001 - never let telemetry break a request
            pass


def _lookup_price(model: str | None) -> tuple[float, float, float] | None:
    if not model:
        return None
    if model in PRICE_TABLE:
        return PRICE_TABLE[model]
    # Fall back to a known prefix (date-suffixed variants of the same model
    # share the same price): try splitting on the LAST '-' iteratively.
    parts = model.split("-")
    for n in range(len(parts) - 1, 0, -1):
        candidate = "-".join(parts[:n])
        if candidate in PRICE_TABLE:
            return PRICE_TABLE[candidate]
    return None


def cost_event_from_openai_response(response_json: dict) -> Event:
    """Parse the OpenAI/Anthropic-shape `usage` block into a
    `gen_ai.cost` event. Sees both OpenAI's
    `usage.prompt_tokens_details.cached_tokens` and Anthropic's flat
    `cache_creation_input_tokens` / `cache_read_input_tokens`."""
    model = response_json.get("model")
    usage = response_json.get("usage") or {}
    in_toks = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
    out_toks = (usage.get("completion_tokens")
                or usage.get("output_tokens") or 0)
    cached = ((usage.get("prompt_tokens_details") or {}).get("cached_tokens")
              or usage.get("cache_read_input_tokens") or 0)
    attrs = {
        "gen_ai.response.model": model,
        "gen_ai.usage.input_tokens": in_toks,
        "gen_ai.usage.output_tokens": out_toks,
        "gen_ai.usage.cached_input_tokens": cached,
    }
    price = _lookup_price(model)
    if price is not None and (in_toks or out_toks):
        in_price, cached_price, out_price = price  # USD per 1M
        attrs["gen_ai.cost.usd"] = (
            (in_toks - cached) * in_price / 1e6
            + cached * cached_price / 1e6
            + out_toks * out_price / 1e6
        )
    else:
        attrs["gen_ai.cost.usd"] = None
    return Event(kind="gen_ai.cost", attrs=attrs)
