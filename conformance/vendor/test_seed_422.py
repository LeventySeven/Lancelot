"""Byte-exact 422 conformance (run UNMODIFIED through the swap; the bodies
below are verbatim FastAPI — deep/01 §0.3)."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

app = FastAPI()


@app.get("/items/{item_id}")
def read(item_id: int, q: int):
    return {"item_id": item_id, "q": q}


client = TestClient(app)


def test_missing_query_422_byte_exact():
    r = client.get("/items/3")
    assert r.status_code == 422
    assert r.json() == {"detail": [{"type": "missing", "loc": ["query", "q"],
                                    "msg": "Field required", "input": None}]}


def test_path_int_parsing_422_byte_exact():
    r = client.get("/items/foo?q=1")
    assert r.status_code == 422
    assert r.json() == {"detail": [{"type": "int_parsing",
        "loc": ["path", "item_id"],
        "msg": "Input should be a valid integer, unable to parse string as an integer",
        "input": "foo"}]}
