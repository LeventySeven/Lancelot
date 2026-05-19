from lancelot import App
from lancelot.testclient import TestClient
from lancelot.params import Depends
from lancelot.security import (OAuth2PasswordBearer, APIKeyHeader, HTTPBearer,
                               HTTPBasic, APIKeyQuery, APIKeyCookie)
from lancelot.middleware import CORSMiddleware


def test_p4_dod():
    app = App()
    ak = APIKeyHeader(name="X-Key")

    @app.get("/k")
    def k(key=Depends(ak)):
        return {"key": key}

    app.add_middleware(CORSMiddleware, allow_origins=["https://e.com"],
                       allow_methods=["GET"])

    with TestClient(app) as c:
        assert c.get("/k").status_code == 401
        assert c.get("/k").headers["www-authenticate"] == "APIKey"
        assert c.get("/k", headers={"X-Key": "v"}).json() == {"key": "v"}
        cs = c.get("/openapi.json").json()["components"]["securitySchemes"]
        assert cs["APIKeyHeader"] == {"type": "apiKey", "in": "header",
                                      "name": "X-Key"}
        r = c.get("/k", headers={"X-Key": "v", "Origin": "https://e.com"})
        assert r.headers["access-control-allow-origin"] == "https://e.com"
