"""Depends() conformance (run UNMODIFIED through the swap)."""
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

app = FastAPI()


def common(skip: int = 0):
    return {"skip": skip}


@app.get("/x")
def x(c: dict = Depends(common)):
    return c


client = TestClient(app)


def test_depends_default_and_override_via_query():
    assert client.get("/x").json() == {"skip": 0}
    assert client.get("/x?skip=5").json() == {"skip": 5}
