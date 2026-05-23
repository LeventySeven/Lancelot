"""Test suite for the drop-in fixture app.

Uses TestClient (Starlette's, also exposed by FastAPI). For Lancelot to be
a drop-in here, the TestClient must work against Lancelot's ASGI surface
unchanged.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from .main import app


client = TestClient(app)


def test_root_ok():
    r = client.get("/")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_create_item_happy_path():
    r = client.post("/items", json={"name": "widget", "value": 1})
    assert r.status_code == 200
    data = r.json()
    assert data["created"] == {"name": "widget", "value": 1}
    assert data["token"] == "fixture-token"


def test_create_item_validation_fails_with_custom_handler():
    r = client.post("/items", json={"name": "bad", "value": -1})
    # The custom handler kicks in at HTTPException-raise time.
    assert r.status_code == 422
    assert r.json() == {"error": "value must be non-negative", "via": "custom-handler"}


def test_stream():
    with client.stream("GET", "/stream") as r:
        chunks = list(r.iter_text())
        body = "".join(chunks)
    assert body == "chunk-0\nchunk-1\nchunk-2\nchunk-3\nchunk-4\n"
