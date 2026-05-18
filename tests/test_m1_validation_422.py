import json
import typing

from lancelot.params import extract_field, Required, Path, Header
from lancelot.validation import compile_validator, errors_to_422, collect_errors


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


def test_string_too_short_ctx():
    fi = extract_field("item_id", typing.Annotated[str, Path(min_length=3)], Required)
    _, errs = compile_validator(fi).validate("fo", present=True)
    assert errs == [{"type": "string_too_short", "loc": ["path", "item_id"],
                     "msg": "String should have at least 3 characters",
                     "input": "fo", "ctx": {"min_length": 3}}]


def test_gt_ctx_type_tracks_declared_type():
    fi_f = extract_field("item_id", typing.Annotated[float, Path(gt=3)], Required)
    _, ef = compile_validator(fi_f).validate("2", present=True)
    assert ef[0]["ctx"] == {"gt": 3.0}  # float -> 3.0
    fi_i = extract_field("item_id", typing.Annotated[int, Path(gt=3)], Required)
    _, ei = compile_validator(fi_i).validate("2", present=True)
    assert ei[0]["ctx"] == {"gt": 3}  # int -> 3


def test_errors_accumulate_in_bucket_order():
    q = compile_validator(extract_field("q", int, Required))
    h = compile_validator(extract_field("h", typing.Annotated[int, Header()], Required))
    errs = collect_errors([(q, "x", True), (h, "y", True)])
    assert [e["loc"][0] for e in errs] == ["query", "header"]
    assert len(errs) == 2  # all accumulated, not fail-fast
