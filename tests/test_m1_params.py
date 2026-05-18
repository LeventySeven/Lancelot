import typing

from lancelot.params import Query, Path, Depends, Default, Required, extract_field


def test_markers_and_annotated_extraction():
    q = Query(default=5, ge=1)
    assert q.default == 5 and q.constraints == {"ge": 1} and q.in_ == "query"
    assert Path().default is Required  # Path has no default => required
    d = Depends(lambda: 1)
    assert callable(d.dependency)

    def handler(a: int, b: typing.Annotated[int, Query(default=2)], c: str = "x"):
        ...

    sig = typing.get_type_hints(handler, include_extras=True)
    fa = extract_field("a", int, Required)
    assert fa.annotation is int and fa.required is True and fa.marker.in_ == "query"
    fb = extract_field("b", sig["b"], Required)
    assert fb.required is False and fb.default == 2 and fb.marker.in_ == "query"
    fc = extract_field("c", str, "x")
    assert fc.required is False and fc.default == "x"
