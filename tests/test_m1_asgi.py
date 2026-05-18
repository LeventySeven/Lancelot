import asyncio
import contextlib

from lancelot import App


def _drive_lifespan(asgi):
    sent = []
    inbox = ["lifespan.startup", "lifespan.shutdown"]

    async def receive():
        return {"type": inbox.pop(0)}

    async def send(m):
        sent.append(m["type"])

    asyncio.new_event_loop().run_until_complete(
        asgi({"type": "lifespan", "state": {}}, receive, send)
    )
    return sent


def test_asgi_lifespan_protocol():
    order = []

    @contextlib.asynccontextmanager
    async def ls(app):
        order.append("up")
        yield
        order.append("down")

    app = App(lifespan=ls)
    sent = _drive_lifespan(app.asgi())
    assert sent == ["lifespan.startup.complete", "lifespan.shutdown.complete"]
    assert order == ["up", "down"]
