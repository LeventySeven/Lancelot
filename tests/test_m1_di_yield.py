import asyncio

from lancelot.params import Depends
from lancelot.di import compile_dependant, solve_async
from lancelot.requests import Request


def test_yield_teardown_after_response_and_reverse_order():
    events = []

    def outer():
        events.append("outer-setup")
        try:
            yield "O"
        finally:
            events.append("outer-teardown")

    def inner(o: str = Depends(outer)):
        events.append("inner-setup")
        try:
            yield o + "I"
        finally:
            events.append("inner-teardown")

    def handler(v: str = Depends(inner)):
        events.append("handler")
        return v

    async def go():
        plan = compile_dependant(handler)
        req = Request("GET", "/", {}, {}, {}, {}, b"", None)
        async with solve_async(plan, req) as res:
            assert res.kwargs == {"v": "OI"}
            handler(**res.kwargs)
            events.append("response-sent")

    asyncio.new_event_loop().run_until_complete(go())
    assert events == ["outer-setup", "inner-setup", "handler",
                      "response-sent", "inner-teardown", "outer-teardown"]


def test_handler_exception_propagates_into_yield_dep_and_500s():
    """B4 (DoD #4): a raising handler must throw the exception INTO the
    yield-dep generator (so `except:`/`finally:` rollback runs) and the
    request must 500, not crash/200."""
    import lancelot
    from lancelot.testclient import TestClient
    from lancelot.params import Depends

    seen = []

    def db():
        try:
            yield "session"
        except RuntimeError:
            seen.append("rolled-back")
            raise
        finally:
            seen.append("closed")

    app = lancelot.App()

    @app.get("/boom")
    def boom(s: str = Depends(db)):
        raise RuntimeError("handler blew up")

    with TestClient(app) as c:
        r = c.get("/boom")
    assert r.status_code == 500
    assert "rolled-back" in seen  # exception WAS thrown into the generator
    assert seen[-1] == "closed"   # finally still ran
