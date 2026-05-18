"""RealTestClient: Starlette's TestClient (exactly what FastAPI's TestClient
IS — harness-design §1.1) constructed from a Lancelot app's spec-compliant
app.asgi() callable. The only glue the conformance harness needs (§1.2)."""
from starlette.testclient import TestClient as _StarletteTestClient


def RealTestClient(app, **kw):
    asgi = app.asgi() if hasattr(app, "asgi") else app
    return _StarletteTestClient(asgi, **kw)
