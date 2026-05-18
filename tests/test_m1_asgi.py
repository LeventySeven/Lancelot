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


def test_asgi_http_get_post_404_405():
    import httpx

    app = App()

    @app.get("/items/{i}")
    async def read(i):
        return {"i": i}

    @app.post("/echo")
    def echo_sync():
        return {"ok": True}

    async def go():
        transport = httpx.ASGITransport(app=app.asgi())
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.get("/items/7")
            assert r.status_code == 200 and r.json() == {"i": "7"}
            assert r.headers["content-type"].startswith("application/json")
            p = await c.post("/echo")
            assert p.status_code == 200 and p.json() == {"ok": True}
            miss = await c.get("/nope")
            assert miss.status_code == 404
            wrong = await c.post("/items/7")
            assert wrong.status_code == 405

    asyncio.new_event_loop().run_until_complete(go())
