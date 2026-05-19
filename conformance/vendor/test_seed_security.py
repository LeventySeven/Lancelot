"""Security conformance (covered shapes), run UNMODIFIED through the swap."""
from fastapi import FastAPI, Depends
from fastapi.security import HTTPBearer, APIKeyHeader
from fastapi.testclient import TestClient

app = FastAPI()
bearer = HTTPBearer()
apikey = APIKeyHeader(name="X-Key")


@app.get("/me")
def me(c=Depends(bearer)):
    return {"s": c.scheme}


@app.get("/k")
def k(key=Depends(apikey)):
    return {"key": key}


client = TestClient(app)


def test_bearer_401_and_200():
    r = client.get("/me")
    assert r.status_code == 401
    assert r.json() == {"detail": "Not authenticated"}
    assert r.headers["www-authenticate"] == "Bearer"
    ok = client.get("/me", headers={"Authorization": "Bearer t"})
    assert ok.json() == {"s": "Bearer"}


def test_apikey_and_security_scheme_in_openapi():
    assert client.get("/k").headers["www-authenticate"] == "APIKey"
    cs = client.get("/openapi.json").json()["components"]["securitySchemes"]
    assert cs["HTTPBearer"] == {"type": "http", "scheme": "bearer"}
    assert cs["APIKeyHeader"] == {"type": "apiKey", "in": "header", "name": "X-Key"}
