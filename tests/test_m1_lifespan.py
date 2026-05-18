import contextlib

import pytest

from lancelot.app import App, _run_lifespan_sync


def test_lifespan_order_and_failfast():
    events = []

    @contextlib.asynccontextmanager
    async def ls(app):
        events.append("startup")
        app.state.ready = True
        yield
        events.append("shutdown")

    app = App(lifespan=ls)
    with _run_lifespan_sync(app):
        assert app.state.ready is True
        assert events == ["startup"]
    assert events == ["startup", "shutdown"]

    @contextlib.asynccontextmanager
    async def bad(app):
        raise RuntimeError("boom")
        yield

    app2 = App(lifespan=bad)
    with pytest.raises(RuntimeError, match="boom"):
        with _run_lifespan_sync(app2):
            pass
