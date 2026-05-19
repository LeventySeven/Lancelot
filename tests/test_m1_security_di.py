from lancelot import App
from lancelot.testclient import TestClient
from lancelot.params import Depends
from lancelot.security import HTTPBearer, APIKeyHeader

bearer = HTTPBearer()
apikey = APIKeyHeader(name="X-Key")


def test_security_dep_401_and_200():
    app = App()

    @app.get("/me")
    def me(cred=Depends(bearer)):
        return {"scheme": cred.scheme, "tok": cred.credentials}

    with TestClient(app) as c:
        r0 = c.get("/me")
        assert r0.status_code == 401
        assert r0.json() == {"detail": "Not authenticated"}
        assert r0.headers["www-authenticate"] == "Bearer"
        r1 = c.get("/me", headers={"Authorization": "Bearer xyz"})
        assert r1.status_code == 200
        assert r1.json() == {"scheme": "Bearer", "tok": "xyz"}


def test_apikey_dep_end_to_end():
    app = App()

    @app.get("/k")
    def k(key=Depends(apikey)):
        return {"key": key}

    with TestClient(app) as c:
        assert c.get("/k").status_code == 401
        assert c.get("/k").headers["www-authenticate"] == "APIKey"
        assert c.get("/k", headers={"X-Key": "v"}).json() == {"key": "v"}
