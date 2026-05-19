from lancelot import App
from lancelot.testclient import TestClient
from lancelot.params import Depends


def test_p2_dod():
    app = App()

    def d():
        return 7

    @app.get("/a/{i}")
    def a(i: int, n: int = 1, x: int = Depends(d)):
        return {"i": i, "n": n, "x": x}

    with TestClient(app) as c:
        assert c.get("/a/2?n=9").json() == {"i": 2, "n": 9, "x": 7}
        assert c.get("/a/bad").status_code == 422
