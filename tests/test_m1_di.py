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
