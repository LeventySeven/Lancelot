from lancelot import App
from lancelot.testclient import TestClient
from lancelot.exceptions import HTTPException


def test_httpexception_maps_to_json_with_headers():
    app = App()

    @app.get("/boom")
    def boom():
        raise HTTPException(status_code=401, detail="Not authenticated",
                            headers={"WWW-Authenticate": "Bearer"})

    with TestClient(app) as c:
        r = c.get("/boom")
    assert r.status_code == 401
    assert r.json() == {"detail": "Not authenticated"}
    assert r.headers["www-authenticate"] == "Bearer"
