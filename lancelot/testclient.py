"""TestClient — a thin wrapper over Starlette's TestClient constructed from
the app's spec-compliant `app.asgi()` callable.

Plan deviation (necessary — documented): the plan's Task 6 built this on
`httpx.Client(transport=httpx.ASGITransport(...))`, but `httpx.ASGITransport`
implements only the *async* transport interface (`handle_async_request`/
`aclose`) and cannot drive the *sync* `httpx.Client` (AttributeError:
'ASGITransport' object has no attribute 'handle_request'). The correct,
battle-tested sync-over-ASGI driver is Starlette's TestClient — which is
*exactly* what FastAPI's TestClient is (harness-design §1.1), so this also
keeps our dev TestClient and the conformance harness on one implementation
(correct lifespan + websocket + `.post(json=)` semantics for free)."""
from starlette.testclient import TestClient as _StarletteTestClient


class TestClient(_StarletteTestClient):
    """`TestClient(app)` drives `app.asgi()`. `with TestClient(app)` runs the
    app lifespan (Starlette's TestClient handles startup/shutdown + the
    request portal correctly)."""

    def __init__(self, app, base_url: str = "http://testserver", **kw):
        asgi = app.asgi() if hasattr(app, "asgi") else app
        # Default to NOT-raising server exceptions: Lancelot's own internal
        # test suite asserts on the 500 wire-shape (ServerErrorMiddleware
        # contract). The conformance swap uses Starlette's TestClient
        # directly (RealTestClient — see conformance/swap.py), which keeps
        # the Starlette default raise_server_exceptions=True so vendored
        # `pytest.raises(...)` assertions behave verbatim.
        kw.setdefault("raise_server_exceptions", False)
        super().__init__(asgi, base_url=base_url, **kw)
