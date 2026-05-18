import pydantic

from lancelot.validation import BodyValidator


class Item(pydantic.BaseModel):
    name: str
    age: int


def test_body_single_pass_and_nested_loc():
    v = BodyValidator(list[Item])
    val, errs = v.validate_json(b'[{"age":"five"}]')
    assert {"type": "missing", "loc": ["body", 0, "name"],
            "msg": "Field required", "input": {"age": "five"}} in errs
    assert any(e["type"] == "int_parsing" and e["loc"] == ["body", 0, "age"]
               for e in errs)


def test_malformed_json_is_json_invalid_422():
    v = BodyValidator(Item)
    val, errs = v.validate_json(b'{bad')
    assert errs[0]["type"] == "json_invalid"
    assert errs[0]["loc"][0] == "body"
    assert "ctx" in errs[0] and "error" in errs[0]["ctx"]
