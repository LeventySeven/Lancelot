from lancelot import App
from lancelot.testclient import TestClient


def test_openapi_json_served():
    app = App()

    @app.get("/ping")
    def ping():
        return {"ok": True}

    with TestClient(app) as c:
        r = c.get("/openapi.json")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")
        d = r.json()
        assert d["openapi"] == "3.1.0"
        assert "/ping" in d["paths"]
        assert app.openapi() is app.openapi()  # memoized
