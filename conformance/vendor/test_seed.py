"""Seed of the conformance corpus — FastAPI-shaped behavioral tests, run
UNMODIFIED through the conftest swap. The full pinned 581-file FastAPI vendor
+ reference/candidate differential harness is M1-P7. Each test exercises a
spine behavior M1-P1 must already pass."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

app = FastAPI()


@app.get("/")
def root():
    return {"msg": "ok"}


@app.get("/items/{item_id}")
def item(item_id):
    return {"item_id": item_id}


client = TestClient(app)


def test_root():
    assert client.get("/").status_code == 200
    assert client.get("/").json() == {"msg": "ok"}


def test_path_param():
    assert client.get("/items/abc").json() == {"item_id": "abc"}


def test_not_found():
    assert client.get("/missing").status_code == 404


def test_method_not_allowed():
    assert client.post("/").status_code == 405
