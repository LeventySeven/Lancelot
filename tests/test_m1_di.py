import typing

from lancelot.params import Depends, Query
from lancelot.di import compile_dependant


def dep_a():
    return "a"


def test_compile_dependant_builds_plan_at_registration():
    def handler(x: int, y: typing.Annotated[str, Query()] = "d",
                a: str = Depends(dep_a)):
        ...

    plan = compile_dependant(handler)
    assert plan.handler is handler
    names = {f.name for f in plan.fields}
    assert names == {"x", "y"}  # `a` is a dependency, not a field
    assert [d.call for d in plan.dependencies] == [dep_a]
    assert plan.field("x").required is True
    assert plan.field("y").required is False


def _req(query=None):
    from lancelot.requests import Request

    return Request(method="GET", path="/", query_params=query or {}, headers={},
                   cookies={}, path_params={}, body=b"", app=None)


def test_solve_binds_params_and_one_level_depends():
    from lancelot.di import solve

    def dep():
        return 99

    def handler(x: int, a: int = Depends(dep)):
        return x + a

    plan = compile_dependant(handler)
    res = solve(plan, _req(query={"x": "1"}))
    assert res.errors == []
    assert res.kwargs == {"x": 1, "a": 99}


def test_solve_missing_required_is_422_error():
    from lancelot.di import solve

    def handler(x: int):
        ...

    plan = compile_dependant(handler)
    res = solve(plan, _req(query={}))
    assert res.kwargs is None
    assert res.errors == [{"type": "missing", "loc": ["query", "x"],
                           "msg": "Field required", "input": None}]
