from lancelot import App
from lancelot.middleware import CORSMiddleware
from lancelot.testclient import TestClient


def _app():
    app = App()

    @app.get("/")
    def root():
        return {"ok": True}

    app.add_middleware(CORSMiddleware, allow_origins=["https://ex.com"],
                       allow_methods=["GET"], allow_headers=["x-test"])
    return app


def test_cors_simple_and_preflight():
    with TestClient(_app()) as c:
        r = c.get("/", headers={"Origin": "https://ex.com"})
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        assert r.headers["access-control-allow-origin"] == "https://ex.com"

        pf = c.request("OPTIONS", "/", headers={
            "Origin": "https://ex.com",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "x-test"})
        assert pf.status_code == 200
        assert pf.headers["access-control-allow-origin"] == "https://ex.com"
        assert "GET" in pf.headers["access-control-allow-methods"]

        r2 = c.get("/", headers={"Origin": "https://evil.com"})
        assert "access-control-allow-origin" not in r2.headers

        r3 = c.get("/")  # no Origin -> passthrough, no CORS headers
        assert r3.status_code == 200
        assert "access-control-allow-origin" not in r3.headers
