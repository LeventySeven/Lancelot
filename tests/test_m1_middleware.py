from lancelot import App
from lancelot.testclient import TestClient


class HeaderMW:  # an unmodified pure-ASGI middleware
    def __init__(self, app, value):
        self.app = app
        self.value = value

    async def __call__(self, scope, receive, send):
        async def send2(msg):
            if msg["type"] == "http.response.start":
                msg = dict(msg)
                msg["headers"] = list(msg.get("headers", [])) + [
                    (b"x-mw", self.value.encode())]
            await send(msg)

        await self.app(scope, receive, send2)


def test_add_middleware_runs_at_app_boundary():
    app = App()

    @app.get("/")
    def root():
        return {"ok": True}

    app.add_middleware(HeaderMW, value="hit")
    with TestClient(app) as c:
        r = c.get("/")
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert r.headers["x-mw"] == "hit"
