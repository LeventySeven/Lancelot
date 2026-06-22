"""M20 Batch 1 — verified correctness/adoption fixes from the byte-path mine.

Two source-verified gaps vs FastAPI (research/bytepath_mine/FINDINGS.md):
  1. Decimal int/float fidelity — `_json_default` always returned float, so a
     whole-value Decimal("2") serialized as 2.0 (silent corruption for money /
     Numeric(_, 0) APIs). FastAPI encoders.py:84 emits int when exponent >= 0.
  2. FileResponse / RedirectResponse were not importable from lancelot.responses
     (FastAPI-migration blocker: `from fastapi.responses import FileResponse`).
"""
import decimal
import json

import pytest


# ---- Fix 1: Decimal int/float fidelity (validation._json_default) ----

def test_decimal_whole_value_encodes_as_int():
    from lancelot.validation import _json_default

    out = _json_default(decimal.Decimal("2"))
    assert out == 2
    assert isinstance(out, int)  # NOT 2.0


def test_decimal_with_fraction_encodes_as_float():
    from lancelot.validation import _json_default

    out = _json_default(decimal.Decimal("2.50"))
    assert out == 2.5
    assert isinstance(out, float)


@pytest.mark.parametrize("raw,expected,typ", [
    ("0", 0, int),
    ("-3", -3, int),
    ("100", 100, int),
    ("1.5", 1.5, float),
    ("-0.25", -0.25, float),
])
def test_decimal_matrix(raw, expected, typ):
    from lancelot.validation import _json_default

    out = _json_default(decimal.Decimal(raw))
    assert out == expected
    assert isinstance(out, typ)


def test_decimal_primary_wire_path_is_precision_preserving_string():
    # DELIBERATE DIVERGENCE from FastAPI (documented, not a bug): the primary
    # wire path is pydantic-core, which serializes Decimal as a precision-
    # preserving JSON *string* ("2"), NOT a number. FastAPI's jsonable_encoder
    # emits a number (and loses precision via float for fractionals). Matching
    # FastAPI here would mean overriding pydantic-core globally — invasive +
    # precision-lossy — so it stays a divergence to revisit deliberately. The
    # _json_default fallback (above) is FastAPI-aligned (int/float) for the rare
    # path pydantic-core/orjson don't own.
    from lancelot.validation import _wire_json

    parsed = json.loads(_wire_json({"amount": decimal.Decimal("2")}))
    assert parsed["amount"] == "2"
    assert isinstance(parsed["amount"], str)


# ---- Fix 2: FileResponse / RedirectResponse re-export (lancelot.responses) ----

def test_file_and_redirect_response_importable():
    from lancelot.responses import FileResponse, RedirectResponse

    assert FileResponse.__name__ == "FileResponse"
    assert RedirectResponse.__name__ == "RedirectResponse"


def test_responses_unknown_attr_still_raises():
    import lancelot.responses as r

    with pytest.raises(AttributeError):
        _ = r.DefinitelyNotAResponse
