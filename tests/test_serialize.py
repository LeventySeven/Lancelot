import lancelot._native as n


def test_to_json_one_pass():
    assert n._to_json({"a": 1, "b": [2, 3]}) == b'{"a":1,"b":[2,3]}'
    assert n._to_json("hi") == b'"hi"'
