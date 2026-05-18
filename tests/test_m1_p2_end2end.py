import pydantic

from lancelot import App
from lancelot.testclient import TestClient
from lancelot.params import Depends


class Item(pydantic.BaseModel):
    name: str
    price: float


def test_typed_query_body_depends_and_422():
    app = App()

    def auth():
        return "user1"

    @app.get("/n/{n}")
    def read(n: int, q: int = 0, who: str = Depends(auth)):
        return {"n": n, "q": q, "who": who}

    @app.post("/items")
    def create(item: Item):
        return {"name": item.name, "price": item.price}

    with TestClient(app) as c:
        r = c.get("/n/5?q=3")
        assert r.status_code == 200
        assert r.json() == {"n": 5, "q": 3, "who": "user1"}

        bad = c.get("/n/notint")
        assert bad.status_code == 422
        assert bad.json() == {"detail": [{"type": "int_parsing",
            "loc": ["path", "n"],
            "msg": "Input should be a valid integer, unable to parse string as an integer",
            "input": "notint"}]}

        ok = c.post("/items", json={"name": "x", "price": 1.5})
        assert ok.json() == {"name": "x", "price": 1.5}

        miss = c.post("/items", json={"name": "x"})
        assert miss.status_code == 422
        assert miss.json()["detail"][0]["loc"] == ["body", "price"]
