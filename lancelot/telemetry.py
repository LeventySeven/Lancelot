"""M2-P3 — auto OTel GenAI telemetry. Every `lancelot.llm` call emits
a `gen_ai.cost` event with token breakdown (per the OTel GenAI semconv:
`gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`,
`gen_ai.usage.cached_input_tokens`, `gen_ai.response.model`).

Lancelot does NOT depend on opentelemetry-api at runtime. Users plug an
OTel exporter (or Langfuse/Phoenix/Helicone) into the sink themselves
via `set_sink(callable)`. Default sink = None (no-op, zero perf cost).

M14: this event's `gen_ai.cost.usd` is always None — Lancelot does NOT
ship rates (they change weekly; a stale hardcoded table silently
diverges from real billing). Cost is computed in ONE place only,
`lancelot.upstream_metrics`, against the rates the USER configures via
`set_rate_table()` / `LANCELOT_LLM_RATES`. The SINK relays tokens+model;
the operator joins them to their own rate sheet."""
from dataclasses import dataclass


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
    # Lancelot never ships rates → cost is None here; computed only in
    # upstream_metrics with the user's configured rates (see module docstring).
    attrs["gen_ai.cost.usd"] = None
    return Event(kind="gen_ai.cost", attrs=attrs)
