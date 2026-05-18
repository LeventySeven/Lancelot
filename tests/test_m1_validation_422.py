import json

from lancelot.params import extract_field, Required
from lancelot.validation import compile_validator, errors_to_422


def test_missing_scalar_query_is_byte_exact():
    fi = extract_field("query", int, Required)
    v = compile_validator(fi)
    value, errs = v.validate(None, present=False)
    assert errs == [{"type": "missing", "loc": ["query", "query"],
                     "msg": "Field required", "input": None}]


def test_int_parsing_query_is_byte_exact():
    fi = extract_field("query", int, Required)
    v = compile_validator(fi)
    value, errs = v.validate("42.5", present=True)
    assert errs == [{"type": "int_parsing", "loc": ["query", "query"],
                     "msg": "Input should be a valid integer, unable to parse string as an integer",
                     "input": "42.5"}]


def test_envelope_is_detail_list_422():
    status, body = errors_to_422([{"type": "missing", "loc": ["query", "q"],
                                   "msg": "Field required", "input": None}])
    assert status == 422
    assert json.loads(body) == {"detail": [{"type": "missing", "loc": ["query", "q"],
                                            "msg": "Field required", "input": None}]}
    assert b'"url"' not in body  # MUST NOT emit url (deep/01 §0.2)
