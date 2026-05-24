"""M7 T1.3 + T2.2 — LLM-aware upstream metrics & error taxonomy.

T1.3: TransportPool emits llm.{provider,model,tokens.input,tokens.output,
      cost_usd,first_token_ms} on every outbound LLM call. Cost from a
      user-supplied rate table (Lancelot does NOT ship rates).

T2.2: Structured error class (`transient` / `permanent` / `timeout` /
      `quota` / `upstream_5xx`) per Commure's "payer APIs just work 80%
      of the time" pattern.
"""
import asyncio

import httpx
import pytest

pytest.importorskip("httpx")

from lancelot.llm import TransportPool
from lancelot.upstream_metrics import (
    calc_cost_usd, classify_status, detect_model, detect_provider,
    extract_tokens, record_upstream, reset_for_tests, set_rate_table,
)


# ----- T1.3.a provider detection -----------------------------------------

def test_t1_3_detect_provider_openai():
    assert detect_provider("https://api.openai.com/v1/chat/completions") == "openai"


def test_t1_3_detect_provider_anthropic():
    assert detect_provider("https://api.anthropic.com/v1/messages") == "anthropic"


def test_t1_3_detect_provider_google():
    assert detect_provider(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent"
    ) == "google"


def test_t1_3_detect_provider_unknown_fallback():
    assert detect_provider("https://my-self-hosted-llm.local/api") == "unknown"


# ----- T1.3.b model detection from body ----------------------------------

def test_t1_3_detect_model_from_body():
    assert detect_model({"model": "gpt-4o-mini", "usage": {}}) == "gpt-4o-mini"
    assert detect_model({"model": "claude-3-5-sonnet"}) == "claude-3-5-sonnet"
    assert detect_model({}) == "unknown"
    assert detect_model(None) == "unknown"


# ----- T1.3.c token extraction across providers --------------------------

def test_t1_3_extract_tokens_openai_shape():
    body = {"usage": {"prompt_tokens": 123, "completion_tokens": 45}}
    assert extract_tokens(body) == (123, 45)


def test_t1_3_extract_tokens_anthropic_shape():
    body = {"usage": {"input_tokens": 200, "output_tokens": 80}}
    assert extract_tokens(body) == (200, 80)


def test_t1_3_extract_tokens_google_shape():
    body = {"usageMetadata": {"promptTokenCount": 50,
                              "candidatesTokenCount": 30}}
    assert extract_tokens(body) == (50, 30)


def test_t1_3_extract_tokens_missing_returns_zero():
    assert extract_tokens({}) == (0, 0)
    assert extract_tokens(None) == (0, 0)
    assert extract_tokens({"usage": "string"}) == (0, 0)


# ----- T1.3.d cost calc from rate table ----------------------------------

def test_t1_3_calc_cost_with_table(monkeypatch):
    reset_for_tests()
    set_rate_table({
        "openai": {"gpt-4o-mini": {"input_per_1k": 0.15,
                                    "output_per_1k": 0.60}},
    })
    # 1000 in + 500 out = $0.15 + $0.30 = $0.45
    cost = calc_cost_usd("openai", "gpt-4o-mini", 1000, 500)
    assert abs(cost - 0.45) < 1e-9


def test_t1_3_calc_cost_default_fallback():
    reset_for_tests()
    set_rate_table({
        "openai": {"default": {"input_per_1k": 0.10,
                                "output_per_1k": 0.20}}
    })
    # Unknown model under openai → uses "default" entry.
    cost = calc_cost_usd("openai", "gpt-9000-future", 1000, 1000)
    assert abs(cost - 0.30) < 1e-9


def test_t1_3_calc_cost_no_table_returns_zero():
    reset_for_tests()
    assert calc_cost_usd("openai", "gpt-4o", 1000, 500) == 0.0


# ----- T2.2 error taxonomy -----------------------------------------------

@pytest.mark.parametrize("status,cls", [
    (200, "ok"), (204, "ok"),
    (429, "quota"),
    (408, "timeout"), (504, "timeout"),
    (400, "permanent"), (401, "permanent"), (403, "permanent"),
    (404, "permanent"), (422, "permanent"),
    (502, "transient"), (503, "transient"), (521, "transient"),
    (500, "upstream_5xx"), (505, "upstream_5xx"),
    (301, "permanent"),
])
def test_t2_2_classify_status(status, cls):
    assert classify_status(status) == cls


# ----- T1.3 + T2.2 record_upstream returns the info struct ---------------

def test_t1_3_record_upstream_returns_info():
    """`record_upstream` works even without OTel installed (it just
    returns the computed info struct; metric emit is a no-op then)."""
    reset_for_tests()
    set_rate_table({
        "openai": {"gpt-4o": {"input_per_1k": 1.0, "output_per_1k": 2.0}},
    })
    info = record_upstream(
        "https://api.openai.com/v1/chat/completions",
        status=200, duration_s=0.123,
        body={"model": "gpt-4o",
              "usage": {"prompt_tokens": 1000, "completion_tokens": 500}})
    assert info["provider"] == "openai"
    assert info["model"] == "gpt-4o"
    assert info["class"] == "ok"
    assert info["tokens_in"] == 1000
    assert info["tokens_out"] == 500
    assert abs(info["cost_usd"] - 2.0) < 1e-9  # 1k in × $1 + 500 out × $2


# ----- T1.3 cost accumulates into parent's cost_usd ContextVar -----------

def test_t1_3_cost_accumulates_into_context_var():
    """When TransportPool.request() hits an LLM endpoint with rates set,
    the parent's `cost_usd` ContextVar gets += cost. Sim.ai pattern."""
    from lancelot.context import cost_usd

    reset_for_tests()
    set_rate_table({
        "openai": {"gpt-4o-mini": {"input_per_1k": 0.1,
                                    "output_per_1k": 0.2}}
    })

    def fake(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "model": "gpt-4o-mini",
            "usage": {"prompt_tokens": 1000, "completion_tokens": 500},
        })

    pool = TransportPool(transport=httpx.MockTransport(fake))

    async def run():
        cost_usd.set(0.0)
        await pool.request("POST",
                            "https://api.openai.com/v1/chat/completions")
        await pool.request("POST",
                            "https://api.openai.com/v1/chat/completions")
        await pool.aclose()
        return cost_usd.get()

    final_cost = asyncio.run(run())
    # 2 calls × ($0.10 + $0.10) = $0.40
    assert abs(final_cost - 0.40) < 1e-9
