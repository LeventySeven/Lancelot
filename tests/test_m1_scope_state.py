from lancelot import App, Request
from lancelot.testclient import TestClient


class SetState:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        scope.setdefault("state", {})["uid"] = 7
        await self.app(scope, receive, send)


def test_request_injectable_and_scope_state():
    app = App()

    @app.get("/who")
    def who(request: Request):
        return {"uid": request.state["uid"], "method": request.method}

    app.add_middleware(SetState)
    with TestClient(app) as c:
        r = c.get("/who")
    assert r.status_code == 200
    assert r.json() == {"uid": 7, "method": "GET"}
