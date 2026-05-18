"""DoD #3 (fail-fast lifespan) on the LIVE code path — the ASGI lifespan
protocol in lancelot.asgi (`_run_lifespan_sync` was removed as dead code:
TestClient subclasses Starlette's, which drives lifespan itself; ordered
startup/shutdown is covered by tests/test_m1_asgi.py)."""
import asyncio
import contextlib

from lancelot import App


def _drive(asgi, msgs):
    sent = []
    inbox = list(msgs)

    async def receive():
        return {"type": inbox.pop(0)}

    async def send(m):
        sent.append(m)

    asyncio.new_event_loop().run_until_complete(
        asgi({"type": "lifespan", "state": {}}, receive, send)
    )
    return sent


def test_startup_exception_is_failfast_not_swallowed():
    @contextlib.asynccontextmanager
    async def bad(app):
        raise RuntimeError("boom")
        yield

    app = App(lifespan=bad)
    sent = _drive(app.asgi(), ["lifespan.startup"])
    assert sent[0]["type"] == "lifespan.startup.failed"
    assert "boom" in sent[0]["message"]


def test_state_mutated_in_startup_visible():
    @contextlib.asynccontextmanager
    async def ls(app):
        app.state.ready = True
        yield

    app = App(lifespan=ls)
    _drive(app.asgi(), ["lifespan.startup", "lifespan.shutdown"])
    assert app.state.ready is True
