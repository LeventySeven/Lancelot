import contextlib

from lancelot import App
from lancelot.testclient import TestClient


def test_testclient_get_post_and_lifespan():
    started = []

    @contextlib.asynccontextmanager
    async def ls(app):
        started.append(True)
        app.state.v = 9
        yield

    app = App(lifespan=ls)

    @app.get("/")
    def root():
        return {"hello": "world"}

    @app.post("/sum")
    def s():
        return {"n": 3}

    with TestClient(app) as c:
        assert started == [True]
        assert c.get("/").json() == {"hello": "world"}
        assert c.post("/sum", json={"a": 1}).json() == {"n": 3}
